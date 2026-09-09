"""Assistants: an identity, a schedule and a mailbox around `sanduk run`.

`run` is one container and no memory of it. An assistant is a directory whose
`assistant.toml` says how to run, a `workspace/` the agent keeps its memory in,
and a SQLite file holding what has to outlive a run: when the next wakeup is
due, which process owns one now, what came in and what went out.

Nothing here starts a container. A wakeup composes a task and calls the `run`
command, so an assistant can do nothing a typed `sanduk run` cannot.

Config is a file the operator edits and the database is bookkeeping only. A
schedule that lived in the database would be edited through a verb nobody wants
to write, and a queue that lived in a file would need the locking SQLite has.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sanduk.errors import AgentboxError
from sanduk.runs import owner_alive
from sanduk.util import note, state_dir

CONFIG_NAME = "assistant.toml"
DEFAULT_EVERY = "30m"
# A failing assistant backs off per consecutive failure, to this ceiling. Past
# it the wakeups are neither useful nor free.
MAX_BACKOFF = 24 * 3600
# What `run` exits with when a signal tore it down. An interrupted wakeup is not
# a broken assistant, so it is not counted as a failure.
INTERRUPTED = 130

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

SCHEMA = """
CREATE TABLE IF NOT EXISTS assistants (
  name TEXT PRIMARY KEY,
  dir TEXT NOT NULL,
  next_due_at INTEGER NOT NULL,
  last_run_at INTEGER,
  failures INTEGER NOT NULL DEFAULT 0,
  disabled INTEGER NOT NULL DEFAULT 0,
  claimed_by INTEGER,
  claimed_at INTEGER
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  exit_code INTEGER,
  report_path TEXT,
  stats TEXT
);
CREATE TABLE IF NOT EXISTS inbox (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  body TEXT NOT NULL,
  consumed_at INTEGER
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  run_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  body TEXT NOT NULL,
  delivered_at INTEGER,
  approved_at INTEGER,
  rejected_at INTEGER
);
"""


def seconds(text: str) -> int:
    """`30m` -> 1800. Intervals, not cron: a schedule of wakeups needs no more,
    and a cron parser is a dependency this package does not have."""
    value = str(text).strip()
    if len(value) < 2 or value[-1] not in UNITS or not value[:-1].isdigit():
        raise AgentboxError(f"{text!r} is not an interval like 45s, 30m, 2h, 1d")
    total = int(value[:-1]) * UNITS[value[-1]]
    if total <= 0:
        raise AgentboxError(f"{text!r} is not a positive interval")
    return total


@dataclass(frozen=True)
class Assistant:
    """One assistant's configuration, as read from disk."""

    name: str
    dir: Path
    agent: str
    provider: str
    model: str | None
    runtime: str | None
    proxy: bool
    timeout: int
    every: int
    brief: Path | None
    gate: Path | None
    max_failures: int
    approval: bool
    mounts: list[str]
    args: list[str]

    @property
    def workspace(self) -> Path:
        return self.dir / "workspace"

    @property
    def reports(self) -> Path:
        return self.dir / "reports"


def load(directory: Path) -> Assistant:
    """Read `<directory>/assistant.toml`."""
    directory = directory.expanduser().resolve()
    path = directory / CONFIG_NAME
    if not path.is_file():
        raise AgentboxError(f"no {CONFIG_NAME} in {directory}")
    try:
        conf = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise AgentboxError(f"{path}: {e}") from None

    unknown = set(conf) - {
        "name", "agent", "provider", "model", "runtime", "proxy", "timeout",
        "every", "brief", "gate", "max_failures", "approval", "mounts", "args",
    }  # fmt: skip
    if unknown:
        raise AgentboxError(f"{path}: unknown keys {', '.join(sorted(unknown))}")

    def resolve(key: str) -> Path | None:
        value = conf.get(key)
        return None if value is None else (directory / str(value))

    assistant = Assistant(
        name=str(conf.get("name", directory.name)),
        dir=directory,
        agent=str(conf.get("agent", "claude")),
        provider=str(conf.get("provider", "anthropic")),
        model=None if conf.get("model") is None else str(conf["model"]),
        runtime=None if conf.get("runtime") is None else str(conf["runtime"]),
        proxy=bool(conf.get("proxy", True)),
        timeout=int(conf.get("timeout", 900)),
        every=seconds(conf.get("every", DEFAULT_EVERY)),
        brief=resolve("brief"),
        gate=resolve("gate"),
        max_failures=int(conf.get("max_failures", 3)),
        approval=bool(conf.get("approval", False)),
        mounts=[anchor(directory, str(m)) for m in conf.get("mounts", [])],
        args=[str(a) for a in conf.get("args", [])],
    )
    if not assistant.proxy:
        # Unattended runs are the ones nobody watches; without the relay the
        # container holds the key and can reach anything.
        note(f"{assistant.name}: proxy = false, so the container holds the key")
    if assistant.brief and not assistant.brief.is_file():
        raise AgentboxError(f"{path}: brief {assistant.brief} does not exist")
    return assistant


