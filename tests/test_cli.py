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
