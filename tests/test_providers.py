"""Provider records and the route table.

The route table is both the egress allowlist and the protocol declaration, so a
mistake here is either a 403 on a legitimate call or a policy that never fires.
"""

import pytest

from sanduk import proxy
from sanduk.errors import AgentboxError
from sanduk.providers import (
    ANTHROPIC_PROVIDER,
    PROTOCOLS,
    PROVIDERS,
    get_provider,
    parse_upstream,
)


def test_anthropic_is_the_default_provider():
    assert get_provider().name == "anthropic"


def test_unknown_provider_names_the_known_ones():
    with pytest.raises(KeyError, match="anthropic"):
        get_provider("gemini")


def test_every_route_names_a_real_protocol():
    for provider in (ANTHROPIC_PROVIDER,):
        for path, name in provider.routes.items():
            assert name is None or name in PROTOCOLS, f"{provider.name} {path}"


def test_the_default_allowlist_matches_the_provider_routes():
    """proxy.DEFAULT_ALLOW is what cli.py passes when no --proxy-allow-path is
    given, and scripts/sanduk.py carries its own copy of it."""
    assert set(proxy.DEFAULT_ALLOW) == set(ANTHROPIC_PROVIDER.routes)


def test_only_messages_carries_a_protocol():
    """count_tokens has a body but is not a completion, so clamping its
    max_tokens is meaningless. /v1/models has no body at all."""
    p = ANTHROPIC_PROVIDER
    assert p.protocol("/v1/messages") is PROTOCOLS["anthropic-messages"]
    assert p.protocol("/v1/messages/count_tokens") is None
    assert p.protocol("/v1/models") is None


def test_an_unknown_path_has_no_protocol():
    assert ANTHROPIC_PROVIDER.protocol("/v1/nope") is None


def test_a_bare_credential_header_round_trips():
    p = ANTHROPIC_PROVIDER
    assert p.auth_header == "x-api-key"
    assert p.auth_value("KEY") == "KEY"
    assert p.presented("KEY") == "KEY"


def test_added_paths_are_admitted_without_policy():
    """--proxy-allow-path widens egress; it must not silently gain a protocol
    and start rewriting bodies the provider never declared."""
    cfg = proxy.Config(
        "key", "tok", ["/v1/messages", "/v1/custom"], "host", False, max_tokens_cap=10
    )
    assert cfg.allow_paths == {"/v1/messages", "/v1/custom"}
    assert cfg.protocol("/v1/messages") is PROTOCOLS["anthropic-messages"]
    assert cfg.protocol("/v1/custom") is None


def test_the_cap_field_comes_from_the_protocol():
    assert PROTOCOLS["anthropic-messages"].cap_field == "max_tokens"


def test_claude_code_and_the_anthropic_provider_agree_on_env_names():
    """agent.py names what Claude Code reads; the provider names what sanduk
    writes. They are separate on purpose, so nothing enforces the match but
    this. If they diverge, the default wiring breaks silently."""
    from sanduk import agent

    assert ANTHROPIC_PROVIDER.key_env == agent.KEY_ENV
    assert ANTHROPIC_PROVIDER.base_url_env == agent.BASE_URL_ENV


# --- upstream parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://127.0.0.1:8080", ("http", "127.0.0.1:8080")),
        ("http://localhost:1234", ("http", "localhost:1234")),
        ("http://[::1]:8080", ("http", "[::1]:8080")),
        ("https://api.openai.com", ("https", "api.openai.com")),
        ("openrouter.ai", ("https", "openrouter.ai")),
        ("https://api.openai.com/", ("https", "api.openai.com")),
    ],
)
def test_upstream_parsing(given, expected):
    assert parse_upstream(given) == expected


@pytest.mark.parametrize(
    "given",
    ["http://example.com", "http://192.168.1.9:8080", "http://10.0.0.2"],
)
def test_plaintext_off_machine_is_refused(given):
    """The relay writes the real key into every forwarded request."""
    with pytest.raises(AgentboxError, match="plaintext"):
        parse_upstream(given)


def test_plaintext_off_machine_can_be_forced():
    assert parse_upstream("http://example.com", insecure=True) == ("http", "example.com")


def test_a_path_in_the_upstream_is_refused():
    """OpenRouter's /api/v1 belongs in the allowlist, not here. Silently
    dropping it would send every request to the wrong path."""
    with pytest.raises(AgentboxError, match="path"):
        parse_upstream("https://openrouter.ai/api/v1")


