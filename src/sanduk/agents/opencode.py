"""`opencode run --format json`.

opencode has no base-URL variable either, but it does take its whole config from
one: `OPENCODE_CONFIG_CONTENT` holds the JSON that would otherwise be an
`opencode.json` file. That is why sanduk writes no file into the bind mount --
a config there would sit in the user's repository and be editable by the agent
that reads it.

The provider block picks an npm driver by wire protocol, so opencode reaches
every provider sanduk has: `@ai-sdk/openai-compatible` for Chat Completions,
`@ai-sdk/anthropic` for Messages.
"""

from __future__ import annotations

import argparse
import json
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

PROVIDER_ID = "sanduk"
KEY_ENV = "OPENCODE_RELAY_KEY"

DRIVERS = {
    ANTHROPIC_MESSAGES: "@ai-sdk/anthropic",
    OPENAI_CHAT: "@ai-sdk/openai-compatible",
}


def driver(provider: Provider) -> str:
    """The npm driver matching this provider's wire protocol."""
    if ANTHROPIC_MESSAGES in completion_protocols(provider):
        return DRIVERS[ANTHROPIC_MESSAGES]
    return DRIVERS[OPENAI_CHAT]


class OpenCodeReader(Reader):
    """Records are `{type, part}`; `part.type` repeats the kind hyphenated.

    Token counts arrive per step, not once at the end, and `part.tokens.input`
    excludes the cache -- `input: 50` beside `cache.read: 7185` on one step --
    so the input total is the sum of the three.
    """

    def __init__(self) -> None:
        self.text = ""
        self.failed = ""
        self.stepped = False
        self.tokens = {"input": 0, "output": 0, "cached": 0}
        self.cost = 0.0

    def event(self, record: dict[str, Any], quiet: bool) -> None:
        kind = str(record.get("type", ""))
        part = record.get("part") or {}
        if "error" in kind:
            self.failed = str(record.get("error") or kind)
            return
        if kind == "step_finish":
            self.stepped = True
            counts = part.get("tokens") or {}
            cache = counts.get("cache") or {}
            self.tokens["cached"] += cache.get("read", 0)
            self.tokens["input"] += (
                counts.get("input", 0) + cache.get("read", 0) + cache.get("write", 0)
            )
            self.tokens["output"] += counts.get("output", 0)
            self.cost += part.get("cost") or 0
            return
        if kind == "text" and part.get("text"):
            self.text = str(part["text"])
            if not quiet:
                print(f"  . {self.text.strip()[:160]}")
        elif kind == "tool_use" and not quiet:
            print(f"  > {part.get('tool')}")

    def finish(self) -> Outcome | None:
        if not self.stepped and not self.failed:
            return None
        cached = self.tokens["cached"]
        return Outcome(
            ok=not self.failed,
            text="" if self.failed else self.text,
            error=self.failed,
            stats=(
                f"{self.tokens['input']:,} in ({cached:,} cached) / "
                f"{self.tokens['output']:,} out, ${self.cost:.4f}"
            ),
        )


class OpenCode(Agent):
    name = "opencode"
    image = "sanduk-opencode:latest"
    containerfile = RESOURCES / "Containerfile.opencode"
    protocols = frozenset({ANTHROPIC_MESSAGES, OPENAI_CHAT})

    def check(self, args: argparse.Namespace, provider: Provider) -> None:
        super().check(args, provider)
        if not args.model:
            # The config names one model and --model selects it. Without a name
            # there is nothing to put in either, and opencode would ask.
            raise AgentboxError("--model is required with --agent opencode")

    def config(self, args: argparse.Namespace, provider: Provider, base: str) -> str:
        """The opencode.json that `OPENCODE_CONFIG_CONTENT` carries."""
        return json.dumps(
            {
                "provider": {
                    PROVIDER_ID: {
                        "npm": driver(provider),
                        "name": "sanduk relay",
                        "options": {
                            "baseURL": base,
                            # Left as a reference so the credential stays in its
                            # own variable rather than inside this blob.
                            "apiKey": f"{{env:{KEY_ENV}}}",
                        },
                        "models": {args.model: {"name": args.model}},
                    }
                }
            },
            separators=(",", ":"),
        )

    def wire(
        self, args: argparse.Namespace, provider: Provider, root: str | None
    ) -> Wiring:
        base = (root or f"{provider.scheme}://{provider.host}") + provider.api_prefix
        return Wiring(
            key_env=args.agent_key_env or KEY_ENV,
            base_url_env=args.agent_base_url_env or "OPENCODE_RELAY_BASE_URL",
            base_url=base,
            env={"OPENCODE_CONFIG_CONTENT": self.config(args, provider, base)},
        )

    def argv(
        self, args: argparse.Namespace, provider: Provider, task: str, wiring: Wiring
    ) -> list[str]:
        return [
            "run",
            "--format",
            "json",
            # Nothing is there to answer a prompt, and the container is the
            # boundary this would otherwise duplicate.
            "--auto",
            "--model",
            f"{PROVIDER_ID}/{args.model}",
            task,
        ]

    def reader(self) -> Reader:
        return OpenCodeReader()
