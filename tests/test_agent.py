"""Agent handlers: the registry, the plugin path, and each shipped handler.

Nothing here starts a process. `launch` is exercised through the readers, which
are the only stateful part of a handler.
"""

import argparse
import json
from types import SimpleNamespace

import pytest

from sanduk.agent import Agent, Outcome, Reader, Wiring, agent_names, get_agent, registry
from sanduk.agents import BUILTIN
from sanduk.agents.claude import ClaudeCode
from sanduk.agents.codex import Codex
from sanduk.agents.hax import Hax
from sanduk.agents.opencode import OpenCode
from sanduk.agents.pi import Pi
from sanduk.errors import AgentboxError
from sanduk.providers import get_provider

RELAY = "http://10.0.0.1:9"


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
    assert set(agent_names()) >= {"claude", "codex", "hax", "opencode", "pi"}
    assert registry()["claude"] is ClaudeCode


@pytest.mark.parametrize("agent", BUILTIN)
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

    def argv(self, args, provider, task, wiring):
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


def argv_of(agent, args, provider, task="go"):
    """An agent's argv, with this run's wiring resolved the way cli.py does."""
    return agent.argv(args, provider, task, agent.wire(args, provider, RELAY))


def test_hax_selects_the_compatible_provider_matching_the_protocol():
    args = flags(agent="hax", model="qwen3", max_turns=3)
    assert argv_of(Hax(), args, get_provider("anthropic"))[:2] == [
        "--json",
        "--provider=anthropic-compatible",
    ]
    assert "--provider=openai-compatible" in argv_of(Hax(), args, get_provider("openai"))


def test_hax_takes_the_turn_cap_from_the_environment():
    """hax has no --max-turns flag; the setting has a HAX_ variable instead."""
    args = flags(agent="hax", max_turns=3)
    assert "3" not in " ".join(argv_of(Hax(), args, get_provider("openai")))
    assert Hax().wire(args, get_provider("openai"), None).env["HAX_MAX_TURNS"] == "3"


def test_the_task_is_the_last_hax_argument():
    argv = argv_of(Hax(), flags(agent="hax"), get_provider("openai"), "do the thing")
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


# --- codex ------------------------------------------------------------------


def test_codex_speaks_responses_and_nothing_else():
    """wire_api accepts only "responses". openai serves that route, and so does
    a current llama-server through openai-compat; the other two do not."""
    for name in ("openai", "openai-compat"):
        Codex().check(flags(agent="codex"), get_provider(name))
    for name in ("anthropic", "openrouter"):
        with pytest.raises(AgentboxError, match="cannot talk"):
            Codex().check(flags(agent="codex"), get_provider(name))


def test_codex_takes_its_endpoint_on_the_command_line():
    """There is no base-URL variable; the endpoint is a config key. This is why
    argv is handed the wiring."""
    argv = argv_of(Codex(), flags(agent="codex"), get_provider("openai"))
    assert 'model_providers.sanduk.base_url="http://10.0.0.1:9/v1"' in argv
    assert 'model_provider="sanduk"' in argv
    assert 'model_providers.sanduk.wire_api="responses"' in argv


def test_codex_names_the_key_variable_rather_than_the_key():
    """env_key is a variable name. The credential reaches the container under
    it, and never through this argv, which inspect can read."""
    agent, args = Codex(), flags(agent="codex")
    wiring = agent.wire(args, get_provider("openai"), RELAY)
    argv = agent.argv(args, get_provider("openai"), "go", wiring)
    assert f'model_providers.sanduk.env_key="{wiring.key_env}"' in argv
    assert wiring.key_env == "CODEX_RELAY_KEY"


def test_codex_disables_its_own_sandbox():
    """The container is the boundary; a sandbox inside it would only stop the
    agent doing the work it was given."""
    argv = argv_of(Codex(), flags(agent="codex"), get_provider("openai"))
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"


def test_codex_counts_cached_input_inside_input():
    outcome = drain(
        Codex().reader(),
        [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 2252,
                    "cached_input_tokens": 2192,
                    "output_tokens": 40,
                },
            },
        ],
    )
    assert outcome == Outcome(
        ok=True, text="done", stats="2,252 in (2,192 cached) / 40 out"
    )


def test_a_codex_command_traces_once(capsys):
    """codex reports one command as item.started and item.completed. Tracing
    both printed every command twice."""
    reader = Codex().reader()
    item = {"type": "command_execution", "command": "head -n 1 a.py"}
    for kind in ("item.started", "item.completed"):
        reader.event({"type": kind, "item": item}, quiet=False)
    assert capsys.readouterr().out.count("head -n 1 a.py") == 1


