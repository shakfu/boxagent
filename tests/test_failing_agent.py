"""An agent that goes wrong: a non-zero exit, a kill mid-stream, a hang.

`launch` spawns whatever argv it is given, so a Python one-liner stands in for
the container. Nothing is built and no engine is contacted.
"""

import json
import signal
import sys
import time
from types import SimpleNamespace

import pytest

from sanduk.agent import KEY_ENV, REPORT_NAME, Outcome, Reader, launch
from sanduk.agents.claude import ClaudeCode
from sanduk.cli import main
from sanduk.errors import AgentboxError
from sanduk.runs import runs_dir

TOOL_USE = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
}
KILL = "os.kill(os.getpid(), signal.SIGKILL)"


def result(text="done", is_error=False):
    """Claude Code's terminal record."""
    return {
        "type": "result",
        "result": text,
        "is_error": is_error,
        "num_turns": 1,
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


def agent(*lines, then=""):
    """A stand-in agent: print each line, a dict as one JSON record, then `then`."""
    body = [
        f"print({(line if isinstance(line, str) else json.dumps(line))!r}, flush=True)"
        for line in lines
    ]
    script = "\n".join(["import os, signal, sys, time", *body, then])
    return [sys.executable, "-c", script]


# --- launch -----------------------------------------------------------------


def test_a_nonzero_exit_with_no_result_is_returned_not_raised():
    outcome, rc = launch(ClaudeCode(), agent(TOOL_USE, then="sys.exit(3)"), 30, True)
    assert (outcome, rc) == (None, 3)


def test_an_agent_killed_mid_stream_keeps_what_it_had_traced(capsys):
    outcome, rc = launch(ClaudeCode(), agent(TOOL_USE, then=KILL), 30, False)
    assert outcome is None
    assert rc == -signal.SIGKILL
    assert "> Bash" in capsys.readouterr().out


def test_an_agent_that_hangs_without_printing_is_killed_at_the_timeout():
    """The deadline is a timer: a silent agent never returns to the read loop."""
    started = time.monotonic()
    with pytest.raises(AgentboxError) as e:
        launch(ClaudeCode(), agent(then="time.sleep(60)"), 0.5, True)
    assert e.value.code == 124
    assert time.monotonic() - started < 10


class Lines(Reader):
    def __init__(self):
        self.lines, self.records = [], []

    def event(self, record, quiet):
        self.records.append(record)

    def line(self, text, quiet):
        self.lines.append(text)

    def finish(self):
        return Outcome(ok=True)


def test_a_line_that_is_not_a_record_reaches_the_reader_as_text():
    """A stray line, or one that only looks like JSON, must not end the run."""
    seen = Lines()
    stand_in = agent("plain text", "{not json", {"type": "result"})
    launch(SimpleNamespace(reader=lambda: seen), stand_in, 30, True)
    assert seen.lines == ["plain text", "{not json"]
    assert seen.records == [{"type": "result"}]


# --- run --------------------------------------------------------------------


class Engine:
    """Runs the stand-in agent in place of a container, and records teardown."""

    def __init__(self, argv):
        self.argv, self.names, self.destroyed = argv, [], []

    def require(self):
        pass

    def image_exists(self, image):
        return True

    def run_argv(self, spec):
        self.names.append(spec.name)
        return self.argv

    def destroy(self, name, keep=False):
        self.destroyed.append(name)


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-ant-test")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "work"


def run_with(monkeypatch, work, argv, *flags):
    engine = Engine(argv)
    monkeypatch.setattr("sanduk.cli.get_runtime", lambda _: engine)
    base = ["run", "task", "-w", str(work), "--mode", "open", "--skip-key-check"]
    return main([*base, *flags]), engine


def test_a_failing_agent_exits_with_its_code_and_leaves_nothing(monkeypatch, work):
    code, engine = run_with(monkeypatch, work, agent(TOOL_USE, then="sys.exit(3)"))
    assert code == 3
    assert len(engine.names) == 1 and engine.destroyed == engine.names
    assert list(runs_dir().glob("*.json")) == []


def test_an_agent_that_reports_an_error_fails_the_run(monkeypatch, work, capsys):
    stand_in = agent(result("rate limited", is_error=True))
    code, engine = run_with(monkeypatch, work, stand_in)
    assert code == 1
    assert "rate limited" in capsys.readouterr().err
    assert engine.destroyed == engine.names


def test_a_hung_agent_exits_124_and_its_container_is_deleted(monkeypatch, work):
    """A timeout kill must not leave a container alive holding the key."""
    stand_in = agent(then="time.sleep(60)")
    code, engine = run_with(monkeypatch, work, stand_in, "--timeout", "1")
    assert code == 124
    assert len(engine.names) == 1 and engine.destroyed == engine.names
    assert list(runs_dir().glob("*.json")) == []


def test_a_timed_out_run_still_records_why(monkeypatch, work, tmp_path):
    stats = tmp_path / "stats.json"
    flags = ["--timeout", "1", "--stats-file", str(stats)]
    code, _ = run_with(monkeypatch, work, agent(then="time.sleep(60)"), *flags)
    assert code == 124
    found = json.loads(stats.read_text())
    assert found["exit"] == 124 and "--timeout" in found["error"]


def test_a_failed_run_still_records_its_cost_and_copies_its_report(
    monkeypatch, work, tmp_path
):
    """A failed wakeup still cost tokens, and its partial report is worth reading."""
    stats, copy = tmp_path / "stats.json", tmp_path / "copy.md"
    write = f"open({str(work / REPORT_NAME)!r}, 'w').write('partial')"
    stand_in = agent(result("rate limited", is_error=True), then=write)
    flags = ["--stats-file", str(stats), "--report", str(copy)]
    code, _ = run_with(monkeypatch, work, stand_in, *flags)
    assert code == 1
    assert copy.read_text() == "partial"
    found = json.loads(stats.read_text())
    assert found["exit"] == 1 and found["error"] == "rate limited"
    assert "10 in" in found["stats"]


def test_a_clean_exit_with_no_final_result_is_a_failure(monkeypatch, work, capsys):
    """An agent cut off mid-run has answered nothing, whatever its exit status."""
    code, _ = run_with(monkeypatch, work, agent(TOOL_USE))
    assert code == 1
    assert "without a final result" in capsys.readouterr().err


def test_a_killed_agent_exits_the_way_a_shell_reports_a_signal(monkeypatch, work):
    code, _ = run_with(monkeypatch, work, agent(TOOL_USE, then=KILL))
    assert code == 128 + signal.SIGKILL


def test_no_report_is_said_and_the_final_message_printed(monkeypatch, work, capsys):
    code, _ = run_with(monkeypatch, work, agent(result("the answer is 4")))
    out, err = capsys.readouterr()
    assert code == 0
    assert f"wrote no {REPORT_NAME}" in err
    assert "the answer is 4" in out
