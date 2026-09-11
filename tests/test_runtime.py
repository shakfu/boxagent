"""Runtime tests: engine registry, argv construction, image listing.

No containers and no network: everything here is a pure function or is
monkeypatched.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from sanduk import runtime
from sanduk.agent import BASE_URL_ENV, KEY_ENV
from sanduk.cli import build_spec, parse_args, relay_root, select
from sanduk.errors import AgentboxError
from sanduk.runtime import ContainerSpec, Mount, get_runtime

KEY = "sk-ant-api03-SECRET"


def argv_for(*flags, workdir, network=None):
    args = parse_args(["run", *flags])
    sel = select(args)
    wiring = sel.agent.wire(args, sel.provider, relay_root(args))
    return get_runtime().run_argv(
        build_spec(args, sel, wiring, "n", workdir, "task", network)
    )


# --- registry ---------------------------------------------------------------


def test_default_runtime_is_apple_container():
    assert get_runtime().cli == "container"


def test_unknown_runtime_names_the_known_ones():
    with pytest.raises(AgentboxError, match="apple, docker"):
        get_runtime("nerdctl")


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


# --- networks ---------------------------------------------------------------


def create_fails(monkeypatch):
    """Every engine call fails, as a second `network create` does; no waiting."""
    monkeypatch.setattr(
        runtime,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="", stderr="has a pending operation"
        ),
    )
    monkeypatch.setattr(runtime.time, "sleep", lambda s: None)


def test_a_network_another_run_is_creating_is_waited_for(monkeypatch):
    """Two runs found no network and both created it; the second create failed."""
    engine = get_runtime()
    answers = iter([None, None, ("10.0.0.1", "10.0.0.0/24")])
    monkeypatch.setattr(engine, "network_info", lambda name: next(answers))
    create_fails(monkeypatch)
    assert engine.ensure_network("n") == ("10.0.0.1", "10.0.0.0/24")


def test_a_network_that_never_appears_is_still_an_error(monkeypatch):
    engine = get_runtime()
    monkeypatch.setattr(engine, "network_info", lambda name: None)
    create_fails(monkeypatch)
    with pytest.raises(AgentboxError, match="pending operation"):
        engine.ensure_network("n")


# --- docker -----------------------------------------------------------------
#
# No daemon is contacted: every subprocess call is replaced. What these pin is
# the shape of each command and how its output is read, which is the whole of
# what a Runtime subclass is.


def responses(monkeypatch, *, returncode=0, stdout=""):
    """Record the argv of every engine call, answering each the same way."""
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(runtime, "run", fake)
    return calls


def test_docker_is_in_the_registry():
    assert get_runtime("docker").cli == "docker"


def test_docker_deletes_with_rm():
    """`container delete` is `docker rm`; the base class runs stop first."""
    assert get_runtime("docker").delete_verb == "rm"


def test_docker_needs_no_network_holder():
    """Docker creates the bridge with the network; vmnet only while attached."""
    assert not get_runtime("docker").needs_network_holder


def test_an_oci_runtime_reaches_docker_before_the_image():
    """gVisor or Kata in place of runc: the argv is the whole of the change."""
    spec = ContainerSpec(name="n", image="img", oci_runtime="runsc")
    argv = get_runtime("docker").run_argv(spec)
    assert argv[argv.index("--runtime") + 1] == "runsc"
    assert argv.index("--runtime") < argv.index("img")


def test_without_an_oci_runtime_docker_keeps_its_default():
    argv = get_runtime("docker").run_argv(ContainerSpec(name="n", image="img"))
    assert "--runtime" not in argv
    assert get_runtime("docker").hold_network_up("sanduk-net", "img") is None


def test_docker_image_exists_is_an_exit_status(monkeypatch):
    calls = responses(monkeypatch, returncode=0)
    assert get_runtime("docker").image_exists("sanduk-hax:latest")
    assert calls == [["docker", "image", "inspect", "sanduk-hax:latest"]]


def test_docker_image_exists_is_false_when_inspect_fails(monkeypatch):
    responses(monkeypatch, returncode=1)
    assert not get_runtime("docker").image_exists("nope:latest")


DOCKER_NETWORK = """
[{"Name": "sanduk-net", "Internal": true,
  "IPAM": {"Config": [{"Subnet": "172.20.0.0/16", "Gateway": "172.20.0.1"}]}}]