def test_an_item_level_codex_error_is_traced_and_not_fatal(capsys):
    """A missing model entry arrives as an error item, not turn.failed."""
    reader = Codex().reader()
    reader.event(
        {"type": "item.completed", "item": {"type": "error", "message": "no metadata"}},
        quiet=False,
    )
    reader.event({"type": "turn.completed", "usage": {}}, quiet=False)
    assert "! no metadata" in capsys.readouterr().out
    outcome = reader.finish()
    assert outcome is not None and outcome.ok


def test_a_failed_codex_turn_is_not_ok():
    outcome = drain(Codex().reader(), [{"type": "turn.failed", "error": "no credit"}])
    assert outcome is not None and not outcome.ok and outcome.error == "no credit"


# --- opencode ---------------------------------------------------------------


def test_opencode_reaches_every_provider():
    for name in ("anthropic", "openai", "openrouter", "openai-compat"):
        OpenCode().check(flags(agent="opencode", model="m"), get_provider(name))


def test_opencode_needs_a_model():
    """The config names one model and --model selects it; without a name there
    is nothing to put in either."""
    with pytest.raises(AgentboxError, match="--model is required"):
        OpenCode().check(flags(agent="opencode"), get_provider("openai"))


def test_opencode_config_travels_in_the_environment(monkeypatch):
    """Not a file: opencode.json in the workdir would sit in the user's
    repository and be editable by the agent that reads it."""
    wiring = OpenCode().wire(
        flags(agent="opencode", model="m"), get_provider("openai"), RELAY
    )
    config = json.loads(wiring.env["OPENCODE_CONFIG_CONTENT"])
    provider = config["provider"]["sanduk"]
    assert provider["options"]["baseURL"] == "http://10.0.0.1:9/v1"
    assert provider["models"] == {"m": {"name": "m"}}


def test_the_opencode_config_carries_no_credential():
    wiring = OpenCode().wire(
        flags(agent="opencode", model="m"), get_provider("openai"), RELAY
    )
    blob = wiring.env["OPENCODE_CONFIG_CONTENT"]
    assert f"{{env:{wiring.key_env}}}" in blob
    assert wiring.key_env == "OPENCODE_RELAY_KEY"


@pytest.mark.parametrize(
    ("provider", "npm"),
    [
        ("anthropic", "@ai-sdk/anthropic"),
        ("openai", "@ai-sdk/openai-compatible"),
        ("openrouter", "@ai-sdk/openai-compatible"),
        ("openai-compat", "@ai-sdk/openai-compatible"),
    ],
)
def test_the_driver_follows_the_wire_protocol(provider, npm):
    wiring = OpenCode().wire(
        flags(agent="opencode", model="m"), get_provider(provider), RELAY
    )
    config = json.loads(wiring.env["OPENCODE_CONFIG_CONTENT"])
    assert config["provider"]["sanduk"]["npm"] == npm


def test_opencode_selects_the_model_through_its_own_provider_id():
    argv = argv_of(
        OpenCode(), flags(agent="opencode", model="qwen3"), get_provider("openai")
    )
    assert argv[argv.index("--model") + 1] == "sanduk/qwen3"
    assert "--auto" in argv


def test_opencode_sums_the_per_step_token_counts():
    """Counts arrive per step, and part.tokens.input excludes the cache: one
    real step reported input 50 beside cache.read 7185."""
    outcome = drain(
        OpenCode().reader(),
        [
            {"type": "tool_use", "part": {"type": "tool", "tool": "write"}},
            {
                "type": "step_finish",
                "part": {
                    "tokens": {"input": 50, "output": 394, "cache": {"read": 7185}},
                    "cost": 0.0,
                },
            },
            {"type": "text", "part": {"type": "text", "text": "Done."}},
            {
                "type": "step_finish",
                "part": {
                    "tokens": {"input": 416, "output": 130, "cache": {"read": 7235}},
                    "cost": 0.0,
                },
            },
        ],
    )
    assert outcome == Outcome(
        ok=True, text="Done.", stats="14,886 in (14,420 cached) / 524 out, $0.0000"
    )


def test_no_step_means_no_outcome():
    assert drain(OpenCode().reader(), [{"type": "step_start", "part": {}}]) is None


# --- pi ---------------------------------------------------------------------


def test_pi_reaches_every_provider():
    for name in ("anthropic", "openai", "openrouter", "openai-compat"):
        Pi().check(flags(agent="pi", model="m"), get_provider(name))


def test_pi_needs_a_model():
    """The provider block lists the models pi may select and --model picks one
    out of it; neither has anything to hold without a name."""
    with pytest.raises(AgentboxError, match="--model is required"):
        Pi().check(flags(agent="pi"), get_provider("openai"))


