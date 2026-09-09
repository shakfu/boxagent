"""Integration tests. These boot real containers, so they are excluded from
`make test` and run under `make test-container`.

They make no API calls: the relay is proved by the 401 an invalid key earns from
the real endpoint, which is itself proof the request got there.

RUNTIME, AGENT, IMAGE and NETWORK select what is booted, so the same suite runs
against Apple `container` on macOS and against Docker on Linux. Docker on a
native daemon is the only configuration that can prove --proxy at all: Apple's
engine and Docker Desktop both keep the bridge inside a VM.
"""

import argparse
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sanduk import assistants, proxy
from sanduk.agent import get_agent
from sanduk.errors import AgentboxError
from sanduk.providers import OPENAI_CHAT, get_provider
from sanduk.runtime import get_runtime, wait_for_gateway

pytestmark = pytest.mark.container

ENGINE = get_runtime(os.environ.get("RUNTIME", "apple"))
AGENT = get_agent(os.environ.get("AGENT", "claude"))
IMAGE = os.environ.get("IMAGE", AGENT.image)
NETWORK = os.environ.get("NETWORK", "sanduk-net")
FAKE_KEY = "sk-ant-api03-REAL-KEY-STAYS-ON-HOST"

# What to ask each image, and what the answer has to match. Test-local: an
# agent handler has no business declaring a string that exists only to be
# asserted here. Patterns rather than substrings because opencode and pi print
# a bare version and no name, so there is nothing to match on but its shape.
# hermes has no version flag at all: its CLI is Fire over one function, so the
# question that proves the image runs it is --help.
VERSION_MARKER = {
    "claude": ("--version", r"Claude Code"),
    "codex": ("--version", r"codex-cli"),
    "hax": ("--version", r"hax"),
    "hermes": ("--help", r"--query|QUERY"),
    "opencode": ("--version", r"\d+\.\d+\.\d+"),
    "pi": ("--version", r"\d+\.\d+\.\d+"),
    "prime": ("--version", r"\d+\.\d+\.\d+"),
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
    question, _ = VERSION_MARKER[AGENT.name]
    out = subprocess.run(
        [ENGINE.cli, "run", "--rm", IMAGE, question], capture_output=True, text=True
    )
    # Both streams: prime-agent prints its version on stderr, pi on stdout.
    printed = out.stdout + out.stderr
    assert re.search(VERSION_MARKER[AGENT.name][1], printed), printed


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


# --- a wakeup, end to end ---------------------------------------------------


REPLY = "Wrote the report."


class ChatStub(BaseHTTPRequestHandler):
    """An OpenAI Chat Completions endpoint that answers once, and counts.

    The agent's own model is not what is under test here. What is: an
    `assistant.toml` on disk becoming a container, a relayed request, a report,
    and a row in the database.
    """

    protocol_version = "HTTP/1.1"
    calls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send(json.dumps({"data": [{"id": "stub-model"}]}).encode())

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        ChatStub.calls += 1
        streaming = b'"stream": true' in raw or b'"stream":true' in raw
        message = {"role": "assistant", "content": REPLY}
        usage = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
        if not streaming:
            self._send(
                json.dumps(
                    {
                        "id": "stub",
                        "object": "chat.completion",
                        "model": "stub-model",
                        "choices": [
                            {"index": 0, "message": message, "finish_reason": "stop"}
                        ],
                        "usage": usage,
                    }
                ).encode()
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": REPLY}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": usage},
        ]
        for chunk in chunks:
            self._chunk(f"data: {json.dumps(chunk)}\n\n".encode())
        self._chunk(b"data: [DONE]\n\n")
        self._chunk(b"")

    def _chunk(self, payload):
        self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
        self.wfile.flush()

    def _send(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def chat_stub():
    ChatStub.calls = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ChatStub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def assistant_home(tmp_path, monkeypatch, chat_stub):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    home = tmp_path / "triage"
    home.mkdir()
    (home / "brief.md").write_text("Say hello. Write one line to REPORT.md.")
    (home / "assistant.toml").write_text(
        f"""
name = "triage"
agent = "{AGENT.name}"
provider = "openai-compat"
model = "stub-model"
runtime = "{ENGINE.name}"
every = "1h"
timeout = 120
brief = "brief.md"
args = ["--upstream", "http://127.0.0.1:{chat_stub}", "--skip-key-check"]
"""
    )
    return home


def relays() -> bool:
    """Whether this agent can be pointed at the relay at all. hermes cannot:
    it ignores every endpoint override, and its handler says so."""
    args = argparse.Namespace(
        agent=AGENT.name, model="stub-model", proxy=True, effort=None,
        max_turns=None, allowed_tools=None, permission_mode=None, bare=False,
        agent_key_env=None, agent_base_url_env=None,
    )  # fmt: skip
    try:
        AGENT.check(args, get_provider("openai-compat"))
    except AgentboxError:
        return False
    return True


def test_a_wakeup_is_a_container_a_relayed_call_and_a_row(assistant_home):
    """The whole assistant path, with nothing stubbed but the model: config on
    disk -> container -> relay -> report -> database."""
    if OPENAI_CHAT not in AGENT.protocols:
        pytest.skip(f"{AGENT.name} does not speak Chat Completions; the stub only does")
    if not relays():
        pytest.skip(f"{AGENT.name} cannot be pointed at the relay")
    db = assistants.connect()
    assistants.register(db, assistants.load(assistant_home))
    assistants.tell(db, "triage", "this message rides the wakeup")

    assistants.tick(db)

    recorded = list(db.execute("SELECT * FROM runs"))
    assert len(recorded) == 1, recorded
    assert ChatStub.calls >= 1, "the container never reached the relay"
    assert recorded[0]["exit_code"] == 0
    assert recorded[0]["stats"], "the wakeup recorded no token line"
    # The message was answered, so it is consumed; the outbox has the result.
    assert assistants.pending(db, "triage") == []
    assert len(assistants.outbox(db, "triage")) == 1
    # Scheduled an hour out, and the wakeup's own containers are gone. The
    # holder another test in this module is standing up does not count.
    assert assistants.row(db, "triage")["next_due_at"] > assistants.now() + 3000
    left = [c.name for c in ENGINE.list_containers("sanduk-") if "hold" not in c.name]
    assert left == []
