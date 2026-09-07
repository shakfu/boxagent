"""Container runtimes.

Every subprocess call to a container engine lives here, so adding Docker or
Podman is a subclass plus a registry entry rather than a grep for "container"
across the package.

Only Apple's `container` is implemented. A second engine must supply four
things: the CLI name, the verb that deletes a container (`rm`, not `delete`),
how `network inspect` reports the gateway, and whether the host bridge needs a
placeholder container to exist at all.
"""

from __future__ import annotations

import json
import shutil
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from boxagent.errors import AgentboxError
from boxagent.util import note, run

DEFAULT_IMAGE = "boxagent:latest"

# Shipped inside the package so a `pip install boxagent` can build the image
# without a checkout. __file__ rather than importlib.resources: the engine
# needs a real path on disk for `build -f`, which a Traversable does not
# promise.
DEFAULT_CONTAINERFILE = Path(__file__).parent / "resources" / "Containerfile"


@dataclass
class ContainerSpec:
    """One container to run. Engine-neutral; `Runtime.run_argv` renders it."""

    name: str
    image: str
    command: list[str] = field(default_factory=list)
    cpus: int = 4
    memory: str = "4G"
    mount: tuple[Path, str] | None = None  # (host dir, path inside)
    inherit_env: list[str] = field(default_factory=list)  # -e NAME: value from us
    env: list[str] = field(default_factory=list)  # -e K=V
    network: str | None = None
    detach: bool = False
    entrypoint: str | None = None


class Runtime:
    """A container engine driven through its CLI."""

    name = ""
    cli = ""
    delete_verb = "delete"
    install_hint = ""
    # vmnet-style engines only create the host bridge while a container is
    # attached; Docker and Podman create it with the network.
    needs_network_holder = False

    # --- preflight ----------------------------------------------------------

    def require(self) -> None:
        if shutil.which(self.cli) is None:
            raise AgentboxError(f"`{self.cli}` not found on PATH. {self.install_hint}")
        self.require_service()

    def require_service(self) -> None:
        """Engines with a background daemon check it here."""

    # --- images -------------------------------------------------------------

    def image_exists(self, image: str) -> bool:
        raise NotImplementedError

    def build_image(self, image: str, containerfile: Path | str) -> None:
        cf = Path(containerfile).resolve()
        if not cf.is_file():
            raise AgentboxError(f"no Containerfile at {cf}")
        note(f"building {image} from {cf}")
        r = run([self.cli, "build", "-t", image, "-f", str(cf), str(cf.parent)])
        if r.returncode != 0:
            raise AgentboxError(f"build failed (exit {r.returncode})")

    # --- networks -----------------------------------------------------------

    def network_info(self, name: str) -> tuple[str, str] | None:
        """(gateway, subnet), or None if the network does not exist."""
        raise NotImplementedError

    def ensure_network(self, name: str) -> tuple[str, str]:
        """Create `name` as an egress-blocked network if it is not already there."""
        info = self.network_info(name)
        if info:
            return info
        note(f"creating internal network {name}")
        r = run([self.cli, "network", "create", "--internal", name], capture_output=True)
        if r.returncode != 0:
            raise AgentboxError(f"could not create network {name}: {r.stderr.strip()}")
        info = self.network_info(name)
        if not info:
            raise AgentboxError(f"network {name} created but has no address")
        return info

    def hold_network_up(self, network: str, image: str) -> str | None:
        """Start a placeholder container so the host bridge exists.

        Without it the proxy cannot bind the gateway address and would have to
        fall back to 0.0.0.0, which puts it on Wi-Fi and LAN too. Returns the
        container to tear down, or None when the engine does not need one.
        """
        if not self.needs_network_holder:
            return None
        spec = ContainerSpec(
            name=f"boxagent-hold-{uuid.uuid4().hex[:6]}",
            image=image,
            cpus=1,
            memory="256M",
            network=network,
            detach=True,
            entrypoint="sleep",
            command=["86400"],
        )
        r = run(self.run_argv(spec), capture_output=True)
        if r.returncode != 0:
            raise AgentboxError(f"could not start network holder: {r.stderr.strip()}")
        return spec.name

    # --- containers ---------------------------------------------------------

    def run_argv(self, spec: ContainerSpec) -> list[str]:
        argv = [
            self.cli,
            "run",
            "--name",
            spec.name,
            "--cpus",
            str(spec.cpus),
            "--memory",
            spec.memory,
        ]
        if spec.detach:
            argv.append("-d")
        if spec.mount:
            host, dest = spec.mount
            argv += ["-v", f"{host}:{dest}", "-w", dest]
        # Bare -e NAME: the engine inherits the value from this process, so the
        # value stays out of the argv and out of the host's process list.
        for key in spec.inherit_env:
            argv += ["-e", key]
        for kv in spec.env:
            argv += ["-e", kv]
        if spec.network:
            argv += ["--network", spec.network]
        if spec.entrypoint:
            argv += ["--entrypoint", spec.entrypoint]
        argv.append(spec.image)
        return argv + spec.command

    def destroy(self, name: str, keep: bool = False) -> None:
        if keep:
            note(
                f"keeping container {name} (`{self.cli} inspect {name}` exposes "
                f"the API key; `{self.cli} {self.delete_verb} {name}` when done)"
            )
            return
        run([self.cli, "stop", name], capture_output=True)
        r = run([self.cli, self.delete_verb, name], capture_output=True)
        if r.returncode != 0:
            note(f"could not delete {name}: {r.stderr.strip()}")
        else:
            note(f"deleted {name}")


class AppleContainer(Runtime):
    """Apple's `container`: Linux containers as lightweight VMs on macOS."""

    name = "apple"
    cli = "container"
    delete_verb = "delete"
    install_hint = "Install from github.com/apple/container."
    needs_network_holder = True

    def require_service(self) -> None:
        st = run([self.cli, "system", "status"], capture_output=True)
        if st.returncode != 0 or "running" not in st.stdout:
            raise AgentboxError(
                "container system is not running. Start it with: container system start"
            )

    def image_exists(self, image: str) -> bool:
        out = run([self.cli, "image", "list"], capture_output=True)
        if out.returncode != 0:
            return False
        name, _, tag = image.partition(":")
        tag = tag or "latest"
        for line in out.stdout.splitlines()[1:]:
            f = line.split()
            if len(f) >= 2 and f[0].endswith(name) and f[1] == tag:
                return True
        return False

    def network_info(self, name: str) -> tuple[str, str] | None:
        r = run([self.cli, "network", "inspect", name], capture_output=True)
        if r.returncode != 0:
            return None
        try:
            st = json.loads(r.stdout)[0]["status"]
            return str(st["ipv4Gateway"]), str(st["ipv4Subnet"])
        except (ValueError, KeyError, IndexError):
            return None


RUNTIMES: dict[str, type[Runtime]] = {"apple": AppleContainer}
DEFAULT_RUNTIME = "apple"


def get_runtime(name: str = DEFAULT_RUNTIME) -> Runtime:
    try:
        return RUNTIMES[name]()
    except KeyError:
        known = ", ".join(sorted(RUNTIMES))
        raise AgentboxError(f"unknown runtime {name!r}; known: {known}") from None


def wait_for_gateway(gateway: str, timeout: float = 30) -> bool:
    """Poll until the gateway address is bindable on this host."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.socket()
            s.bind((gateway, 0))
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False
