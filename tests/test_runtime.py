"""Runtime tests: engine registry, argv construction, image listing.

No containers and no network: everything here is a pure function or is
monkeypatched.
"""

from types import SimpleNamespace

import pytest

from sanduk import runtime
from sanduk.agent import BASE_URL_ENV, KEY_ENV
from sanduk.cli import build_spec, parse_args
from sanduk.errors import AgentboxError
from sanduk.runtime import ContainerSpec, get_runtime

KEY = "sk-ant-api03-SECRET"


def argv_for(*flags, workdir, network=None):
    args = parse_args(list(flags))
    return get_runtime().run_argv(build_spec(args, "n", workdir, "task", network))


# --- registry ---------------------------------------------------------------


def test_default_runtime_is_apple_container():
    assert get_runtime().cli == "container"


def test_unknown_runtime_names_the_known_ones():
    with pytest.raises(AgentboxError, match="apple"):
        get_runtime("docker")


# --- argv construction ------------------------------------------------------


def test_key_value_never_appears_in_argv(tmp_path):
    """-e KEY_ENV is the bare-name form: the engine inherits the value, so the
    key stays out of the host process list."""
    argv = argv_for("task", "-w", str(tmp_path), workdir=tmp_path)
    assert KEY not in " ".join(argv)
    assert argv[argv.index("-e") + 1] == KEY_ENV


def test_non_proxy_run_sets_no_network_and_no_base_url(tmp_path):
    argv = argv_for("task", "-w", str(tmp_path), workdir=tmp_path)
    assert "--network" not in argv
    assert BASE_URL_ENV not in argv


def test_proxy_run_adds_network_and_base_url(tmp_path):
    argv = argv_for(
        "task", "-w", str(tmp_path), "--proxy", workdir=tmp_path, network="sanduk-net"
    )
    assert argv[argv.index("--network") + 1] == "sanduk-net"
    assert BASE_URL_ENV in argv


def test_permissions_are_bypassed_by_default(tmp_path):
    """Nothing is there to answer a prompt."""
    argv = argv_for("task", "-w", str(tmp_path), workdir=tmp_path)
    assert "--dangerously-skip-permissions" in argv


def test_explicit_permission_mode_replaces_the_default(tmp_path):
    argv = argv_for(
        "task", "-w", str(tmp_path), "--permission-mode", "acceptEdits", workdir=tmp_path
    )
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_workdir_is_mounted_at_work(tmp_path):
    argv = argv_for("task", "-w", str(tmp_path), workdir=tmp_path)
    assert argv[argv.index("-v") + 1] == f"{tmp_path}:/work"


def test_the_network_holder_is_detached_and_runs_no_agent():
    """It exists only so vmnet creates the host bridge."""
    spec = ContainerSpec(
        name="sanduk-hold-x",
        image="sanduk:latest",
        network="sanduk-net",
        detach=True,
        entrypoint="sleep",
        command=["86400"],
    )
    argv = get_runtime().run_argv(spec)
    assert "-d" in argv
    assert "-v" not in argv
    assert argv[-4:] == ["--entrypoint", "sleep", "sanduk:latest", "86400"]


# --- image listing ----------------------------------------------------------


def test_image_exists_reads_the_listing(monkeypatch):
    listing = (
        "NAME      TAG      DIGEST\n"
        "sanduk  latest   7429d9f6127f\n"
        "alpine    3.20     d9e853e87e55\n"
    )
    monkeypatch.setattr(
        runtime, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=listing)
    )
    engine = get_runtime()
    assert engine.image_exists("sanduk:latest")
    assert engine.image_exists("sanduk") is True  # tag defaults to latest
    assert not engine.image_exists("sanduk:test")
    assert not engine.image_exists("missing:latest")
