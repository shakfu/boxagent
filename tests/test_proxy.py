"""Relay tests: token, path, policy, header injection, streaming.

Every test runs against a local fake upstream, so the suite makes no API calls,
costs nothing, and needs no key.

Every test taking the `relay` fixture runs twice: once against the package and
once against `scripts/sanduk.py`, which carries its own copy of the relay. The
relay is the security boundary and exists in both, so a fix applied to one and
not the other is the failure this catches. It replaces an AST comparison of the
two sources, which could not survive the package gaining providers the script
does not have.
"""

import gzip
import http.client
import importlib.util
import json
import pathlib
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sanduk import providers, proxy

SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "sanduk.py"


def _load_script():
    """Import scripts/sanduk.py as a module. Its PEP 723 header is a comment,
    and nothing runs at import: main() is behind an __main__ guard."""
    spec = importlib.util.spec_from_file_location("sanduk_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RELAYS = {"package": proxy}
if SCRIPT.is_file():
    RELAYS["script"] = _load_script()

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


@pytest.fixture(params=sorted(RELAYS), ids=sorted(RELAYS))
def relay_module(request):
    """The relay implementation under test. Tests that patch relay internals
    must reach through this, not through `proxy`: the script's Handler is a
    different class from the package's."""
    return RELAYS[request.param]


@pytest.fixture
def relay(relay_module, upstream):
    """Both copies of the relay, driven through one identical interface."""
    impl = relay_module
    hostport, seen = upstream
    started = []

    def make(**kw):
        kw.setdefault("upstream", hostport)
        srv, port = impl.start_proxy(REAL_KEY, TOKEN, "127.0.0.1", **kw)
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


def test_responses_without_usage_get_no_suffix(relay, relay_module, monkeypatch):
    """/v1/models reports no tokens; the line must not grow four zero fields."""
    notes = []
    monkeypatch.setattr(relay_module.Handler, "note", lambda self, msg: notes.append(msg))
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


# --- openai-compat ----------------------------------------------------------
#
# These use no plaintext_upstream monkeypatch. The provider declares scheme
# http, so the relay has to choose HTTPConnection by itself; patching the TLS
# class away would hide the bug this is here to catch.


@pytest.fixture
def openai_upstream():
    """A fake OpenAI-shaped server, reachable only over plaintext http."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen["headers"] = {k.lower(): v for k, v in self.headers.items()}
            seen["body"] = json.loads(raw) if raw else None
            payload = json.dumps(
                {
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "prompt_tokens_details": {"cached_tokens": 64},
                    }
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
def compat_relay(openai_upstream):
    hostport, seen = openai_upstream
    started = []

    def make(api_key="", **kw):
        kw.setdefault("upstream", hostport)
        kw.setdefault("provider", providers.OPENAI_COMPAT_PROVIDER)
        srv, port = proxy.start_proxy(api_key, TOKEN, "127.0.0.1", **kw)
        started.append(srv)
        return port, seen, srv

    yield make
    for srv in started:
        srv.shutdown()


def bearer_call(port, path, token=TOKEN, body=None, extra=None):
    headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
    headers.update(extra or {})
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers=headers,
        method="POST" if body else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def chat(model="local-model", cap=None):
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if cap is not None:
        payload["max_completion_tokens"] = cap
    return json.dumps(payload).encode()


def test_plaintext_upstream_is_reached_without_tls(compat_relay):
    """The provider's scheme, not a patched-out HTTPSConnection, is what makes
    a local llama-server reachable."""
    port, seen, _ = compat_relay()
    assert bearer_call(port, "/v1/chat/completions", body=chat())[0] == 200
    assert seen["body"]["model"] == "local-model"


def test_bearer_token_is_checked(compat_relay):
    port, _, _ = compat_relay()
    assert bearer_call(port, "/v1/models", token="guessed")[0] == 401


def test_a_bare_token_without_the_bearer_scheme_is_rejected(compat_relay):
    port, _, _ = compat_relay()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/models", headers={"authorization": TOKEN}
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=20)
    assert e.value.code == 401


def test_no_auth_header_is_written_when_there_is_no_key(compat_relay):
    """A local llama-server has no credential. Forwarding the run token would
    leak the relay's own access control to the upstream."""
    port, seen, _ = compat_relay(api_key="")
    assert bearer_call(port, "/v1/chat/completions", body=chat())[0] == 200
    assert "authorization" not in seen["headers"]


def test_a_key_is_written_as_a_bearer_credential(compat_relay):
    port, seen, _ = compat_relay(api_key="sk-upstream")
    assert bearer_call(port, "/v1/chat/completions", body=chat())[0] == 200
    assert seen["headers"]["authorization"] == "Bearer sk-upstream"


def test_container_credentials_never_reach_an_openai_upstream(compat_relay):
    """The container may present anything; none of it may be forwarded."""
    port, seen, _ = compat_relay(api_key="sk-upstream")
    bearer_call(
        port,
        "/v1/chat/completions",
        body=chat(),
        extra={"x-api-key": "smuggled", "api-key": "smuggled"},
    )
    assert seen["headers"]["authorization"] == "Bearer sk-upstream"
    assert "x-api-key" not in seen["headers"]


def test_the_cap_clamps_the_openai_field_not_max_tokens(compat_relay):
    """max_tokens is Anthropic's name. Clamping it here would leave the real
    limit untouched and silently do nothing."""
    port, seen, _ = compat_relay(max_tokens_cap=4000)
    bearer_call(port, "/v1/chat/completions", body=chat(cap=64000))
    assert seen["body"]["max_completion_tokens"] == 4000
    assert "max_tokens" not in seen["body"]


def test_the_model_allowlist_applies(compat_relay):
    port, _, _ = compat_relay(allow_models={"local-model"})
    assert bearer_call(port, "/v1/chat/completions", body=chat("other"))[0] == 403
    assert bearer_call(port, "/v1/chat/completions", body=chat())[0] == 200


def test_openai_usage_including_nested_cached_tokens_is_logged(compat_relay, monkeypatch):
    notes = []
    monkeypatch.setattr(proxy.Handler, "note", lambda self, msg: notes.append(msg))
    port, _, _ = compat_relay()
    assert bearer_call(port, "/v1/chat/completions", body=chat())[0] == 200
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not notes:
        time.sleep(0.01)
    assert notes, "relay logged nothing"
    assert " in=100 cache_read=64 out=20" in notes[-1]
    assert "cache_write" not in notes[-1]


# --- credential stripping, every provider -----------------------------------


@pytest.fixture
def any_upstream(plaintext_upstream):
    """Records what a forwarded request carried. Plaintext, so the https
    providers reach it through the patched TLS class."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
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


SMUGGLED = {
    "x-api-key": "smuggled-anthropic",
    "authorization": "Bearer smuggled-openai",
    "api-key": "smuggled-azure",
    "proxy-authorization": "Basic smuggled",
}


@pytest.mark.parametrize("name", sorted(providers.PROVIDERS))
def test_no_container_credential_reaches_any_upstream(any_upstream, name):
    """The relay supplies the credential; whatever the container presents is
    dropped. Getting this wrong forwards an attacker-chosen key upstream, and
    it fails silently, so every provider is checked rather than the one whose
    header the relay happens to write."""
    hostport, seen = any_upstream
    provider = providers.get_provider(name)
    real = "sk-REAL-KEY-FROM-THE-HOST"
    srv, port = proxy.start_proxy(
        real, TOKEN, "127.0.0.1", upstream=hostport, provider=provider
    )
    try:
        path = next(p for p in provider.routes if p.endswith("/models"))
        headers = dict(SMUGGLED)
        # The run token has to arrive in the header this provider reads, or the
        # request is rejected before any of this is exercised.
        headers[provider.auth_header] = provider.auth_value(TOKEN)
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            assert r.status == 200
    finally:
        srv.shutdown()

    forwarded = seen["headers"]
    for header, value in SMUGGLED.items():
        assert forwarded.get(header) != value, f"{name} forwarded {header}"
    # The provider's own auth header carries the host's key, nothing else.
    assert forwarded[provider.auth_header] == provider.auth_value(real)
    for header in ("x-api-key", "authorization", "api-key"):
        if header != provider.auth_header:
            assert header not in forwarded, f"{name} leaked {header}"


@pytest.mark.parametrize("name", sorted(providers.PROVIDERS))
def test_the_run_token_is_never_forwarded(any_upstream, name):
    """The token authenticates the container to the relay and means nothing
    upstream. Forwarding it would put the relay's own access control on the
    wire for no reason."""
    hostport, seen = any_upstream
    provider = providers.get_provider(name)
    srv, port = proxy.start_proxy(
        "sk-REAL", TOKEN, "127.0.0.1", upstream=hostport, provider=provider
    )
    try:
        path = next(p for p in provider.routes if p.endswith("/models"))
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            headers={provider.auth_header: provider.auth_value(TOKEN)},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            assert r.status == 200
    finally:
        srv.shutdown()
    assert TOKEN not in " ".join(seen["headers"].values())


# --- stream usage injection -------------------------------------------------
#
# A streamed OpenAI response carries no usage unless the request asked for it,
# so without this the log line reads usage=? on every streamed call. OpenRouter
# speaks the same protocol and sends usage unasked, which is why the flag sits
# on the provider and not on the protocol.


def streaming(**extra):
    payload = {
        "model": "local-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    payload.update(extra)
    return json.dumps(payload).encode()


def test_include_usage_is_added_for_openai(compat_relay):
    port, seen, _ = compat_relay()
    assert bearer_call(port, "/v1/chat/completions", body=streaming())[0] == 200
    assert seen["body"]["stream_options"] == {"include_usage": True}


def test_include_usage_is_added_with_no_other_policy_set(compat_relay):
    """apply_policy used to return early unless --allow-model or
    --max-tokens-cap was given, so injection would never have fired on a plain
    run, which is every run."""
    port, seen, _ = compat_relay(allow_models=None, max_tokens_cap=None)
    bearer_call(port, "/v1/chat/completions", body=streaming())
    assert seen["body"]["stream_options"]["include_usage"] is True


def test_existing_stream_options_are_preserved(compat_relay):
    port, seen, _ = compat_relay()
    body = streaming(stream_options={"something_else": 1})
    bearer_call(port, "/v1/chat/completions", body=body)
    assert seen["body"]["stream_options"] == {
        "something_else": 1,
        "include_usage": True,
    }


def test_a_non_streamed_request_is_untouched(compat_relay):
    """Non-streamed responses carry usage already; the field would be noise."""
    port, seen, _ = compat_relay()
    bearer_call(port, "/v1/chat/completions", body=chat())
    assert "stream_options" not in seen["body"]


def test_openrouter_is_not_given_stream_options(any_upstream):
    """It returns usage in the final chunk unasked, and deviates from the spec
    by putting a non-empty choices array in that chunk."""
    hostport, seen = any_upstream
    provider = providers.get_provider("openrouter")
    srv, port = proxy.start_proxy(
        "sk-REAL", TOKEN, "127.0.0.1", upstream=hostport, provider=provider
    )
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/chat/completions",
            data=streaming(),
            headers={
                "authorization": f"Bearer {TOKEN}",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            assert r.status == 200
    finally:
        srv.shutdown()
    assert "stream_options" not in (seen.get("body") or {})


def test_anthropic_is_not_given_stream_options(relay):
    """Not part of the Messages API; usage arrives in message_start."""
    port, seen, _ = relay()
    body = json.dumps(
        {
            "model": "claude-opus-5",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    assert call(port, "/v1/messages", body=body)[0] == 200
    assert "stream_options" not in seen["body"]


def test_openai_style_sse_usage_is_read():
    """Intermediate chunks carry "usage": null when include_usage is on; only
    the final one has counts, and its choices array is empty."""
    sniffer = proxy.UsageSniffer("text/event-stream", "", providers.OPENAI_CHAT_PROTOCOL)
    for chunk in (
        b'data: {"choices":[{"delta":{"content":"o"}}],"usage":null}\n\n',
        b'data: {"choices":[{"delta":{"content":"k"}}],"usage":null}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":3,'
        b'"prompt_tokens_details":{"cached_tokens":8}}}\n\n',
        b"data: [DONE]\n\n",
    ):
        sniffer.feed(chunk)
    sniffer.close()
    assert sniffer.digest() == " in=11 cache_read=8 out=3"


def test_openrouter_style_sse_usage_is_read():
    """Its final usage chunk keeps a non-empty choices array, unlike OpenAI's."""
    sniffer = proxy.UsageSniffer("text/event-stream", "", providers.OPENAI_CHAT_PROTOCOL)
    sniffer.feed(
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
    )
    sniffer.close()
    assert sniffer.digest() == " in=5 cache_read=0 out=2"


def test_responses_usage_is_read_with_its_own_nesting():
    sniffer = proxy.UsageSniffer(
        "application/json", "", providers.OPENAI_RESPONSES_PROTOCOL
    )
    sniffer.feed(
        json.dumps(
            {
                "usage": {
                    "input_tokens": 40,
                    "output_tokens": 9,
                    "input_tokens_details": {"cached_tokens": 32},
                }
            }
        ).encode()
    )
    sniffer.close()
    assert sniffer.digest() == " in=40 cache_read=32 out=9"


# --- chunked requests -------------------------------------------------------


def test_a_chunked_request_body_is_read_and_forwarded(relay):
    """A client that streams its request sends no Content-Length. Reading zero
    bytes there left the body in the socket, where the next parse read it as a
    request line and answered 400. Measured with prime-agent, whose system
    prompt is large enough that its client streams the request."""
    port, seen, _ = relay()
    payload = json.dumps(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "x" * 5000}],
        }
    ).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request(
        "POST",
        "/v1/messages",
        body=iter([payload[:2000], payload[2000:]]),
        headers={"x-api-key": TOKEN, "content-type": "application/json"},
    )
    response = conn.getresponse()
    response.read()
    assert response.status == 200
    assert seen["body"] == json.loads(payload)
    conn.close()


