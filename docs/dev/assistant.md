# Assistants: scheduled runs, persistent state, a mailbox

Status: done, 2026-09-09. Target: 0.2.2. All three phases landed.

Scope: sanduk grows a second mode. `run` stays what it is -- one container, one report, nothing kept. `assistant` adds identity, a schedule, state that outlives a run, and a mailbox. The sandbox model does not change: one container per wakeup, deleted at the end, key on the host, relay per run.

Acceptance bar: an assistant runs on a schedule for a week with no command typed by hand, its results readable afterwards, and a `sanduk clean` in another terminal cannot kill a wakeup in flight.

## The four pieces

| piece | where it lives | why there |
| --- | --- | --- |
| identity | `<dir>/assistant.toml`, human-edited | config an operator reviews belongs in a file they can diff, not a database |
| memory | `<dir>/workspace/`, mounted at `/work` | the agent already writes there; memory is files, readable between wakeups |
| runtime state | SQLite at `$XDG_STATE_HOME/sanduk/assistants.db` | schedule bookkeeping, claims and queues need atomic writes; `runs.py` already uses that directory |
| mailbox | `inbox` and `outbox` tables, plus `<dir>/reports/<ts>.md` | a queue needs single-writer semantics; a report is a file you can read without sanduk |

## Configuration

```toml
# ~/assistants/triage/assistant.toml
name    = "triage"
agent   = "pi"
provider = "anthropic"
model   = "claude-sonnet-5"
proxy   = true
timeout = 900

every   = "30m"        # interval, not cron: no parser, no dependency
gate    = "./gate.sh"  # optional. non-zero exit skips the wakeup
brief   = "brief.md"   # the standing instruction, prepended to any inbox items
max_failures = 3       # consecutive failures before the assistant is disabled
```

`tomllib` is stdlib on 3.11, which is the floor sanduk already declares. Intervals rather than cron expressions: a cron parser is a dependency or 200 lines, and "every 30m" is what a wakeup schedule actually needs. `at = "09:00"` can follow if a fixed hour turns out to matter.

## Schema

```sql
CREATE TABLE assistants (
  name TEXT PRIMARY KEY, dir TEXT NOT NULL,
  next_due_at INTEGER NOT NULL, last_run_at INTEGER,
  failures INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
  claimed_by INTEGER, claimed_at INTEGER          -- pid, for single-writer claims
);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  started_at INTEGER NOT NULL, ended_at INTEGER, exit_code INTEGER,
  report_path TEXT
);
CREATE TABLE inbox (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  created_at INTEGER NOT NULL, body TEXT NOT NULL, consumed_at INTEGER
);
CREATE TABLE outbox (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, run_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL, body TEXT NOT NULL, delivered_at INTEGER
);
```

Claims use the same rule as `runs.py`: a claim whose pid is gone is stale and may be taken; a live pid is left alone. One mechanism for "who owns this", two tables.

## Commands

| command | does |
| --- | --- |
| `sanduk assistant add <dir>` | read `assistant.toml`, register, set `next_due_at` |
| `sanduk assistant list` \| `show <name>` | registry, schedule, failure count, last run |
| `sanduk assistant enable\|disable <name>` | operator switch, separate from the failure switch |
| `sanduk tell <name> <text>` | append to `inbox` |
| `sanduk tick [--name X]` | run every assistant that is due, once. What cron or launchd calls |
| `sanduk serve [--interval 60]` | foreground loop over `tick` |
| `sanduk outbox [--name X] [--deliver CMD]` | read results; pipe undelivered ones to a command and mark them delivered |
| `sanduk runs [--name X]` | run history with exit codes and durations |

`tick` is the primitive and `serve` is a loop over it, so the scheduler is testable without a process that stays up, and either an operator's cron or sanduk's own loop can drive it. `serve` restores the signal handlers it finds, and so does `run`: a wakeup installs its own while it holds a container, and the loop's have to survive it.

## One wakeup

1. Claim the assistant row, or skip it: another process owns it.

2. Run `gate` if configured. Non-zero exit means skip, and costs no tokens.

3. Build the task: the brief, then every unconsumed `inbox` body.

4. Call the existing `run` path with the assistant's settings, `-w <dir>/workspace`, `-o <dir>/reports/<utc timestamp>.md`.

5. Record the run, append the report to `outbox`, mark inbox items consumed.

6. On success: `failures = 0`, `next_due_at = now + every`. On failure: `failures += 1`, back off `every * 2**failures`, and disable at `max_failures` with the reason recorded.

Nothing here reimplements container work. `run()` in `cli.py` is the callee; the assistant layer builds its argument namespace and reads its exit code.

## Required fixes before this is safe

1. **`stop` and `clean` are global** (`cli.py:389`, `cli.py:403`): they act on every `sanduk-` container, which includes a wakeup in flight. Skip containers whose run record has a live owner, and say which were skipped. This is a prerequisite, not a nicety -- unattended runs are when nobody sees it happen.

2. **Concurrency**: settled by building it. `tick` runs wakeups one at a time, so there is no `--jobs`: `run` installs signal handlers, Python allows that only on the main thread, and a wakeup per worker thread would lose the teardown those handlers exist for. Concurrency needs a process per wakeup, which is phase 2's problem if it is anyone's.

3. **Report path per run**: `run` deletes a stale `REPORT.md` at the start (`cli.py:589`), so a per-wakeup `-o` path is what keeps history. Already possible; the assistant layer must use it rather than reading the workspace copy.

## Deliberate, not accidental

- Memory is agent-written and read as instruction on the next wakeup. That is persuasion across runs. The relay's allowlist and the internal network still bound egress, so `proxy = true` is the default in `assistant.toml` and a warning fires without it.

- The outbox holds model output. `--deliver` hands it to a command the operator names; sanduk ships no platform adapters and holds no messaging credential.

- An assistant that disables itself stays disabled until an operator enables it. A failing assistant that retries forever is a bill, not a feature.

## Phases

| phase | contents | rough size |
| --- | --- | --- |
| 1 | config, schema, `assistant add/list/show/enable/disable`, `tell`, `tick`, `outbox --deliver`, `runs`, the `stop`/`clean` fix | done: 480 lines, 39 tests |
| 2 | `serve` (done), token counts per wakeup once `run` returns them | serve: 40 lines, 6 tests |
| 3 | `--mount` and the `mounts` key, approvals on delivery | done: 130 lines, 25 tests |

Phase 1 is usable on its own: cron calls `tick`, an operator reads `outbox`. Gates, backoff and self-disable landed with it rather than in phase 2 -- an unattended assistant that retries a broken configuration forever is a bill.

## What phase 1 left out

- Token counts per wakeup. `run()` returns an exit code and prints its stats through `note`, so the `runs` table records duration and exit code only. Recording tokens means giving `run` a return value beyond the status, which is a change to the run path rather than to this module.

- Concurrency inside `serve`. It is `tick` on a loop, so wakeups stay sequential for the same reason.
