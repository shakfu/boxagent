"""Subprocess, state-directory and reporting helpers shared by every module."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sanduk.errors import AgentboxError

PROG = "sanduk"

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(value: str | int) -> int:
    """A duration: a bare number is seconds, a suffix multiplies it.

    `900`, `"900"`, `"45s"`, `"15m"`, `"2h"`, `"1d"`. Every duration sanduk
    takes reads the same way, because `every = "30m"` beside `timeout = 900`
    in one config file says nothing about which key means what.
    """
    text = str(value).strip()
    if text.isdigit():
        total = int(text)
    elif len(text) > 1 and text[-1] in UNITS and text[:-1].isdigit():
        total = int(text[:-1]) * UNITS[text[-1]]
    else:
        raise AgentboxError(
            f"{value!r} is not a duration: seconds as a number, or a suffix "
            f"of s, m, h or d -- 900, 45s, 15m, 2h, 1d"
        )
    if total <= 0:
        raise AgentboxError(f"{value!r} is not a positive duration")
    return total


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
