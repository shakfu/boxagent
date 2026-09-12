"""Host-side relay between the container and the model provider.

The container has no route off the host, so this relay is its only path to the
API. Since it has to exist anyway, injecting the key here costs two lines and
keeps the credential on the host.

Provider coupling lives in `sanduk.providers`. The constants below are the
Anthropic defaults, kept as module names because `scripts/sanduk.py` shares
them.
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import sys
import threading
import time
import zlib
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlsplit

from sanduk.providers import ANTHROPIC, ANTHROPIC_PROVIDER, PROTOCOLS, Protocol, Provider

UPSTREAM = "api.anthropic.com"
# Exact matches, not prefixes: "/v1/models" as a prefix also admits
# "/v1/models-internal-secret".
DEFAULT_ALLOW = frozenset({"/v1/messages", "/v1/messages/count_tokens", "/v1/models"})

# Headers that describe one hop and must not be relayed to the next.
HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
# Every credential header we know of, not just the one the active provider
# reads. A header that is inert for one provider is the credential for another:
# `api-key` means nothing to Anthropic and is the key for Azure OpenAI. Dropping
# only the active provider's would forward the rest.
CREDENTIAL_HEADERS = {
    "x-api-key",
    "authorization",
    "api-key",
    "x-goog-api-key",
}
# Credentials arriving from the container are dropped; we supply our own.
# accept-encoding is dropped and re-offered as gzip in `relay`: the API prefers
# brotli when a client lists it, and nothing in the standard library decodes it.
STRIP_REQ = (
    HOP
    | CREDENTIAL_HEADERS
    | {
        "host",
        "content-length",
        "accept-encoding",
    }
)
STRIP_RESP = HOP | {"content-length"}

# What to call a refusal, by status. An agent branches on these strings.
REFUSAL_KINDS = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "budget_exceeded",
    403: "forbidden",
}


# A chunk-size line is a few hex digits; anything longer is not one.
CHUNK_LINE_MAX = 1024


class Config:
    """One run's relay policy and counters."""

    def __init__(
        self,
        api_key: str,
        token: str,
        allow_paths: Iterable[str] | None,
        upstream: str,
        log_bodies: bool,
        allow_models: Iterable[str] | None = None,
        max_tokens_cap: int | None = None,
        budget: float | None = None,
        log_dir: str | None = None,
        provider: Provider | None = None,
    ) -> None:
        self.api_key = api_key
        self.token = token
        self.provider = provider or ANTHROPIC_PROVIDER
        # None means the provider's own routes. A bare list of paths keeps the
        # protocol the provider declares for each; a path the provider does not
        # know (--proxy-allow-path) is admitted with no protocol, so no body
        # policy fires on it.
        paths = self.provider.routes if allow_paths is None else allow_paths
        self.routes: dict[str, str | None] = {
            path: self.provider.routes.get(path) for path in paths
        }
        self.allow_paths = frozenset(self.routes)
        self.upstream = upstream
        self.log_bodies = log_bodies
        self.allow_models = frozenset(allow_models) if allow_models else None
        self.max_tokens_cap = max_tokens_cap
        # Dollars, and what has been spent of them. Enforced between calls:
        # a streamed response reports its cost at the end, so the call that
        # crosses the line is paid for before the line is seen.
        self.budget = budget
        self.spent = 0.0
        # Held across a budgeted call from the check to the accounting, so the
        # ceiling is crossed by one call rather than by every call in flight.
        self.gate = threading.Lock()
        # Calls whose cost could not be read. Spending against a total that is
        # known to be short is what `--budget` exists to prevent, so any is
        # enough to stop the run.
        self.unpriced = 0
        self.log_dir = log_dir
        self.body_seq = 0
        self.requests = 0
        self.rejected = 0
        self.lock = threading.Lock()

    def protocol(self, path: str) -> Protocol | None:
        """The wire protocol declared for `path`, or None if it carries none."""
        name = self.routes.get(path)
        return PROTOCOLS[name] if name else None


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], cfg: Config) -> None:
        super().__init__(addr, Handler)
        self.cfg = cfg


