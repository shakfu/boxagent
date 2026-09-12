"""Subprocess, state-directory and reporting helpers shared by every module."""

from __future__ import annotations

import os
import stat
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


def open_unfollowed(path: Path) -> int | None:
    """`path` as a descriptor, or None if it is not an ordinary file.

    For anything the agent writes: it owns the mount, so a symlink it leaves
    there names a path this process resolves against the host root and the
    container could not reach at all. O_NOFOLLOW rather than an is_symlink()
    test, which the agent can swap between the test and the open. O_NONBLOCK
    because a fifo at that name blocks the open until something writes to it,
    and the container that would have is deleted by then.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        if path.is_symlink():
            note(f"refusing {path.name}: a symlink to {os.readlink(path)}")
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        note(f"refusing {path.name}: not a regular file")
        return None
    return fd


def read_unfollowed(path: Path) -> str | None:
    """The text of `path`, or None if it is not an ordinary file."""
    fd = open_unfollowed(path)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as fh:
        return fh.read().decode(errors="replace")


def copy_unfollowed(fd: int, dest: Path) -> None:
    """Copy an already-opened file to `dest`, following no symlink there.

    The destination is the operator's path, but an assistant's reports
    directory can itself be inside a mount, which puts the same planted
    symlink on this end of the copy.
    """
    try:
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise AgentboxError(f"cannot write the report to {dest}: {e}") from None
    with os.fdopen(out, "wb") as fh:
        os.lseek(fd, 0, os.SEEK_SET)
        while chunk := os.read(fd, 1 << 16):
            fh.write(chunk)
