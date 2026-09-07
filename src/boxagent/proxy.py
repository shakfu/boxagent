"""Host-side relay between the container and the model provider.

The container has no route off the host, so this relay is its only path to the
API. Since it has to exist anyway, injecting the key here costs two lines and
keeps the credential on the host.

Provider coupling is confined to the module constants below and to the
`x-api-key` header written in `Handler.relay`.
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
# Credentials arriving from the container are dropped; we supply our own.
# accept-encoding is dropped and re-offered as gzip in `relay`: the API prefers
# brotli when a client lists it, and nothing in the standard library decodes it.
STRIP_REQ = HOP | {
    "host",
    "x-api-key",
    "authorization",
    "content-length",
    "accept-encoding",
}
STRIP_RESP = HOP | {"content-length"}


class Config:
    """One run's relay policy and counters."""

    def __init__(
        self,
        api_key: str,
        token: str,
        allow_paths: Iterable[str],
        upstream: str,
        log_bodies: bool,
        allow_models: Iterable[str] | None = None,
        max_tokens_cap: int | None = None,
        log_dir: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.token = token
        self.allow_paths = frozenset(allow_paths)
        self.upstream = upstream
        self.log_bodies = log_bodies
        self.allow_models = frozenset(allow_models) if allow_models else None
        self.max_tokens_cap = max_tokens_cap
        self.log_dir = log_dir
        self.body_seq = 0
        self.requests = 0
        self.rejected = 0
        self.lock = threading.Lock()


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

    def __init__(self, content_type: str, content_encoding: str = "") -> None:
        self.usage: dict[str, int] = {}
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
        self.usage.update({k: v for k, v in found.items() if isinstance(v, int)})

    def digest(self, expected: bool = False) -> str:
        u = self.usage
        if not u:
            return " usage=?" if expected else ""
        return (
            f" in={u.get('input_tokens', 0)}"
            f" cache_write={u.get('cache_creation_input_tokens', 0)}"
            f" cache_read={u.get('cache_read_input_tokens', 0)}"
            f" out={u.get('output_tokens', 0)}"
        )


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
        if not secrets.compare_digest(self.headers.get("x-api-key", ""), self.cfg.token):
            self.refuse(401, "wrong or missing run token")
            return False
        if urlsplit(self.path).path not in self.cfg.allow_paths:
            self.refuse(403, f"path not in {sorted(self.cfg.allow_paths)}")
            return False
        return True

    def apply_policy(self, body: bytes | None) -> tuple[bytes | None, bool]:
        """Enforce model and token limits here, where the container cannot edit
        them. Returns (body to send, keep going). The flag is separate from the
        body because a bodyless GET is allowed and also has no body to send."""
        cfg = self.cfg
        if not body or (cfg.allow_models is None and cfg.max_tokens_cap is None):
            return body, True
        if urlsplit(self.path).path != "/v1/messages":
            return body, True
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.refuse(400, "body is not JSON")
            return None, False
        if cfg.allow_models is not None and payload.get("model") not in cfg.allow_models:
            self.refuse(403, f"model {payload.get('model')!r} not allowed")
            return None, False
        if cfg.max_tokens_cap is not None:
            asked = payload.get("max_tokens")
            if not isinstance(asked, int) or asked > cfg.max_tokens_cap:
                payload["max_tokens"] = cfg.max_tokens_cap
                return json.dumps(payload).encode(), True
        return body, True

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
        headers["x-api-key"] = cfg.api_key  # the only place the key appears
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
            conn = http.client.HTTPSConnection(cfg.upstream, timeout=900)
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

        sniffer = UsageSniffer(
            resp.getheader("Content-Type", ""), resp.getheader("Content-Encoding", "")
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
            f"{sniffer.digest(expected=urlsplit(self.path).path == '/v1/messages')}"
        )

    do_GET = do_POST = do_DELETE = do_PUT = relay


def start_proxy(
    api_key: str,
    token: str,
    host: str,
    port: int = 0,
    allow_paths: Iterable[str] = DEFAULT_ALLOW,
    upstream: str = UPSTREAM,
    log_bodies: bool = False,
    allow_models: Iterable[str] | None = None,
    max_tokens_cap: int | None = None,
    log_dir: str | None = None,
) -> tuple[ProxyServer, int]:
    """Start the relay on a background thread. Returns (server, port).

    `host` is required: binding the right interface is the access control.
    """
    cfg = Config(
        api_key,
        token,
        allow_paths,
        upstream,
        log_bodies,
        allow_models,
        max_tokens_cap,
        log_dir,
    )
    srv = ProxyServer((host, port), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, int(srv.server_address[1])
