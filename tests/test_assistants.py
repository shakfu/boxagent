"""Assistants: config, the state a run outlives, and one wakeup.

Nothing here starts a container. `wake` is exercised with `sanduk.cli.main`
replaced, which is the seam the whole module is built on: an assistant can do
nothing a typed `sanduk run` cannot.
"""

import json
import os
import signal
import sqlite3

import pytest

from sanduk import assistants
from sanduk.errors import AgentboxError
from sanduk.util import seconds

CONFIG = """
name = "triage"
agent = "pi"
provider = "openai-compat"
model = "local-model"
every = "30m"
brief = "brief.md"
"""


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def home(state):
    """An assistant directory with a config and a brief."""
    d = state / "triage"
    d.mkdir()
    (d / "assistant.toml").write_text(CONFIG)
    (d / "brief.md").write_text("Triage the inbox.")
    return d


@pytest.fixture
def db(state):
    return assistants.connect()


@pytest.fixture
def registered(db, home):
    assistants.register(db, assistants.load(home))
    return assistants.load(home)


def runs_of(db):
    return list(db.execute("SELECT * FROM runs ORDER BY id"))


# --- config -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("45s", 45), ("30m", 1800), ("2h", 7200), ("1d", 86400), ("900", 900), (900, 900)],
)
def test_a_duration_is_seconds_unless_it_carries_a_unit(text, expected):
    assert seconds(text) == expected


@pytest.mark.parametrize("text", ["", "m", "0m", "-5m", "0", "1w", "half an hour"])
def test_anything_else_is_refused_by_name(text):
    with pytest.raises(AgentboxError, match="duration"):
        seconds(text)


def test_a_timeout_reads_the_same_way_as_an_interval(home):
    """One config file with `every = "30m"` beside `timeout = 900` said
    nothing about which key meant what; now both take either spelling."""
    (home / "assistant.toml").write_text(CONFIG + '\ntimeout = "5m"\n')
    assert assistants.load(home).timeout == 300


def test_a_config_is_read_with_the_directory_as_its_root(home):
    found = assistants.load(home)
    assert found.name == "triage"
    assert found.agent == "pi" and found.model == "local-model"
    assert found.every == 1800
    assert found.brief == home / "brief.md"
    assert found.workspace == home / "workspace"
    # Not stated in the config, so the defaults that matter for an unattended
    # run: the strictest mode, and a bounded wakeup.
    assert found.mode == "sealed" and found.timeout == 900


def test_a_missing_config_says_where_it_looked(state):
    with pytest.raises(AgentboxError, match=r"assistant\.toml"):
        assistants.load(state)


def test_an_unknown_key_is_refused_rather_than_ignored(home):
    """A typo in a schedule is a wakeup that never happens."""
    (home / "assistant.toml").write_text(CONFIG + '\nevry = "5m"\n')
    with pytest.raises(AgentboxError, match="unknown keys evry"):
        assistants.load(home)


def test_a_brief_that_is_not_there_fails_at_load(home):
    (home / "brief.md").unlink()
    with pytest.raises(AgentboxError, match="brief"):
        assistants.load(home)


def test_opening_the_container_up_says_so(home, capsys):
    (home / "assistant.toml").write_text(CONFIG + '\nmode = "open"\n')
    assert assistants.load(home).mode == "open"
    assert "holds the key" in capsys.readouterr().err


def test_the_boolean_mode_replaced_still_reads(home, capsys):
    """`proxy = false` was the old spelling of `mode = "open"`."""
    (home / "assistant.toml").write_text(CONFIG + "\nproxy = false\n")
    assert assistants.load(home).mode == "open"
    assert "`proxy` is now `mode`" in capsys.readouterr().err


def test_a_mode_that_does_not_exist_names_the_ones_that_do(home):
    (home / "assistant.toml").write_text(CONFIG + '\nmode = "airgapped"\n')
    with pytest.raises(AgentboxError, match="key-safe"):
        assistants.load(home)


def test_a_key_safe_assistant_passes_the_mode_through(db, home, ran):
    calls, _ = ran
    (home / "assistant.toml").write_text(CONFIG + '\nmode = "key-safe"\n')
    found = assistants.load(home)
    assistants.register(db, found)
    assistants.wake(db, found)
    argv = calls[0]
    assert argv[argv.index("--mode") + 1] == "key-safe"


# --- state ------------------------------------------------------------------