def test_the_pi_config_travels_in_the_environment():
    """Not a file: models.json under the bind mount would sit in the user's
    repository and be editable by the agent that reads it."""
    wiring = Pi().wire(flags(agent="pi", model="m"), get_provider("openai"), RELAY)
    config = json.loads(wiring.env["SANDUK_PI_MODELS"])
    provider = config["providers"]["sanduk"]
    assert provider["baseUrl"] == "http://10.0.0.1:9/v1"
    assert provider["models"] == [{"id": "m"}]


def test_the_pi_config_carries_no_credential():
    wiring = Pi().wire(flags(agent="pi", model="m"), get_provider("openai"), RELAY)
    assert f"${wiring.key_env}" in wiring.env["SANDUK_PI_MODELS"]
    assert wiring.key_env == "PI_RELAY_KEY"


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("anthropic", "anthropic-messages"),
        ("openai", "openai-completions"),
        ("openrouter", "openai-completions"),
        ("openai-compat", "openai-completions"),
    ],
)
def test_pi_names_the_api_its_provider_speaks(provider, expected):
    """Chat Completions wins where a provider serves it and Responses both: it
    is the route every OpenAI-shaped provider here has."""
    wiring = Pi().wire(flags(agent="pi", model="m"), get_provider(provider), RELAY)
    config = json.loads(wiring.env["SANDUK_PI_MODELS"])
    assert config["providers"]["sanduk"]["api"] == expected


def test_pi_gets_the_bare_root_for_anthropic():
    """Measured: pi appends /v1/messages itself, so a base ending in /v1 sent it
    to /v1/v1/messages and the relay refused the path."""
    wiring = Pi().wire(flags(agent="pi", model="m"), get_provider("anthropic"), RELAY)
    config = json.loads(wiring.env["SANDUK_PI_MODELS"])
    assert config["providers"]["sanduk"]["baseUrl"] == RELAY


def test_pi_selects_the_model_through_its_own_provider_id():
    argv = argv_of(Pi(), flags(agent="pi", model="qwen3"), get_provider("openai"))
    assert "sanduk/qwen3" in argv


def test_a_pi_task_survives_a_leading_dash():
    """The task is a positional, so it goes after --."""
    argv = argv_of(Pi(), flags(agent="pi", model="m"), get_provider("openai"), "--help")
    assert argv[-2:] == ["--", "--help"]


def test_pi_counts_the_cache_inside_input():
    """`input` excludes the cache: input 19 beside cacheRead 1754 on one
    message. The run's input is the sum of the three."""
    outcome = drain(
        Pi().reader(),
        [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {
                        "input": 19,
                        "output": 131,
                        "cacheRead": 1754,
                        "cacheWrite": 0,
                        "cost": {"total": 0.25},
                    },
                },
            },
            {"type": "agent_end", "willRetry": False},
        ],
    )
    assert outcome is not None and outcome.ok
    assert outcome.text == "done"
    assert outcome.stats == "1,773 in (1,754 cached) / 131 out, $0.2500"


def test_pi_reads_the_assistants_text_and_no_one_elses():
    """The user message and every tool result arrive as message_end too."""
    reader = Pi().reader()
    for role, text in (("user", "the task"), ("toolResult", "file contents")):
        reader.event(
            {
                "type": "message_end",
                "message": {"role": role, "content": [{"type": "text", "text": text}]},
            },
            quiet=True,
        )
    reader.event({"type": "agent_end", "willRetry": False}, quiet=True)
    outcome = reader.finish()
    assert outcome is not None and outcome.text == ""


def test_a_pi_retry_is_not_the_end_of_the_run():
    """pi ends an agent per failed attempt and retries up to three times."""
    assert drain(Pi().reader(), [{"type": "agent_end", "willRetry": True}]) is None


def test_a_pi_provider_error_is_not_ok():
    outcome = drain(
        Pi().reader(),
        [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "API key is invalid.",
                },
            },
            {"type": "agent_settled"},
        ],
    )
    assert outcome is not None and not outcome.ok
    assert outcome.error == "API key is invalid."


def test_a_pi_retry_that_lands_clears_the_attempt_that_did_not():
    outcome = drain(
        Pi().reader(),
        [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "Connection error.",
                },
            },
            {"type": "agent_end", "willRetry": True},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                    "usage": {"input": 5, "output": 2},
                },
            },
            {"type": "agent_end", "willRetry": False},
        ],
    )
    assert outcome is not None and outcome.ok and outcome.text == "hello"
