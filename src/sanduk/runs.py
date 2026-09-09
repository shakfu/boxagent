"""Ownership records for the containers a run holds.

SIGKILL runs no teardown, so a killed run leaves its container alive with the
run token still inside it. Each run writes a record naming the containers it
owns and the pid that owns them, and every later run deletes the containers
whose owner is gone. Nothing reaps at kill time, because nothing can: the fix
is the next run, not the dying one.

The record, not the container name, is what marks a container reapable.
`--keep` releases it, so a container the caller asked to inspect is never swept
by the run after it.

A pid whose number has been reused reads as alive and its record is skipped.
That misses an orphan until the number is free again; the opposite error would
delete a container another process is still using.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sanduk.errors import AgentboxError
from sanduk.runtime import CONTAINER_PREFIX, Runtime, get_runtime
from sanduk.util import note, state_dir


def runs_dir() -> Path:
    """Where records live, under sanduk's state directory."""
    return state_dir() / "runs"


def owner_alive(pid: int) -> bool:
    """Whether the process that claimed a record is still there.

    Signal 0 checks for the process without sending anything. A pid owned by
    another user answers with PermissionError, which is still a live process
    and still not ours to reap.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Run:
    """One run's claim on the containers it started."""

    path: Path
    runtime: str
    containers: list[str]

    def _write(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "runtime": self.runtime,
                    "containers": self.containers,
                }
            )
        )

    def add(self, name: str) -> None:
        """Claim a container started after the record was written."""
        self.containers.append(name)
        self._write()

    def release(self) -> None:
        """Give up the claim. The containers are gone, or deliberately kept."""
        self.path.unlink(missing_ok=True)


def claim(runtime: str, name: str) -> Run:
    """Record that this process owns `name` on `runtime`.

    A state directory that cannot be written raises, stopping the run: a run
    with no record leaks its container silently when it is killed.
    """
    directory = runs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    run = Run(directory / f"{name}.json", runtime, [name])
    run._write()
    return run


def _records() -> list[tuple[Path, dict[str, Any]]]:
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(runs_dir().glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        out.append((path, record))
    return out


def live_containers() -> set[str]:
    """Containers a running process still claims.

    `stop` and `clean` act on a name prefix, which cannot tell a wakeup in
    flight from a leftover. The records can.
    """
    held: set[str] = set()
    for _path, record in _records():
        if owner_alive(int(record.get("pid", 0))):
            held.update(str(c) for c in record.get("containers", []))
    return held


def sweep() -> list[str]:
    """Delete the containers of every run whose owner is gone.

    Returns what was deleted. A record naming an engine this host cannot reach
    is left alone rather than dropped: the containers it names are still there,
    and the engine may be back on the next run.
    """
    reaped: list[str] = []
    engines: dict[str, tuple[Runtime, set[str]]] = {}
    for path, record in _records():
        if owner_alive(int(record.get("pid", 0))):
            continue
        name = str(record.get("runtime", ""))
        if name not in engines:
            try:
                engine = get_runtime(name)
                found = {c.name for c in engine.list_containers(CONTAINER_PREFIX)}
            except (AgentboxError, OSError):
                continue
            engines[name] = (engine, found)
        engine, found = engines[name]
        for container in record.get("containers", []):
            if container not in found:
                continue
            note(f"reaping {container}: the run that started it was killed")
            engine.destroy(container)
            reaped.append(container)
        path.unlink(missing_ok=True)
    return reaped