def test_registering_twice_keeps_the_schedule(db, home):
    found = assistants.load(home)
    assistants.register(db, found)
    db.execute("UPDATE assistants SET next_due_at = 4102444800 WHERE name = 'triage'")
    assistants.register(db, found)
    assert assistants.row(db, "triage")["next_due_at"] == 4102444800


def test_an_unknown_assistant_names_the_command_that_lists_them(db):
    with pytest.raises(AgentboxError, match="assistant list"):
        assistants.row(db, "nope")


def test_a_claim_is_refused_while_another_live_process_holds_it(db, registered):
    # pid 1 is alive and is not us, which is what the claim is checking for.
    db.execute("UPDATE assistants SET claimed_by = 1 WHERE name = 'triage'")
    assert assistants.take(db, "triage") is False


def test_a_claim_left_by_a_dead_process_is_taken(db, registered):
    db.execute("UPDATE assistants SET claimed_by = ? WHERE name = 'triage'", (2**31 - 1,))
    assert assistants.take(db, "triage") is True
    assert assistants.row(db, "triage")["claimed_by"] == os.getpid()
    assistants.release(db, "triage")
    assert assistants.row(db, "triage")["claimed_by"] is None


def test_only_a_due_and_enabled_assistant_is_due(db, registered):
    assert [r["name"] for r in assistants.due(db)] == ["triage"]
    db.execute("UPDATE assistants SET next_due_at = 4102444800 WHERE name = 'triage'")
    assert assistants.due(db) == []
    # --name asks for one whether or not it is due; disabled still means no.
    assert [r["name"] for r in assistants.due(db, "triage")] == ["triage"]
    assistants.set_disabled(db, "triage", True)
    assert assistants.due(db, "triage") == []


def test_a_message_waits_until_it_is_consumed(db, registered):
    assistants.tell(db, "triage", "look at PR 12")
    waiting = assistants.pending(db, "triage")
    assert [r["body"] for r in waiting] == ["look at PR 12"]
    assistants.consume(db, [int(waiting[0]["id"])])
    assert assistants.pending(db, "triage") == []


def test_a_failure_backs_off_and_the_third_one_disables(db, registered):
    for expected in (1, 2):
        assistants.schedule_next(db, registered, ok=False)
        assert assistants.row(db, "triage")["failures"] == expected
        assert not assistants.row(db, "triage")["disabled"]
    assistants.schedule_next(db, registered, ok=False)
    assert assistants.row(db, "triage")["disabled"] == 1


def test_a_success_clears_the_failures_and_schedules_the_interval(db, registered):
    assistants.schedule_next(db, registered, ok=False)
    before = assistants.now()
    assistants.schedule_next(db, registered, ok=True)
    found = assistants.row(db, "triage")
    assert found["failures"] == 0
    assert before + registered.every <= found["next_due_at"] <= assistants.now() + 1800


# --- one wakeup -------------------------------------------------------------


@pytest.fixture
def ran(monkeypatch):
    """Replace the run command; keep what it was asked to do."""
    calls: list[list[str]] = []
    code = {"value": 0}

    def fake(argv):
        calls.append(list(argv))
        report = argv[argv.index("-o") + 1]
        stats = argv[argv.index("--stats-file") + 1]
        if code["value"] == 0:
            with open(report, "w") as f:
                f.write("the report")
        with open(stats, "w") as f:
            json.dump({"exit": code["value"], "stats": "900 in / 40 out"}, f)
        return code["value"]

    monkeypatch.setattr("sanduk.cli.main", fake)
    return calls, code


def test_a_wakeup_runs_the_run_command_and_nothing_else(db, registered, ran):
    calls, _ = ran
    assert assistants.wake(db, registered) == 0
    argv = calls[0]
    assert argv[0] == "run"
    assert argv[argv.index("--mode") + 1] == "sealed"
    assert argv[argv.index("--agent") + 1] == "pi"
    assert argv[argv.index("--model") + 1] == "local-model"
    assert argv[argv.index("-w") + 1] == str(registered.workspace)


def test_a_wakeup_keeps_its_task_and_its_report(db, registered, ran):
    assistants.tell(db, "triage", "look at PR 12")
    assistants.wake(db, registered)
    reports = sorted(p.name for p in registered.reports.iterdir())
    assert len(reports) == 2 and reports[0].endswith(".md")
    task = next(registered.reports.glob("*.task.md")).read_text()
    assert "Triage the inbox." in task and "look at PR 12" in task


