#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Run a Claude Code agent in a disposable Linux VM via Apple's `container`.

Lifecycle: validate key -> build image if absent -> run one container -> read
the agent's report off a bind mount -> delete the container.

The API key is read from the ANTHROPIC_API_KEY environment variable and passed
with `container run -e ANTHROPIC_API_KEY` (the bare-name form, which tells
`container` to inherit the value from this process). It is never a command-line
argument, so it does not appear in the host's process list. It is still visible
inside the VM and in `container inspect` output while the container exists.
"""

import argparse
import http.client
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import secrets
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_IMAGE = "agentbox:latest"
REPORT_NAME = "REPORT.md"

# The image definition, embedded so this script builds its own image with
# nothing beside it on disk. Kept byte-identical to
# src/agentbox/resources/Containerfile; tests/test_script.py enforces that.
CONTAINERFILE = r"""FROM docker.io/library/node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep ca-certificates curl jq \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# node:22-slim already ships uid 1000 as `node`; reuse it rather than adding one.
RUN mkdir -p /work && chown node:node /work
USER node
ENV HOME=/home/node \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /work
ENTRYPOINT ["claude"]
"""

REPORT_INSTRUCTION = (
    "\n\nWhen you are done, write your findings to ./{report} in the working "
    "directory. That file is the only output that survives; anything you print "
    "to the terminal is discarded when the container is deleted."
)


# --- relay ------------------------------------------------------------------
# The container has no route off the host, so this relay is its only path to
# the API. Since it has to exist anyway, injecting the key here costs two
# lines and keeps the credential on the host.

UPSTREAM = "api.anthropic.com"
# Exact matches, not prefixes: "/v1/models" as a prefix also admits
# "/v1/models-internal-secret".
DEFAULT_ALLOW = frozenset({"/v1/messages", "/v1/messages/count_tokens", "/v1/models"})

# Headers that describe one hop and must not be relayed to the next.
HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
# Credentials arriving from the container are dropped; we supply our own.
STRIP_REQ = HOP | {"host", "x-api-key", "authorization", "content-length"}
STRIP_RESP = HOP | {"content-length"}


class Config:
    def __init__(self, api_key, token, allow_paths, upstream, log_bodies,
                 allow_models=None, max_tokens_cap=None, log_dir=None):
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def cfg(self):
        return self.server.cfg

    def log_message(self, fmt, *a):
        pass  # replaced by explicit logging in relay()

    def note(self, msg):
        print(f"proxy: {msg}", file=sys.stderr, flush=True)

    def refuse(self, code, why):
        with self.cfg.lock:
            self.cfg.rejected += 1
        self.note(f"REJECT {self.client_address[0]} {self.command} {self.path}: {why}")
        body = b'{"type":"error","error":{"type":"forbidden"}}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if not secrets.compare_digest(self.headers.get("x-api-key", ""),
                                      self.cfg.token):
            self.refuse(401, "wrong or missing run token")
            return False
        if urlsplit(self.path).path not in self.cfg.allow_paths:
            self.refuse(403, f"path not in {sorted(self.cfg.allow_paths)}")
            return False
        return True

    def apply_policy(self, body):
        """Enforce model and token limits here, where the container cannot edit
        them. Returns the body to send, or None if the request was refused."""
        cfg = self.cfg
        if not body or (cfg.allow_models is None and cfg.max_tokens_cap is None):
            return body
        if urlsplit(self.path).path != "/v1/messages":
            return body
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.refuse(400, "body is not JSON")
            return None
        if cfg.allow_models is not None and payload.get("model") not in cfg.allow_models:
            self.refuse(403, f"model {payload.get('model')!r} not allowed")
            return None
        if cfg.max_tokens_cap is not None:
            asked = payload.get("max_tokens")
            if not isinstance(asked, int) or asked > cfg.max_tokens_cap:
                payload["max_tokens"] = cfg.max_tokens_cap
                return json.dumps(payload).encode()
        return body

    def log_body(self, body):
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
        digest = (f"body {seq:03d} {len(body) / 1024:.1f}KB "
                  f"model={payload.get('model', '?')} "
                  f"max_tokens={payload.get('max_tokens', '?')} "
                  f"effort={payload.get('output_config', {}).get('effort', '-')} "
                  f"msgs={len(payload.get('messages', []))} "
                  f"tools={len(payload.get('tools', []))}")

        if cfg.log_dir:
            path = os.path.join(cfg.log_dir, f"{seq:03d}.json")
            with open(path, "wb") as fh:
                fh.write(body)
            digest += f" -> {path}"
        self.note(digest)

    def relay(self):
        if not self.authorized():
            return
        cfg = self.cfg
        started = time.monotonic()

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        body = self.apply_policy(body)
        if body is None:
            return
        if cfg.log_bodies and body:
            self.log_body(body)

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in STRIP_REQ}
        headers["Host"] = cfg.upstream
        headers["x-api-key"] = cfg.api_key          # the only place the key appears
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
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.note("client hung up mid-stream")
        finally:
            conn.close()

        with cfg.lock:
            cfg.requests += 1
        self.note(f"{self.client_address[0]} {self.command} {self.path} "
                  f"-> {resp.status} {sent}B {time.monotonic() - started:.1f}s")

    do_GET = do_POST = do_DELETE = do_PUT = relay


def start_proxy(api_key, token, host, port=0, allow_paths=DEFAULT_ALLOW,
          upstream=UPSTREAM, log_bodies=False, allow_models=None,
          max_tokens_cap=None, log_dir=None):
    """Start the relay on a background thread. Returns (server, port).

    `host` is required: binding the right interface is the access control.
    """
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.cfg = Config(api_key, token, allow_paths, upstream, log_bodies,
                     allow_models, max_tokens_cap, log_dir)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def die(msg, code=2):
    print(f"agentbox: {msg}", file=sys.stderr)
    raise SystemExit(code)


def run(cmd, **kw):
    """subprocess.run with a list argv. Never shell=True."""
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


# --- preflight ---------------------------------------------------------------

def require_container_cli():
    if shutil.which("container") is None:
        die("`container` not found on PATH. Install from github.com/apple/container.")
    st = run(["container", "system", "status"], capture_output=True)
    if st.returncode != 0 or "running" not in st.stdout:
        die("container system is not running. Start it with: container system start")


FIREWALL = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def _firewall_entries():
    """[(path, blocked)] from socketfilterfw --listapps."""
    r = run([FIREWALL, "--listapps"], capture_output=True)
    if r.returncode != 0:
        return []
    entries, path = [], None
    for line in r.stdout.splitlines():
        stripped = line.strip()
        head, _, rest = stripped.partition(":")
        if head.strip().isdigit():
            path = rest.strip()
        elif path and stripped.startswith("("):
            entries.append((path, "block" in stripped.lower()))
            path = None
    return entries


def firewall_warning():
    """The macOS application firewall drops container->host connections to a
    blocked binary with no error: the agent's first API call hangs until
    --timeout. Homebrew's python is shipped blocked on this machine; Xcode's
    and uv's are allowed. Framework builds register as Resources/Python.app,
    not as the bin/pythonX.Y that sys.executable resolves to."""
    if not os.path.exists(FIREWALL):
        return
    state = run([FIREWALL, "--getglobalstate"], capture_output=True)
    if state.returncode != 0 or "enabled" not in state.stdout.lower():
        return

    exe = os.path.realpath(sys.executable)
    version_root = os.path.dirname(os.path.dirname(exe))
    names = {sys.executable, exe,
             os.path.join(version_root, "Resources", "Python.app")}

    blocked = [path for path, is_blocked in _firewall_entries()
               if is_blocked and path in names]
    if not blocked:
        return          # unlisted signed interpreters are auto-allowed

    target = blocked[0]
    print(f"agentbox: WARNING the macOS firewall is on and\n"
          f"  {target}\n"
          f"  is set to block incoming connections.\n"
          f"  The agent's calls to the proxy will hang until --timeout. Either\n"
          f"  re-run with /usr/bin/python3, or allow this interpreter once:\n"
          f"    sudo {FIREWALL} --unblockapp {target}", file=sys.stderr)


def validate_key(key, base_url):
    """One cheap request, so a bad key fails in 0.2s instead of ~174s of retries."""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            die(f"{KEY_ENV} rejected by {base_url} (HTTP {e.code}).")
        # Any other status still proves the endpoint answered; let the agent try.
        return True
    except urllib.error.URLError as e:
        die(f"cannot reach {base_url}: {e.reason}")


# --- network ---------------------------------------------------------------

def network_info(name):
    """(gateway, subnet) for a container network, or None if it does not exist."""
    r = run(["container", "network", "inspect", name], capture_output=True)
    if r.returncode != 0:
        return None
    try:
        st = json.loads(r.stdout)[0]["status"]
        return st["ipv4Gateway"], st["ipv4Subnet"]
    except (ValueError, KeyError, IndexError):
        return None


def ensure_network(name):
    """Create `name` as an egress-blocked network if it is not already there."""
    info = network_info(name)
    if info:
        return info
    print(f"agentbox: creating internal network {name}", file=sys.stderr)
    r = run(["container", "network", "create", "--internal", name],
            capture_output=True)
    if r.returncode != 0:
        die(f"could not create network {name}: {r.stderr.strip()}")
    info = network_info(name)
    if not info:
        die(f"network {name} created but has no address")
    return info


def hold_network_up(network, image):
    """Start a placeholder container so the host bridge exists.

    vmnet creates the bridge interface for a network only while a container is
    attached to it. Without this the proxy cannot bind the gateway address and
    would have to fall back to 0.0.0.0, which puts it on Wi-Fi and LAN too.
    """
    name = f"agentbox-hold-{uuid.uuid4().hex[:6]}"
    r = run(["container", "run", "-d", "--name", name, "--network", network,
             "--cpus", "1", "--memory", "256M",
             "--entrypoint", "sleep", image, "86400"], capture_output=True)
    if r.returncode != 0:
        die(f"could not start network holder: {r.stderr.strip()}")
    return name


def wait_for_gateway(gateway, timeout=30):
    """Poll until the gateway address is bindable on this host."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.socket()
            s.bind((gateway, 0))
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


