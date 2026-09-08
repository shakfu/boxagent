"""Command line entry point: parse flags, wire the pieces, tear everything down.

Lifecycle: validate key -> build image if absent -> run one container -> read
the agent's report off a bind mount -> delete the container.

The API key is read from the provider's environment variable (ANTHROPIC_API_KEY
by default) and passed with the bare-name `-e` form, which tells the engine to
inherit the value from this process. It is
never a command-line argument, so it does not appear in the host's process
list. Without --proxy it is still visible inside the container and in `inspect`
output while the container exists.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shlex
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from sanduk.agent import (
    DEFAULT_AGENT,
    REPORT_INSTRUCTION,
    REPORT_NAME,
    Agent,
    Outcome,
    Wiring,
    agent_names,
    get_agent,
    launch,
)
from sanduk.errors import AgentboxError
from sanduk.preflight import firewall_warning, validate_key
from sanduk.providers import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    Provider,
    get_provider,
    parse_upstream,
)
from sanduk.proxy import ProxyServer, start_proxy
from sanduk.runtime import (
    DEFAULT_RUNTIME,
    RUNTIMES,
    ContainerSpec,
    get_runtime,
    wait_for_gateway,
)
from sanduk.util import note


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sanduk",
        description="Run an agent in a disposable container.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The API key comes from the provider's environment variable only.\n"
            "\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...\n"
            "  sanduk 'Summarise every .py file in this directory.' -w ./work\n"
            "  sanduk --task-file brief.md -w ./repo --keep\n"
            "  sanduk 'Review this.' --provider openai-compat \\\n"
            "      --upstream http://127.0.0.1:8080 --proxy\n"
        ),
    )
    p.add_argument("task", nargs="?", help="the task prompt (or use --task-file)")
    p.add_argument("--task-file", type=Path, help="read the task prompt from a file")
    p.add_argument(
        "-w",
        "--workdir",
        type=Path,
        default=Path("./work"),
        help="host directory bind-mounted at /work (default: ./work)",
    )
    p.add_argument(
        "-o", "--report", type=Path, help="copy the agent's REPORT.md here after the run"
    )
    p.add_argument(
        "--runtime",
        choices=sorted(RUNTIMES),
        default=DEFAULT_RUNTIME,
        help=f"container engine (default: {DEFAULT_RUNTIME})",
    )

    g = p.add_argument_group("image")
    g.add_argument(
        "-i",
        "--image",
        help="image to run (default: the agent's own, e.g. sanduk:latest)",
    )
    g.add_argument(
        "--containerfile",
        type=Path,
        help="Containerfile used when the image must be built "
        "(default: the agent's, shipped in the package)",
    )
    g.add_argument("--rebuild", action="store_true", help="rebuild the image first")

    g = p.add_argument_group("agent")
    g.add_argument(
        "--agent",
        default=DEFAULT_AGENT,
        metavar="NAME",
        # Not argparse choices: a handler outside the registry is named as
        # module:Class, which choices cannot express.
        help=f"agent handler (default: {DEFAULT_AGENT}; installed: "
        f"{', '.join(agent_names())}), or module:Class for an unpackaged one. "
        "claude speaks Anthropic Messages only; hax also speaks OpenAI Chat "
        "Completions, which is what the other providers need.",
    )
    g.add_argument("--model", help="model id, e.g. claude-opus-5")
    g.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    g.add_argument("--max-turns", type=int)
    g.add_argument("--allowed-tools", help='claude only; e.g. "Read Edit Bash(git *)"')
    g.add_argument(
        "--permission-mode",
        choices=["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"],
        help="claude only; default: --dangerously-skip-permissions (nobody is "
        "there to answer a prompt)",
    )
    g.add_argument(
        "--bare",
        action="store_true",
        help="drop project context: no hooks, LSP, plugins, or CLAUDE.md / "
        "AGENTS.md discovery",
    )
    g.add_argument(
        "--no-report-instruction",
        action="store_true",
        help="do not append the write-a-REPORT.md instruction",
    )

    g = p.add_argument_group("container")
    g.add_argument("--cpus", type=int, default=4)
    g.add_argument("--memory", default="4G")
    g.add_argument("--timeout", type=int, default=900, help="seconds (default: 900)")
    g.add_argument(
        "-e",
        "--env",
        action="append",
        default=[],
        metavar="K=V",
        help="extra environment variable (repeatable)",
    )
    g.add_argument(
        "--base-url",
        help="point the agent at this endpoint directly, without a relay",
    )
    g.add_argument("--network", help="attach to this container network")

    g = p.add_argument_group("provider")
    g.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default=DEFAULT_PROVIDER,
        help=f"upstream API and its wire protocol (default: {DEFAULT_PROVIDER})",
    )
    g.add_argument(
        "--upstream",
        help="where the relay forwards, as scheme://host:port with no path "
        "(e.g. http://127.0.0.1:8080 for a local llama-server). Defaults to "
        "the provider's own endpoint.",
    )
    g.add_argument(
        "--insecure-upstream",
        action="store_true",
        help="permit a plaintext http upstream that is not loopback. The API "
        "key is then sent in clear.",
    )
    g.add_argument(
        "--agent-key-env",
        help="environment variable the agent reads its credential from inside "
        "the container (default: the provider's)",
    )
    g.add_argument(
        "--agent-base-url-env",
        help="environment variable the agent reads its base URL from inside "
        "the container (default: the provider's)",
    )

    g = p.add_argument_group("proxy (key never enters the container)")
    g.add_argument(
        "--proxy",
        action="store_true",
        help="run the agent on an egress-blocked network and relay "
        "its API calls through a host-side proxy that holds the "
        "key. The container gets a per-run token instead.",
    )
    g.add_argument(
        "--proxy-network",
        default="sanduk-net",
        help="internal network to create/use (default: sanduk-net)",
    )
    g.add_argument(
        "--proxy-port",
        type=int,
        default=0,
        help="host port for the proxy (default: an ephemeral one)",
    )
    g.add_argument(
        "--proxy-allow-path",
        action="append",
        help="allowed upstream path, matched exactly (repeatable)",
    )
    g.add_argument(
        "--allow-model",
        action="append",
        help="restrict the agent to these model ids (repeatable); "
        "enforced on the host, where the container cannot edit it",
    )
    g.add_argument(
        "--max-tokens-cap",
        type=int,
        help="clamp max_tokens on every request the agent sends",
    )
    g.add_argument(
        "--log-bodies",
        action="store_true",
        help="record every request body the agent sends upstream: a "
        "digest line per call, full JSON under --log-dir",
    )
    g.add_argument(
        "--log-dir",
        type=Path,
        default=Path("./sanduk-logs"),
        help="where --log-bodies writes full request JSON (default: "
        "./sanduk-logs). Deliberately outside the bind mount, "
        "so the agent cannot read or edit its own audit trail.",
    )

    g = p.add_argument_group("lifecycle")
    g.add_argument(
        "--keep",
        action="store_true",
        help="do not delete the container when the run ends",
    )
    g.add_argument(
        "-q", "--quiet", action="store_true", help="suppress the per-event trace"
    )
    g.add_argument(
        "--dry-run", action="store_true", help="print the container command and exit"
    )
    g.add_argument("--skip-key-check", action="store_true")
    return p.parse_args(argv)


def resolve_provider(args: argparse.Namespace) -> Provider:
    """The provider record, with --upstream applied if given."""
    provider = get_provider(args.provider)
    if args.upstream:
        scheme, host = parse_upstream(args.upstream, args.insecure_upstream)
        provider = replace(provider, scheme=scheme, host=host)
    return provider


@dataclass(frozen=True)
class Selection:
    """What the flags select. None of it depends on the relay being up yet."""

    agent: Agent
    provider: Provider
    image: str
    containerfile: Path


def select(args: argparse.Namespace) -> Selection:
    """Resolve the agent and provider together, and refuse an unusable pair."""
    agent = get_agent(args.agent)
    provider = resolve_provider(args)
    agent.check(args, provider)
    containerfile = args.containerfile or agent.containerfile
    if not agent.image or not containerfile:
        raise AgentboxError(
            f"agent {agent.name!r} names no image or Containerfile; pass "
            "--image and --containerfile, or fix the handler"
        )
    return Selection(
        agent=agent,
        provider=provider,
        image=args.image or agent.image,
        containerfile=containerfile,
    )


def relay_root(args: argparse.Namespace, gateway: str = "", port: int = 0) -> str | None:
    """The endpoint root the container is pointed at, or None for the agent's own."""
    if args.proxy:
        return f"http://{gateway}:{port}"
    base_url: str | None = args.base_url
    return base_url


