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
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import namedtuple
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from sanduk import assistants
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
    registry,
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
from sanduk.runs import Run, claim, live_containers, sweep
from sanduk.runtime import (
    CONTAINER_PREFIX,
    DEFAULT_RUNTIME,
    RUNTIMES,
    Container,
    ContainerSpec,
    Mount,
    get_runtime,
    wait_for_gateway,
)
from sanduk.util import note, seconds

COMMANDS = (
    "run",
    "assistant",
    "tell",
    "tick",
    "serve",
    "outbox",
    "approve",
    "reject",
    "runs",
    "build",
    "shell",
    "ps",
    "stop",
    "clean",
    "destroy",
    "system",
    "list",
)

# Where -w lands inside the container. Every other mount is refused this path
# and anything under it.
WORKDIR_DEST = "/work"

# What a mode is, in the two properties that differ: whether the host keeps the
# key, and whether the container has a route off the host. The fourth
# combination -- the key in the container, no route to any provider -- is a run
# that cannot call anything, so it is not a mode.
Mode = namedtuple("Mode", "relayed egress network")
MODES = {
    "open": Mode(relayed=False, egress=True, network=None),
    "key-safe": Mode(relayed=True, egress=True, network="sanduk-open"),
    "sealed": Mode(relayed=True, egress=False, network="sanduk-net"),
}
DEFAULT_MODE = "open"

# The network holder is started for the run's length plus this, so teardown
# happens while the bridge is still up.
HOLDER_MARGIN = 300
# A week. Not a technical limit: the holder is a container that sits for as
# long as it is told, and this is the guard against `--timeout 9000000` being
# a typo that parks one for months.
MAX_TIMEOUT = 7 * 86400

RUN_EPILOG = (
    "The API key comes from the provider's environment variable only.\n"
    "\n"
    "  export ANTHROPIC_API_KEY=sk-ant-...\n"
    "  sanduk run 'Summarise every .py file here.' -w ./work\n"
    "  sanduk run --task-file brief.md -w ./repo --keep\n"
    "  sanduk run 'Review this.' --agent hax --provider openai-compat \\\n"
    "      --upstream http://127.0.0.1:8080 --proxy\n"
)