# --- image -------------------------------------------------------------------

def image_exists(image):
    out = run(["container", "image", "list"], capture_output=True)
    if out.returncode != 0:
        return False
    name, _, tag = image.partition(":")
    tag = tag or "latest"
    for line in out.stdout.splitlines()[1:]:
        f = line.split()
        if len(f) >= 2 and f[0].endswith(name) and f[1] == tag:
            return True
    return False


def build_image(image, containerfile=None):
    """Build `image` from `containerfile`, or from the embedded copy.

    With no path given the embedded Containerfile is written to a temporary
    directory and built from there. That directory is the build context, so it
    holds nothing else. An explicit --containerfile that is missing is an
    error rather than a silent fall back to ours.
    """
    if containerfile is None:
        with tempfile.TemporaryDirectory(prefix="agentbox-build-") as tmp:
            cf = Path(tmp) / "Containerfile"
            cf.write_text(CONTAINERFILE)
            return _build(image, cf)
    cf = Path(containerfile).resolve()
    if not cf.is_file():
        die(f"no Containerfile at {cf}")
    return _build(image, cf)


def _build(image, cf):
    print(f"agentbox: building {image} from {cf}", file=sys.stderr)
    r = run(["container", "build", "-t", image, "-f", str(cf), str(cf.parent)])
    if r.returncode != 0:
        die(f"build failed (exit {r.returncode})")


