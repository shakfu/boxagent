"""Agent handlers: the registry, the plugin path, and each shipped handler.

Nothing here starts a process. `launch` is exercised through the readers, which
are the only stateful part of a handler.
"""

import argparse
from types import SimpleNamespace

import pytest

from sanduk.agent import Agent, Outcome, Reader, Wiring, agent_names, get_agent, registry
from sanduk.agents.claude import ClaudeCode
from sanduk.agents.hax import Hax
from sanduk.errors import AgentboxError
from sanduk.providers import get_provider


def flags(**kw) -> argparse.Namespace:
    """The subset of the command line a handler reads."""
    defaults = dict(
        agent="claude",
        model=None,
        effort=None,
        max_turns=None,
        allowed_tools=None,
        permission_mode=None,
        bare=False,
        agent_key_env=None,
        agent_base_url_env=None,
    )
    return argparse.Namespace(**{**defaults, **kw})


# --- registry and plugin loading --------------------------------------------


def test_the_shipped_handlers_are_found_without_install_metadata():
    """A source checkout has no entry points; losing claude there would be the
    worst possible failure mode, so the built-ins are seeded directly."""
    assert set(agent_names()) >= {"claude", "hax"}
    assert registry()["claude"] is ClaudeCode


@pytest.mark.parametrize("agent", [ClaudeCode, Hax])
def test_every_shipped_handler_names_an_image_and_a_containerfile(agent):
    """The Containerfile is package data reached through __file__; a rename that
    misses one handler is only visible on a build otherwise."""
    assert agent.image
    assert agent.containerfile is not None and agent.containerfile.is_file()


def test_an_unknown_agent_names_the_known_ones():
    with pytest.raises(AgentboxError, match="claude"):
        get_agent("nope")


class Fake(Agent):
    """A handler a user could write, in their own module."""

    name = "fake"
    image = "fake:latest"
    protocols = frozenset({"anthropic-messages"})

    def argv(self, args, provider, task):
        return [task]

    def wire(self, args, provider, root):
        return Wiring(key_env="FAKE_KEY", base_url_env="FAKE_URL", base_url=root)

    def reader(self):
        raise NotImplementedError


def test_a_handler_outside_the_registry_loads_by_path():
    agent = get_agent("test_agent:Fake")
    assert isinstance(agent, Fake)
    assert agent.image == "fake:latest"


def test_a_path_that_is_not_an_agent_is_refused():
    with pytest.raises(AgentboxError, match=r"not a sanduk\.agent\.Agent"):
        get_agent("test_agent:flags")


def test_a_path_that_does_not_import_is_refused():
    with pytest.raises(AgentboxError, match="could not load"):
        get_agent("no_such_module:Thing")


def test_a_plugin_cannot_shadow_a_shipped_handler(monkeypatch, capsys):
    """Replacing `claude` would change what runs in the container without
    changing the command line."""

    class Impostor(Fake):
        name = "claude"

    ep = SimpleNamespace(name="claude", load=lambda: Impostor)
    monkeypatch.setattr("sanduk.agent.entry_points", lambda group: [ep])
    registry.cache_clear()
    try:
        assert registry()["claude"] is ClaudeCode
        assert "cannot replace" in capsys.readouterr().err
    finally:
        registry.cache_clear()


def test_a_broken_plugin_does_not_take_the_run_down(monkeypatch, capsys):
    def explode():
        raise ImportError("no")

    ep = SimpleNamespace(name="broken", load=explode)
    monkeypatch.setattr("sanduk.agent.entry_points", lambda group: [ep])
    registry.cache_clear()
    try:
        assert "claude" in registry()
        assert "failed to load" in capsys.readouterr().err
    finally:
        registry.cache_clear()


def test_a_plugin_is_registered_under_its_own_name(monkeypatch):
    ep = SimpleNamespace(name="fake", load=lambda: Fake)
    monkeypatch.setattr("sanduk.agent.entry_points", lambda group: [ep])
    registry.cache_clear()
    try:
        assert registry()["fake"] is Fake
    finally:
        registry.cache_clear()


# --- protocol matching ------------------------------------------------------


@pytest.mark.parametrize("provider", ["openai", "openrouter", "openai-compat"])
def test_claude_refuses_a_provider_it_cannot_speak_to(provider):
    """Claude Code speaks Anthropic Messages only. Letting the run start would
    fail later with a 404 from the relay instead."""
    with pytest.raises(AgentboxError, match="cannot talk"):
        ClaudeCode().check(flags(), get_provider(provider))


@pytest.mark.parametrize(
    "provider", ["anthropic", "openai", "openrouter", "openai-compat"]
)
def test_hax_speaks_to_every_provider(provider):
    Hax().check(flags(agent="hax"), get_provider(provider))


def test_hax_refuses_a_claude_only_restriction():
    """Silently dropping --allowed-tools would weaken a restriction the caller
    asked for."""
    with pytest.raises(AgentboxError, match="allowed-tools"):
        Hax().check(flags(agent="hax", allowed_tools="Read"), get_provider("anthropic"))


# --- wiring -----------------------------------------------------------------


def test_claude_gets_the_bare_relay_root():
    """Claude Code appends /v1/messages itself."""
    wiring = ClaudeCode().wire(flags(), get_provider("anthropic"), "http://10.0.0.1:9")
    assert wiring == Wiring(
        key_env="ANTHROPIC_API_KEY",
        base_url_env="ANTHROPIC_BASE_URL",
        base_url="http://10.0.0.1:9",
    )


def test_claude_is_left_on_its_own_endpoint_without_a_relay():
    assert ClaudeCode().wire(flags(), get_provider("anthropic"), None).base_url is None


