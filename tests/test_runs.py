"""Run records: who owns a container, and what happens when that owner dies.

A killed run cannot delete anything, so the next run does it. Nothing here
starts a container; the engine is a stub that records what it was asked to do.
"""

import json
import subprocess

import pytest

from sanduk.errors import AgentboxError
from sanduk.runs import claim, owner_alive, runs_dir, sweep
from sanduk.runtime import Container


class StubEngine:
    def __init__(self, containers=(), reachable=True):
        self.containers = list(containers)
        self.reachable = reachable
        self.destroyed = []

    def list_containers(self, prefix=""):
        if not self.reachable:
            raise AgentboxError("engine is not running")
        return [
            Container(name=n, image="sanduk:latest", state="running")
            for n in self.containers
            if n.startswith(prefix)
        ]

    def destroy(self, name, keep=False):
        self.destroyed.append(name)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def engine(monkeypatch):
    stub = StubEngine(["sanduk-abcd", "sanduk-hold-ef"])
    monkeypatch.setattr("sanduk.runs.get_runtime", lambda _: stub)
    return stub


def dead_pid() -> int:
    """A pid that has exited and been reaped."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def rewrite(name: str, **fields) -> None:
    path = runs_dir() / f"{name}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))


def test_a_record_names_its_owner_and_every_container_it_starts(state):
    run = claim("apple", "sanduk-abcd")
    run.add("sanduk-hold-ef")
    record = json.loads((runs_dir() / "sanduk-abcd.json").read_text())
    assert record["containers"] == ["sanduk-abcd", "sanduk-hold-ef"]
    assert record["runtime"] == "apple"
    assert owner_alive(record["pid"])


def test_a_live_owner_is_left_alone(state, engine):
    claim("apple", "sanduk-abcd")
    assert sweep() == []
    assert engine.destroyed == []


def test_a_dead_owners_containers_are_deleted(state, engine):
    """The holder counts. A killed --proxy run leaks both."""
    run = claim("apple", "sanduk-abcd")
    run.add("sanduk-hold-ef")
    rewrite("sanduk-abcd", pid=dead_pid())
    assert sweep() == ["sanduk-abcd", "sanduk-hold-ef"]
    assert engine.destroyed == ["sanduk-abcd", "sanduk-hold-ef"]
    assert not list(runs_dir().glob("*.json"))


def test_a_released_record_is_never_swept(state, engine):
    """What --keep does: the container stays, and the next run leaves it."""
    run = claim("apple", "sanduk-abcd")
    run.release()
    assert sweep() == []
    assert engine.destroyed == []


def test_a_container_the_engine_no_longer_has_is_not_deleted(state, engine):
    """A run killed after its own teardown leaves a record and no container.
    Deleting by name anyway would report a failure for each one."""
    engine.containers = []
    claim("apple", "sanduk-abcd")
    rewrite("sanduk-abcd", pid=dead_pid())
    assert sweep() == []
    assert engine.destroyed == []
    assert not list(runs_dir().glob("*.json"))


def test_an_unreachable_engine_keeps_the_record(state, monkeypatch):
    """The containers are still there. Dropping the record would orphan them
    for good; the engine may be back on the next run."""
    monkeypatch.setattr("sanduk.runs.get_runtime", lambda _: StubEngine(reachable=False))
    claim("apple", "sanduk-abcd")
    rewrite("sanduk-abcd", pid=dead_pid())
    assert sweep() == []
    assert (runs_dir() / "sanduk-abcd.json").exists()


def test_a_record_naming_an_unknown_engine_is_left_alone(state):
    claim("nosuch", "sanduk-abcd")
    rewrite("sanduk-abcd", pid=dead_pid())
    assert sweep() == []
    assert (runs_dir() / "sanduk-abcd.json").exists()


def test_an_unreadable_record_is_dropped(state, engine):
    claim("apple", "sanduk-abcd")
    (runs_dir() / "sanduk-abcd.json").write_text("{not json")
    assert sweep() == []
    assert not list(runs_dir().glob("*.json"))


def test_sweeping_an_empty_state_directory_is_not_an_error(state, engine):
    assert sweep() == []