class UsageSniffer:
    """Token counts pulled from a relayed response.

    Two shapes, because Claude Code uses both: server-sent events carry usage in
    `message_start` and `message_delta`, and a plain JSON response carries it
    once at the top level. Only SSE lines holding `"usage"` are parsed, so a
    stream is still forwarded chunk by chunk; a JSON body has nothing to read
    until it is whole, so it is buffered to `LIMIT` and parsed at the end.
    """

    LIMIT = 1 << 18
    # Print order. A counter the protocol does not declare is omitted rather
    # than printed as 0, which would be indistinguishable from a real zero.
    ORDER = ("in", "cache_write", "cache_read", "out")

    def __init__(
        self,
        content_type: str,
        content_encoding: str = "",
        protocol: Protocol = ANTHROPIC,
    ) -> None:
        self.usage: dict[str, float] = {}
        self._fields = protocol.usage_fields
        self._sse = "text/event-stream" in content_type
        self._buf = b""
        # wbits 47 reads the gzip header rather than assuming raw deflate.
        self._unzip = (
            zlib.decompressobj(47) if "gzip" in content_encoding.lower() else None
        )

    def feed(self, chunk: bytes) -> None:
        if self._unzip is not None:
            try:
                chunk = self._unzip.decompress(chunk)
            except zlib.error:
                self._unzip = None
                self._buf = b""
                return
            if not chunk:
                return
        if not self._sse:
            if len(self._buf) < self.LIMIT:
                self._buf += chunk
            return
        lines = (self._buf + chunk).split(b"\n")
        self._buf = lines.pop()
        for line in lines:
            if line.startswith(b"data: ") and b'"usage"' in line:
                self._take(line[6:])

    def close(self) -> None:
        if not self._sse and len(self._buf) < self.LIMIT:
            self._take(self._buf)

    def _take(self, raw: bytes) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        message = event.get("message")
        found = event.get("usage") or (message or {}).get("usage") or {}
        self.usage.update(_flatten(found))

    def digest(self, expected: bool = False) -> str:
        u = self.usage
        if not u:
            return " usage=?" if expected else ""
        f = self._fields
        return "".join(f" {name}={u.get(f[name], 0)}" for name in self.ORDER if name in f)


