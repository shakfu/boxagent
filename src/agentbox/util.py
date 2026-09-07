"""Subprocess and reporting helpers shared by every module."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

PROG = "agentbox"


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    """subprocess.run with a list argv. Never shell=True."""
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def note(msg: str) -> None:
    """One progress line on stderr, so stdout stays the agent's."""
    print(f"{PROG}: {msg}", file=sys.stderr, flush=True)
