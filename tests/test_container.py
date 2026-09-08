"""Integration tests. These boot real containers, so they are excluded from
`make test` and run under `make test-container`.

They make no API calls: the relay is proved by the 401 an invalid key earns from
the real endpoint, which is itself proof the request got there.

RUNTIME, AGENT, IMAGE and NETWORK select what is booted, so the same suite runs
against Apple `container` on macOS and against Docker on Linux. Docker on a
native daemon is the only configuration that can prove --proxy at all: Apple's
engine and Docker Desktop both keep the bridge inside a VM.
"""

import json
import os
import re
import subprocess

import pytest

from sanduk import proxy
from sanduk.agent import get_agent
from sanduk.errors import AgentboxError
from sanduk.runtime import get_runtime, wait_for_gateway

pytestmark = pytest.mark.container

ENGINE = get_runtime(os.environ.get("RUNTIME", "apple"))
AGENT = get_agent(os.environ.get("AGENT", "claude"))
IMAGE = os.environ.get("IMAGE", AGENT.image)
NETWORK = os.environ.get("NETWORK", "sanduk-net")
FAKE_KEY = "sk-ant-api03-REAL-KEY-STAYS-ON-HOST"

# A pattern each agent's --version output must match. Test-local: an agent
# handler has no business declaring a string that exists only to be asserted
# here. Patterns rather than substrings because opencode prints a bare version
# and no name, so there is nothing to match on but its shape.
VERSION_MARKER = {
    "claude": r"Claude Code",
    "codex": r"codex-cli",
    "hax": r"hax",
    "opencode": r"\d+\.\d+\.\d+",
}


def sh(script, network=None, env=None):
    cmd = [ENGINE.cli, "run", "--rm"]
    if network:
        cmd += ["--network", network]
    for k in env or {}:
        cmd += ["-e", k]
    cmd += ["--entrypoint", "sh", IMAGE, "-c", script]
    return subprocess.run(
        cmd, capture_output=True, text=True, env={**os.environ, **(env or {})}
    )


@pytest.fixture(scope="module", autouse=True)
def engine_running():
    # require() is the engine's own readiness check. Calling `system status`
    # here instead assumed Apple's CLI, and skipped every Docker run with a
    # message about a service Docker does not have.
    try:
        ENGINE.require()
    except AgentboxError as e:
        pytest.skip(str(e))
    if not ENGINE.image_exists(IMAGE):
        pytest.skip(f"{IMAGE} is not built (`make image`)")


@pytest.fixture(scope="module")
def isolated_network():
    """The bridge exists only while something is attached, so hold it up."""
    gateway, _ = ENGINE.ensure_network(NETWORK)
    holder = ENGINE.hold_network_up(NETWORK, IMAGE)
    assert wait_for_gateway(gateway), f"{gateway} never became bindable"
    yield gateway
    if holder:
        ENGINE.destroy(holder)


def test_the_image_runs_its_agent():
    out = subprocess.run(
        [ENGINE.cli, "run", "--rm", IMAGE, "--version"], capture_output=True, text=True
    )
    assert re.search(VERSION_MARKER[AGENT.name], out.stdout), out.stdout


def test_default_network_reaches_the_internet():
    """The contrast that makes the isolated network meaningful."""
    r = sh(
        'curl -s -m 8 -o /dev/null -w "%{http_code}" https://api.anthropic.com/v1/models'
    )
    assert r.stdout.strip() == "401", r.stdout


def test_internal_network_blocks_egress(isolated_network):
    r = sh(
        'curl -s -m 6 -o /dev/null -w "%{http_code}" https://api.anthropic.com/v1/models;'
        ' echo " "; curl -s -m 6 -o /dev/null -w "%{http_code}" https://1.1.1.1/',
        network=NETWORK,
    )
    assert r.stdout.split() == ["000", "000"], r.stdout


def test_host_gateway_is_reachable_from_the_isolated_network(isolated_network):
    """If this fails the macOS firewall is blocking this interpreter."""
    srv, port = proxy.start_proxy(FAKE_KEY, "tok", isolated_network)
    try:
        r = sh(
            f'curl -s -m 8 -o /dev/null -w "%{{http_code}}" '
            f'http://{isolated_network}:{port}/v1/models -H "x-api-key: wrong"',
            network=NETWORK,
        )
        assert r.stdout.strip() == "401", f"proxy unreachable: {r.stdout!r}"
        assert srv.cfg.rejected == 1
    finally:
        srv.shutdown()


def test_key_never_enters_the_container(isolated_network):
    srv, port = proxy.start_proxy(FAKE_KEY, "tok", isolated_network)
    try:
        env = {
            "ANTHROPIC_API_KEY": "tok",
            "ANTHROPIC_BASE_URL": f"http://{isolated_network}:{port}",
        }
        r = sh("env | grep -c REAL-KEY-STAYS-ON-HOST || true", network=NETWORK, env=env)
        assert r.stdout.strip() == "0", "the real key leaked into the container"
    finally:
        srv.shutdown()


def test_relay_injects_the_key_and_reaches_the_real_endpoint(isolated_network):
    """401 from api.anthropic.com proves the request arrived carrying our key."""
    srv, port = proxy.start_proxy(FAKE_KEY, "tok", isolated_network)
    try:
        env = {
            "ANTHROPIC_API_KEY": "tok",
            "ANTHROPIC_BASE_URL": f"http://{isolated_network}:{port}",
        }
        body = json.dumps(
            {
                "model": "claude-opus-5",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        r = sh(
            f'curl -s -m 20 -o /dev/null -w "%{{http_code}}" -X POST '
            f'"$ANTHROPIC_BASE_URL/v1/messages" -H "x-api-key: $ANTHROPIC_API_KEY" '
            f'-H "content-type: application/json" -H "anthropic-version: 2023-06-01" '
            f"-d '{body}'",
            network=NETWORK,
            env=env,
        )
        assert r.stdout.strip() == "401", r.stdout
        assert srv.cfg.requests == 1
    finally:
        srv.shutdown()


def test_no_sanduk_containers_are_left_behind():
    leftovers = [
        c.name for c in ENGINE.list_containers("sanduk-") if "hold" not in c.name
    ]
    assert leftovers == [], f"orphans: {leftovers}"
