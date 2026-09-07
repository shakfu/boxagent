"""Host checks that run before any container is created.

Both exist because the failure they catch is silent and expensive: a bad key
costs ~174s of in-container retry backoff, and a firewalled interpreter hangs
the agent's first API call until --timeout with no error at all.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

from boxagent.agent import KEY_ENV
from boxagent.errors import AgentboxError
from boxagent.util import run

FIREWALL = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def _firewall_entries() -> list[tuple[str, bool]]:
    """[(path, blocked)] from socketfilterfw --listapps."""
    r = run([FIREWALL, "--listapps"], capture_output=True)
    if r.returncode != 0:
        return []
    entries: list[tuple[str, bool]] = []
    path: str | None = None
    for line in r.stdout.splitlines():
        stripped = line.strip()
        head, _, rest = stripped.partition(":")
        if head.strip().isdigit():
            path = rest.strip()
        elif path and stripped.startswith("("):
            entries.append((path, "block" in stripped.lower()))
            path = None
    return entries


def firewall_warning() -> None:
    """Warn when the macOS application firewall blocks this interpreter.

    A blocked binary drops container->host connections with no error, so the
    agent's first API call hangs until --timeout. Homebrew's python is shipped
    blocked on at least one machine; Xcode's and uv's are allowed. Framework
    builds register as Resources/Python.app, not as the bin/pythonX.Y that
    sys.executable resolves to.
    """
    if not os.path.exists(FIREWALL):
        return
    state = run([FIREWALL, "--getglobalstate"], capture_output=True)
    if state.returncode != 0 or "enabled" not in state.stdout.lower():
        return

    exe = os.path.realpath(sys.executable)
    version_root = os.path.dirname(os.path.dirname(exe))
    names = {sys.executable, exe, os.path.join(version_root, "Resources", "Python.app")}

    blocked = [p for p, is_blocked in _firewall_entries() if is_blocked and p in names]
    if not blocked:
        return  # unlisted signed interpreters are auto-allowed

    target = blocked[0]
    print(
        f"boxagent: WARNING the macOS firewall is on and\n"
        f"  {target}\n"
        f"  is set to block incoming connections.\n"
        f"  The agent's calls to the proxy will hang until --timeout. Either\n"
        f"  re-run with /usr/bin/python3, or allow this interpreter once:\n"
        f"    sudo {FIREWALL} --unblockapp {target}",
        file=sys.stderr,
    )


def validate_key(key: str, base_url: str) -> bool:
    """One cheap request, so a bad key fails in 0.2s instead of ~174s of retries."""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return bool(r.status == 200)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AgentboxError(
                f"{KEY_ENV} rejected by {base_url} (HTTP {e.code})."
            ) from e
        # Any other status still proves the endpoint answered; let the agent try.
        return True
    except urllib.error.URLError as e:
        raise AgentboxError(f"cannot reach {base_url}: {e.reason}") from e
