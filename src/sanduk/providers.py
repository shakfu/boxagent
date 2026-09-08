"""Model providers and the wire protocols they speak.

A provider is where requests go and how they are authenticated. A protocol is
how a request body and a usage block are shaped. The two are not 1:1: OpenAI
serves Responses and Chat Completions on different paths of the same host, and
those two name their token counts differently.

The protocol cannot be recovered from a response body. Anthropic Messages and
OpenAI Responses both report `input_tokens` and `output_tokens`, so a sniffer
reading field names alone cannot tell them apart. `Provider.routes` declares it
per path instead.

Only Anthropic is implemented. A second provider is a `Provider` row and a
`PROVIDERS` entry.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sanduk.errors import AgentboxError

ANTHROPIC_MESSAGES = "anthropic-messages"
OPENAI_CHAT = "openai-chat"
OPENAI_RESPONSES = "openai-responses"


@dataclass(frozen=True)
class Protocol:
    """How one wire format names the things the relay has to read or rewrite."""

    name: str
    # Field --max-tokens-cap clamps. Anthropic calls it max_tokens; OpenAI Chat
    # calls it max_completion_tokens and Responses calls it max_output_tokens.
    cap_field: str
    # Canonical counter -> the key it arrives under in this protocol's `usage`.
    # Dotted for a nested key; UsageSniffer flattens what it reads to match.
    # A counter this protocol does not report is left out, so the log line says
    # 0 only where 0 is the truth.
    usage_fields: Mapping[str, str]


ANTHROPIC = Protocol(
    name=ANTHROPIC_MESSAGES,
    cap_field="max_tokens",
    usage_fields={
        "in": "input_tokens",
        "cache_write": "cache_creation_input_tokens",
        "cache_read": "cache_read_input_tokens",
        "out": "output_tokens",
    },
)

OPENAI_CHAT_PROTOCOL = Protocol(
    name=OPENAI_CHAT,
    cap_field="max_completion_tokens",
    usage_fields={
        "in": "prompt_tokens",
        "cache_read": "prompt_tokens_details.cached_tokens",
        "out": "completion_tokens",
        # No cache_write: OpenAI-shaped responses do not report one.
    },
)

# Responses reuses Anthropic's top-level counter names, which is why a sniffer
# cannot tell the two apart from a body and the protocol has to be declared.
OPENAI_RESPONSES_PROTOCOL = Protocol(
    name=OPENAI_RESPONSES,
    cap_field="max_output_tokens",
    usage_fields={
        "in": "input_tokens",
        "cache_read": "input_tokens_details.cached_tokens",
        "out": "output_tokens",
    },
)

PROTOCOLS: dict[str, Protocol] = {
    p.name: p for p in (ANTHROPIC, OPENAI_CHAT_PROTOCOL, OPENAI_RESPONSES_PROTOCOL)
}


@dataclass(frozen=True)
class Provider:
    """One upstream API: where it is, how it authenticates, what it accepts."""

    name: str
    host: str  # host[:port]. No scheme and no path; see Config.upstream.
    # Exact path -> the wire protocol of the completion request sent to it, or
    # None for a path that carries no body policy and no usage. Exact matches,
    # not prefixes: "/v1/models" as a prefix also admits
    # "/v1/models-internal-secret".
    routes: Mapping[str, str | None]
    key_env: str
    base_url_env: str
    # The path prefix every route shares. An agent that is handed a base URL
    # rather than building one from a pinned host needs it, or its requests
    # land off `routes` and the relay rejects them.
    api_prefix: str = "/v1"
    auth_header: str = "x-api-key"
    # "Bearer" for OpenAI-shaped providers, "" when the header holds the bare
    # credential. Applies to the token the container presents and to the key
    # written upstream.
    auth_scheme: str = ""
    scheme: str = "https"
    has_auth: bool = True
    # Whether a streamed request needs stream_options.include_usage added for
    # the response to report tokens at all. A provider property, not a protocol
    # one: OpenAI and OpenRouter both speak openai-chat, and only OpenAI needs
    # the flag. OpenRouter sends usage in the final chunk unasked.
    stream_usage_option: bool = False
    validate_path: str = "/v1/models"
    validate_headers: Mapping[str, str] = field(default_factory=dict)

    def auth_value(self, credential: str) -> str:
        """The header value carrying `credential`."""
        return f"{self.auth_scheme} {credential}" if self.auth_scheme else credential

    def presented(self, header_value: str) -> str:
        """The credential inside an incoming header value, scheme removed."""
        if not self.auth_scheme:
            return header_value
        prefix = self.auth_scheme.lower() + " "
        if header_value.lower().startswith(prefix):
            return header_value[len(prefix) :].strip()
        return ""

    def protocol(self, path: str) -> Protocol | None:
        name = self.routes.get(path)
        return PROTOCOLS[name] if name else None


ANTHROPIC_PROVIDER = Provider(
    name="anthropic",
    host="api.anthropic.com",
    routes={
        "/v1/messages": ANTHROPIC_MESSAGES,
        # count_tokens carries a body but is not a completion: clamping its
        # max_tokens is meaningless, so it is deliberately policy-exempt.
        "/v1/messages/count_tokens": None,
        "/v1/models": None,
    },
    key_env="ANTHROPIC_API_KEY",
    base_url_env="ANTHROPIC_BASE_URL",
    validate_headers={"anthropic-version": "2023-06-01"},
)

# Any server speaking the OpenAI Chat Completions API: llama.cpp's llama-server,
# Ollama, LM Studio, vLLM. The host is a placeholder; --upstream supplies the
# real one. has_auth is False because a local server usually has no key, and
# requiring one would block the common case. A key is still used if the
# environment holds one, so `llama-server --api-key` also works.
OPENAI_COMPAT_PROVIDER = Provider(
    name="openai-compat",
    host="127.0.0.1:8080",
    routes={
        "/v1/chat/completions": OPENAI_CHAT,
        "/v1/models": None,
    },
    key_env="OPENAI_API_KEY",
    base_url_env="OPENAI_BASE_URL",
    auth_header="authorization",
    auth_scheme="Bearer",
    scheme="http",
    has_auth=False,
    # The provider's contract is the OpenAI API, and include_usage is part of
    # it. A server that ignores the field loses nothing.
    stream_usage_option=True,
)

# OpenAI serves two protocols on two paths, which is the reason routes carry a
# protocol each rather than the provider carrying one.
OPENAI_PROVIDER = Provider(
    name="openai",
    host="api.openai.com",
    routes={
        "/v1/responses": OPENAI_RESPONSES,
        "/v1/chat/completions": OPENAI_CHAT,
        "/v1/models": None,
    },
    key_env="OPENAI_API_KEY",
    base_url_env="OPENAI_BASE_URL",
    auth_header="authorization",
    auth_scheme="Bearer",
    stream_usage_option=True,
)

# OpenRouter's base URL is https://openrouter.ai/api/v1, so every path carries
# the /api/v1 prefix. Validation goes to /api/v1/key, not /api/v1/models:
# models answers 200 with no credential at all, so it would pass any key,
# including a garbage one, and the preflight would prove nothing.
OPENROUTER_PROVIDER = Provider(
    name="openrouter",
    host="openrouter.ai",
    routes={
        "/api/v1/chat/completions": OPENAI_CHAT,
        "/api/v1/models": None,
    },
    key_env="OPENROUTER_API_KEY",
    base_url_env="OPENROUTER_BASE_URL",
    api_prefix="/api/v1",
    auth_header="authorization",
    auth_scheme="Bearer",
    validate_path="/api/v1/key",
)

PROVIDERS: dict[str, Provider] = {
    p.name: p
    for p in (
        ANTHROPIC_PROVIDER,
        OPENAI_PROVIDER,
        OPENROUTER_PROVIDER,
        OPENAI_COMPAT_PROVIDER,
    )
}
DEFAULT_PROVIDER = "anthropic"


def is_loopback(host: str) -> bool:
    """True when `host` cannot leave this machine."""
    name = host.rsplit(":", 1)[0] if ":" in host else host
    name = name.strip("[]")
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def parse_upstream(url: str, insecure: bool = False) -> tuple[str, str]:
    """Split `--upstream` into (scheme, host[:port]).

    Plaintext is refused off-machine: the relay writes the real key into every
    forwarded request, so an http:// upstream that is not loopback puts the
    credential on the wire in clear.
    """
    split = urlsplit(url if "//" in url else f"//{url}", scheme="https")
    if split.scheme not in ("http", "https"):
        raise AgentboxError(
            f"--upstream scheme must be http or https, not {split.scheme!r}"
        )
    if not split.netloc:
        raise AgentboxError(f"--upstream has no host: {url!r}")
    if split.path.rstrip("/"):
        raise AgentboxError(
            f"--upstream carries a path ({split.path!r}). Give scheme://host:port "
            "only; path prefixes belong in --proxy-allow-path and the agent's base URL."
        )
    if split.scheme == "http" and not insecure and not is_loopback(split.netloc):
        raise AgentboxError(
            f"refusing plaintext http to {split.netloc}: the API key would be sent "
            "in clear. Use https, a loopback address, or --insecure-upstream."
        )
    return split.scheme, split.netloc


def get_provider(name: str = DEFAULT_PROVIDER) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS))
        raise KeyError(f"unknown provider {name!r}; known: {known}") from None