def anchor(directory: Path, spec: str) -> str:
    """Make a `HOST:DEST[:ro]` mount's host path absolute, from the config's own
    directory. `run` validates the rest: one place decides what a mount may be."""
    parts = spec.split(":")
    if len(parts) in (2, 3) and parts[0] and not parts[0].startswith("/"):
        parts[0] = str((directory / parts[0]).resolve())
    return ":".join(parts)


# --- state ------------------------------------------------------------------


def db_path() -> Path:
    return state_dir() / "assistants.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: this module says where a transaction begins, which
    # matters for the claim and nowhere else.
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    migrate(db)
    return db


def migrate(db: sqlite3.Connection) -> None:
    """Columns added after a database was first written.

    `CREATE TABLE IF NOT EXISTS` does not add them, and an assistant's history
    is not worth dropping to gain a column.
    """
    have = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
    if "stats" not in have:
        db.execute("ALTER TABLE runs ADD COLUMN stats TEXT")
    held = {row["name"] for row in db.execute("PRAGMA table_info(outbox)")}
    for column in ("approved_at", "rejected_at"):
        if column not in held:
            # Rows written before approvals existed were delivered on sight,
            # which is what an assistant without `approval` still does.
            db.execute(f"ALTER TABLE outbox ADD COLUMN {column} INTEGER")
            if column == "approved_at":
                db.execute("UPDATE outbox SET approved_at = created_at")


def now() -> int:
    return int(time.time())


def register(db: sqlite3.Connection, assistant: Assistant) -> None:
    """Add an assistant, or point an existing name at a new directory. The
    schedule survives a re-add: registering again is not a reason to run."""
    db.execute(
        "INSERT INTO assistants (name, dir, next_due_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET dir = excluded.dir",
        (assistant.name, str(assistant.dir), now()),
    )


def rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(db.execute("SELECT * FROM assistants ORDER BY name"))


def row(db: sqlite3.Connection, name: str) -> sqlite3.Row:
    found = db.execute("SELECT * FROM assistants WHERE name = ?", (name,)).fetchone()
    if found is None:
        raise AgentboxError(f"no assistant named {name!r}. `sanduk assistant list`")
    return cast(sqlite3.Row, found)


def set_disabled(db: sqlite3.Connection, name: str, disabled: bool) -> None:
    row(db, name)
    db.execute(
        "UPDATE assistants SET disabled = ?, failures = 0 WHERE name = ?",
        (1 if disabled else 0, name),
    )


def due(db: sqlite3.Connection, name: str | None = None) -> list[sqlite3.Row]:
    if name is not None:
        found = row(db, name)
        return [] if found["disabled"] else [found]
    return list(
        db.execute(
            "SELECT * FROM assistants WHERE disabled = 0 AND next_due_at <= ? "
            "ORDER BY next_due_at",
            (now(),),
        )
    )