def container_env_names(args: argparse.Namespace, provider: Provider) -> tuple[str, str]:
    """(key var, base-url var) the agent reads inside the container.

    Separate from the provider's own env names, which is where sanduk reads the
    real key on the host. They coincide for Claude Code against Anthropic and
    diverge for everything else, so the agent declares them and --agent-key-env
    overrides them.
    """
    wiring = get_agent(args.agent).wire(args, provider, relay_root(args))
    return wiring.key_env, wiring.base_url_env


def build_spec(
    args: argparse.Namespace,
    sel: Selection,
    wiring: Wiring,
    name: str,
    workdir: Path,
    task: str,
    network: str | None = None,
) -> ContainerSpec:
    """Map parsed flags onto one engine-neutral container description."""
    inherit = [wiring.key_env]
    # In proxy mode the inherited value is the run token, not the real key, and
    # the base URL points back at the host. Both come from the child env (see
    # child_env in run), so neither appears in this argv or in `ps`.
    if wiring.base_url:
        inherit.append(wiring.base_url_env)
    return ContainerSpec(
        name=name,
        image=sel.image,
        command=sel.agent.argv(args, sel.provider, task),
        cpus=args.cpus,
        memory=args.memory,
        mount=(workdir, "/work"),
        inherit_env=inherit,
        # The agent's own settings first, so an explicit -e can override one.
        env=[f"{k}={v}" for k, v in wiring.env.items()] + list(args.env),
        network=network,
    )