# --- run ---------------------------------------------------------------------

def build_argv(args, name, workdir, task_text, network=None):
    argv = [
        "container", "run",
        "--name", name,
        "--cpus", str(args.cpus),
        "--memory", args.memory,
        "-v", f"{workdir}:/work",
        "-w", "/work",
        "-e", KEY_ENV,                      # bare name: inherit, keep it out of argv
    ]
    # In proxy mode the inherited value is the run token, not the real key, and
    # ANTHROPIC_BASE_URL points back at the host. Both come from the child env
    # (see child_env), so neither appears in this argv or in `ps`.
    if args.proxy or args.base_url:
        argv += ["-e", "ANTHROPIC_BASE_URL"]
    if network:
        argv += ["--network", network]
    for kv in args.env:
        argv += ["-e", kv]
    argv.append(args.image)

    claude = ["-p", task_text, "--output-format", "stream-json", "--verbose"]
    if args.bare:
        claude.append("--bare")
    if args.permission_mode:
        claude += ["--permission-mode", args.permission_mode]
    else:
        claude.append("--dangerously-skip-permissions")
    if args.model:
        claude += ["--model", args.model]
    if args.effort:
        claude += ["--effort", args.effort]
    if args.allowed_tools:
        claude += ["--allowed-tools", args.allowed_tools]
    if args.max_turns:
        claude += ["--max-turns", str(args.max_turns)]
    return argv + claude


