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
        self.usage: dict[str, int] = {}
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


def _flatten(usage: object, prefix: str = "") -> dict[str, int]:
    """Integer counters from a usage block, nested keys joined with a dot.

    OpenAI reports cached tokens as prompt_tokens_details.cached_tokens, two
    levels down. A flat scan drops it silently and the log line then reads
    cache_read=0, which is indistinguishable from a genuine cache miss.
    """
    out: dict[str, int] = {}
    if not isinstance(usage, dict):
        return out
    for key, value in usage.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
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
        body = b'{"type":"error","error":{"type":"forbidden"}}'
        self.send_response(code)
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

    def relay(self) -> None:
        if not self.authorized():
            return
        cfg = self.cfg
        started = time.monotonic()

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
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
        try:
            while True:
                # read1, not read: read(n) blocks until n bytes arrive, which
                # would stall every server-sent event behind a full buffer.
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
                sent += len(chunk)
                sniffer.feed(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            sniffer.close()
        except (BrokenPipeError, ConnectionResetError):
            self.note("client hung up mid-stream")
        finally:
            conn.close()

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
        allow_models,
        max_tokens_cap,
        log_dir,
        provider,
    )
    srv = ProxyServer((host, port), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, int(srv.server_address[1])