def test_a_nonsense_scheme_is_refused():
    with pytest.raises(AgentboxError, match="scheme"):
        parse_upstream("ftp://example.com")


# --- openai-compat ----------------------------------------------------------


def test_openai_compat_speaks_bearer():
    p = get_provider("openai-compat")
    assert p.auth_header == "authorization"
    assert p.auth_value("KEY") == "Bearer KEY"
    assert p.presented("Bearer KEY") == "KEY"
    assert p.presented("bearer KEY") == "KEY"


def test_openai_compat_rejects_a_bare_credential():
    """Without the scheme the value is not a Bearer credential, and admitting
    it would make the header format unenforced."""
    assert get_provider("openai-compat").presented("KEY") == ""


def test_openai_compat_needs_no_key():
    """A local llama-server usually has no auth; requiring one would block the
    common case."""
    assert get_provider("openai-compat").has_auth is False


def test_openai_chat_declares_no_cache_write():
    """OpenAI-shaped responses do not report cache writes. Printing 0 would be
    indistinguishable from a real zero."""
    fields = PROTOCOLS["openai-chat"].usage_fields
    assert "cache_write" not in fields
    assert fields["cache_read"] == "prompt_tokens_details.cached_tokens"


# --- openai and openrouter --------------------------------------------------


def test_openai_serves_two_protocols_on_two_paths():
    """The reason a route carries a protocol rather than the provider doing so."""
    p = get_provider("openai")
    assert p.protocol("/v1/responses") is PROTOCOLS["openai-responses"]
    assert p.protocol("/v1/chat/completions") is PROTOCOLS["openai-chat"]
    assert p.protocol("/v1/models") is None


def test_responses_and_anthropic_collide_on_counter_names():
    """Neither can be identified from a body, which is why the route table
    declares the protocol instead of the sniffer guessing it."""
    responses = PROTOCOLS["openai-responses"].usage_fields
    anthropic = PROTOCOLS["anthropic-messages"].usage_fields
    assert responses["in"] == anthropic["in"] == "input_tokens"
    assert responses["out"] == anthropic["out"] == "output_tokens"
    assert responses["cache_read"] != anthropic["cache_read"]


def test_each_protocol_clamps_its_own_field():
    assert PROTOCOLS["anthropic-messages"].cap_field == "max_tokens"
    assert PROTOCOLS["openai-chat"].cap_field == "max_completion_tokens"
    assert PROTOCOLS["openai-responses"].cap_field == "max_output_tokens"


def test_openrouter_paths_carry_the_api_prefix():
    """Its base URL is openrouter.ai/api/v1. A path copied from OpenAI's docs
    would 403 here, and silently, since the allowlist is exact."""
    p = get_provider("openrouter")
    assert p.protocol("/api/v1/chat/completions") is PROTOCOLS["openai-chat"]
    assert p.protocol("/v1/chat/completions") is None
    assert "/v1/chat/completions" not in p.routes


def test_openrouter_validates_against_key_not_models():
    """/api/v1/models answers 200 with no credential, so validating there would
    pass any key, including a garbage one. Verified against the live endpoint."""
    assert get_provider("openrouter").validate_path == "/api/v1/key"


def test_every_provider_declares_where_its_key_comes_from():
    seen = {}
    for name, p in PROVIDERS.items():
        assert p.key_env, name
        assert p.base_url_env, name
        seen.setdefault(p.key_env, []).append(name)
    # openai and openai-compat deliberately share OPENAI_API_KEY: an
    # OpenAI-compatible server is usually addressed with the same variable.
    assert seen["ANTHROPIC_API_KEY"] == ["anthropic"]
    assert seen["OPENROUTER_API_KEY"] == ["openrouter"]


def test_only_the_named_providers_authenticate_upstream():
    """openai-compat is the exception, and the only one that may run keyless."""
    keyless = {n for n, p in PROVIDERS.items() if not p.has_auth}
    assert keyless == {"openai-compat"}


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_no_provider_admits_a_path_outside_its_own_routes(name):
    """Exact matching, per provider. A path is never admitted because some
    other provider happens to serve it."""
    p = get_provider(name)
    others = set()
    for other, q in PROVIDERS.items():
        if other != name:
            others |= set(q.routes)
    for path in others - set(p.routes):
        cfg = proxy.Config("k", "t", None, p.host, False, provider=p)
        assert path not in cfg.allow_paths, f"{name} admits {path}"