def test_a_wakeup_records_its_run_and_its_result(db, registered, ran):
    assistants.wake(db, registered)
    recorded = runs_of(db)
    assert len(recorded) == 1 and recorded[0]["exit_code"] == 0
    # What it cost, which `run` prints and does not return.
    assert recorded[0]["stats"] == "900 in / 40 out"
    assert [r["body"] for r in assistants.outbox(db)] == ["the report"]


def test_a_failed_wakeup_leaves_its_messages_for_the_next_one(db, registered, ran):
    """It has not answered them. Consuming on failure loses the request."""
    _, code = ran
    code["value"] = 1
    assistants.tell(db, "triage", "look at PR 12")
    assert assistants.wake(db, registered) == 1
    assert len(assistants.pending(db, "triage")) == 1
    assert assistants.row(db, "triage")["failures"] == 1
    assert assistants.outbox(db)[0]["body"] == "(no report, exit 1)"


def test_a_wakeup_with_nothing_to_do_says_how_to_give_it_something(db, home, ran):
    (home / "assistant.toml").write_text(CONFIG.replace('brief = "brief.md"', ""))
    found = assistants.load(home)
    assistants.register(db, found)
    with pytest.raises(AgentboxError, match="sanduk tell"):
        assistants.wake(db, found)


def gate(home, script):
    path = home / "gate.sh"
    path.write_text(script)
    path.chmod(0o755)
    (home / "assistant.toml").write_text(CONFIG + '\ngate = "gate.sh"\n')
    return assistants.load(home)


def test_a_gate_that_says_no_costs_no_tokens(db, home, ran):
    calls, _ = ran
    found = gate(home, "#!/bin/sh\necho nothing new\nexit 1\n")
    assistants.register(db, found)
    assert assistants.wake(db, found) == 0
    assert calls == []
    # Skipped is not failed: the schedule moves on by one interval.
    assert assistants.row(db, "triage")["failures"] == 0


def test_a_gate_that_says_yes_is_out_of_the_way(db, home, ran):
    calls, _ = ran
    found = gate(home, "#!/bin/sh\nexit 0\n")
    assistants.register(db, found)
    assert assistants.wake(db, found) == 0
    assert len(calls) == 1


def test_a_gate_that_cannot_run_is_an_error_not_a_skip(db, home, ran):
    found = gate(home, "#!/bin/sh\nexit 0\n")
    (home / "gate.sh").chmod(0o644)
    assistants.register(db, found)
    with pytest.raises(AgentboxError, match="not executable"):
        assistants.wake(db, found)


# --- tick and delivery ------------------------------------------------------


def test_tick_wakes_what_is_due(db, registered, ran):
    calls, _ = ran
    assert assistants.tick(db) == 0
    assert len(calls) == 1
    # The interval has not passed, so a second tick does nothing.
    assert assistants.tick(db) == 0
    assert len(calls) == 1


def test_tick_leaves_an_assistant_another_process_is_running(db, registered, ran, capsys):
    calls, _ = ran
    db.execute("UPDATE assistants SET claimed_by = 1 WHERE name = 'triage'")
    assert assistants.tick(db) == 0
    assert calls == []
    assert "another process" in capsys.readouterr().err


def test_tick_releases_the_claim_when_a_wakeup_fails(db, registered, monkeypatch):
    def boom(argv):
        raise AgentboxError("the engine is not running")

    monkeypatch.setattr("sanduk.cli.main", boom)
    with pytest.raises(AgentboxError):
        assistants.tick(db)
    assert assistants.row(db, "triage")["claimed_by"] is None


def test_delivery_pipes_each_entry_and_marks_it(db, registered, ran, tmp_path):
    assistants.wake(db, registered)
    sink = tmp_path / "delivered.txt"
    assert assistants.deliver(db, f"cat >> {sink}") == 1
    assert sink.read_text() == "the report"
    assert assistants.outbox(db, undelivered=True) == []


def test_a_failed_delivery_stops_and_keeps_the_rest(db, registered, ran):
    assistants.wake(db, registered)
    assert assistants.deliver(db, "exit 3") == 0
    assert len(assistants.outbox(db, undelivered=True)) == 1


def test_the_database_lives_under_the_state_directory(state):
    assert assistants.db_path().parent == state / "state" / "sanduk"


def test_a_second_connection_sees_the_same_schema(db, registered):
    other = sqlite3.connect(assistants.db_path())
    other.row_factory = sqlite3.Row
    assert [r["name"] for r in other.execute("SELECT name FROM assistants")] == ["triage"]


