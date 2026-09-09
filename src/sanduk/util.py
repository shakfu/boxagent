"""Subprocess, state-directory and reporting helpers shared by every module."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROG = "sanduk"


def state_dir() -> Path:
    """Where sanduk keeps what outlives a run: `XDG_STATE_HOME`, or
    `~/.local/state`. Nothing here is the user's work; it is bookkeeping."""
    root = os.environ.get("XDG_STATE_HOME")
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base / "sanduk"


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    """subprocess.run with a list argv. Never shell=True."""
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def note(msg: str) -> None:
    """One progress line on stderr, so stdout stays the agent's."""
    print(f"{PROG}: {msg}", file=sys.stderr, flush=True)
