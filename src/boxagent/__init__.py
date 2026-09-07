"""Run an agent in a disposable container.

The agent works in a bind-mounted directory, writes a report, and the container
is deleted. With --proxy it runs on a network with no route off the host and
never holds the API key: a host-side relay injects the credential and the
container gets a per-run token.
"""

from boxagent.agent import KEY_ENV, REPORT_NAME
from boxagent.cli import main
from boxagent.errors import AgentboxError
from boxagent.proxy import start_proxy
from boxagent.runtime import ContainerSpec, Runtime, get_runtime

__all__ = [
    "KEY_ENV",
    "REPORT_NAME",
    "AgentboxError",
    "ContainerSpec",
    "Runtime",
    "get_runtime",
    "main",
    "start_proxy",
]
__version__ = "0.1.0"