def take(db: sqlite3.Connection, name: str) -> bool:
    """Claim an assistant for this process. False means someone else has it.

    Same rule as a run record: a claim whose pid is gone is stale and may be
    taken, a live one is left alone.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        held = row(db, name)["claimed_by"]
        if held is not None and owner_alive(int(held)) and int(held) != os.getpid():
            db.execute("ROLLBACK")
            return False
        db.execute(
            "UPDATE assistants SET claimed_by = ?, claimed_at = ? WHERE name = ?",
            (os.getpid(), now(), name),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return True


def release(db: sqlite3.Connection, name: str) -> None:
    db.execute(
        "UPDATE assistants SET claimed_by = NULL, claimed_at = NULL WHERE name = ?",
        (name,),
    )


def tell(db: sqlite3.Connection, name: str, body: str) -> int:
    row(db, name)
    cur = db.execute(
        "INSERT INTO inbox (name, created_at, body) VALUES (?, ?, ?)",
        (name, now(), body),
    )
    return int(cur.lastrowid or 0)


def pending(db: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return list(
        db.execute(
            "SELECT * FROM inbox WHERE name = ? AND consumed_at IS NULL ORDER BY id",
            (name,),
        )
    )


def consume(db: sqlite3.Connection, ids: list[int]) -> None:
    db.executemany(
        "UPDATE inbox SET consumed_at = ? WHERE id = ?", [(now(), i) for i in ids]
    )


def outbox(
    db: sqlite3.Connection,
    name: str | None = None,
    undelivered: bool = False,
    pending: bool = False,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM outbox WHERE 1 = 1"
    params: list[object] = []
    if name is not None:
        sql += " AND name = ?"
        params.append(name)
    if undelivered:
        sql += " AND delivered_at IS NULL AND rejected_at IS NULL"
    if pending:
        sql += " AND approved_at IS NULL AND rejected_at IS NULL"
    return list(db.execute(sql + " ORDER BY id", params))


def state_of(entry: sqlite3.Row) -> str:
    """What an outbox row is waiting for, in one word."""
    if entry["rejected_at"]:
        return "rejected"
    if entry["delivered_at"]:
        return "delivered"
    if entry["approved_at"] is None:
        return "pending"
    return "approved"


def decide(db: sqlite3.Connection, ids: list[int], approve: bool) -> int:
    """Approve or reject outbox entries. Neither is undone by the other: a
    decision recorded is a decision made."""
    column = "approved_at" if approve else "rejected_at"
    cur = db.executemany(
        f"UPDATE outbox SET {column} = ? WHERE id = ? AND {column} IS NULL "
        "AND delivered_at IS NULL",
        [(now(), i) for i in ids],
    )
    return int(cur.rowcount or 0)


def delivered(db: sqlite3.Connection, ids: list[int]) -> None:
    db.executemany(
        "UPDATE outbox SET delivered_at = ? WHERE id = ?", [(now(), i) for i in ids]
    )


def history(
    db: sqlite3.Connection, name: str | None = None, limit: int = 20
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM runs"
    params: list[object] = []
    if name is not None:
        sql += " WHERE name = ?"
        params.append(name)
    params.append(limit)
    return list(db.execute(sql + " ORDER BY id DESC LIMIT ?", params))


# --- one wakeup -------------------------------------------------------------


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def gate_allows(assistant: Assistant) -> bool:
    """Run the gate script, if there is one. Non-zero exit skips the wakeup.

    The point is to decide cheaply, before any model is paid to decide.
    """
    if assistant.gate is None:
        return True
    if not os.access(assistant.gate, os.X_OK):
        raise AgentboxError(f"gate {assistant.gate} is not executable")
    result = subprocess.run(
        [str(assistant.gate)], cwd=assistant.dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        reason = result.stdout.strip() or result.stderr.strip() or "gate said no"
        note(f"{assistant.name}: skipped, {reason[:120]}")
        return False
    return True


def compose(assistant: Assistant, items: list[sqlite3.Row]) -> str:
    """The standing brief, then anything queued since the last wakeup."""
    parts = []
    if assistant.brief is not None:
        parts.append(assistant.brief.read_text().strip())
    for item in items:
        when = datetime.fromtimestamp(item["created_at"], UTC).isoformat()
        parts.append(f"Message received {when}:\n{item['body'].strip()}")
    if not parts:
        raise AgentboxError(
            f"{assistant.name}: nothing to do -- set `brief` in {CONFIG_NAME} "
            f"or send one with `sanduk tell {assistant.name} '...'`"
        )
    return "\n\n".join(parts)


def run_argv(
    assistant: Assistant,
    task_file: Path,
    report: Path,
    runtime: str | None,
    stats_file: Path | None = None,
) -> list[str]:
    """The `sanduk run` command line one wakeup is."""
    argv = [
        "run",
        "--task-file",
        str(task_file),
        "-w",
        str(assistant.workspace),
        "-o",
        str(report),
        "--agent",
        assistant.agent,
        "--provider",
        assistant.provider,
        "--timeout",
        str(assistant.timeout),
    ]
    engine = runtime or assistant.runtime
    if engine:
        argv += ["--runtime", engine]
    if assistant.model:
        argv += ["--model", assistant.model]
    if assistant.proxy:
        argv.append("--proxy")
    for mount in assistant.mounts:
        argv += ["--mount", mount]
    if stats_file is not None:
        argv += ["--stats-file", str(stats_file)]
    return argv + assistant.args


def wake(db: sqlite3.Connection, assistant: Assistant, runtime: str | None = None) -> int:
    """One wakeup: gate, compose, run, record, reschedule. Returns the exit code.

    A wakeup that fails is scheduling news, not an exception: it backs the
    assistant off and, past `max_failures`, disables it.
    """
    if not gate_allows(assistant):
        schedule_next(db, assistant, ok=True)
        return 0

    items = pending(db, assistant.name)
    task = compose(assistant, items)
    assistant.workspace.mkdir(parents=True, exist_ok=True)
    assistant.reports.mkdir(parents=True, exist_ok=True)
    when = stamp()
    task_file = assistant.reports / f"{when}.task.md"
    report = assistant.reports / f"{when}.md"
    task_file.write_text(task)

    started = now()
    cur = db.execute(
        "INSERT INTO runs (name, started_at, report_path) VALUES (?, ?, ?)",
        (assistant.name, started, str(report)),
    )
    run_id = int(cur.lastrowid or 0)

    # Local: cli imports this module for its commands, so this cannot be a
    # top-level import. main() turns an AgentboxError into an exit code, so a
    # failed wakeup lands here as a number rather than an exception.
    from sanduk.cli import main

    stats_file = state_dir() / "wakeup.json"
    stats_file.unlink(missing_ok=True)
    code = main(run_argv(assistant, task_file, report, runtime, stats_file))
    stats = read_stats(stats_file)

    db.execute(
        "UPDATE runs SET ended_at = ?, exit_code = ?, stats = ? WHERE id = ?",
        (now(), code, stats, run_id),
    )
    body = report.read_text() if report.is_file() else f"(no report, exit {code})"
    db.execute(
        "INSERT INTO outbox (name, run_id, created_at, body, approved_at) "
        "VALUES (?, ?, ?, ?, ?)",
        # Approval gates delivery, which is the only thing that leaves the box:
        # what the agent does, it does inside a container that is then deleted.
        (assistant.name, run_id, now(), body, None if assistant.approval else now()),
    )
    if code == 0:
        # Only a wakeup that finished consumes its messages: a failed one has
        # not answered them, and the next wakeup should still see them.
        consume(db, [int(i["id"]) for i in items])
    schedule_next(db, assistant, ok=code in (0, INTERRUPTED))
    return code


def read_stats(path: Path) -> str | None:
    """The token line `run` recorded, if it got far enough to record one."""
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    finally:
        path.unlink(missing_ok=True)
    line = str(found.get("stats") or "").strip()
    return line or None


def schedule_next(db: sqlite3.Connection, assistant: Assistant, ok: bool) -> None:
    name = assistant.name
    failures = 0 if ok else int(row(db, name)["failures"]) + 1
    delay = assistant.every if ok else min(assistant.every * 2**failures, MAX_BACKOFF)
    disabled = 0 if ok else int(failures >= assistant.max_failures)
    if disabled:
        note(
            f"{name}: disabled after {failures} failures. "
            f"Fix it, then `sanduk assistant enable {name}`"
        )
    db.execute(
        "UPDATE assistants SET next_due_at = ?, last_run_at = ?, failures = ?, "
        "disabled = ? WHERE name = ?",
        (now() + delay, now(), failures, disabled, name),
    )


def tick(
    db: sqlite3.Connection, name: str | None = None, runtime: str | None = None
) -> int:
    """Every assistant that is due, once, in order. One at a time.

    Sequential rather than concurrent: `run` installs signal handlers, and
    Python allows that only on the main thread, so a wakeup per worker thread
    would lose the teardown those handlers exist for.
    """
    worst = 0
    for found in due(db, name):
        assistant = load(Path(found["dir"]))
        if not take(db, assistant.name):
            note(f"{assistant.name}: another process is running it")
            continue
        try:
            note(f"{assistant.name}: waking")
            worst = max(worst, wake(db, assistant, runtime))
        finally:
            release(db, assistant.name)
    return worst


def nap(db: sqlite3.Connection, interval: int) -> float:
    """Seconds until the next wakeup is due, capped at `interval`.

    Capped, so an assistant registered while the loop sleeps waits `interval` at
    worst rather than until whatever was due first.
    """
    found = db.execute(
        "SELECT MIN(next_due_at) AS soonest FROM assistants WHERE disabled = 0"
    ).fetchone()
    soonest = found["soonest"]
    if soonest is None:
        return float(interval)
    return float(max(1, min(interval, int(soonest) - now())))


def serve(db: sqlite3.Connection, interval: int = 60, runtime: str | None = None) -> int:
    """`tick` on a loop, in the foreground, until a signal says stop.

    The loop holds no credential and no container between wakeups; it is a
    timer. Anything that can run it as a service -- launchd, systemd, a
    terminal -- runs it the same way, and `tick` under cron is still the
    version with no process to supervise.
    """
    stopping = threading.Event()

    def halt(signum: int, _frame: object) -> None:
        note(f"signal {signum}: stopping after the current wakeup")
        stopping.set()

    previous = [(s, signal.signal(s, halt)) for s in (signal.SIGINT, signal.SIGTERM)]
    note(f"serving; waking assistants as they come due, at most every {interval}s")
    try:
        while not stopping.is_set():
            # A wakeup that a signal tore down took the signal with it: `run`
            # installs its own handlers while it holds a container, so this
            # loop learns about it from the exit code rather than from `halt`.
            if tick(db, runtime=runtime) == INTERRUPTED:
                break
            if stopping.is_set():
                break
            stopping.wait(nap(db, interval))
    finally:
        for signum, handler in previous:
            signal.signal(signum, handler)
    note("stopped")
    return 0


def deliver(db: sqlite3.Connection, command: str, name: str | None = None) -> int:
    """Pipe every undelivered outbox entry to a command, on stdin.

    sanduk holds no messaging credential and ships no platform adapter: what
    the entry is worth sending to is the operator's to say.
    """
    sent: list[int] = []
    waiting = len(outbox(db, name, pending=True))
    if waiting:
        note(f"{waiting} waiting for approval (`sanduk outbox --pending`)")
    for entry in outbox(db, name, undelivered=True):
        if entry["approved_at"] is None:
            continue
        env = {
            **os.environ,
            "SANDUK_ASSISTANT": entry["name"],
            "SANDUK_RUN_ID": str(entry["run_id"]),
        }
        result = subprocess.run(
            command, shell=True, input=entry["body"], text=True, env=env
        )
        if result.returncode != 0:
            note(f"delivery failed ({result.returncode}); {len(sent)} delivered")
            break
        sent.append(int(entry["id"]))
    delivered(db, sent)
    return len(sent)


def summary(db: sqlite3.Connection, name: str) -> str:
    """One assistant's state, as JSON on stdout: this is what a script reads."""
    found = dict(row(db, name))
    found["pending"] = len(pending(db, name))
    found["undelivered"] = len(outbox(db, name, undelivered=True))
    found["awaiting_approval"] = len(outbox(db, name, pending=True))
    return json.dumps(found, indent=2, sort_keys=True)
