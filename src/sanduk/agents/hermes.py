"""Nous Research's hermes-agent, through its `hermes-agent` entry point.

The odd one out: it prints prose, not a JSON stream. `run_agent:main` is a Fire
CLI whose output is a run of emoji-headed sections ending in a summary block,
so this handler reads lines rather than records -- which is what `Reader.line`
is for.

It speaks OpenAI Chat Completions, and `--mode open` is the only mode it can
run in: nothing found redirects its chat calls at the relay. Measured against
0.19.0 with a stub upstream inside the container, each of these left the call
going to openrouter.ai and returning its own 401: `--base_url=`, the
`OPENROUTER_BASE_URL` variable, and `model.base_url` in `~/.hermes/config.yaml`.
`check` refuses the relayed modes rather than letting a run fail at the first
call, and the container holds the real key, which is what `open` means.

The key still travels in `OPENROUTER_API_KEY` rather than the `--api_key` flag
it also accepts: an argv is visible to `inspect`.
"""

from __future__ import annotations

import argparse
import re
from typing import Any

from sanduk.agent import RESOURCES, Agent, Outcome, Reader, Wiring
from sanduk.errors import AgentboxError
from sanduk.providers import OPENAI_CHAT, Provider

KEY_ENV = "OPENROUTER_API_KEY"

# The summary block hermes prints when a run ends.
COMPLETED = re.compile(r"Completed:\s*(True|False)")
CALLS = re.compile(r"API Calls:\s*(\d+)")
FINAL = "FINAL RESPONSE:"
# hermes prints nothing per tool call -- `run_agent` has no such line -- so the
# trace shows the retries and the answer, which are the two things it does say.
WARNED = re.compile(r"^\s*⚠️\s*(.+)")
FAILED = re.compile(r"^\s*❌\s*(.+)")


class HermesReader(Reader):
    """Prose, read line by line.

    There are no token counts to read: hermes reports API calls and nothing
    else, so `Outcome.stats` says calls. `Outcome.stats` is a line rather than
    a struct for exactly this reason -- there is no shared meaning across
    agents to normalize to.
    """

    def __init__(self) -> None:
        self.completed: bool | None = None
        self.calls = 0
        self.failed = ""
        self.text = ""
        self._final = False

    def event(self, record: dict[str, Any], quiet: bool) -> None:
        """hermes emits no JSON records. A line that happens to be one is
        conversation content, not a protocol."""

    def line(self, text: str, quiet: bool) -> None:
        if self._final:
            # Everything after the header is the answer, until the sign-off.
            # The rules hermes draws around it are not part of it.
            stripped = text.strip()
            if stripped.startswith("👋"):
                self._final = False
                if self.text and not quiet:
                    print(f"  . {self.text.splitlines()[0][:160]}")
            elif stripped and set(stripped) <= {"-", "="}:
                pass
            elif stripped:
                self.text = f"{self.text}\n{stripped}".strip()
            return
        if FINAL in text:
            self._final = True
            return
        found = COMPLETED.search(text)
        if found:
            self.completed = found.group(1) == "True"
            return
        found = CALLS.search(text)
        if found:
            self.calls = int(found.group(1))
            return
        found = FAILED.match(text)
        if found:
            self.failed = found.group(1).strip()
            return
        found = WARNED.match(text)
        if found and not quiet:
            print(f"  ! {found.group(1).strip()[:160]}")

    def finish(self) -> Outcome | None:
        if self.completed is None and not self.failed:
            return None
        return Outcome(
            ok=bool(self.completed) and not self.failed,
            text="" if self.failed else self.text,
            error=self.failed,
            stats=f"{self.calls} api calls",
        )


class Hermes(Agent):
    name = "hermes"
    image = "sanduk-hermes:latest"
    containerfile = RESOURCES / "Containerfile.hermes"
    protocols = frozenset({OPENAI_CHAT})

    def check(self, args: argparse.Namespace, provider: Provider) -> None:
        super().check(args, provider)
        if getattr(args, "proxy", False):
            raise AgentboxError(
                "hermes cannot be pointed at the relay: 0.19.0 ignores "
                "--base_url, OPENROUTER_BASE_URL and config.yaml's base_url "
                "alike. Use --mode open, where the container holds the key."
            )
        if not args.model:
            # There is no default worth guessing: hermes resolves a bare name
            # against OpenRouter's catalogue, which the relay does not serve.
            raise AgentboxError("--model is required with --agent hermes")

    def wire(
        self, args: argparse.Namespace, provider: Provider, root: str | None
    ) -> Wiring:
        base = (root or f"{provider.scheme}://{provider.host}") + provider.api_prefix
        return Wiring(
            key_env=args.agent_key_env or KEY_ENV,
            # Named for --dry-run to print; hermes takes the endpoint on the
            # command line, not from a variable.
            base_url_env=args.agent_base_url_env or "HERMES_RELAY_BASE_URL",
            base_url=base,
        )

    def argv(
        self, args: argparse.Namespace, provider: Provider, task: str, wiring: Wiring
    ) -> list[str]:
        argv = [
            f"--base_url={wiring.base_url or ''}",
            f"--model={args.model}",
            f"--query={task}",
        ]
        if args.max_turns:
            argv.append(f"--max_turns={args.max_turns}")
        return argv

    def reader(self) -> Reader:
        return HermesReader()