def engine_flags() -> argparse.ArgumentParser:
    """Flags every command that talks to a container engine takes.

    Carried by a parent parser rather than the main one: a flag defined in both
    places has the subparser's default overwrite whatever came before the verb,
    so `sanduk --runtime docker run` would silently use the default.
    """
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "--runtime",
        choices=sorted(RUNTIMES),
        default=DEFAULT_RUNTIME,
        help=f"container engine (default: {DEFAULT_RUNTIME})",
    )
    p.add_argument(
        "-q", "--quiet", action="store_true", help="suppress the per-event trace"
    )
    return p


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = sys.argv[1:] if argv is None else argv
    if argv and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        # argparse would say "invalid choice", which is true and unhelpful. The
        # first argument used to be the task, so this is the mistake to expect.
        raise AgentboxError(
            f"{argv[0]!r} is not a command. Did you mean: "
            f"sanduk run {shlex.quote(argv[0])}\n"
            f"commands: {', '.join(COMMANDS)}"
        )

    root = argparse.ArgumentParser(
        prog="sanduk", description="Run an agent in a disposable container."
    )
    sub = root.add_subparsers(dest="command", metavar="<command>", required=True)
    _add_engine_commands(sub)
    _add_assistant_commands(sub)
    _add_list(sub)

    p = sub.add_parser(
        "run",
        parents=[engine_flags()],
        help="run an agent in a disposable container",
        description="Run an agent in a disposable container.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=RUN_EPILOG,
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
        "--mount",
        action="append",
        default=[],
        metavar="HOST:DEST[:ro]",
        help="another host directory in the container, e.g. ../repo:/repo:ro. "
        "Repeatable. Read-write unless :ro",
    )
    p.add_argument(
        "--stats-file",
        type=Path,
        help="write the run's outcome here as JSON: exit, ok, stats, error, report",
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
    g.add_argument(
        "--timeout",
        default="900",
        metavar="DURATION",
        help="how long one run may take: seconds, or a suffix of s, m, h, d "
        "(default: 900)",
    )
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

    g = p.add_argument_group("containment")
    g.add_argument(
        "--mode",
        choices=list(MODES),
        default=None,
        help="open: the container holds the key and reaches anything. "
        "key-safe: the key stays on the host, the container still reaches "
        "anything. sealed: the key stays on the host and the container has no "
        f"route off it (default: {DEFAULT_MODE})",
    )
    # The flag `--mode sealed` replaced. Kept working, and out of --help, so a
    # command line written against 0.2.x still runs.
    g.add_argument("--proxy", action="store_true", help=argparse.SUPPRESS)
    g.add_argument(
        "--proxy-network",
        default=None,
        help="network to create/use (default: sanduk-net sealed, sanduk-open key-safe)",
    )
    g.add_argument(
        "--budget",
        type=float,
        metavar="USD",
        help="stop the run once its calls have cost more than this. The call "
        "that crosses the ceiling is already paid for. Only a provider that "
        "reports cost can enforce it, which today is openrouter",
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
        "--dry-run", action="store_true", help="print the container command and exit"
    )
    g.add_argument("--skip-key-check", action="store_true")
    args = root.parse_args(argv)
    return resolve_mode(args)


def resolve_mode(args: argparse.Namespace) -> argparse.Namespace:
    """Turn `--mode` (or the `--proxy` alias) into the two properties the run
    path reads: `args.proxy` for where the key lives, `args.egress` for the
    route off the host."""
    if getattr(args, "command", None) != "run":
        return args
    if args.mode is None:
        args.mode = "sealed" if args.proxy else DEFAULT_MODE
    elif args.proxy and args.mode != "sealed":
        raise AgentboxError(
            f"--proxy is the old spelling of --mode sealed; it cannot be "
            f"combined with --mode {args.mode}"
        )
    # Seconds from here on: a duration is a spelling, not a type.
    args.timeout = seconds(args.timeout)
    if args.timeout > MAX_TIMEOUT:
        raise AgentboxError(
            f"--timeout {args.timeout}s is longer than a week. The network "
            f"holder is started for the run's length, so a typo here parks a "
            f"container for that long. Pass at most {MAX_TIMEOUT}s"
        )
    if args.budget is not None and args.budget <= 0:
        raise AgentboxError("--budget must be a positive number of dollars")
    mode = MODES[args.mode]
    args.proxy = mode.relayed
    args.egress = mode.egress
    if args.proxy_network is None:
        args.proxy_network = mode.network or MODES["sealed"].network
    return args


def _add_engine_commands(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """The verbs that only talk to a container engine."""
    engine = engine_flags()

    p = sub.add_parser("build", parents=[engine], help="build the agent's image")
    p.add_argument("--agent", default=DEFAULT_AGENT, metavar="NAME")
    p.add_argument("-i", "--image", help="tag to build (default: the agent's)")
    p.add_argument("--containerfile", type=Path, help="default: the agent's")
    p.add_argument(
        "--force", action="store_true", help="rebuild even if the image exists"
    )

    p = sub.add_parser(
        "shell", parents=[engine], help="interactive shell in the agent's image"
    )
    p.add_argument("--agent", default=DEFAULT_AGENT, metavar="NAME")
    p.add_argument("-i", "--image", help="image to enter (default: the agent's)")
    # resolve_image reads it; only run and build can build one.
    p.set_defaults(containerfile=None)

    for verb, helptext in (
        ("ps", f"list {CONTAINER_PREFIX}* containers"),
        ("stop", "stop running sanduk containers, leaving them on disk"),
    ):
        sub.add_parser(verb, parents=[engine], help=helptext)

    p = sub.add_parser(
        "clean", parents=[engine], help="stop and delete sanduk containers"
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="include a container a live run is using (a wedged run, say)",
    )

    p = sub.add_parser(
        "destroy", parents=[engine], help="clean, plus the image and the network"
    )
    p.add_argument("--agent", default=DEFAULT_AGENT, metavar="NAME")
    p.add_argument("-i", "--image", help="image to delete (default: the agent's)")
    p.add_argument(
        "--proxy-network",
        help="also delete this network (default: every network a mode creates)",
    )
    p.set_defaults(containerfile=None, all=False)

    p = sub.add_parser(
        "system", parents=[engine], help="show or change the engine's own service"
    )
    p.add_argument("action", choices=["status", "start", "stop"])


def _add_assistant_commands(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Scheduled runs. Identity is a file, state is SQLite, and a wakeup is one
    `run` -- so nothing here takes engine flags except the one that starts a
    container."""
    p = sub.add_parser("assistant", help="register and inspect assistants")
    action = p.add_subparsers(dest="action", metavar="<action>", required=True)
    add = action.add_parser(
        "add", help=f"register a directory holding {assistants.CONFIG_NAME}"
    )
    add.add_argument("dir", type=Path)
    action.add_parser("list", help="one row per registered assistant")
    for verb, helptext in (
        ("show", "one assistant's state, as JSON"),
        ("enable", "let it run again, and clear its failure count"),
        ("disable", "stop scheduling it"),
    ):
        q = action.add_parser(verb, help=helptext)
        q.add_argument("name")

    p = sub.add_parser("tell", help="queue a message for the next wakeup")
    p.add_argument("name")
    p.add_argument("message", nargs="+", help="the message text")

    p = sub.add_parser("tick", help="run every assistant that is due, once")
    p.add_argument("--name", help="this one only, whether or not it is due")
    p.add_argument(
        "--runtime",
        choices=sorted(RUNTIMES),
        help="override the engine named in assistant.toml",
    )

    p = sub.add_parser("serve", help="tick on a loop, in the foreground")
    p.add_argument(
        "--interval",
        type=int,
        default=60,
        help="seconds between passes at most (default: 60)",
    )
    p.add_argument("--runtime", choices=sorted(RUNTIMES))

    p = sub.add_parser("outbox", help="what assistants have produced")
    p.add_argument("--name")
    p.add_argument(
        "--undelivered", action="store_true", help="only what --deliver has not sent"
    )
    p.add_argument(
        "--pending", action="store_true", help="only what is waiting for approval"
    )
    p.add_argument(
        "--deliver",
        metavar="CMD",
        help="pipe each undelivered entry to CMD on stdin, then mark it delivered",
    )

    for verb, helptext in (
        ("approve", "let outbox entries be delivered"),
        ("reject", "keep outbox entries from ever being delivered"),
    ):
        q = sub.add_parser(verb, help=helptext)
        q.add_argument("id", nargs="+", type=int, help="outbox entry ids")

    p = sub.add_parser("runs", help="wakeup history")
    p.add_argument("--name")
    p.add_argument("--limit", type=int, default=20)


def _add_list(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`list` reads registries, not an engine, so it takes no engine flags."""
    p = sub.add_parser("list", help="show registered agents, providers, runtimes")
    p.add_argument("axis", choices=["agents", "providers", "runtimes"])


def build(args: argparse.Namespace) -> int:
    runtime = get_runtime(args.runtime)
    _, image, containerfile = resolve_image(args)
    runtime.require()
    if runtime.image_exists(image) and not args.force:
        note(f"{image} is already built (--force to rebuild)")
        return 0
    runtime.build_image(image, containerfile)
    return 0


def shell(args: argparse.Namespace) -> int:
    runtime = get_runtime(args.runtime)
    _, image, _ = resolve_image(args)
    runtime.require()
    if not runtime.image_exists(image):
        raise AgentboxError(
            f"{image} is not built. Run: sanduk build --agent {args.agent}"
        )
    # Handed straight to the terminal: this one inherits the tty rather than
    # having its stdout read.
    return subprocess.call(runtime.shell_argv(image))


def ps(args: argparse.Namespace) -> int:
    runtime = get_runtime(args.runtime)
    runtime.require()
    found = runtime.list_containers(CONTAINER_PREFIX)
    for c in found:
        print(f"{c.name:22}  {c.image:24}  {c.state}")
    if not found:
        note("no sanduk containers")
    return 0


def _unowned(containers: list[Container]) -> list[Container]:
    """Those no live run claims. A wakeup on a schedule is nobody's to stop."""
    held = live_containers()
    keep = [c for c in containers if c.name in held]
    if keep:
        names = ", ".join(sorted(c.name for c in keep))
        note(f"leaving {len(keep)} to their running owner: {names}")
    return [c for c in containers if c.name not in held]


def stop(args: argparse.Namespace) -> int:
    runtime = get_runtime(args.runtime)
    runtime.require()
    running = [
        c for c in runtime.list_containers(CONTAINER_PREFIX) if c.state == "running"
    ]
    stoppable = _unowned(running)
    for c in stoppable:
        runtime.stop(c.name)
        note(f"stopped {c.name}")
    if not stoppable:
        note("no running sanduk containers to stop")
    return 0


def clean(args: argparse.Namespace) -> int:
    """Delete every container sanduk named, except one a live run is using.

    Nothing else carries the prefix, and `--all` overrides the exception for a
    run whose process is wedged rather than working.
    """
    runtime = get_runtime(args.runtime)
    runtime.require()
    found = runtime.list_containers(CONTAINER_PREFIX)
    deletable = found if getattr(args, "all", False) else _unowned(found)
    for c in deletable:
        runtime.destroy(c.name)
    if not deletable:
        note("no sanduk containers to delete")
    return 0


def destroy(args: argparse.Namespace) -> int:
    """Everything sanduk made on this engine, except the request-body log.

    That log is written outside the bind mount so the agent cannot edit its own
    audit trail; deleting it as a side effect of a cleanup verb would undo the
    point of putting it there.
    """
    runtime = get_runtime(args.runtime)
    _, image, _ = resolve_image(args)
    clean(args)
    runtime.delete_image(image)
    for network in mode_networks(args.proxy_network):
        runtime.delete_network(network)
    return 0


def mode_networks(named: str | None = None) -> list[str]:
    """Every network a mode creates, plus one the caller named.

    `destroy` says it removes everything sanduk made. Deleting only the mode's
    default left the other mode's bridge behind, and which mode you ran last
    week is not a question a cleanup verb should ask.
    """
    found = [named] if named else []
    found += [mode.network for mode in MODES.values() if mode.network]
    return list(dict.fromkeys(found))


def system(args: argparse.Namespace) -> int:
    # No require() first: whether the engine is usable is what status reports.
    runtime = get_runtime(args.runtime)
    if args.action == "status":
        print(runtime.service_status())
    elif args.action == "start":
        runtime.service_start()
    else:
        runtime.service_stop()
    return 0


def show(args: argparse.Namespace) -> int:
    """One row per registered thing, on stdout."""
    if args.axis == "agents":
        for name, agent in sorted(registry().items()):
            protocols = ", ".join(sorted(agent.protocols))
            print(f"{name:9}  {agent.image:24}  {protocols}")
    elif args.axis == "providers":
        for name, provider in sorted(PROVIDERS.items()):
            url = f"{provider.scheme}://{provider.host}{provider.api_prefix}"
            key = provider.key_env if provider.has_auth else "(no key needed)"
            print(f"{name:14}  {url:34}  {key}")
    else:
        for name, engine in sorted(RUNTIMES.items()):
            found = "installed" if shutil.which(engine.cli) else "not installed"
            print(f"{name:8}  {engine.cli:12}  {found}")
    return 0


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


def resolve_image(args: argparse.Namespace) -> tuple[Agent, str, Path]:
    """(agent, image, Containerfile), before any provider is known.

    `build` and `shell` need this and nothing else; `select` adds the provider.
    """
    agent = get_agent(args.agent)
    containerfile = args.containerfile or agent.containerfile
    if not agent.image or not containerfile:
        raise AgentboxError(
            f"agent {agent.name!r} names no image or Containerfile; pass "
            "--image and --containerfile, or fix the handler"
        )
    return agent, args.image or agent.image, containerfile


def select(args: argparse.Namespace) -> Selection:
    """Resolve the agent and provider together, and refuse an unusable pair."""
    agent, image, containerfile = resolve_image(args)
    provider = resolve_provider(args)
    if getattr(args, "budget", None) is not None:
        if not provider.cost_field:
            raise AgentboxError(
                f"--budget cannot be enforced against {provider.name}: it "
                f"reports tokens, not cost. Only openrouter reports cost"
            )
        if not args.proxy:
            raise AgentboxError(
                "--budget is counted by the relay, which --mode open does not "
                "start. Use --mode key-safe or sealed"
            )
    agent.check(args, provider)
    return Selection(
        agent=agent, provider=provider, image=image, containerfile=containerfile
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


def parse_mounts(values: list[str]) -> list[Mount]:
    """`HOST:DEST[:ro]` per `--mount`.

    Refused: a host directory that is not there, a destination that is not
    absolute, and anything at or under the working directory. The last would
    shadow part of what the agent was pointed at, which is a run reading the
    wrong files rather than one that fails.
    """
    mounts: list[Mount] = []
    claimed: dict[str, Path] = {}
    for value in values:
        parts = value.split(":")
        if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
            raise AgentboxError(f"--mount {value!r}: expected HOST:DEST[:ro]")
        mode = parts[2] if len(parts) == 3 else "rw"
        if mode not in ("ro", "rw"):
            raise AgentboxError(f"--mount {value!r}: mode is ro or rw, not {mode!r}")
        host = Path(parts[0]).expanduser().resolve()
        dest = parts[1]
        if not host.is_dir():
            raise AgentboxError(f"--mount {value!r}: {host} is not a directory")
        if not dest.startswith("/") or dest == "/":
            raise AgentboxError(f"--mount {value!r}: {dest} is not an absolute path")
        if dest == WORKDIR_DEST or dest.startswith(WORKDIR_DEST + "/"):
            raise AgentboxError(
                f"--mount {value!r}: {WORKDIR_DEST} is where -w lands; a mount "
                f"there would shadow the directory the agent was given"
            )
        if dest in claimed:
            raise AgentboxError(
                f"--mount {value!r}: {dest} already holds {claimed[dest]}"
            )
        claimed[dest] = host
        mounts.append(Mount(host=host, dest=dest, ro=mode == "ro"))
    return mounts


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
        command=sel.agent.argv(args, sel.provider, task, wiring),
        cpus=args.cpus,
        memory=args.memory,
        mount=(workdir, WORKDIR_DEST),
        mounts=parse_mounts(getattr(args, "mount", [])),
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


TEARDOWN_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def _exit_on_signal(signum: int, _frame: object) -> None:
    """Turn a terminating signal into the exception the teardown path catches.

    SIGKILL cannot be caught at all, which is what the run records in
    `sanduk.runs` are for: the next run deletes what this one could not.
    """
    raise SystemExit(128 + signum)


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

    name = f"{CONTAINER_PREFIX}{uuid.uuid4().hex[:8]}"
    record: Run | None = None if args.dry_run else claim(args.runtime, name)
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
        gateway, _ = runtime.ensure_network(network, internal=not args.egress)
        token = secrets.token_urlsafe(24)
        if not args.dry_run:
            if args.rebuild or not runtime.image_exists(sel.image):
                runtime.build_image(sel.image, sel.containerfile)
            # Outlive the run: the holder going first takes the bridge, and
            # the relay's address, with it.
            holder = runtime.hold_network_up(
                network, sel.image, args.timeout + HOLDER_MARGIN
            )
            if holder and record:
                record.add(holder)
            if not wait_for_gateway(gateway):
                if holder:
                    runtime.destroy(holder)
                raise AgentboxError(
                    f"{gateway} never became bindable on this host.{runtime.gateway_hint}"
                )
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
    sweep()
    if args.rebuild or not runtime.image_exists(sel.image):
        runtime.build_image(sel.image, sel.containerfile)

    if args.proxy and args.egress:
        note(
            f"relay bound to {gateway}:{port} (bridge only); the key stays "
            f"here, and {network} reaches the internet: what the agent sends "
            f"anywhere else is neither relayed nor recorded"
        )
    elif args.proxy:
        note(
            f"relay bound to {gateway}:{port} (bridge only); "
            f"{network} has no route off the host"
        )
    note(f"{name} -> {workdir}")
    started = time.monotonic()
    # SIGTERM and SIGHUP kill this process with no teardown otherwise, which is
    # the same leak SIGKILL causes and the only one that can be closed here.
    # Restored in the finally below: `serve` calls this in a loop and its own
    # handlers have to survive a wakeup.
    handlers = [(s, signal.signal(s, _exit_on_signal)) for s in TEARDOWN_SIGNALS]
    try:
        outcome, rc = launch(sel.agent, cmd, args.timeout, args.quiet, env=child_env)
    except (KeyboardInterrupt, SystemExit) as e:
        runtime.destroy(name)
        code = e.code if isinstance(e, SystemExit) and isinstance(e.code, int) else 130
        raise AgentboxError("interrupted", code=code) from None
    except AgentboxError:
        # A timeout kill must not leave a container alive holding the key.
        runtime.destroy(name)
        raise
    else:
        # Before the record is released, so a kill in between still leaves the
        # container owned and reapable.
        runtime.destroy(name, args.keep)
    finally:
        if proxy_srv:
            proxy_srv.shutdown()
            cfg = proxy_srv.cfg
            spent = f", ${cfg.spent:.4f} spent" if cfg.provider.cost_field else ""
            note(f"proxy relayed {cfg.requests}, rejected {cfg.rejected}{spent}")
        if holder:
            runtime.destroy(holder)
        if record:
            record.release()
        for signum, handler in handlers:
            signal.signal(signum, handler)

    note(f"{time.monotonic() - started:.1f}s wall")
    if outcome:
        note(agent_stats(outcome.stats, proxy_srv))
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
        budget=args.budget,
        log_dir=str(log_dir) if log_dir else None,
        provider=provider,
    )


def agent_stats(stats: str, relay: ProxyServer | None) -> str:
    """The agent's own tally, minus a cost it had no way to know.

    An agent prices a run from its model catalogue. Through the relay the model
    is named by sanduk's own provider id, which no catalogue has, so the agent
    reports $0.0000 one line under the relay's real figure. Two dollar amounts,
    one of them wrong, is worse than one.
    """
    if relay is None or not relay.cfg.provider.cost_field or relay.cfg.spent <= 0:
        return stats
    return re.sub(r",?\s*\$0\.0+\b", "", stats).strip()


def _collect_report(
    args: argparse.Namespace,
    workdir: Path,
    outcome: Outcome | None,
    rc: int,
) -> int:
    """Copy the report out and record the run, failed or not; return its status."""
    if rc < 0:
        # Killed by a signal: the shell's 128 + N, not a negative status.
        rc = 128 - rc
    if outcome is None:
        # No terminal record: the agent never finished, whatever its status says.
        error, code = "the agent exited without a final result", rc or 1
        note(error)
    elif not outcome.ok:
        error, code = outcome.error, 1
        note(f"agent reported an error: {error}")
    else:
        error, code = "", rc
    report = workdir / REPORT_NAME
    if args.stats_file:
        # The exit code is all a caller gets from `main`, and the token line is
        # printed rather than returned. An assistant recording what a wakeup
        # cost needs it as data.
        args.stats_file.write_text(
            json.dumps(
                {
                    "exit": code,
                    "ok": bool(outcome and outcome.ok),
                    "stats": outcome.stats if outcome else "",
                    "error": error,
                    "report": str(args.report or report) if report.is_file() else None,
                }
            )
        )
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
    return code


def assistant(args: argparse.Namespace) -> int:
    db = assistants.connect()
    if args.action == "add":
        added = assistants.load(args.dir)
        assistants.register(db, added)
        note(f"registered {added.name} -> {added.dir}")
    elif args.action == "list":
        registered = assistants.rows(db)
        for r in registered:
            left = r["next_due_at"] - assistants.now()
            when = "disabled" if r["disabled"] else ("due" if left <= 0 else f"{left}s")
            print(f"{r['name']:16}  {when:10}  {r['dir']}")
        if not registered:
            note("no assistants registered (`sanduk assistant add <dir>`)")
    elif args.action == "show":
        print(assistants.summary(db, args.name))
    else:
        disabled = args.action == "disable"
        assistants.set_disabled(db, args.name, disabled)
        note(f"{args.name} is {'disabled' if disabled else 'enabled'}")
    return 0


def tell(args: argparse.Namespace) -> int:
    db = assistants.connect()
    assistants.tell(db, args.name, " ".join(args.message))
    note(f"queued for {args.name}; it arrives on the next wakeup")
    return 0


def tick(args: argparse.Namespace) -> int:
    return assistants.tick(db=assistants.connect(), name=args.name, runtime=args.runtime)


def serve(args: argparse.Namespace) -> int:
    return assistants.serve(
        assistants.connect(), interval=args.interval, runtime=args.runtime
    )


def outbox(args: argparse.Namespace) -> int:
    db = assistants.connect()
    if args.deliver:
        sent = assistants.deliver(db, args.deliver, args.name)
        note(f"delivered {sent}")
        return 0
    entries = assistants.outbox(
        db, args.name, undelivered=args.undelivered, pending=args.pending
    )
    for entry in entries:
        when = datetime.fromtimestamp(entry["created_at"], UTC).isoformat()
        state = assistants.state_of(entry)
        print(f"--- [{entry['id']}] {entry['name']} run {entry['run_id']} {when} {state}")
        print(entry["body"].rstrip())
    if not entries:
        note("nothing in the outbox")
    return 0


def decide(args: argparse.Namespace) -> int:
    """approve and reject: one verb, two words for the same record."""
    approve = args.command == "approve"
    changed = assistants.decide(assistants.connect(), args.id, approve)
    note(f"{changed} {'approved' if approve else 'rejected'}")
    if changed < len(args.id):
        note("the rest were decided or delivered already")
    return 0


def runs(args: argparse.Namespace) -> int:
    db = assistants.connect()
    found = assistants.history(db, args.name, args.limit)
    for r in found:
        when = datetime.fromtimestamp(r["started_at"], UTC).isoformat()
        took = f"{r['ended_at'] - r['started_at']}s" if r["ended_at"] else "running"
        code = "-" if r["exit_code"] is None else str(r["exit_code"])
        cost = f"  {r['stats']}" if r["stats"] else ""
        print(f"{r['id']:5}  {r['name']:16}  {when}  {took:>8}  exit {code}{cost}")
    if not found:
        note("no wakeups recorded")
    return 0


COMMAND_FUNCS: dict[str, Callable[[argparse.Namespace], int]] = {
    "run": run,
    "assistant": assistant,
    "tell": tell,
    "tick": tick,
    "serve": serve,
    "outbox": outbox,
    "approve": decide,
    "reject": decide,
    "runs": runs,
    "build": build,
    "shell": shell,
    "ps": ps,
    "stop": stop,
    "clean": clean,
    "destroy": destroy,
    "system": system,
    "list": show,
}


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return COMMAND_FUNCS[args.command](args)
    except AgentboxError as e:
        print(f"sanduk: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