def summarize(event, quiet):
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


def launch(argv, timeout, quiet, env=None):
    """Stream stream-json events; return the final `result` event.

    The timer is what enforces --timeout: an agent that hangs without printing
    would never trip a deadline checked inside the read loop.
    """
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True,
                            bufsize=1, env=env)
    timed_out = threading.Event()

    def expire():
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, expire)
    watchdog.start()

    result = None
    try:
        for line in proc.stdout:
            line = line.strip()
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
        die(f"agent exceeded --timeout {timeout}s", code=124)
    return result, proc.returncode


# --- teardown ----------------------------------------------------------------

def destroy(name, keep):
    if keep:
        print(f"agentbox: keeping container {name} "
              f"(`container inspect {name}` exposes the API key; "
              f"`container delete {name}` when done)", file=sys.stderr)
        return
    run(["container", "stop", name], capture_output=True)
    r = run(["container", "delete", name], capture_output=True)
    if r.returncode != 0:
        print(f"agentbox: could not delete {name}: {r.stderr.strip()}", file=sys.stderr)
    else:
        print(f"agentbox: deleted {name}", file=sys.stderr)


# --- cli ---------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="agentbox",
        description="Run a Claude Code agent in a disposable Apple `container` VM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The API key comes from the ANTHROPIC_API_KEY environment variable only.\n"
            "\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...\n"
            "  ./agentbox.py 'Summarise every .py file in this directory.' -w ./work\n"
            "  ./agentbox.py --task-file brief.md -w ./repo --keep\n"
        ),
    )
    p.add_argument("task", nargs="?", help="the task prompt (or use --task-file)")
    p.add_argument("--task-file", type=Path, help="read the task prompt from a file")
    p.add_argument("-w", "--workdir", type=Path, default=Path("./work"),
                   help="host directory bind-mounted at /work (default: ./work)")
    p.add_argument("-o", "--report", type=Path,
                   help="copy the agent's REPORT.md here after the run")

    g = p.add_argument_group("image")
    g.add_argument("-i", "--image", default=DEFAULT_IMAGE,
                   help=f"image to run (default: {DEFAULT_IMAGE})")
    g.add_argument("--containerfile",
                   help="Containerfile used when the image must be built "
                        "(default: the copy embedded in this script)")
    g.add_argument("--rebuild", action="store_true", help="rebuild the image first")

    g = p.add_argument_group("agent")
    g.add_argument("--model", help="model id, e.g. claude-opus-5")
    g.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    g.add_argument("--max-turns", type=int)
    g.add_argument("--allowed-tools", help='e.g. "Read Edit Bash(git *)"')
    g.add_argument("--permission-mode",
                   choices=["acceptEdits", "auto", "bypassPermissions", "manual",
                            "dontAsk", "plan"],
                   help="default: --dangerously-skip-permissions (nobody is "
                        "there to answer a prompt)")
    g.add_argument("--bare", action="store_true",
                   help="claude --bare: no hooks, LSP, plugins, CLAUDE.md "
                        "discovery; auth strictly from ANTHROPIC_API_KEY")
    g.add_argument("--no-report-instruction", action="store_true",
                   help="do not append the write-a-REPORT.md instruction")

    g = p.add_argument_group("vm")
    g.add_argument("--cpus", type=int, default=4)
    g.add_argument("--memory", default="4G")
    g.add_argument("--timeout", type=int, default=900, help="seconds (default: 900)")
    g.add_argument("-e", "--env", action="append", default=[], metavar="K=V",
                   help="extra environment variable (repeatable)")
    g.add_argument("--base-url",
                   help="set ANTHROPIC_BASE_URL inside the container directly")
    g.add_argument("--network", help="attach to this container network")

    g = p.add_argument_group("proxy (key never enters the container)")
    g.add_argument("--proxy", action="store_true",
                   help="run the agent on an egress-blocked network and relay "
                        "its API calls through a host-side proxy that holds the "
                        "key. The container gets a per-run token instead.")
    g.add_argument("--proxy-network", default="agentbox-net",
                   help="internal network to create/use (default: agentbox-net)")
    g.add_argument("--proxy-port", type=int, default=0,
                   help="host port for the proxy (default: an ephemeral one)")
    g.add_argument("--proxy-allow-path", action="append",
                   help="allowed upstream path, matched exactly (repeatable)")
    g.add_argument("--allow-model", action="append",
                   help="restrict the agent to these model ids (repeatable); "
                        "enforced on the host, where the container cannot edit it")
    g.add_argument("--max-tokens-cap", type=int,
                   help="clamp max_tokens on every request the agent sends")
    g.add_argument("--log-bodies", action="store_true",
                   help="record every request body the agent sends upstream: a "
                        "digest line per call, full JSON under --log-dir")
    g.add_argument("--log-dir", type=Path, default=Path("./agentbox-logs"),
                   help="where --log-bodies writes full request JSON (default: "
                        "./agentbox-logs). Deliberately outside the bind mount, "
                        "so the agent cannot read or edit its own audit trail.")

    g = p.add_argument_group("lifecycle")
    g.add_argument("--keep", action="store_true",
                   help="do not delete the container when the run ends")
    g.add_argument("-q", "--quiet", action="store_true",
                   help="suppress the per-event trace")
    g.add_argument("--dry-run", action="store_true",
                   help="print the container command and exit")
    g.add_argument("--skip-key-check", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if bool(args.task) == bool(args.task_file):
        die("give exactly one of: a task argument, or --task-file")
    task = args.task_file.read_text() if args.task_file else args.task
    if not task.strip():
        die("task is empty")
    if not args.no_report_instruction:
        task += REPORT_INSTRUCTION.format(report=REPORT_NAME)

    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        die(f"{KEY_ENV} is not set. export it, then re-run.")

    workdir = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    stale = workdir / REPORT_NAME
    if stale.exists():
        stale.unlink()

    if not args.dry_run and not args.skip_key_check:
        # Before anything is started, so a bad key cannot leak a container.
        validate_key(key, "https://api.anthropic.com" if args.proxy
                     else (args.base_url or "https://api.anthropic.com"))

    name = f"agentbox-{uuid.uuid4().hex[:8]}"
    network, proxy_srv, port, holder = args.network, None, 0, None
    child_env = os.environ.copy()

    if args.proxy:
        require_container_cli()
        firewall_warning()
        network = args.proxy_network
        gateway, _ = ensure_network(network)
        token = secrets.token_urlsafe(24)
        if not args.dry_run:
            if args.rebuild or not image_exists(args.image):
                build_image(args.image, args.containerfile)
            holder = hold_network_up(network, args.image)
            if not wait_for_gateway(gateway):
                destroy(holder, keep=False)
                die(f"{gateway} never became bindable on this host")
            # Bound to the bridge address only: unreachable from Wi-Fi or LAN.
            log_dir = None
            if args.log_bodies:
                log_dir = (args.log_dir / name).resolve()
                log_dir.mkdir(parents=True, exist_ok=True)
                print(f"agentbox: request bodies -> {log_dir}", file=sys.stderr)
            proxy_srv, port = start_proxy(
                key, token, gateway, args.proxy_port,
                allow_paths=args.proxy_allow_path or DEFAULT_ALLOW,
                log_bodies=args.log_bodies, allow_models=args.allow_model,
                max_tokens_cap=args.max_tokens_cap, log_dir=str(log_dir)
                if log_dir else None)
        # The container inherits the token under the name ANTHROPIC_API_KEY.
        # The real key stays in this process and in the proxy thread only.
        child_env[KEY_ENV] = token
        child_env["ANTHROPIC_BASE_URL"] = f"http://{gateway}:{port}"
    elif args.base_url:
        child_env["ANTHROPIC_BASE_URL"] = args.base_url

    cmd = build_argv(args, name, workdir, task, network=network)

    if args.dry_run:
        print(shlex.join(cmd))
        if args.proxy:
            print(f"# proxy: {gateway} -> https://api.anthropic.com")
            print(f"# container env: {KEY_ENV}=<run token> "
                  f"ANTHROPIC_BASE_URL={child_env['ANTHROPIC_BASE_URL']}")
        return 0

    require_container_cli()
    if args.rebuild or not image_exists(args.image):
        build_image(args.image, args.containerfile)

    if args.proxy:
        print(f"agentbox: proxy bound to {gateway}:{port} (bridge only); "
              f"{network} has no route off the host", file=sys.stderr)
    print(f"agentbox: {name} -> {workdir}", file=sys.stderr)
    started = time.monotonic()
    try:
        result, rc = launch(cmd, args.timeout, args.quiet, env=child_env)
    except KeyboardInterrupt:
        destroy(name, keep=False)
        die("interrupted", code=130)
    except SystemExit:
        # A timeout kill must not leave a container alive holding the key.
        destroy(name, keep=False)
        raise
    finally:
        if proxy_srv:
            proxy_srv.shutdown()
            c = proxy_srv.cfg
            print(f"agentbox: proxy relayed {c.requests}, rejected {c.rejected}",
                  file=sys.stderr)
        if holder:
            destroy(holder, keep=False)
    destroy(name, args.keep)

    report = workdir / REPORT_NAME
    print(f"\nagentbox: {time.monotonic() - started:.1f}s wall", file=sys.stderr)
    if result:
        u = result.get("usage", {})
        cached = u.get("cache_read_input_tokens", 0)
        total_in = (u.get("input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0) + cached)
        print(f"agentbox: {result.get('num_turns', '?')} turns, "
              f"{total_in:,} in ({cached:,} cached) / "
              f"{u.get('output_tokens', 0):,} out, "
              f"${result.get('total_cost_usd', 0):.4f}", file=sys.stderr)
        if result.get("is_error"):
            print(f"agentbox: agent reported an error: {result.get('result')}",
                  file=sys.stderr)
            return 1

    if report.is_file():
        if args.report:
            shutil.copy(report, args.report)
            print(f"agentbox: report -> {args.report}", file=sys.stderr)
        else:
            print(f"agentbox: report -> {report}", file=sys.stderr)
    else:
        print(f"agentbox: the agent wrote no {REPORT_NAME}", file=sys.stderr)
        if result and result.get("result"):
            print(f"\n{result['result']}")
    return 0 if rc == 0 else rc


if __name__ == "__main__":
    sys.exit(main())
