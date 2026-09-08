"""Anthropic's `claude`, in `-p --output-format stream-json` mode."""

from __future__ import annotations

import argparse
from typing import Any

from sanduk.agent import RESOURCES, Agent, Outcome, Reader, Wiring
from sanduk.providers import ANTHROPIC_MESSAGES, Provider


class ClaudeReader(Reader):
    def __init__(self) -> None:
        self.result: dict[str, Any] | None = None

    def event(self, record: dict[str, Any], quiet: bool) -> None:
        kind = record.get("type")
        if kind == "result":
            self.result = record
            return
        if quiet:
            return
        if kind == "assistant":
            for b in record.get("message", {}).get("content", []):
                if b.get("type") == "text" and b.get("text", "").strip():
                    print(f"  . {b['text'].strip()[:160]}")
                elif b.get("type") == "tool_use":
                    print(f"  > {b.get('name')}")
        elif kind == "user":
            for b in record.get("message", {}).get("content", []):
                if b.get("type") == "tool_result" and b.get("is_error"):
                    print("  ! tool error")

    def finish(self) -> Outcome | None:
        if self.result is None:
            return None
        usage = self.result.get("usage", {})
        assert isinstance(usage, dict)
        cached = usage.get("cache_read_input_tokens", 0)
        # Claude Code reports cache reads and writes outside input_tokens.
        total_in = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + cached
        )
        text = str(self.result.get("result", ""))
        failed = bool(self.result.get("is_error"))
        return Outcome(
            ok=not failed,
            text="" if failed else text,
            error=text if failed else "",
            stats=(
                f"{self.result.get('num_turns', '?')} turns, "
                f"{total_in:,} in ({cached:,} cached) / "
                f"{usage.get('output_tokens', 0):,} out, "
                f"${self.result.get('total_cost_usd', 0):.4f}"
            ),
        )


class ClaudeCode(Agent):
    name = "claude"
    image = "sanduk:latest"
    containerfile = RESOURCES / "Containerfile.claude"
    protocols = frozenset({ANTHROPIC_MESSAGES})

    def argv(self, args: argparse.Namespace, provider: Provider, task: str) -> list[str]:
        argv = ["-p", task, "--output-format", "stream-json", "--verbose"]
        if args.bare:
            argv.append("--bare")
        if args.permission_mode:
            argv += ["--permission-mode", args.permission_mode]
        else:
            argv.append("--dangerously-skip-permissions")
        if args.model:
            argv += ["--model", args.model]
        if args.effort:
            argv += ["--effort", args.effort]
        if args.allowed_tools:
            argv += ["--allowed-tools", args.allowed_tools]
        if args.max_turns:
            argv += ["--max-turns", str(args.max_turns)]
        return argv

    def wire(
        self, args: argparse.Namespace, provider: Provider, root: str | None
    ) -> Wiring:
        # Claude Code appends /v1/messages itself, so the base URL is the bare
        # root. Left None off-relay: the built-in api.anthropic.com is correct,
        # and setting the variable would only add a way to get it wrong.
        return Wiring(
            key_env=args.agent_key_env or provider.key_env,
            base_url_env=args.agent_base_url_env or provider.base_url_env,
            base_url=root,
        )

    def reader(self) -> Reader:
        return ClaudeReader()
