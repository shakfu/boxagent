"""The agent strategy: what sanduk needs to know about a CLI it runs.

An agent handler answers four questions and nothing else: which image carries
the program, what flags drive it headlessly, which variables it reads its
endpoint and credential from, and how to read the JSON it streams back. The
runner knows none of those answers, so a new agent is a new handler and no edit
here.

Handlers are looked up in the `sanduk.agents` entry-point group, so a handler in
a separate distribution is found the same way the shipped ones are. `--agent
mypkg.handlers:MyAgent` loads one that is not packaged at all.

Reading the stream is stateful and the handler is not: `Agent.reader()` returns
a fresh `Reader` per run. That keeps a handler safe to hold in the registry and
keeps one run's token tally out of the next one's.

Token accounting is per agent, not per provider: Claude Code reports cache reads
outside `input_tokens` and hax normalizes them inside it, so a shared summary
line would silently miscount one of them. See docs/agents.md to write your own.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import cache
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from sanduk.errors import AgentboxError
from sanduk.providers import Provider
from sanduk.util import note

# Containerfiles ship inside the package so a `pip install sanduk` can build an
# image without a checkout. __file__ rather than importlib.resources: the engine
# needs a real path on disk for `build -f`, which a Traversable does not
# promise.
RESOURCES = Path(__file__).parent / "resources"

ENTRY_POINT_GROUP = "sanduk.agents"
DEFAULT_AGENT = "claude"

REPORT_NAME = "REPORT.md"
REPORT_INSTRUCTION = (
    "\n\nWhen you are done, write your findings to ./{report} in the working "
    "directory. That file is the only output that survives; anything you print "
    "to the terminal is discarded when the container is deleted."
)

# What Claude Code reads inside the container. sanduk.providers declares the
# same names for the anthropic provider, and the container's variables are
# named from there; tests/test_providers.py asserts the two agree.
KEY_ENV = "ANTHROPIC_API_KEY"
BASE_URL_ENV = "ANTHROPIC_BASE_URL"


@dataclass(frozen=True)
class Wiring:
    """How the container is pointed at the endpoint.

    `base_url` is None when the agent should use its own built-in endpoint,
    which is the no-relay case for an agent whose provider endpoint is pinned.
    `env` is fixed settings the agent needs, as K=V pairs; an explicit `-e` on
    the command line still wins.
    """

    key_env: str
    base_url_env: str
    base_url: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Outcome:
    """A finished run, in terms cli.py can print without knowing the agent."""

    ok: bool
    text: str = ""  # final assistant message, when the agent reports one
    error: str = ""
    stats: str = ""  # one line: turns, tokens, cost


def completion_protocols(provider: Provider) -> set[str]:
    """The wire protocols this provider accepts a completion on."""
    return {name for name in provider.routes.values() if name}


class Reader(ABC):
    """One run's JSON stream, consumed as it arrives."""

    @abstractmethod
    def event(self, record: dict[str, Any], quiet: bool) -> None:
        """Consume one record: trace it, tally it, or keep it."""

    @abstractmethod
    def finish(self) -> Outcome | None:
        """The run's outcome, or None when no terminal record arrived."""