"""


def test_docker_reads_the_gateway_from_the_ipam_block(monkeypatch):
    """Apple reports it under status.ipv4Gateway; Docker under IPAM.Config."""
    responses(monkeypatch, stdout=DOCKER_NETWORK)
    assert get_runtime("docker").network_info("sanduk-net") == (
        "172.20.0.1",
        "172.20.0.0/16",
    )


@pytest.mark.parametrize("stdout", ["", "not json", "[]", '[{"IPAM": {}}]'])
def test_docker_network_info_is_none_when_the_gateway_is_absent(monkeypatch, stdout):
    """A network with no address must read as absent, not as a partial answer:
    ensure_network turns None into an error naming the network."""
    responses(monkeypatch, stdout=stdout)
    assert get_runtime("docker").network_info("sanduk-net") is None


def test_docker_network_info_is_none_when_the_network_is_missing(monkeypatch):
    responses(monkeypatch, returncode=1, stdout=DOCKER_NETWORK)
    assert get_runtime("docker").network_info("sanduk-net") is None


def test_an_unreachable_docker_daemon_is_named(monkeypatch):
    responses(monkeypatch, returncode=1)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    with pytest.raises(AgentboxError, match="daemon"):
        get_runtime("docker").require()


def test_a_snap_docker_daemon_cannot_run_an_agent(monkeypatch):
    """Its AppArmor profile blocks every exec under no-new-privileges."""
    responses(monkeypatch, stdout="Ubuntu Core 24\n")
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/snap/bin/docker")
    with pytest.raises(AgentboxError, match="snap package"):
        get_runtime("docker").require_run()


def test_a_snap_docker_daemon_still_answers_the_other_verbs(monkeypatch):
    """ps, clean and destroy work there; destroy removes an older run's network."""
    responses(monkeypatch, stdout="Ubuntu Core 24\n")
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/snap/bin/docker")
    get_runtime("docker").require()


def test_a_native_docker_daemon_can_run_an_agent(monkeypatch):
    calls = responses(monkeypatch, stdout="Ubuntu 24.04.5 LTS\n")
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    get_runtime("docker").require_run()
    assert ["docker", "info", "--format", "{{.OperatingSystem}}"] in calls


def one_spec() -> ContainerSpec:
    return ContainerSpec(
        name="sanduk-x",
        image="sanduk:latest",
        command=["-p", "go"],
        mount=(Path("/tmp/w"), "/work"),
        inherit_env=["ANTHROPIC_API_KEY"],
        network="sanduk-net",
    )


def without_hardening(engine, argv: list[str]) -> list[str]:
    """The spec-derived half of an argv. Hardening is per engine by design."""
    block = list(engine.hardening)
    start = argv.index(block[0])
    assert argv[start : start + len(block)] == block
    return argv[:start] + argv[start + len(block) :]


def test_the_two_engines_render_one_spec_identically():
    """run_argv is shared. Apple's engine adopted Docker's flag surface, so the
    day that stops being true, this fails rather than a container run. The
    hardening block is the one deliberate difference."""
    apple, docker = get_runtime("apple"), get_runtime("docker")
    a, d = apple.run_argv(one_spec()), docker.run_argv(one_spec())
    assert without_hardening(apple, a)[1:] == without_hardening(docker, d)[1:]
    assert (a[0], d[0]) == ("container", "docker")


@pytest.mark.parametrize("name", ["apple", "docker"])
def test_every_container_drops_its_capabilities(name):
    """The agent runs unprivileged and only reads, writes and forks; --init so
    a shell it leaves behind is reaped rather than held by pid 1."""
    argv = get_runtime(name).run_argv(one_spec())
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--init" in argv


def test_only_docker_bounds_pids_and_new_privileges():
    """A shared kernel is where these matter, and Apple's CLI has neither
    flag: passing them there would fail the run rather than harden it."""
    docker = get_runtime("docker").run_argv(one_spec())
    assert docker[docker.index("--security-opt") + 1] == "no-new-privileges"
    assert docker[docker.index("--pids-limit") + 1] == "1024"
    apple = get_runtime("apple").run_argv(one_spec())
    assert "--pids-limit" not in apple and "--security-opt" not in apple


def test_only_docker_explains_a_gateway_that_will_not_bind():
    """The VM case is the one --proxy failure a user cannot diagnose from the
    address alone."""
    assert "VM" in get_runtime("docker").gateway_hint
    assert get_runtime("apple").gateway_hint == ""


# --- listing containers -----------------------------------------------------