# --- serve ------------------------------------------------------------------


def test_the_nap_is_the_time_until_the_next_wakeup(db, registered):
    """Capped by the interval, so an assistant registered mid-sleep waits an
    interval at worst rather than until whatever was due first."""
    assert assistants.nap(db, 60) == 1.0
    db.execute(
        "UPDATE assistants SET next_due_at = ? WHERE name = 'triage'",
        (assistants.now() + 10,),
    )
    assert assistants.nap(db, 60) == 10.0
    db.execute(
        "UPDATE assistants SET next_due_at = ? WHERE name = 'triage'",
        (assistants.now() + 9999,),
    )
    assert assistants.nap(db, 60) == 60.0


def test_the_nap_with_nothing_registered_is_the_interval(db):
    assert assistants.nap(db, 30) == 30.0


def test_a_disabled_assistant_does_not_shorten_the_nap(db, registered):
    assistants.set_disabled(db, "triage", True)
    assert assistants.nap(db, 45) == 45.0


def test_serve_ticks_until_a_signal_says_stop(db, registered, monkeypatch):
    """SIGTERM between wakeups: the pass in flight finishes, then the loop
    stops. The handler is this loop's, not the one `run` installs."""
    passes = []

    def one_pass(db_, name=None, runtime=None):
        passes.append(name)
        os.kill(os.getpid(), signal.SIGTERM)
        return 0

    monkeypatch.setattr(assistants, "tick", one_pass)
    assert assistants.serve(db, interval=60) == 0
    assert len(passes) == 1


def test_serve_stops_when_a_wakeup_was_torn_down(db, registered, monkeypatch):
    """`run` installs its own handlers while it holds a container, so a signal
    during a wakeup reaches this loop as an exit code."""
    passes = []
    monkeypatch.setattr(
        assistants,
        "tick",
        lambda db_, name=None, runtime=None: passes.append(1) or assistants.INTERRUPTED,
    )
    assert assistants.serve(db, interval=60) == 0
    assert len(passes) == 1


def test_serve_leaves_the_handlers_it_found(db, monkeypatch):
    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(
        assistants, "tick", lambda db_, name=None, runtime=None: assistants.INTERRUPTED
    )
    assistants.serve(db, interval=60)
    assert signal.getsignal(signal.SIGTERM) is before


def test_an_interrupted_wakeup_is_not_a_failure(db, registered, monkeypatch):
    """Ctrl-C three times should not disable an assistant that works."""
    monkeypatch.setattr("sanduk.cli.main", lambda argv: assistants.INTERRUPTED)
    assert assistants.wake(db, registered) == assistants.INTERRUPTED
    found = assistants.row(db, "triage")
    assert found["failures"] == 0 and not found["disabled"]
    # It did not answer them, so the messages are still there.
    assistants.tell(db, "triage", "look at PR 12")
    assistants.wake(db, registered)
    assert len(assistants.pending(db, "triage")) == 1


def test_a_run_that_recorded_no_stats_leaves_the_column_empty(
    db, registered, monkeypatch
):
    """A run that fails before the agent starts writes no stats file."""
    monkeypatch.setattr("sanduk.cli.main", lambda argv: 2)
    assistants.wake(db, registered)
    assert runs_of(db)[0]["stats"] is None


def failing_with(error):
    """A run command that fails the way `run` does: a stats file, then a code."""

    def fake(argv):
        stats = argv[argv.index("--stats-file") + 1]
        with open(stats, "w") as f:
            json.dump({"exit": 124, "stats": "", "error": error}, f)
        return 124

    return fake


def test_a_wakeup_records_why_it_failed(db, registered, monkeypatch):
    """The exit code alone cannot tell a timeout from the agent's own error."""
    why = "agent exceeded --timeout 120s"
    monkeypatch.setattr("sanduk.cli.main", failing_with(why))
    assistants.wake(db, registered)
    assert runs_of(db)[0]["error"] == why
    assert assistants.outbox(db)[0]["body"] == f"(no report, exit 124: {why})"


def test_a_wakeup_that_worked_records_no_error(db, registered, ran):
    assistants.wake(db, registered)
    assert runs_of(db)[0]["error"] is None


