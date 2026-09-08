"""CLI tests: argument rules, the report instruction, teardown of stale state.

`main` returns an exit code rather than raising, so these assert on the code.
Every run here is a --dry-run: nothing is built and nothing is started.
"""

import pytest

from sanduk.agent import KEY_ENV, REPORT_NAME
from sanduk.cli import main
from sanduk.errors import AgentboxError

KEY = "sk-ant-api03-SECRET"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)


def test_task_and_task_file_are_mutually_exclusive(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing")
    assert main(["task", "--task-file", str(brief)]) == 2


def test_a_task_is_required():
    assert main([]) == 2


def test_an_empty_task_is_rejected(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("   \n")
    assert main(["--task-file", str(brief)]) == 2


def test_missing_key_exits_before_anything_starts(monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert main(["task"]) == 2


def test_unknown_runtime_is_rejected_by_argparse():
    with pytest.raises(SystemExit):
        main(["task", "--runtime", "nope"])


def test_report_instruction_is_appended(tmp_path, capsys):
    main(["summarise", "-w", str(tmp_path), "--dry-run"])
    assert REPORT_NAME in capsys.readouterr().out


def test_no_report_instruction_flag_suppresses_it(tmp_path, capsys):
    main(["summarise", "-w", str(tmp_path), "--dry-run", "--no-report-instruction"])
    assert REPORT_NAME not in capsys.readouterr().out


def test_dry_run_does_not_write_a_task_file(tmp_path):
    """The prompt goes in via -p; a copy on the mount only confused the agent."""
    main(["summarise", "-w", str(tmp_path), "--dry-run"])
    assert list(tmp_path.iterdir()) == []


def test_stale_report_is_removed_before_a_run(tmp_path):
    stale = tmp_path / REPORT_NAME
    stale.write_text("from a previous run")
    main(["summarise", "-w", str(tmp_path), "--dry-run"])
    assert not stale.exists()


def test_boxagent_error_carries_its_own_exit_code():
    assert AgentboxError("timed out", code=124).code == 124


# --- provider selection and key isolation -----------------------------------
#
# Three keys are exported at once on the author's machine, so "which key does a
# run read" and "which one reaches the container" stop being the same question.

ALL_KEYS = {
    "ANTHROPIC_API_KEY": "sk-ant-SECRET",
    "OPENAI_API_KEY": "sk-openai-SECRET",
    "OPENROUTER_API_KEY": "sk-or-SECRET",
}


@pytest.fixture
def all_keys(monkeypatch):
    for name, value in ALL_KEYS.items():
        monkeypatch.setenv(name, value)
    return ALL_KEYS


@pytest.mark.parametrize(
    ("agent", "provider", "expected"),
    [
        ("claude", "anthropic", "ANTHROPIC_API_KEY"),
        ("hax", "openai", "OPENAI_API_KEY"),
        ("hax", "openrouter", "OPENROUTER_API_KEY"),
    ],
)
def test_the_provider_decides_which_key_is_read(
    monkeypatch, agent, provider, expected, capsys
):
    """With the other two still exported, removing the provider's own key must
    fail. Falling back to whichever key happens to be set would send the wrong
    credential to the wrong API."""
    for name, value in ALL_KEYS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(expected)
    assert main(["task", "--agent", agent, "--provider", provider]) == 2
    assert expected in capsys.readouterr().err


def test_openai_compat_runs_without_any_key(monkeypatch, tmp_path, capsys):
    """A local llama-server has no credential; requiring one would block it."""
    for name in ALL_KEYS:
        monkeypatch.delenv(name, raising=False)
    code = main(
        [
            "task",
            "-w",
            str(tmp_path),
            "--dry-run",
            "--agent",
            "hax",
            "--provider",
            "openai-compat",
            "--upstream",
            "http://127.0.0.1:8080",
        ]
    )
    assert code == 0, capsys.readouterr().err


def spec_for(flags, tmp_path):
    from sanduk.cli import build_spec, parse_args, relay_root, select

    args = parse_args(flags)
    sel = select(args)
    wiring = sel.agent.wire(args, sel.provider, relay_root(args))
    return build_spec(args, sel, wiring, "sanduk-test", tmp_path, "task")


def test_the_container_inherits_only_the_agent_variables(all_keys, tmp_path):
    """The other two keys stay on the host. The container's environment is the
    inherit list and nothing else. hax reads its own HAX_-prefixed names, which
    is what --agent-key-env exists to override."""
    spec = spec_for(
        [
            "task",
            "-w",
            str(tmp_path),
            "--proxy",
            "--agent",
            "hax",
            "--provider",
            "openai",
        ],
        tmp_path,
    )
    assert spec.inherit_env == ["HAX_OPENAI_API_KEY", "HAX_OPENAI_BASE_URL"]
    for name in ALL_KEYS:
        assert name not in spec.inherit_env


def test_no_key_value_appears_in_the_container_argv(all_keys, tmp_path):
    """The bare-name -e form exists so values stay out of argv and out of ps."""
    from sanduk.runtime import get_runtime

    spec = spec_for(
        [
            "task",
            "-w",
            str(tmp_path),
            "--proxy",
            "--agent",
            "hax",
            "--provider",
            "openai",
        ],
        tmp_path,
    )
    rendered = " ".join(get_runtime().run_argv(spec))
    for value in all_keys.values():
        assert value not in rendered


def test_agent_env_names_can_be_overridden(tmp_path, monkeypatch):
    """An agent that reads a different variable than the provider declares is
    pointed at the relay with a flag, not a new module."""
    from sanduk.cli import container_env_names, parse_args, resolve_provider

    args = parse_args(
        [
            "task",
            "--agent",
            "hax",
            "--provider",
            "openrouter",
            "--agent-key-env",
            "MY_TOKEN",
            "--agent-base-url-env",
            "MY_BASE_URL",
        ]
    )
    names = container_env_names(args, resolve_provider(args))
    assert names == ("MY_TOKEN", "MY_BASE_URL")


def test_agent_env_names_default_to_the_provider(tmp_path):
    from sanduk.cli import container_env_names, parse_args, resolve_provider

    args = parse_args(["task", "--provider", "anthropic"])
    names = container_env_names(args, resolve_provider(args))
    assert names == ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
