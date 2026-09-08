"""`hax --json`: a static C binary that streams one record per session item.

hax is what makes the openai, openrouter, and openai-compat providers reachable:
it speaks OpenAI Chat Completions as well as Anthropic Messages, and Claude Code
speaks only the latter.

Both endpoints hax would reach a first-party provider on are pinned, so every
run here uses one of its two `*-compatible` providers, whose base URL and key
come from the environment. That is also the uniform case: the same two variables
carry a relayed endpoint and a direct one.
"""

from __future__ import annotations

import argparse
from typing import Any

from sanduk.agent import (
    RESOURCES,
    Agent,
    Outcome,
    Reader,
    Wiring,
    completion_protocols,
)
from sanduk.errors import AgentboxError
from sanduk.providers import ANTHROPIC_MESSAGES, OPENAI_CHAT, Provider


def family(provider: Provider) -> str:
    """The hax provider family matching this provider's wire protocol."""
    if ANTHROPIC_MESSAGES in completion_protocols(provider):
        return "anthropic"
    return "openai"


class HaxReader(Reader):
    def __init__(self) -> None:
        self.result: dict[str, Any] | None = None
        # The result record carries turns and cost but no token totals; those
        # are only in the per-round-trip turn_usage items.
        self.tokens = {"input": 0, "output": 0, "cached": 0}

    def event(self, record: dict[str, Any], quiet: bool) -> None:
        if record.get("type") == "result":
            self.result = record
            return
        kind = record.get("kind")
        if kind == "turn_usage":
            usage = record.get("usage", {})
            for name in self.tokens:
                self.tokens[name] += usage.get(name, 0)
            return
        if quiet:
            return
        if kind == "assistant" and record.get("text", "").strip():
            print(f"  . {record['text'].strip()[:160]}")
        elif kind == "tool_call":
            print(f"  > {record.get('tool_name')}")

    def finish(self) -> Outcome | None:
        if self.result is None:
            return None
        outcome = str(self.result.get("outcome", ""))
        ok = outcome == "complete"
        cached = self.tokens["cached"]
        return Outcome(
            ok=ok,
            text=str(self.result.get("text", "")),
            # A stopped run carries no error string, so the outcome name
            # (max_turns, interrupted, paused) is what there is to report.
            error="" if ok else str(self.result.get("error") or outcome),
            stats=(
                f"{self.result.get('turns', '?')} turns, "
                # hax normalizes cache reads and writes into input.
                f"{self.tokens['input']:,} in ({cached:,} cached) / "
                f"{self.tokens['output']:,} out, "
                f"${self.result.get('cost', 0):.4f}"
            ),
        )


class Hax(Agent):
    name = "hax"
    image = "sanduk-hax:latest"
    containerfile = RESOURCES / "Containerfile.hax"
    protocols = frozenset({ANTHROPIC_MESSAGES, OPENAI_CHAT})

    def check(self, args: argparse.Namespace, provider: Provider) -> None:
        super().check(args, provider)
        for flag, value in (
            ("--allowed-tools", args.allowed_tools),
            ("--permission-mode", args.permission_mode),
        ):
            # Dropping these silently would weaken a restriction the caller
            # asked for. hax has no approval gate at all, by design: the
            # container is the boundary.
            if value:
                raise AgentboxError(
                    f"{flag} is a Claude Code flag; hax has no equivalent"
                )

    def argv(
        self, args: argparse.Namespace, provider: Provider, task: str, wiring: Wiring
    ) -> list[str]:
        argv = ["--json", f"--provider={family(provider)}-compatible"]
        if args.model:
            argv.append(f"--model={args.model}")
        if args.effort:
            argv.append(f"--effort={args.effort}")
        if args.bare:
            argv.append("--bare")
        argv.append(task)
        return argv

    def wire(
        self, args: argparse.Namespace, provider: Provider, root: str | None
    ) -> Wiring:
        prefix = family(provider).upper()
        env = {
            # The proxy network has no route off the host, so hax's model
            # metadata fetch can only time out. Empty disables it.
            "HAX_CATALOG_URL": "",
            "HAX_NOTIFY": "off",
        }
        if args.max_turns:
            env["HAX_MAX_TURNS"] = str(args.max_turns)
        base = root or f"{provider.scheme}://{provider.host}"
        return Wiring(
            key_env=args.agent_key_env or f"HAX_{prefix}_API_KEY",
            base_url_env=args.agent_base_url_env or f"HAX_{prefix}_BASE_URL",
            # hax posts <base>/messages or <base>/chat/completions, so the base
            # URL carries the prefix the relay's routes are declared on.
            base_url=base + provider.api_prefix,
            env=env,
        )

    def reader(self) -> Reader:
        return HaxReader()