class Agent(ABC):
    """One agent CLI, driven headlessly and read back as a JSON stream.

    Subclass this, or match its shape: nothing here is checked with isinstance
    except the entry-point loader, which wants a real subclass so a typo in a
    plugin fails at load rather than mid-run.
    """

    #: Registry key and `--agent` value.
    name = ""
    #: Image and Containerfile are per agent: `--agent hax` must not silently
    #: reuse an image that has only Claude Code in it. Both are required; there
    #: is no default that could be right for someone else's agent.
    image = ""
    containerfile: Path | None = None
    #: Wire protocols this agent can speak, from sanduk.providers.
    protocols: frozenset[str] = frozenset()

    def check(self, args: argparse.Namespace, provider: Provider) -> None:
        """Refuse a combination this agent cannot serve, before anything runs."""
        if not completion_protocols(provider) & self.protocols:
            speaks = ", ".join(sorted(self.protocols)) or "nothing"
            raise AgentboxError(
                f"{self.name} cannot talk to the {provider.name} provider: it "
                f"speaks {speaks}. Use a different --agent or --provider."
            )

    @abstractmethod
    def wire(
        self, args: argparse.Namespace, provider: Provider, root: str | None
    ) -> Wiring:
        """Where the agent finds its endpoint. `root` is scheme://host[:port]."""

    @abstractmethod
    def argv(
        self,
        args: argparse.Namespace,
        provider: Provider,
        task: str,
        wiring: Wiring,
    ) -> list[str]:
        """Flags appended after the image in the container argv.

        `wiring` is this run's own, already resolved: an agent that takes its
        endpoint as a command-line option rather than from the environment
        reads it from there. Nothing secret belongs in the result -- the
        credential reaches the container through `Wiring.key_env`, and this
        argv is visible to `inspect`.
        """

    @abstractmethod
    def reader(self) -> Reader:
        """A fresh stream reader for one run."""


# --- registry ---------------------------------------------------------------


@cache
def registry() -> dict[str, type[Agent]]:
    """Every handler this installation can run, by name.

    The shipped handlers are seeded directly rather than discovered: a source
    checkout that was never installed has no entry-point metadata, and losing
    `claude` there would be a worse failure than any purity gained.
    """
    from sanduk.agents import BUILTIN

    agents: dict[str, type[Agent]] = {a.name: a for a in BUILTIN}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            loaded = ep.load()
        except Exception as e:  # a broken plugin must not take the run down
            note(f"agent plugin {ep.name!r} failed to load: {e}")
            continue
        if not (isinstance(loaded, type) and issubclass(loaded, Agent)):
            note(f"agent plugin {ep.name!r} is not an Agent subclass; ignored")
            continue
        name = loaded.name or ep.name
        # Shadowing a shipped handler would change what runs in the container
        # without changing the command line. Refuse rather than surprise.
        if name in agents and agents[name] is not loaded:
            note(f"agent plugin {ep.name!r} cannot replace the built-in {name!r}")
            continue
        agents[name] = loaded
    return agents


def agent_names() -> list[str]:
    return sorted(registry())


def get_agent(spec: str = DEFAULT_AGENT) -> Agent:
    """A handler by registry name, or by `module:Class` for an unpackaged one."""
    if ":" in spec:
        return _load_path(spec)()
    try:
        return registry()[spec]()
    except KeyError:
        known = ", ".join(agent_names())
        raise AgentboxError(
            f"unknown agent {spec!r}; known: {known}. A handler outside the "
            "registry is given as module:Class."
        ) from None


def _load_path(spec: str) -> type[Agent]:
    module_name, _, attr = spec.partition(":")
    try:
        loaded = getattr(import_module(module_name), attr)
    except (ImportError, AttributeError) as e:
        raise AgentboxError(f"could not load agent {spec!r}: {e}") from None
    if not (isinstance(loaded, type) and issubclass(loaded, Agent)):
        raise AgentboxError(f"{spec} is not a sanduk.agent.Agent subclass")
    return loaded


# --- running ----------------------------------------------------------------


def launch(
    agent: Agent,
    argv: list[str],
    timeout: float,
    quiet: bool,
    env: dict[str, str] | None = None,
) -> tuple[Outcome | None, int]:
    """Stream the agent's JSONL; return its outcome and the exit code.

    The timer is what enforces the timeout: an agent that hangs without printing
    would never trip a deadline checked inside the read loop.
    """
    reader = agent.reader()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, expire)
    watchdog.start()

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            reader.event(record, quiet)
        proc.wait(timeout=30)
    except (KeyboardInterrupt, SystemExit):
        # Killing the client does not stop the container; the caller deletes it.
        proc.kill()
        raise
    finally:
        watchdog.cancel()

    if timed_out.is_set():
        raise AgentboxError(f"agent exceeded --timeout {timeout:g}s", code=124)
    return reader.finish(), proc.returncode
