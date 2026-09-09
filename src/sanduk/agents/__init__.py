"""The handlers sanduk ships.

`BUILTIN` is what `agent.registry` seeds itself with. A handler you write goes
in your own distribution and is advertised in the `sanduk.agents` entry-point
group; it does not belong here.
"""

from __future__ import annotations

from sanduk.agent import Agent
from sanduk.agents.claude import ClaudeCode
from sanduk.agents.codex import Codex
from sanduk.agents.hax import Hax
from sanduk.agents.opencode import OpenCode
from sanduk.agents.pi import Pi
from sanduk.agents.prime import Prime

BUILTIN: tuple[type[Agent], ...] = (ClaudeCode, Codex, Hax, OpenCode, Pi, Prime)

__all__ = ["BUILTIN", "ClaudeCode", "Codex", "Hax", "OpenCode", "Pi", "Prime"]