@pytest.mark.parametrize(
    ("provider", "prefix", "family"),
    [
        ("anthropic", "/v1", "ANTHROPIC"),
        ("openai", "/v1", "OPENAI"),
        ("openai-compat", "/v1", "OPENAI"),
        # OpenRouter serves every route under /api/v1, so a base URL ending in
        # /v1 would post off the relay's route table.
        ("openrouter", "/api/v1", "OPENAI"),
    ],
)
def test_hax_base_url_carries_the_provider_prefix(provider, prefix, family):
    wiring = Hax().wire(flags(agent="hax"), get_provider(provider), "http://10.0.0.1:9")
    assert wiring.base_url == f"http://10.0.0.1:9{prefix}"
    assert wiring.key_env == f"HAX_{family}_API_KEY"
    assert wiring.base_url_env == f"HAX_{family}_BASE_URL"


def test_hax_without_a_relay_points_at_the_provider_itself():
    wiring = Hax().wire(flags(agent="hax"), get_provider("anthropic"), None)
    assert wiring.base_url == "https://api.anthropic.com/v1"


def test_hax_disables_the_catalog_fetch():
    """The proxy network has no route off the host: the fetch can only hang."""
    wiring = Hax().wire(flags(agent="hax"), get_provider("openai"), "http://10.0.0.1:9")
    assert wiring.env["HAX_CATALOG_URL"] == ""


def test_agent_key_env_overrides_the_handler():
    wiring = Hax().wire(
        flags(agent="hax", agent_key_env="MY_TOKEN"), get_provider("openai"), None
    )
    assert wiring.key_env == "MY_TOKEN"


# --- argv -------------------------------------------------------------------


def test_hax_selects_the_compatible_provider_matching_the_protocol():
    args = flags(agent="hax", model="qwen3", max_turns=3)
    assert Hax().argv(args, get_provider("anthropic"), "go")[:2] == [
        "--json",
        "--provider=anthropic-compatible",
    ]
    assert "--provider=openai-compatible" in Hax().argv(
        args, get_provider("openai"), "go"
    )


def test_hax_takes_the_turn_cap_from_the_environment():
    """hax has no --max-turns flag; the setting has a HAX_ variable instead."""
    args = flags(agent="hax", max_turns=3)
    assert "3" not in " ".join(Hax().argv(args, get_provider("openai"), "go"))
    assert Hax().wire(args, get_provider("openai"), None).env["HAX_MAX_TURNS"] == "3"


def test_the_task_is_the_last_hax_argument():
    argv = Hax().argv(flags(agent="hax"), get_provider("openai"), "do the thing")
    assert argv[-1] == "do the thing"


# --- readers ----------------------------------------------------------------


def drain(reader: Reader, records: list[dict]) -> Outcome | None:
    for record in records:
        reader.event(record, quiet=True)
    return reader.finish()


def test_a_reader_is_fresh_per_run():
    """Two runs off one handler must not share a token tally."""
    agent = Hax()
    first, second = agent.reader(), agent.reader()
    first.event({"kind": "turn_usage", "usage": {"input": 100}}, quiet=True)
    assert second.tokens["input"] == 0


def test_no_terminal_record_means_no_outcome():
    assert drain(ClaudeCode().reader(), [{"type": "assistant"}]) is None
    assert drain(Hax().reader(), [{"kind": "assistant", "text": "hi"}]) is None


def test_claude_totals_cache_reads_outside_input_tokens():
    outcome = drain(
        ClaudeCode().reader(),
        [
            {
                "type": "result",
                "result": "done",
                "num_turns": 4,
                "total_cost_usd": 0.25,
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "output_tokens": 5,
                },
            }
        ],
    )
    assert outcome == Outcome(
        ok=True, text="done", stats="4 turns, 60 in (30 cached) / 5 out, $0.2500"
    )


def test_hax_totals_the_per_turn_usage_items():
    """The result record carries turns and cost but no token counts, and hax
    normalizes cache reads into input rather than reporting them beside it."""
    outcome = drain(
        Hax().reader(),
        [
            {"kind": "turn_usage", "usage": {"input": 40, "output": 3, "cached": 30}},
            {"kind": "turn_usage", "usage": {"input": 20, "output": 2, "cached": 0}},
            {
                "type": "result",
                "outcome": "complete",
                "text": "done",
                "turns": 2,
                "cost": 0.25,
            },
        ],
    )
    assert outcome == Outcome(
        ok=True, text="done", stats="2 turns, 60 in (30 cached) / 5 out, $0.2500"
    )


@pytest.mark.parametrize("outcome", ["error", "max_turns", "interrupted"])
def test_any_hax_outcome_but_complete_is_a_failure(outcome):
    result = drain(Hax().reader(), [{"type": "result", "outcome": outcome}])
    assert result is not None and not result.ok
    assert outcome in result.error


def test_a_claude_error_result_becomes_the_error_not_the_text():
    result = drain(
        Hax().reader(),
        [{"type": "result", "outcome": "error", "error": "rate limited"}],
    )
    assert result is not None and result.error == "rate limited"


def test_traced_events_name_the_tool(capsys):
    ClaudeCode().reader().event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
        },
        quiet=False,
    )
    Hax().reader().event({"kind": "tool_call", "tool_name": "bash"}, quiet=False)
    out = capsys.readouterr().out
    assert "> Bash" in out and "> bash" in out


def test_quiet_suppresses_the_trace_but_not_the_tally(capsys):
    reader = Hax().reader()
    reader.event({"kind": "tool_call", "tool_name": "bash"}, quiet=True)
    reader.event({"kind": "turn_usage", "usage": {"input": 7}}, quiet=True)
    assert capsys.readouterr().out == ""
    assert reader.tokens["input"] == 7
