"""Relay tests: token, path, policy, header injection, streaming.

Every test runs against a local fake upstream, so the suite makes no API calls,
costs nothing, and needs no key.
"""

import gzip
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from boxagent import proxy

REAL_KEY = "sk-ant-api03-REAL-KEY-STAYS-ON-HOST"
TOKEN = "run-token-for-tests"


def call(port, path, token=TOKEN, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"x-api-key": token, "content-type": "application/json"},
        method="POST" if body else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def message(model="claude-opus-5", max_tokens=64000):
    return json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()


@pytest.fixture
def plaintext_upstream(monkeypatch):
    """The relay always speaks TLS; the fake upstreams here are plaintext."""
    monkeypatch.setattr(http.client, "HTTPSConnection", http.client.HTTPConnection)


@pytest.fixture
def upstream(plaintext_upstream):
    """Fake api.anthropic.com. Yields (hostport, seen) where seen records the
    last request the relay actually forwarded."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen["key"] = self.headers.get("x-api-key")
            seen["headers"] = {k.lower(): v for k, v in self.headers.items()}
            seen["body"] = json.loads(raw) if raw else None
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = respond

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


@pytest.fixture
def relay(upstream):
    hostport, seen = upstream
    started = []

    def make(**kw):
        kw.setdefault("upstream", hostport)
        srv, port = proxy.start_proxy(REAL_KEY, TOKEN, "127.0.0.1", **kw)
        started.append(srv)
        return port, seen, srv

    yield make
    for srv in started:
        srv.shutdown()


# --- access control ---------------------------------------------------------


def test_wrong_token_is_rejected(relay):
    port, _, _ = relay()
    assert call(port, "/v1/models", token="guessed")[0] == 401


def test_missing_token_is_rejected(relay):
    port, _, _ = relay()
    assert call(port, "/v1/models", token="")[0] == 401


def test_lookalike_path_is_rejected(relay):
    """A prefix match would admit this; the allowlist is exact."""
    port, _, _ = relay()
    assert call(port, "/v1/models-internal-secret")[0] == 403


def test_query_string_still_matches(relay):
    """Claude Code calls /v1/messages?beta=true. Matching the raw path would 403."""
    port, _, _ = relay()
    assert call(port, "/v1/messages?beta=true", body=message())[0] == 200


def test_rejections_are_counted(relay):
    port, _, srv = relay()
    call(port, "/v1/models", token="guessed")
    call(port, "/v1/nope")
    assert (srv.cfg.requests, srv.cfg.rejected) == (0, 2)


# --- credential handling ----------------------------------------------------


def test_real_key_is_injected_and_token_is_not_forwarded(relay):
    port, seen, _ = relay()
    assert call(port, "/v1/messages", body=message())[0] == 200
    assert seen["key"] == REAL_KEY
    assert TOKEN not in json.dumps(seen["headers"])


def test_client_authorization_header_is_dropped(relay):
    """A container that sets its own Authorization must not reach upstream."""
    port, seen, _ = relay()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=message(),
        headers={
            "x-api-key": TOKEN,
            "content-type": "application/json",
            "authorization": "Bearer smuggled",
        },
    )
    urllib.request.urlopen(req, timeout=20).read()
    assert "authorization" not in seen["headers"]


def test_anthropic_version_header_is_forwarded(relay):
    """Denylist, not whitelist: provider headers must survive the hop."""
    port, seen, _ = relay()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=message(),
        headers={
            "x-api-key": TOKEN,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "x-1",
        },
    )
    urllib.request.urlopen(req, timeout=20).read()
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["headers"]["anthropic-beta"] == "x-1"


# --- body policy ------------------------------------------------------------


def test_disallowed_model_is_rejected(relay):
    port, _, _ = relay(allow_models={"claude-opus-5"})
    assert call(port, "/v1/messages", body=message("claude-sonnet-5"))[0] == 403


def test_allowed_model_passes(relay):
    port, _, _ = relay(allow_models={"claude-opus-5"})
    assert call(port, "/v1/messages", body=message("claude-opus-5"))[0] == 200


def test_max_tokens_is_clamped(relay):
    port, seen, _ = relay(max_tokens_cap=4000)
    call(port, "/v1/messages", body=message(max_tokens=999999))
    assert seen["body"]["max_tokens"] == 4000


def test_max_tokens_under_the_cap_is_untouched(relay):
    port, seen, _ = relay(max_tokens_cap=64000)
    call(port, "/v1/messages", body=message(max_tokens=1024))
    assert seen["body"]["max_tokens"] == 1024


def test_malformed_json_is_rejected_when_policy_is_on(relay):
    port, _, _ = relay(max_tokens_cap=4000)
    assert call(port, "/v1/messages", body=b"{oops")[0] == 400


def test_body_is_untouched_when_no_policy_is_set(relay):
    port, seen, _ = relay()
    call(port, "/v1/messages", body=message(max_tokens=999999))
    assert seen["body"]["max_tokens"] == 999999


def test_bodyless_get_reaches_upstream(relay):
    """A GET carries no body, which must not read as a policy refusal."""
    port, seen, _ = relay()
    assert call(port, "/v1/models")[0] == 200
    # The upstream records this before it answers, so it cannot race the client.
    assert seen["key"] == REAL_KEY


def test_request_bodies_are_written_to_the_log_dir(relay, tmp_path):
    """The audit trail lands outside the bind mount, one file per call."""
    port, _, _ = relay(log_bodies=True, log_dir=str(tmp_path))
    call(port, "/v1/messages", body=message())
    assert [p.name for p in tmp_path.iterdir()] == ["001.json"]
    assert json.loads((tmp_path / "001.json").read_text())["model"] == "claude-opus-5"


# --- streaming --------------------------------------------------------------


def test_sse_is_streamed_not_buffered(plaintext_upstream):
    """read(n) blocks until n bytes arrive and would stall every event behind a
    full buffer; the relay must use read1."""

    class Streamer(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(4):
                ev = f'data: {{"i":{i}}}\n\n'.encode()
                self.wfile.write(b"%x\r\n" % len(ev) + ev + b"\r\n")
                self.wfile.flush()
                time.sleep(0.3)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    up = ThreadingHTTPServer(("127.0.0.1", 0), Streamer)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    srv, port = proxy.start_proxy(
        REAL_KEY, TOKEN, "127.0.0.1", upstream=f"127.0.0.1:{up.server_address[1]}"
    )
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=b"{}",
        headers={"x-api-key": TOKEN, "content-type": "application/json"},
    )
    t0 = time.monotonic()
    arrivals = []
    with urllib.request.urlopen(req, timeout=30) as r:
        while True:
            line = r.readline()
            if not line:
                break
            if line.startswith(b"data:"):
                arrivals.append(time.monotonic() - t0)
    srv.shutdown()
    up.shutdown()
    assert len(arrivals) == 4
    assert max(arrivals) - min(arrivals) > 0.5, f"arrived together: {arrivals}"


# --- usage accounting -------------------------------------------------------


def test_usage_sniffer_survives_chunk_boundaries():
    """Chunks split wherever the socket happens to, including mid-line."""
    stream = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"usage":'
        b'{"input_tokens":4,"cache_creation_input_tokens":18234,'
        b'"cache_read_input_tokens":0,"output_tokens":1}}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","usage":{"output_tokens":112}}\n\n'
    )
    s = proxy.UsageSniffer("text/event-stream")
    for i in range(0, len(stream), 7):
        s.feed(stream[i : i + 7])
    assert s.usage["input_tokens"] == 4
    assert s.usage["cache_creation_input_tokens"] == 18234
    assert s.usage["cache_read_input_tokens"] == 0
    assert s.usage["output_tokens"] == 112, "message_delta must win over message_start"
    assert s.digest() == " in=4 cache_write=18234 cache_read=0 out=112"


def test_usage_sniffer_is_quiet_when_there_is_none():
    s = proxy.UsageSniffer("text/event-stream")
    s.feed(b'data: {"type":"ping"}\n\n')
    assert s.digest() == ""


def test_usage_sniffer_reads_a_non_streamed_json_body():
    """Claude Code calls the API without `stream`, so usage arrives once, whole."""
    body = json.dumps(
        {
            "type": "message",
            "usage": {
                "input_tokens": 7,
                "cache_creation_input_tokens": 13609,
                "cache_read_input_tokens": 150898,
                "output_tokens": 12388,
            },
        }
    ).encode()
    s = proxy.UsageSniffer("application/json")
    for i in range(0, len(body), 11):
        s.feed(body[i : i + 11])
    assert s.digest() == ""  # nothing is parseable until the body is complete
    s.close()
    assert s.digest() == " in=7 cache_write=13609 cache_read=150898 out=12388"


def test_usage_sniffer_gives_up_on_an_oversized_json_body():
    s = proxy.UsageSniffer("application/json")
    s.feed(b'{"usage":{"input_tokens":1},"pad":"' + b"x" * proxy.UsageSniffer.LIMIT)
    s.close()
    assert s.digest() == ""


def test_streamed_usage_reaches_the_log_line(plaintext_upstream, monkeypatch):
    class Streamer(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for ev in (
                b'data: {"type":"message_start","message":{"usage":'
                b'{"input_tokens":3,"cache_creation_input_tokens":0,'
                b'"cache_read_input_tokens":210330,"output_tokens":1}}}\n\n',
                b'data: {"type":"message_delta","usage":{"output_tokens":16517}}\n\n',
            ):
                self.wfile.write(b"%x\r\n" % len(ev) + ev + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    notes = []
    monkeypatch.setattr(proxy.Handler, "note", lambda self, msg: notes.append(msg))

    up = ThreadingHTTPServer(("127.0.0.1", 0), Streamer)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    srv, port = proxy.start_proxy(
        REAL_KEY, TOKEN, "127.0.0.1", upstream=f"127.0.0.1:{up.server_address[1]}"
    )
    assert call(port, "/v1/messages", body=message())[0] == 200

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not notes:
        time.sleep(0.01)
    srv.shutdown()
    up.shutdown()
    assert notes, "relay logged nothing"
    assert "cache_read=210330" in notes[-1]
    assert "out=16517" in notes[-1]


def test_responses_without_usage_get_no_suffix(relay, monkeypatch):
    """/v1/models reports no tokens; the line must not grow four zero fields."""
    notes = []
    monkeypatch.setattr(proxy.Handler, "note", lambda self, msg: notes.append(msg))
    port, _, _ = relay()
    assert call(port, "/v1/models")[0] == 200
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not notes:
        time.sleep(0.01)
    assert notes and "cache_read" not in notes[-1]


def test_gzipped_usage_is_decompressed():
    """The API answers in gzip; the sniffer would otherwise read opaque bytes."""
    body = json.dumps({"usage": {"input_tokens": 7, "output_tokens": 12388}}).encode()
    s = proxy.UsageSniffer("application/json", "gzip")
    s.feed(gzip.compress(body))
    s.close()
    assert s.digest() == " in=7 cache_write=0 cache_read=0 out=12388"


def test_corrupt_gzip_does_not_break_the_relay():
    s = proxy.UsageSniffer("application/json", "gzip")
    s.feed(b"not actually gzip")
    s.close()
    assert s.digest() == ""


def test_brotli_is_never_offered_upstream(relay):
    """The API prefers brotli when offered, and the stdlib cannot decode it."""
    port, seen, _ = relay()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=message(),
        headers={
            "x-api-key": TOKEN,
            "content-type": "application/json",
            "accept-encoding": "gzip, deflate, br",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    assert seen["headers"]["accept-encoding"] == "gzip"


def test_a_client_refusing_compression_is_not_given_it(relay):
    port, seen, _ = relay()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=message(),
        headers={
            "x-api-key": TOKEN,
            "content-type": "application/json",
            "accept-encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    assert seen["headers"]["accept-encoding"] == "identity"