def test_a_database_written_before_stats_existed_gains_the_columns(state):
    old = assistants.connect()
    old.execute("DROP TABLE runs")
    old.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "started_at INTEGER NOT NULL, ended_at INTEGER, exit_code INTEGER, "
        "report_path TEXT)"
    )
    old.execute("INSERT INTO runs (name, started_at) VALUES ('triage', 1)")
    old.close()
    db = assistants.connect()
    have = {r["name"] for r in db.execute("PRAGMA table_info(runs)")}
    assert have >= {"stats", "error"}
    # The history survived the column.
    assert [r["name"] for r in db.execute("SELECT name FROM runs")] == ["triage"]


# --- mounts and approvals ---------------------------------------------------


def test_a_configured_mount_is_anchored_to_the_assistant(home, db, ran):
    """`../repo:/repo:ro` in a config means beside the config, not beside
    whatever directory the scheduler happened to run from."""
    calls, _ = ran
    (home.parent / "repo").mkdir()
    (home / "assistant.toml").write_text(CONFIG + '\nmounts = ["../repo:/repo:ro"]\n')
    found = assistants.load(home)
    assert found.mounts == [f"{home.parent / 'repo'}:/repo:ro"]
    assistants.register(db, found)
    assistants.wake(db, found)
    argv = calls[0]
    assert argv[argv.index("--mount") + 1] == f"{home.parent / 'repo'}:/repo:ro"


def test_an_absolute_mount_is_left_alone(home):
    (home / "assistant.toml").write_text(CONFIG + '\nmounts = ["/srv/data:/data"]\n')
    assert assistants.load(home).mounts == ["/srv/data:/data"]


def test_without_approval_a_result_is_deliverable_at_once(db, registered, ran):
    assistants.wake(db, registered)
    assert assistants.state_of(assistants.outbox(db)[0]) == "approved"


def approving(home, db):
    (home / "assistant.toml").write_text(CONFIG + "\napproval = true\n")
    found = assistants.load(home)
    assistants.register(db, found)
    return found


def test_with_approval_a_result_waits(db, home, ran, tmp_path):
    """Delivery is the only thing that leaves the box, so it is what a person
    gets to hold. What the agent did, it did inside a deleted container."""
    found = approving(home, db)
    assistants.wake(db, found)
    entry = assistants.outbox(db)[0]
    assert assistants.state_of(entry) == "pending"
    sink = tmp_path / "sent.txt"
    assert assistants.deliver(db, f"cat >> {sink}") == 0
    assert not sink.exists()

    assert assistants.decide(db, [int(entry["id"])], approve=True) == 1
    assert assistants.deliver(db, f"cat >> {sink}") == 1
    assert sink.read_text() == "the report"


def test_a_rejected_result_is_never_delivered(db, home, ran, tmp_path):
    found = approving(home, db)
    assistants.wake(db, found)
    entry = assistants.outbox(db)[0]
    assistants.decide(db, [int(entry["id"])], approve=False)
    assert assistants.state_of(assistants.outbox(db)[0]) == "rejected"
    assert assistants.deliver(db, f"cat >> {tmp_path / 'sent.txt'}") == 0
    # A rejection is not undone by approving afterwards.
    assert assistants.decide(db, [int(entry["id"])], approve=True) == 1
    assert assistants.state_of(assistants.outbox(db)[0]) == "rejected"


def test_a_decision_already_made_is_not_made_twice(db, home, ran):
    found = approving(home, db)
    assistants.wake(db, found)
    entry = int(assistants.outbox(db)[0]["id"])
    assert assistants.decide(db, [entry], approve=True) == 1
    assert assistants.decide(db, [entry], approve=True) == 0


def test_delivery_says_what_is_waiting(db, home, ran, capsys, tmp_path):
    found = approving(home, db)
    assistants.wake(db, found)
    assistants.deliver(db, f"cat >> {tmp_path / 'sent.txt'}")
    assert "waiting for approval" in capsys.readouterr().err


def test_an_outbox_written_before_approvals_stays_deliverable(state):
    """Rows from before the columns were delivered on sight; adding a gate
    should not retroactively hold them."""
    old = assistants.connect()
    old.execute("DROP TABLE outbox")
    old.execute(
        "CREATE TABLE outbox (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "run_id INTEGER NOT NULL, created_at INTEGER NOT NULL, body TEXT NOT NULL, "
        "delivered_at INTEGER)"
    )
    old.execute(
        "INSERT INTO outbox (name, run_id, created_at, body) VALUES ('t', 1, 7, 'x')"
    )
    old.close()
    db = assistants.connect()
    entry = assistants.outbox(db)[0]
    assert assistants.state_of(entry) == "approved"
    assert entry["approved_at"] == 7
