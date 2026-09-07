"""The one error type this package raises."""

from __future__ import annotations


class AgentboxError(Exception):
    """A failure the caller can act on.

    Library code raises this; only `cli.main` decides what a process does about
    it. `code` becomes the exit status there.
    """

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code
