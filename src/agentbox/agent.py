"""The agent CLI that runs inside the container.

Only Claude Code is implemented. It is isolated here so a second agent is a new
module plus a flag, not an edit spread through the runner.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
from typing import Any

from agentbox.errors import AgentboxError

KEY_ENV = "ANTHROPIC_API_KEY"
BASE_URL_ENV = "ANTHROPIC_BASE_URL"
REPORT_NAME = "REPORT.md"

REPORT_INSTRUCTION = (
    "\n\nWhen you are done, write your findings to ./{report} in the working "
    "directory. That file is the only output that survives; anything you print "
    "to the terminal is discarded when the container is deleted."
)


def claude_argv(args: argparse.Namespace, task: str) -> list[str]:
    """The `claude` flags, appended after the image in the container argv."""
    argv = ["-p", task, "--output-format", "stream-json", "--verbose"]
    if args.bare:
        argv.append("--bare")
    if args.permission_mode:
        argv += ["--permission-mode", args.permission_mode]
    else:
        argv.append("--dangerously-skip-permissions")
    if args.model:
        argv += ["--model", args.model]
    if args.effort:
        argv += ["--effort", args.effort]
    if args.allowed_tools:
        argv += ["--allowed-tools", args.allowed_tools]
    if args.max_turns:
        argv += ["--max-turns", str(args.max_turns)]
    return argv


def summarize(event: dict[str, Any], quiet: bool) -> None:
    """One compact line per stream-json event."""
    if quiet:
        return
    t = event.get("type")
    if t == "assistant":
        for b in event.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                print(f"  . {b['text'].strip()[:160]}")
            elif b.get("type") == "tool_use":
                print(f"  > {b.get('name')}")
    elif t == "user":
        for b in event.get("message", {}).get("content", []):
            if b.get("type") == "tool_result" and b.get("is_error"):
                print("  ! tool error")


def launch(
    argv: list[str],
    timeout: float,
    quiet: bool,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Stream stream-json events; return the final `result` event and exit code.

    The timer is what enforces the timeout: an agent that hangs without printing
    would never trip a deadline checked inside the read loop.
    """
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, bufsize=1, env=env)
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, expire)
    watchdog.start()

    result: dict[str, Any] | None = None
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                result = event
            else:
                summarize(event, quiet)
        proc.wait(timeout=30)
    except KeyboardInterrupt:
        proc.kill()
        raise
    finally:
        watchdog.cancel()

    if timed_out.is_set():
        raise AgentboxError(f"agent exceeded --timeout {timeout:g}s", code=124)
    return result, proc.returncode