def _flatten(usage: object, prefix: str = "") -> dict[str, float]:
    """Integer counters from a usage block, nested keys joined with a dot.

    OpenAI reports cached tokens as prompt_tokens_details.cached_tokens, two
    levels down. A flat scan drops it silently and the log line then reads
    cache_read=0, which is indistinguishable from a genuine cache miss.
    """
    out: dict[str, float] = {}
    if not isinstance(usage, dict):
        return out
    for key, value in usage.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            out[f"{prefix}{key}"] = value
        elif isinstance(value, dict):
            out.update(_flatten(value, f"{prefix}{key}."))
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def cfg(self) -> Config:
        return cast(ProxyServer, self.server).cfg

    def log_message(self, fmt: str, *a: object) -> None:
        pass  # replaced by explicit logging in relay()

    def note(self, msg: str) -> None:
        print(f"proxy: {msg}", file=sys.stderr, flush=True)

    def refuse(self, code: int, why: str) -> None:
        with self.cfg.lock:
            self.cfg.rejected += 1
        self.note(f"REJECT {self.client_address[0]} {self.command} {self.path}: {why}")
        # The reason, not just the status: an agent that only sees "forbidden"
        # logs that, retries on it, and tells its user nothing. This is
        # sanduk's own policy talking to a container that already knows it is
        # behind a relay, so there is nothing here it does not know.
        body = json.dumps(
            {
                "type": "error",
                "error": {"type": REFUSAL_KINDS.get(code, "forbidden"), "message": why},
            }
        ).encode()
        self.send_response(code)
        # The request body is still in the socket, unread: a refusal happens
        # before it is worth reading. Reusing the connection had the next parse
        # read that body as a request line and answer 400 to everything after
        # it. The header both tells the client and sets close_connection.
        self.send_header("Connection", "close")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        p = self.cfg.provider
        presented = p.presented(self.headers.get(p.auth_header, ""))
        if not secrets.compare_digest(presented, self.cfg.token):
            self.refuse(401, f"wrong or missing run token in {p.auth_header}")
            return False
        if urlsplit(self.path).path not in self.cfg.allow_paths:
            self.refuse(403, f"path not in {sorted(self.cfg.allow_paths)}")
            return False
        return True

    def apply_policy(self, body: bytes | None) -> tuple[bytes | None, bool]:
        """Enforce model and token limits here, where the container cannot edit
        them, and add whatever the provider needs to report usage.

        Returns (body to send, keep going). The flag is separate from the body
        because a bodyless GET is allowed and also has no body to send.
        """
        cfg = self.cfg
        proto = cfg.protocol(urlsplit(self.path).path)
        if not body or proto is None:
            return body, True
        policed = cfg.allow_models is not None or cfg.max_tokens_cap is not None
        if not policed and not cfg.provider.stream_usage_option:
            return body, True
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.refuse(400, "body is not JSON")
            return None, False
        if cfg.allow_models is not None and payload.get("model") not in cfg.allow_models:
            self.refuse(403, f"model {payload.get('model')!r} not allowed")
            return None, False
        edited = False
        if cfg.max_tokens_cap is not None:
            asked = payload.get(proto.cap_field)
            if not isinstance(asked, int) or asked > cfg.max_tokens_cap:
                payload[proto.cap_field] = cfg.max_tokens_cap
                edited = True
        if cfg.provider.stream_usage_option and payload.get("stream"):
            options = payload.get("stream_options")
            if not isinstance(options, dict):
                options = {}
            if options.get("include_usage") is not True:
                options["include_usage"] = True
                payload["stream_options"] = options
                edited = True
        return (json.dumps(payload).encode() if edited else body), True

    def log_body(self, body: bytes) -> None:
        """One digest line to stderr; the full body to a file if log_dir is set.

        Bodies contain the system prompt, every tool schema, and the contents of
        every file the agent has read, so they belong in a file the agent cannot
        reach, not in terminal scrollback.
        """
        cfg = self.cfg
        with cfg.lock:
            cfg.body_seq += 1
            seq = cfg.body_seq

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        digest = (
            f"body {seq:03d} {len(body) / 1024:.1f}KB "
            f"model={payload.get('model', '?')} "
            f"max_tokens={payload.get('max_tokens', '?')} "
            f"effort={payload.get('output_config', {}).get('effort', '-')} "
            f"msgs={len(payload.get('messages', []))} "
            f"tools={len(payload.get('tools', []))} "
            f"stream={payload.get('stream', False)}"
        )

        if cfg.log_dir:
            path = os.path.join(cfg.log_dir, f"{seq:03d}.json")
            with open(path, "wb") as fh:
                fh.write(body)
            digest += f" -> {path}"
        self.note(digest)

    def read_body(self) -> bytes | None:
        """The request body, however it arrived.

        A client that streams its request sends `Transfer-Encoding: chunked`
        and no `Content-Length`. Reading zero bytes there left the body in the
        socket, where the next parse read it as a request line and answered
        400. Measured with prime-agent, whose system prompt is large enough
        that its client streams the request.
        """
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self.read_chunked()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else None

    def read_chunked(self) -> bytes:
        """De-chunk into memory. The relay forwards with a Content-Length of
        its own, and `transfer-encoding` is a hop header it drops anyway."""
        body = bytearray()
        while True:
            line = self.rfile.readline(CHUNK_LINE_MAX).strip()
            size = int(line.split(b";")[0] or b"0", 16)
            if size == 0:
                break
            body += self.rfile.read(size)
            self.rfile.read(2)  # the CRLF that ends each chunk
        # Trailers, then the blank line that closes them.
        while self.rfile.readline(CHUNK_LINE_MAX).strip():
            pass
        return bytes(body)

    def charge(self, sniffer: UsageSniffer, status: int, proto: Protocol | None) -> None:
        """Record what this call cost, or record that it could not be read.

        A response that owes a usage block and carries none used to count as
        zero, which is indistinguishable from a free call and lets a run spend
        past its ceiling without the total moving. It is counted as unpriced
        instead, and the next call is refused rather than charged against a
        figure known to be short.
        """
        cfg = self.cfg
        field = cfg.provider.cost_field
        if not field:
            return
        cost = sniffer.usage.get(field)
        if cost is not None:
            with cfg.lock:
                cfg.spent += float(cost)
        elif status < 400 and proto is not None:
            # Only where one was owed: an error costs nothing, and a path
            # admitted by --proxy-allow-path declares no protocol at all.
            with cfg.lock:
                cfg.unpriced += 1

    def relay(self) -> None:
        if not self.authorized():
            return
        cfg = self.cfg
        if cfg.budget is None:
            self.forward()
            return
        # One budgeted call at a time, from the check through the accounting.
        # Checking `spent` and updating it after the response completes bounds
        # nothing on its own: every call in flight has already passed the check.
        # An agent holding the run token can open as many as it likes, and five
        # concurrent ones spent $2.00 against a $1.00 ceiling with none refused.
        #
        # Serialising is the only bound available here. Reserving credit up
        # front would need a price for a call that has not been made, and the
        # relay deliberately keeps no price table: it reads what the provider
        # charged out of the response. The cost is that a budgeted run relays
        # one call at a time, which is the trade `--budget` already implies.
        with cfg.gate:
            if cfg.unpriced:
                self.refuse(
                    402,
                    f"{cfg.unpriced} call(s) reported no cost, so ${cfg.spent:.4f} "
                    f"is a floor rather than a total and a ${cfg.budget:.4f} "
                    f"ceiling cannot be enforced. Re-run without --budget to "
                    f"continue",
                )
                return
            if cfg.spent >= cfg.budget:
                # Stop after crossing, not before: what the next call will cost
                # is not knowable, and every estimate of it is wrong in one
                # direction or the other. The ceiling is a line the run stops
                # past, and the message says so rather than reading like
                # arithmetic gone wrong.
                self.refuse(
                    402,
                    f"over budget: ${cfg.spent:.4f} spent against a "
                    f"${cfg.budget:.4f} ceiling. The call that crossed it was "
                    f"already paid for",
                )
                return
            self.forward()

    def forward(self) -> None:
        cfg = self.cfg
        started = time.monotonic()

        try:
            body = self.read_body()
        except ValueError:
            # What is left in the socket is not a request, so this connection
            # cannot be reused: the next parse would answer 400 as well.
            self.close_connection = True
            self.refuse(400, "malformed chunked request body")
            return
        body, keep_going = self.apply_policy(body)
        if not keep_going:
            return
        if cfg.log_bodies and body:
            self.log_body(body)

        headers = {k: v for k, v in self.headers.items() if k.lower() not in STRIP_REQ}
        headers["Host"] = cfg.upstream
        # The only place the key appears. A provider with no upstream auth (a
        # local llama-server) gets no header at all; the container's own
        # credentials are stripped either way by STRIP_REQ.
        if cfg.api_key:
            headers[cfg.provider.auth_header] = cfg.provider.auth_value(cfg.api_key)
        # Narrow the offer only for clients that already accept gzip; anything
        # else is forwarded verbatim, so `identity` stays `identity`.
        accepted = self.headers.get("accept-encoding", "")
        if "gzip" in accepted.lower():
            headers["Accept-Encoding"] = "gzip"
        elif accepted:
            headers["Accept-Encoding"] = accepted
        if body is not None:
            headers["Content-Length"] = str(len(body))

        try:
            # Resolved per call, not at import: the test suite swaps the TLS
            # class for a plaintext one.
            connect = (
                http.client.HTTPSConnection
                if cfg.provider.scheme == "https"
                else http.client.HTTPConnection
            )
            conn = connect(cfg.upstream, timeout=900)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            self.note(f"upstream failed: {e}")
            self.send_error(502, "upstream unreachable")
            return

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in STRIP_RESP:
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        proto = cfg.protocol(urlsplit(self.path).path)
        sniffer = UsageSniffer(
            resp.getheader("Content-Type", ""),
            resp.getheader("Content-Encoding", ""),
            proto or ANTHROPIC,
        )
        sent = 0
        gone = charged = False
        try:
            while True:
                # read1, not read: read(n) blocks until n bytes arrive, which
                # would stall every server-sent event behind a full buffer.
                chunk = resp.read1(65536)
                if not chunk:
                    break
                sent += len(chunk)
                sniffer.feed(chunk)
                if gone:
                    continue
                try:
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # Keep draining upstream rather than returning here. The
                    # call is billed whether or not the client stayed to read
                    # it, and what it cost arrives at the end of the stream:
                    # leaving early recorded a real charge as zero.
                    self.note("client hung up mid-stream; still reading the cost")
                    self.close_connection = gone = True
            sniffer.close()
            # Before the client sees the end of the response: it may send the
            # next request the moment it does, and a budget checked against a
            # total that has not caught up would let that one through.
            self.charge(sniffer, resp.status, proto)
            charged = True
            if not gone:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.note("client hung up mid-stream")
        finally:
            conn.close()
            if not charged:
                # However this ended, the call was made and the provider
                # billed it. Leaving on the exception recorded that as zero.
                sniffer.close()
                self.charge(sniffer, resp.status, proto)

        with cfg.lock:
            cfg.requests += 1
        self.note(
            f"{self.client_address[0]} {self.command} {self.path} "
            f"-> {resp.status} {sent}B {time.monotonic() - started:.1f}s"
            f"{sniffer.digest(expected=proto is not None)}"
        )

    do_GET = do_POST = do_DELETE = do_PUT = relay


def start_proxy(
    api_key: str,
    token: str,
    host: str,
    port: int = 0,
    allow_paths: Iterable[str] | None = None,
    upstream: str | None = None,
    log_bodies: bool = False,
    allow_models: Iterable[str] | None = None,
    max_tokens_cap: int | None = None,
    budget: float | None = None,
    log_dir: str | None = None,
    provider: Provider | None = None,
) -> tuple[ProxyServer, int]:
    """Start the relay on a background thread. Returns (server, port).

    `host` is required: binding the right interface is the access control.
    `allow_paths` and `upstream` default to the provider's, so a caller that
    names a provider cannot silently inherit another one's allowlist.
    """
    cfg = Config(
        api_key,
        token,
        allow_paths,
        upstream or (provider or ANTHROPIC_PROVIDER).host,
        log_bodies,
        allow_models=allow_models,
        max_tokens_cap=max_tokens_cap,
        budget=budget,
        log_dir=log_dir,
        provider=provider,
    )
    srv = ProxyServer((host, port), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, int(srv.server_address[1])
