"""OpenAI's `codex exec --json`.

codex speaks the OpenAI Responses API and nothing else: `wire_api` accepts only
`responses`. Two of sanduk's providers serve that route: `openai`, and
`openai-compat` against a llama-server new enough to answer `/v1/responses`.
`check` refuses `anthropic` and `openrouter`.

It also learns its endpoint differently from every other agent. There is no base
URL variable; the endpoint is a config key, overridden per run with `-c`. That
is why `argv` takes the wiring.
"""

from __future__ import annotations

import argparse
from typing import Any

from sanduk.agent import RESOURCES, Agent, Outcome, Reader, Wiring
from sanduk.providers import OPENAI_RESPONSES, Provider

# The provider id sanduk defines in codex's config. Any name would do; this one
# says where it came from when it turns up in a trace.
PROVIDER_ID = "sanduk"
KEY_ENV = "CODEX_RELAY_KEY"


class CodexReader(Reader):
    def __init__(self) -> None:
        self.result: dict[str, Any] | None = None
        self.text = ""
        self.failed = ""

    def event(self, record: dict[str, Any], quiet: bool) -> None:
        kind = record.get("type")
        if kind == "turn.completed":
            self.result = record
            return
        if kind in ("turn.failed", "error"):
            self.failed = str(record.get("error") or record.get("message") or kind)
            return
        if not kind or not kind.startswith("item."):
            return
        item = record.get("item", {})
        itype = item.get("type")
        # The last agent_message is the answer; keep it whether tracing or not.
        if itype == "agent_message":
            self.text = str(item.get("text", ""))
        if quiet:
            return
        # A command arrives twice, as item.started and item.completed; a message
        # and an item-level error only as item.completed. Tracing every item
        # record printed each command twice. Commands trace on started, so the
        # line appears while the command runs rather than after it.
        if itype == "command_execution" and kind == "item.started":
            print(f"  > {str(item.get('command', ''))[:160]}")
        elif kind != "item.completed":
            return
        elif itype == "agent_message" and self.text.strip():
            print(f"  . {self.text.strip()[:160]}")
        elif itype == "error":
            # Not fatal: codex reports a failed turn as turn.failed. Traced
            # because a swallowed error record is the run's only warning.
            print(f"  ! {str(item.get('message', ''))[:160]}")

    def finish(self) -> Outcome | None:
        if self.result is None and not self.failed:
            return None
        usage = (self.result or {}).get("usage", {})
        cached = usage.get("cached_input_tokens", 0)
        return Outcome(
            ok=not self.failed,
            text="" if self.failed else self.text,
            error=self.failed,
            stats=(
                # codex reports no turn count and no cost; what it has is
                # tokens, and cached input is counted inside input_tokens.
                f"{usage.get('input_tokens', 0):,} in ({cached:,} cached) / "
                f"{usage.get('output_tokens', 0):,} out"
            ),
        )


class Codex(Agent):
    name = "codex"
    image = "sanduk-codex:latest"
    containerfile = RESOURCES / "Containerfile.codex"
    protocols = frozenset({OPENAI_RESPONSES})

    def wire(
        self, args: argparse.Namespace, provider: Provider, root: str | None
    ) -> Wiring:
        base = root or f"{provider.scheme}://{provider.host}"
        return Wiring(
            key_env=args.agent_key_env or KEY_ENV,
            # Named for completeness and for --dry-run to print; codex reads the
            # endpoint from the config override in argv, not from a variable.
            base_url_env=args.agent_base_url_env or "CODEX_RELAY_BASE_URL",
            base_url=base + provider.api_prefix,
            env={
                # No route off the proxy network, so an update check can only
                # stall the run.
                "CODEX_DISABLE_UPDATE_CHECK": "1",
            },
        )

    def argv(
        self, args: argparse.Namespace, provider: Provider, task: str, wiring: Wiring
    ) -> list[str]:
        # -c values are TOML, so a string carries its own quotes.
        def config(key: str, value: str) -> list[str]:
            return ["-c", f'{key}="{value}"']

        argv = ["exec", "--json"]
        # The container is the boundary; codex's own sandbox inside it would
        # only stop the agent doing the work it was given.
        argv += ["--sandbox", "danger-full-access"]
        # Nothing on this image is the user's, but a stray config would still
        # be a second source of truth for the endpoint.
        argv += ["--ignore-user-config"]
        # The bind mount is usually not a repository, and codex refuses an
        # untrusted directory. The container is the boundary that check exists
        # to approximate.
        argv += ["--skip-git-repo-check"]
        argv += config("model_provider", PROVIDER_ID)
        argv += config(f"model_providers.{PROVIDER_ID}.name", "sanduk relay")
        argv += config(f"model_providers.{PROVIDER_ID}.base_url", wiring.base_url or "")
        argv += config(f"model_providers.{PROVIDER_ID}.env_key", wiring.key_env)
        argv += config(f"model_providers.{PROVIDER_ID}.wire_api", "responses")
        if args.model:
            argv += ["--model", args.model]
        argv.append(task)
        return argv

    def reader(self) -> Reader:
        return CodexReader()