def read_task(args: argparse.Namespace) -> str:
    if bool(args.task) == bool(args.task_file):
        raise AgentboxError("give exactly one of: a task argument, or --task-file")
    task: str = args.task_file.read_text() if args.task_file else args.task
    if not task.strip():
        raise AgentboxError("task is empty")
    if not args.no_report_instruction:
        task += REPORT_INSTRUCTION.format(report=REPORT_NAME)
    return task


def run(args: argparse.Namespace) -> int:
    runtime = get_runtime(args.runtime)
    sel = select(args)
    provider = sel.provider
    api_url = f"{provider.scheme}://{provider.host}"
    task = read_task(args)

    key = os.environ.get(provider.key_env, "").strip()
    if not key and provider.has_auth:
        raise AgentboxError(f"{provider.key_env} is not set. export it, then re-run.")

    workdir = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    stale = workdir / REPORT_NAME
    if stale.exists():
        stale.unlink()

    if not args.dry_run and not args.skip_key_check:
        # Before anything is started, so a bad key cannot leak a container.
        validate_key(key, api_url if args.proxy else (args.base_url or api_url), provider)

    name = f"sanduk-{uuid.uuid4().hex[:8]}"
    network: str | None = args.network
    proxy_srv: ProxyServer | None = None
    holder: str | None = None
    gateway, port = "", 0
    token = ""
    child_env = os.environ.copy()

    if args.proxy:
        runtime.require()
        firewall_warning()
        network = args.proxy_network
        gateway, _ = runtime.ensure_network(network)
        token = secrets.token_urlsafe(24)
        if not args.dry_run:
            if args.rebuild or not runtime.image_exists(sel.image):
                runtime.build_image(sel.image, sel.containerfile)
            holder = runtime.hold_network_up(network, sel.image)
            if not wait_for_gateway(gateway):
                if holder:
                    runtime.destroy(holder)
                raise AgentboxError(f"{gateway} never became bindable on this host")
            proxy_srv, port = _start_relay(args, key, token, gateway, name, provider)

    wiring = sel.agent.wire(args, provider, relay_root(args, gateway, port))
    # In proxy mode the container gets the run token; the real key stays in this
    # process and in the proxy thread. Either way the value comes from the child
    # env, so it appears in no argv and in no `ps` line.
    secret = token if args.proxy else key
    if secret:
        child_env[wiring.key_env] = secret
    if wiring.base_url:
        child_env[wiring.base_url_env] = wiring.base_url

    cmd = runtime.run_argv(
        build_spec(args, sel, wiring, name, workdir, task, network=network)
    )

    if args.dry_run:
        print(shlex.join(cmd))
        if args.proxy:
            print(f"# proxy: {gateway} -> {api_url} ({provider.name})")
        print(
            f"# container env: {wiring.key_env}=<credential> "
            f"{wiring.base_url_env}={wiring.base_url or '<agent default>'}"
        )
        return 0

    runtime.require()
    if args.rebuild or not runtime.image_exists(sel.image):
        runtime.build_image(sel.image, sel.containerfile)

    if args.proxy:
        note(
            f"proxy bound to {gateway}:{port} (bridge only); "
            f"{network} has no route off the host"
        )
    note(f"{name} -> {workdir}")
    started = time.monotonic()
    try:
        outcome, rc = launch(sel.agent, cmd, args.timeout, args.quiet, env=child_env)
    except KeyboardInterrupt:
        runtime.destroy(name)
        raise AgentboxError("interrupted", code=130) from None
    except AgentboxError:
        # A timeout kill must not leave a container alive holding the key.
        runtime.destroy(name)
        raise
    finally:
        if proxy_srv:
            proxy_srv.shutdown()
            cfg = proxy_srv.cfg
            note(f"proxy relayed {cfg.requests}, rejected {cfg.rejected}")
        if holder:
            runtime.destroy(holder)
    runtime.destroy(name, args.keep)

    note(f"{time.monotonic() - started:.1f}s wall")
    if outcome:
        note(outcome.stats)
        if not outcome.ok:
            note(f"agent reported an error: {outcome.error}")
            return 1

    return _collect_report(args, workdir, outcome, rc)


