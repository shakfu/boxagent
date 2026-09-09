"""earendil-works' `pi -p --mode json`.

pi speaks all three protocols the relay carries, so it reaches every provider.
It picks one per provider through the `api` field of a custom provider block.

The endpoint is neither a variable nor a flag: pi reads providers from
`models.json` in its config directory. The image's entrypoint writes that file
from `SANDUK_MODELS_JSON` before exec'ing pi, so the config lands in the
container's home rather than the bind mount, where it would sit in the user's
repository and be editable by the agent reading it.
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
from sanduk.providers import (
    ANTHROPIC_MESSAGES,
    OPENAI_CHAT,
    OPENAI_RESPONSES,
    Provider,
)

PROVIDER_ID = "sanduk"
KEY_ENV = "PI_RELAY_KEY"
# sanduk's own variable, read by the entrypoint in each image, not by pi.
CONFIG_ENV = "SANDUK_MODELS_JSON"

APIS = {
    ANTHROPIC_MESSAGES: "anthropic-messages",
    OPENAI_CHAT: "openai-completions",
    OPENAI_RESPONSES: "openai-responses",
}


def api(provider: Provider) -> str:
    """The `api` value for pi's provider block.

    Chat Completions is preferred where a provider serves both it and
    Responses: it is the route every OpenAI-shaped provider here has.
    """
    speaks = completion_protocols(provider)
    for protocol in (ANTHROPIC_MESSAGES, OPENAI_CHAT, OPENAI_RESPONSES):
        if protocol in speaks:
            return APIS[protocol]
    raise AgentboxError(f"pi cannot talk to {provider.name}")


class PiReader(Reader):
    """Events are `{type, ...}` JSON lines.

    Counts come off the assistant's own `message_end`, where they are final for
    that message. `message_update` carries the same field cumulatively while a
    message streams, and reading both would count every message twice.

    `input` excludes the cache -- `input: 19` beside `cacheRead: 1754` on one
    message -- so the run's input is the sum of the three.
    """

    def __init__(self) -> None:
        self.text = ""
        self.failed = ""
        self.ended = False
        self.tokens = {"input": 0, "output": 0, "cached": 0}
        self.cost = 0.0

    def event(self, record: dict[str, Any], quiet: bool) -> None:
        kind = str(record.get("type", ""))
        if kind == "message_end":
            self._message(record.get("message") or {}, quiet)
        elif kind == "tool_execution_start" and not quiet:
            print(f"  > {record.get('toolName', '')}")
        elif kind == "auto_retry_start" and not quiet:
            attempt = f"{record.get('attempt')}/{record.get('maxAttempts')}"
            print(f"  ! retry {attempt}: {record.get('errorMessage', '')}"[:160])
        elif kind in ("agent_end", "agent_settled"):
            # pi retries a failed provider call up to three times, ending an
            # agent each time. Only the one that will not retry is the end.
            self.ended = self.ended or not record.get("willRetry")

    def _message(self, message: dict[str, Any], quiet: bool) -> None:
        if message.get("role") != "assistant":
            return
        usage = message.get("usage") or {}
        cached = usage.get("cacheRead", 0)
        self.tokens["cached"] += cached
        self.tokens["input"] += (
            usage.get("input", 0) + cached + usage.get("cacheWrite", 0)
        )
        self.tokens["output"] += usage.get("output", 0)
        self.cost += (usage.get("cost") or {}).get("total", 0) or 0
        if message.get("stopReason") == "error":
            self.failed = str(message.get("errorMessage") or "the provider call failed")
            return
        # A retry that lands clears the attempt that did not.
        self.failed = ""
        text = text_of(message)
        if text:
            self.text = text
            if not quiet:
                print(f"  . {text.strip()[:160]}")

    def finish(self) -> Outcome | None:
        if not self.ended and not self.failed:
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


def text_of(message: dict[str, Any]) -> str:
    """The assistant text of one message, whatever shape its content takes."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)


class Pi(Agent):
    name = "pi"
    image = "sanduk-pi:latest"
    containerfile = RESOURCES / "Containerfile.pi"
    protocols = frozenset({ANTHROPIC_MESSAGES, OPENAI_CHAT, OPENAI_RESPONSES})
    # Attributes rather than constants: prime-agent is this CLI in another
    # build, and the variable names are all that differ.
    key_env = KEY_ENV
    base_url_env = "PI_RELAY_BASE_URL"
    config_env = CONFIG_ENV
    # Flags that refuse the mounted directory's own configuration. A build
    # without them passes none: an unknown flag is a run that does not start.
    trust_flags: tuple[str, ...] = ("--no-approve",)

    def check(self, args: argparse.Namespace, provider: Provider) -> None:
        super().check(args, provider)
        if not args.model:
            # The provider block lists the models pi may select, and --model
            # picks one out of it. Neither has anything to hold otherwise.
            raise AgentboxError("--model is required with --agent pi")

    def key_reference(self) -> str:
        """How the provider block names the variable holding the credential.

        pi interpolates `$NAME`; measured against 0.85.1. The credential itself
        never goes in the file: it is visible to `inspect` there.
        """
        return f"${self.key_env}"

    def models(self, args: argparse.Namespace, provider: Provider, base: str) -> str:
        """The models.json the image's entrypoint writes."""
        return json.dumps(
            {
                "providers": {
                    PROVIDER_ID: {
                        "name": "sanduk relay",
                        "baseUrl": base,
                        "api": api(provider),
                        # A reference, so the credential stays in its own
                        # variable rather than inside this blob.
                        "apiKey": self.key_reference(),
                        "models": [{"id": args.model}],
                    }
                }
            },
            separators=(",", ":"),
        )

    def wire(
        self, args: argparse.Namespace, provider: Provider, root: str | None
    ) -> Wiring:
        base = root or f"{provider.scheme}://{provider.host}"
        # pi's anthropic-messages driver appends /v1/messages to what it is
        # given, so that one gets the bare root. Measured: a base ending in
        # /v1 sent it to /v1/v1/messages, which the relay refused.
        if api(provider) != APIS[ANTHROPIC_MESSAGES]:
            base += provider.api_prefix
        return Wiring(
            key_env=args.agent_key_env or self.key_env,
            base_url_env=args.agent_base_url_env or self.base_url_env,
            base_url=base,
            env={self.config_env: self.models(args, provider, base)},
        )

    def argv(
        self, args: argparse.Namespace, provider: Provider, task: str, wiring: Wiring
    ) -> list[str]:
        return [
            "--print",
            "--mode",
            "json",
            # Nothing outlives the container, so a session file is only a write.
            "--no-session",
            # The bind mount is the user's repository; its own config would
            # otherwise steer the run that was asked for here.
            *self.trust_flags,
            "--model",
            f"{PROVIDER_ID}/{args.model}",
            # Everything after this is the task, whatever it starts with.
            "--",
            task,
        ]

    def reader(self) -> Reader:
        return PiReader()