def test_a_chunked_request_survives_a_second_on_one_connection(relay):
    """The failure this fixes was a leftover body parsed as the next request,
    so one request through a fresh connection proves nothing."""
    port, seen, _ = relay()
    conn = http.client.HTTPConnection("127.0.0.1", port)
    for n in range(2):
        body = json.dumps({"model": "m", "max_tokens": 8, "n": n}).encode()
        conn.request(
            "POST",
            "/v1/messages",
            body=iter([body]),
            headers={"x-api-key": TOKEN, "content-type": "application/json"},
        )
        response = conn.getresponse()
        response.read()
        assert response.status == 200, n
        assert seen["body"]["n"] == n
    conn.close()


def test_a_malformed_chunk_size_is_refused(relay):
    port, _, _ = relay()
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.putrequest("POST", "/v1/messages", skip_accept_encoding=True)
    conn.putheader("x-api-key", TOKEN)
    conn.putheader("content-type", "application/json")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    conn.send(b"zz\r\nnonsense\r\n0\r\n\r\n")
    assert conn.getresponse().status == 400
    conn.close()


def test_a_refused_request_closes_its_connection(relay):
    """The body is still unread when a refusal is written. Reusing the
    connection had the next parse read that body as a request line, and every
    later request on it answered 400."""
    port, _, _ = relay()
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request(
        "POST",
        "/v1/messages",
        body=message(),
        headers={"x-api-key": "wrong", "content-type": "application/json"},
    )
    response = conn.getresponse()
    response.read()
    assert response.status == 401
    assert response.will_close, "a refused connection must not be reused"
    conn.close()