APPLE_LIST = (
    "ID                IMAGE           OS     ARCH   STATE    IP\n"
    "sanduk-e859b868   sanduk-hax:latest  linux  arm64  running  192.168.128.5/24\n"
    "sanduk-hold-0af2  sanduk-hax:latest  linux  arm64  running  192.168.128.2/24\n"
    "buildkit          builder:0.13.0     linux  arm64  running  192.168.64.2/24\n"
)
DOCKER_LIST = (
    "sanduk-e859b868\tsanduk-hax:latest\trunning\nbuildkit\tbuilder:0.13.0\trunning\n"
)


def test_apple_lists_containers_by_column(monkeypatch):
    responses(monkeypatch, stdout=APPLE_LIST)
    names = [c.name for c in get_runtime("apple").list_containers("sanduk-")]
    assert names == ["sanduk-e859b868", "sanduk-hold-0af2"]


def test_docker_lists_containers_by_format(monkeypatch):
    calls = responses(monkeypatch, stdout=DOCKER_LIST)
    found = get_runtime("docker").list_containers("sanduk-")
    assert [(c.name, c.image, c.state) for c in found] == [
        ("sanduk-e859b868", "sanduk-hax:latest", "running")
    ]
    assert "{{.Names}}\t{{.Image}}\t{{.State}}" in calls[0]


def test_an_empty_prefix_lists_everything(monkeypatch):
    responses(monkeypatch, stdout=DOCKER_LIST)
    assert len(get_runtime("docker").list_containers()) == 2


@pytest.mark.parametrize("engine", ["apple", "docker"])
def test_a_failed_listing_is_empty_not_an_error(monkeypatch, engine):
    """Teardown calls this. Raising there would strand a container."""
    responses(monkeypatch, returncode=1, stdout=DOCKER_LIST)
    assert get_runtime(engine).list_containers("sanduk-") == []


# --- images, networks, and the engine's own service --------------------------


@pytest.mark.parametrize(("engine", "verb"), [("apple", "delete"), ("docker", "rm")])
def test_the_delete_verb_covers_images_and_networks(monkeypatch, engine, verb):
    """One split, three resources: `container delete` against `docker rm`."""
    calls = responses(monkeypatch)
    e = get_runtime(engine)
    e.delete_image("sanduk:latest")
    e.delete_network("sanduk-net")
    assert calls[0][1:] == ["image", verb, "sanduk:latest"]
    assert calls[1][1:] == ["network", verb, "sanduk-net"]


def test_an_engine_without_a_service_command_says_so():
    """Docker's daemon belongs to launchd, systemd, or Desktop. Pretending to
    start it would fail somewhere less obvious."""
    for call in (get_runtime("docker").service_start, get_runtime("docker").service_stop):
        with pytest.raises(AgentboxError, match="managed outside sanduk"):
            call()


def test_apple_owns_its_service_commands(monkeypatch):
    calls = responses(monkeypatch)
    get_runtime("apple").service_start()
    assert calls[0] == ["container", "system", "start"]


def test_service_status_answers_rather_than_raising(monkeypatch):
    """`system status` exists to report a broken engine, so it must not need a
    working one."""
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    assert "not found on PATH" in get_runtime("docker").service_status()


def test_a_read_only_mount_is_spelled_the_way_both_engines_read_it():
    """`-v host:dest:ro` is Docker's alone; --mount readonly is shared. Measured
    against Apple's engine: a write into it is refused."""
    spec = one_spec()
    spec.mounts = [Mount(host=Path("/tmp/repo"), dest="/repo", ro=True)]
    for name in ("apple", "docker"):
        argv = get_runtime(name).run_argv(spec)
        assert argv[argv.index("--mount") + 1] == (
            "type=bind,source=/tmp/repo,target=/repo,readonly"
        )


def test_a_writable_extra_mount_leaves_readonly_off():
    spec = one_spec()
    spec.mounts = [Mount(host=Path("/tmp/repo"), dest="/repo")]
    argv = get_runtime().run_argv(spec)
    assert argv[argv.index("--mount") + 1] == "type=bind,source=/tmp/repo,target=/repo"


def test_the_holder_sleeps_as_long_as_it_is_told(monkeypatch):
    """It was a flat day, which is a ceiling nothing announced: a longer run
    lost its bridge mid-flight and failed as if the network had broken."""
    engine = get_runtime("apple")
    seen = []
    monkeypatch.setattr(engine, "run_argv", lambda spec: seen.append(spec) or ["true"])
    responses(monkeypatch, returncode=0)
    engine.hold_network_up("sanduk-net", "img", 3600)
    engine.hold_network_up("sanduk-net", "img")
    assert [spec.command for spec in seen] == [["3600"], [str(runtime.HOLDER_SECONDS)]]
    assert seen[0].entrypoint == "sleep"
