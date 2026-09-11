"""Run an agent in a disposable container.

The agent works in a bind-mounted directory, writes a report, and the container
is deleted. With --proxy it runs on a network with no route off the host and
never holds the API key: a host-side relay injects the credential and the
container gets a per-run token.
"""

from sanduk.agent import (
    KEY_ENV,
    REPORT_NAME,
    Agent,
    Outcome,
    Reader,
    Wiring,
    get_agent,
)
from sanduk.cli import main
from sanduk.errors import AgentboxError
from sanduk.proxy import start_proxy
from sanduk.runtime import ContainerSpec, Runtime, get_runtime

__all__ = [
    "KEY_ENV",
    "REPORT_NAME",
    "Agent",
    "AgentboxError",
    "ContainerSpec",
    "Outcome",
    "Reader",
    "Runtime",
    "Wiring",
    "get_agent",
    "get_runtime",
    "main",
    "start_proxy",
]
__version__ = "0.2.4"