def _start_relay(
    args: argparse.Namespace,
    key: str,
    token: str,
    gateway: str,
    name: str,
    provider: Provider,
) -> tuple[ProxyServer, int]:
    """Bind the relay to the bridge address only: unreachable from Wi-Fi or LAN."""
    log_dir = None
    if args.log_bodies:
        log_dir = (args.log_dir / name).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        note(f"request bodies -> {log_dir}")
    return start_proxy(
        key,
        token,
        gateway,
        args.proxy_port,
        allow_paths=args.proxy_allow_path or None,
        log_bodies=args.log_bodies,
        allow_models=args.allow_model,
        max_tokens_cap=args.max_tokens_cap,
        log_dir=str(log_dir) if log_dir else None,
        provider=provider,
    )


def _collect_report(
    args: argparse.Namespace,
    workdir: Path,
    outcome: Outcome | None,
    rc: int,
) -> int:
    report = workdir / REPORT_NAME
    if report.is_file():
        if args.report:
            shutil.copy(report, args.report)
            note(f"report -> {args.report}")
        else:
            note(f"report -> {report}")
    else:
        note(f"the agent wrote no {REPORT_NAME}")
        if outcome and outcome.text:
            print(f"\n{outcome.text}")
    return 0 if rc == 0 else rc


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except AgentboxError as e:
        print(f"sanduk: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
