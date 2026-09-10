"""CLI tests: argument rules, the report instruction, teardown of stale state.

`main` returns an exit code rather than raising, so these assert on the code.
Every run here is a --dry-run: nothing is built and nothing is started.
"""

import argparse
import json
from types import SimpleNamespace

import pytest

from sanduk import assistants
from sanduk.agent import KEY_ENV, REPORT_NAME, Outcome
from sanduk.agents.claude import ClaudeCode
from sanduk.cli import (
    MAX_TIMEOUT,
    _collect_report,
    agent_stats,
    main,
    parse_args,
    parse_mounts,
)
from sanduk.errors import AgentboxError
from sanduk.runtime import Container

KEY = "sk-ant-api03-SECRET"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)


def test_task_and_task_file_are_mutually_exclusive(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing")
    assert main(["run", "task", "--task-file", str(brief)]) == 2


def test_a_task_is_required():
    assert main(["run"]) == 2


def test_a_command_is_required():
    with pytest.raises(SystemExit):
        main([])


def test_a_bare_task_names_the_run_command():
    """The first argument used to be the task. Inferring `run` from a word that
    matches no command would run `clean` for a task that says clean."""
    with pytest.raises(AgentboxError, match="sanduk run"):
        parse_args(["Summarise every file here."])


def test_an_empty_task_is_rejected(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("   \n")
    assert main(["run", "--task-file", str(brief)]) == 2


def test_missing_key_exits_before_anything_starts(monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert main(["run", "task"]) == 2


def test_unknown_runtime_is_rejected_by_argparse():
    with pytest.raises(SystemExit):
        main(["run", "task", "--runtime", "nope"])


def test_report_instruction_is_appended(tmp_path, capsys):
    main(["run", "summarise", "-w", str(tmp_path), "--dry-run"])
    assert REPORT_NAME in capsys.readouterr().out


def test_no_report_instruction_flag_suppresses_it(tmp_path, capsys):
    main(
        ["run", "summarise", "-w", str(tmp_path), "--dry-run", "--no-report-instruction"]
    )
    assert REPORT_NAME not in capsys.readouterr().out


def test_dry_run_does_not_write_a_task_file(tmp_path):
    """The prompt goes in via -p; a copy on the mount only confused the agent."""
    main(["run", "summarise", "-w", str(tmp_path), "--dry-run"])
    assert list(tmp_path.iterdir()) == []


def test_stale_report_is_removed_before_a_run(tmp_path):
    stale = tmp_path / REPORT_NAME
    stale.write_text("from a previous run")
    main(["run", "summarise", "-w", str(tmp_path), "--dry-run"])
    assert not stale.exists()


def test_boxagent_error_carries_its_own_exit_code():
    assert AgentboxError("timed out", code=124).code == 124


# --- provider selection and key isolation -----------------------------------
#
# Three keys are exported at once on the author's machine, so "which key does a
# run read" and "which one reaches the container" stop being the same question.

ALL_KEYS = {
    "ANTHROPIC_API_KEY": "sk-ant-SECRET",
    "OPENAI_API_KEY": "sk-openai-SECRET",
    "OPENROUTER_API_KEY": "sk-or-SECRET",
}


@pytest.fixture
def all_keys(monkeypatch):
    for name, value in ALL_KEYS.items():
        monkeypatch.setenv(name, value)
    return ALL_KEYS


@pytest.mark.parametrize(
    ("agent", "provider", "expected"),
    [
        ("claude", "anthropic", "ANTHROPIC_API_KEY"),
        ("hax", "openai", "OPENAI_API_KEY"),
        ("hax", "openrouter", "OPENROUTER_API_KEY"),
    ],
)
def test_the_provider_decides_which_key_is_read(
    monkeypatch, agent, provider, expected, capsys
):
    """With the other two still exported, removing the provider's own key must
    fail. Falling back to whichever key happens to be set would send the wrong
    credential to the wrong API."""
    for name, value in ALL_KEYS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(expected)
    assert main(["run", "task", "--agent", agent, "--provider", provider]) == 2
    assert expected in capsys.readouterr().err


def test_openai_compat_runs_without_any_key(monkeypatch, tmp_path, capsys):
    """A local llama-server has no credential; requiring one would block it."""
    for name in ALL_KEYS:
        monkeypatch.delenv(name, raising=False)
    code = main(
        [
            "run",
            "task",
            "-w",
            str(tmp_path),
            "--dry-run",
            "--agent",
            "hax",
            "--provider",
            "openai-compat",
            "--upstream",
            "http://127.0.0.1:8080",
        ]
    )
    assert code == 0, capsys.readouterr().err


def spec_for(flags, tmp_path):
    from sanduk.cli import build_spec, parse_args, relay_root, select

    args = parse_args(flags)
    sel = select(args)
    wiring = sel.agent.wire(args, sel.provider, relay_root(args))
    return build_spec(args, sel, wiring, "sanduk-test", tmp_path, "task")


def test_the_container_inherits_only_the_agent_variables(all_keys, tmp_path):
    """The other two keys stay on the host. The container's environment is the
    inherit list and nothing else. hax reads its own HAX_-prefixed names, which
    is what --agent-key-env exists to override."""
    spec = spec_for(
        [
            "run",
            "task",
            "-w",
            str(tmp_path),
            "--proxy",
            "--agent",
            "hax",
            "--provider",
            "openai",
        ],
        tmp_path,
    )
    assert spec.inherit_env == ["HAX_OPENAI_API_KEY", "HAX_OPENAI_BASE_URL"]
    for name in ALL_KEYS:
        assert name not in spec.inherit_env


def test_no_key_value_appears_in_the_container_argv(all_keys, tmp_path):
    """The bare-name -e form exists so values stay out of argv and out of ps."""
    from sanduk.runtime import get_runtime

    spec = spec_for(
        [
            "run",
            "task",
            "-w",
            str(tmp_path),
            "--proxy",
            "--agent",
            "hax",
            "--provider",
            "openai",
        ],
        tmp_path,
    )
    rendered = " ".join(get_runtime().run_argv(spec))
    for value in all_keys.values():
        assert value not in rendered


def test_agent_env_names_can_be_overridden(tmp_path, monkeypatch):
    """An agent that reads a different variable than the provider declares is
    pointed at the relay with a flag, not a new module."""
    from sanduk.cli import container_env_names, parse_args, resolve_provider

    args = parse_args(
        [
            "run",
            "task",
            "--agent",
            "hax",
            "--provider",
            "openrouter",
            "--agent-key-env",
            "MY_TOKEN",
            "--agent-base-url-env",
            "MY_BASE_URL",
        ]
    )
    names = container_env_names(args, resolve_provider(args))
    assert names == ("MY_TOKEN", "MY_BASE_URL")


def test_agent_env_names_default_to_the_provider(tmp_path):
    from sanduk.cli import container_env_names, parse_args, resolve_provider

    args = parse_args(["run", "task", "--provider", "anthropic"])
    names = container_env_names(args, resolve_provider(args))
    assert names == ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")


# --- list -------------------------------------------------------------------


def test_list_agents_shows_what_each_one_speaks(capsys):
    assert main(["list", "agents"]) == 0
    out = capsys.readouterr().out
    assert "claude" in out and "sanduk-hax:latest" in out
    assert "openai-chat" in out


def test_list_providers_shows_the_url_an_agent_must_be_given(capsys):
    """The prefix is the field a handler gets wrong, so it is what is printed."""
    assert main(["list", "providers"]) == 0
    out = capsys.readouterr().out
    assert "https://openrouter.ai/api/v1" in out
    assert "https://api.anthropic.com/v1" in out


def test_list_providers_names_the_one_needing_no_key(capsys):
    main(["list", "providers"])
    assert "(no key needed)" in capsys.readouterr().out


def test_list_runtimes_reports_what_is_installed(capsys):
    assert main(["list", "runtimes"]) == 0
    out = capsys.readouterr().out
    assert "apple" in out and "docker" in out
    assert "installed" in out


def test_an_unknown_axis_is_rejected():
    with pytest.raises(SystemExit):
        main(["list", "engines"])


def test_list_takes_no_engine_flags():
    """It reads registries; naming a runtime would imply it contacts one."""
    with pytest.raises(SystemExit):
        main(["list", "agents", "--runtime", "docker"])


# --- engine commands --------------------------------------------------------
#
# No engine is contacted. A stub records what each verb asked for, which is the
# whole of what these commands do beyond printing.


class StubEngine:
    cli = "stub"

    def __init__(self, containers=(), images=()):
        self.containers = list(containers)
        self.images = set(images)
        self.built, self.stopped, self.destroyed = [], [], []
        self.deleted_images, self.deleted_networks, self.service = [], [], []

    def require(self):
        pass

    def image_exists(self, image):
        return image in self.images

    def build_image(self, image, containerfile):
        self.built.append((image, containerfile))

    def list_containers(self, prefix=""):
        return [c for c in self.containers if c.name.startswith(prefix)]

    def stop(self, name):
        self.stopped.append(name)

    def destroy(self, name, keep=False):
        self.destroyed.append(name)

    def shell_argv(self, image):
        return ["stub", "run", "--rm", "-it", image]

    def delete_image(self, image):
        self.deleted_images.append(image)

    def delete_network(self, name):
        self.deleted_networks.append(name)

    def service_status(self):
        return "stub is running"

    def service_start(self):
        self.service.append("start")

    def service_stop(self):
        self.service.append("stop")


@pytest.fixture
def engine(monkeypatch):
    stub = StubEngine()
    monkeypatch.setattr("sanduk.cli.get_runtime", lambda _: stub)
    return stub


def running(*names):
    return [Container(name=n, image="sanduk:latest", state="running") for n in names]


def test_build_uses_the_agents_image_and_containerfile(engine):
    assert main(["build", "--agent", "hax"]) == 0
    image, containerfile = engine.built[0]
    assert image == "sanduk-hax:latest"
    assert containerfile.name == "Containerfile.hax"


def test_build_is_a_no_op_when_the_image_exists(engine, capsys):
    engine.images.add("sanduk:latest")
    assert main(["build"]) == 0
    assert engine.built == []
    assert "already built" in capsys.readouterr().err


def test_force_rebuilds_an_existing_image(engine):
    engine.images.add("sanduk:latest")
    main(["build", "--force"])
    assert engine.built == [("sanduk:latest", ClaudeCode.containerfile)]


def test_ps_prints_one_row_per_container(engine, capsys):
    engine.containers = running("sanduk-a1b2", "sanduk-hold-c3")
    assert main(["ps"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert out[0].split() == ["sanduk-a1b2", "sanduk:latest", "running"]


def test_ps_leaves_stdout_empty_when_there_are_none(engine, capsys):
    assert main(["ps"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no sanduk containers" in captured.err


def test_stop_leaves_stopped_containers_alone(engine):
    engine.containers = [
        *running("sanduk-a1b2"),
        Container(name="sanduk-dead", image="sanduk:latest", state="stopped"),
    ]
    main(["stop"])
    assert engine.stopped == ["sanduk-a1b2"]


def test_clean_deletes_running_and_stopped_alike(engine):
    engine.containers = [
        *running("sanduk-a1b2"),
        Container(name="sanduk-dead", image="sanduk:latest", state="stopped"),
    ]
    main(["clean"])
    assert engine.destroyed == ["sanduk-a1b2", "sanduk-dead"]


def test_only_the_sanduk_prefix_is_touched(engine):
    """A container the user named is not sanduk's to delete."""
    engine.containers = running("sanduk-a1b2", "buildkit", "my-postgres")
    main(["clean"])
    assert engine.destroyed == ["sanduk-a1b2"]


def test_shell_says_how_to_build_a_missing_image(engine, capsys):
    assert main(["shell", "--agent", "hax"]) == 2
    assert "sanduk build --agent hax" in capsys.readouterr().err


def test_shell_hands_the_terminal_a_tty_argv(engine, monkeypatch):
    """It cannot go through run_argv, which builds no -it, nor through launch,
    which reads stdout as a JSON stream."""
    engine.images.add("sanduk:latest")
    seen = []
    monkeypatch.setattr("sanduk.cli.subprocess.call", lambda argv: seen.append(argv) or 0)
    assert main(["shell"]) == 0
    assert "-it" in seen[0]


def test_destroy_removes_the_containers_image_and_network(engine):
    engine.containers = running("sanduk-a1b2")
    assert main(["destroy", "--agent", "hax", "--proxy-network", "sanduk-ci"]) == 0
    assert engine.destroyed == ["sanduk-a1b2"]
    assert engine.deleted_images == ["sanduk-hax:latest"]
    assert engine.deleted_networks == ["sanduk-ci", "sanduk-open", "sanduk-net"]


def test_destroy_leaves_the_request_body_log_alone(engine, tmp_path, monkeypatch):
    """It is written outside the bind mount so the agent cannot edit its own
    audit trail. A cleanup verb deleting it would undo that."""
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "sanduk-logs"
    logs.mkdir()
    (logs / "0001.json").write_text("{}")
    main(["destroy"])
    assert (logs / "0001.json").exists()


def test_system_status_reports_without_requiring_a_running_engine(engine, capsys):
    assert main(["system", "status"]) == 0
    assert capsys.readouterr().out.strip() == "stub is running"


@pytest.mark.parametrize("action", ["start", "stop"])
def test_system_passes_start_and_stop_through(engine, action):
    assert main(["system", action]) == 0
    assert engine.service == [action]


# --- ownership and the assistant commands -----------------------------------


@pytest.fixture
def owned(monkeypatch):
    """One container a live run claims; the rest are nobody's."""
    monkeypatch.setattr("sanduk.cli.live_containers", lambda: {"sanduk-live"})


def test_clean_leaves_a_container_a_live_run_is_using(engine, owned, capsys):
    """A wakeup on a schedule is running while someone types `make clean`."""
    engine.containers = running("sanduk-live", "sanduk-old")
    assert main(["clean"]) == 0
    assert engine.destroyed == ["sanduk-old"]
    assert "sanduk-live" in capsys.readouterr().err


def test_clean_all_deletes_it_anyway(engine, owned):
    """For a run whose process is wedged rather than working."""
    engine.containers = running("sanduk-live", "sanduk-old")
    assert main(["clean", "--all"]) == 0
    assert engine.destroyed == ["sanduk-live", "sanduk-old"]


def test_stop_leaves_a_container_a_live_run_is_using(engine, owned):
    engine.containers = running("sanduk-live", "sanduk-old")
    assert main(["stop"]) == 0
    assert engine.stopped == ["sanduk-old"]


@pytest.fixture
def assistant_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    d = tmp_path / "triage"
    d.mkdir()
    (d / "assistant.toml").write_text('agent = "pi"\nmodel = "m"\nbrief = "b.md"\n')
    (d / "b.md").write_text("Triage.")
    return d


def test_an_assistant_is_registered_by_its_directory(assistant_dir, capsys):
    assert main(["assistant", "add", str(assistant_dir)]) == 0
    assert main(["assistant", "list"]) == 0
    assert "triage" in capsys.readouterr().out


def test_a_message_is_queued_for_the_next_wakeup(assistant_dir, capsys):
    main(["assistant", "add", str(assistant_dir)])
    assert main(["tell", "triage", "look", "at", "PR", "12"]) == 0
    assert main(["assistant", "show", "triage"]) == 0
    assert '"pending": 1' in capsys.readouterr().out


def test_telling_an_unknown_assistant_is_an_error(assistant_dir):
    assert main(["tell", "nope", "hello"]) == 2


def test_an_empty_outbox_and_history_are_not_errors(assistant_dir, capsys):
    main(["assistant", "add", str(assistant_dir)])
    assert main(["outbox"]) == 0
    assert main(["runs"]) == 0
    err = capsys.readouterr().err
    assert "nothing in the outbox" in err and "no wakeups recorded" in err


def test_runs_says_why_a_wakeup_failed(assistant_dir, capsys):
    assistants.connect().execute(
        "INSERT INTO runs (name, started_at, ended_at, exit_code, error) "
        "VALUES ('triage', 1, 121, 124, 'agent exceeded --timeout 120s')"
    )
    assert main(["runs"]) == 0
    assert "agent exceeded --timeout 120s" in capsys.readouterr().out


def test_the_stats_file_records_what_a_run_cost(tmp_path):
    """The exit code is all `main` returns and the token line is printed, so a
    caller recording what a wakeup cost has nowhere else to read it."""
    stats = tmp_path / "stats.json"
    (tmp_path / REPORT_NAME).write_text("done")
    args = argparse.Namespace(report=None, stats_file=stats)
    outcome = Outcome(ok=True, text="", error="", stats="10 in / 2 out")
    assert _collect_report(args, tmp_path, outcome, 0) == 0
    found = json.loads(stats.read_text())
    assert found["stats"] == "10 in / 2 out"
    assert found["ok"] is True and found["exit"] == 0
    assert found["report"] == str(tmp_path / REPORT_NAME)


def test_the_stats_file_of_a_run_that_reported_nothing(tmp_path):
    stats = tmp_path / "stats.json"
    args = argparse.Namespace(report=None, stats_file=stats)
    assert _collect_report(args, tmp_path, None, 124) == 124
    found = json.loads(stats.read_text())
    assert found == {
        "exit": 124,
        "ok": False,
        "stats": "",
        "error": "the agent exited without a final result",
        "report": None,
    }


# --- extra mounts -----------------------------------------------------------


def test_a_mount_is_read_write_unless_it_says_otherwise(tmp_path):
    mounts = parse_mounts([f"{tmp_path}:/repo", f"{tmp_path}:/notes:ro"])
    assert [(m.dest, m.ro) for m in mounts] == [("/repo", False), ("/notes", True)]
    assert mounts[0].host == tmp_path


def test_a_relative_host_path_is_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    assert parse_mounts(["repo:/repo"])[0].host == tmp_path / "repo"


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        ("/tmp", "HOST:DEST"),
        ("/tmp:/a:rx", "mode is ro or rw"),
        ("/tmp:/a:ro:extra", "HOST:DEST"),
        (":/a", "HOST:DEST"),
        ("/nowhere-at-all:/a", "not a directory"),
        ("/tmp:relative", "not an absolute path"),
        ("/tmp:/", "not an absolute path"),
    ],
)
def test_a_mount_that_cannot_be_meant_is_refused(spec, why):
    with pytest.raises(AgentboxError, match=why):
        parse_mounts([spec])


@pytest.mark.parametrize("dest", ["/work", "/work/sub"])
def test_a_mount_may_not_shadow_the_working_directory(tmp_path, dest):
    """It would hide part of what -w put there: a run reading the wrong files
    rather than one that fails."""
    with pytest.raises(AgentboxError, match="where -w lands"):
        parse_mounts([f"{tmp_path}:{dest}"])


def test_one_destination_holds_one_directory(tmp_path):
    with pytest.raises(AgentboxError, match="already holds"):
        parse_mounts([f"{tmp_path}:/a", f"{tmp_path}:/a"])


def test_an_extra_mount_reaches_the_container_argv(tmp_path, capsys):
    assert main(["run", "task", "-w", str(tmp_path), "--mount", f"{tmp_path}:/repo:ro",
                 "--dry-run"]) == 0  # fmt: skip
    rendered = capsys.readouterr().out
    assert f"type=bind,source={tmp_path},target=/repo,readonly" in rendered


def test_approve_and_reject_name_what_they_changed(assistant_dir, capsys):
    """Ids come from the outbox listing, so both verbs take numbers."""
    main(["assistant", "add", str(assistant_dir)])
    db = __import__("sanduk.assistants", fromlist=["x"]).connect()
    db.execute(
        "INSERT INTO outbox (name, run_id, created_at, body) VALUES "
        "('triage', 1, 1, 'first'), ('triage', 1, 2, 'second')"
    )
    db.execute("UPDATE outbox SET approved_at = NULL")
    assert main(["approve", "1"]) == 0
    assert main(["reject", "2"]) == 0
    assert main(["outbox"]) == 0
    printed = capsys.readouterr().out
    assert "[1] triage run 1" in printed and "approved" in printed
    assert "rejected" in printed
    assert main(["outbox", "--pending"]) == 0


def test_approving_something_already_decided_says_so(assistant_dir, capsys):
    main(["assistant", "add", str(assistant_dir)])
    db = __import__("sanduk.assistants", fromlist=["x"]).connect()
    db.execute(
        "INSERT INTO outbox (name, run_id, created_at, body, approved_at) VALUES "
        "('triage', 1, 1, 'first', 5)"
    )
    assert main(["approve", "1"]) == 0
    assert "already" in capsys.readouterr().err


# --- containment modes ------------------------------------------------------


def test_the_default_mode_relays_nothing():
    """The container holds the key and reaches anything: filesystem isolation
    and nothing more."""
    args = parse_args(["run", "task"])
    assert args.mode == "open"
    assert args.proxy is False and args.egress is True


@pytest.mark.parametrize(
    ("mode", "relayed", "egress", "network"),
    [
        ("open", False, True, "sanduk-net"),
        ("key-safe", True, True, "sanduk-open"),
        ("sealed", True, False, "sanduk-net"),
    ],
)
def test_each_mode_is_two_properties(mode, relayed, egress, network):
    args = parse_args(["run", "task", "--mode", mode])
    assert (args.proxy, args.egress) == (relayed, egress)
    assert args.proxy_network == network


def test_proxy_is_the_old_spelling_of_sealed():
    """A command line written against 0.2.x still runs."""
    args = parse_args(["run", "task", "--proxy"])
    assert args.mode == "sealed"
    assert args.proxy is True and args.egress is False


def test_the_old_spelling_cannot_contradict_the_new_one():
    with pytest.raises(AgentboxError, match="old spelling"):
        parse_args(["run", "task", "--proxy", "--mode", "open"])


def test_an_unknown_mode_is_refused_by_argparse():
    with pytest.raises(SystemExit):
        parse_args(["run", "task", "--mode", "airgapped"])


def test_a_named_network_survives_the_mode_that_would_pick_one(tmp_path):
    args = parse_args(["run", "task", "--mode", "key-safe", "--proxy-network", "mine"])
    assert args.proxy_network == "mine"


def test_only_sealed_asks_for_a_network_with_no_route_off_the_host(tmp_path, monkeypatch):
    """key-safe is the same relay on a routable network: the key stays here and
    the container still reaches the internet."""
    seen = {}

    class Engine(StubEngine):
        def ensure_network(self, name, internal=True):
            seen[name] = internal
            return "10.0.0.1", "10.0.0.0/24"

        def run_argv(self, spec):
            return ["stub", "run", spec.image]

    stub = Engine()
    monkeypatch.setattr("sanduk.cli.get_runtime", lambda _: stub)
    for mode in ("sealed", "key-safe"):
        main(["run", "task", "-w", str(tmp_path), "--mode", mode, "--dry-run"])
    assert seen == {"sanduk-net": True, "sanduk-open": False}


def test_destroy_takes_every_network_a_mode_creates(engine):
    """A run in one mode and a destroy in another used to leave the other
    mode's bridge behind."""
    assert main(["destroy"]) == 0
    assert set(engine.deleted_networks) == {"sanduk-net", "sanduk-open"}


# --- how long a run may last ------------------------------------------------


def test_a_timeout_longer_than_the_holder_would_sit_for_is_refused():
    """The holder is started for the run's length, so a typo here parks a
    container for months."""
    with pytest.raises(AgentboxError, match="longer than a week"):
        parse_args(["run", "task", "--timeout", str(MAX_TIMEOUT + 1)])
    assert parse_args(["run", "task", "--timeout", "7d"]).timeout == MAX_TIMEOUT


@pytest.mark.parametrize("value", ["0", "-30", "5 minutes", "1w"])
def test_a_timeout_that_is_not_a_duration_is_refused(value):
    with pytest.raises(AgentboxError, match="duration"):
        parse_args(["run", "task", "--timeout", value])


@pytest.mark.parametrize(("value", "expected"), [("90", 90), ("15m", 900), ("2h", 7200)])
def test_a_timeout_carries_its_unit_or_means_seconds(value, expected):
    assert parse_args(["run", "task", "--timeout", value]).timeout == expected


def test_the_holder_is_started_for_longer_than_the_run(tmp_path, monkeypatch):
    """It going first takes the bridge, and the relay's address, with it."""
    held = {}

    class Engine(StubEngine):
        gateway_hint = ""

        def ensure_network(self, name, internal=True):
            return "10.0.0.1", "10.0.0.0/24"

        def hold_network_up(self, network, image, seconds=None):
            held["seconds"] = seconds
            return None

        def run_argv(self, spec):
            return ["stub", "run", spec.image]

    monkeypatch.setattr("sanduk.cli.get_runtime", lambda _: Engine())
    argv = ["run", "task", "-w", str(tmp_path), "--mode", "sealed"]
    # The run stops at the gateway this stub cannot make bindable; the holder
    # is started before that, which is what this asserts.
    assert main([*argv, "--timeout", "60", "--skip-key-check"]) != 0
    assert held["seconds"] > 60


# --- OCI runtime ------------------------------------------------------------


def test_an_oci_runtime_is_refused_where_there_is_none_to_swap(tmp_path, capsys):
    """Apple's engine runs each container as its own VM already."""
    argv = ["run", "task", "-w", str(tmp_path), "--oci-runtime", "runsc"]
    assert main([*argv, "--dry-run"]) == 2
    assert "--runtime docker" in capsys.readouterr().err


def test_an_oci_runtime_reaches_the_docker_argv(tmp_path, capsys):
    argv = ["run", "task", "-w", str(tmp_path), "--runtime", "docker"]
    assert main([*argv, "--oci-runtime", "runsc", "--dry-run"]) == 0
    assert "--runtime runsc" in capsys.readouterr().out


# --- cost budget ------------------------------------------------------------


def test_a_budget_needs_a_provider_that_reports_cost(tmp_path, monkeypatch, capsys):
    """Anthropic and OpenAI report tokens; pricing them would mean a table this
    package would have to keep current."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    argv = ["run", "task", "-w", str(tmp_path), "--mode", "sealed", "--budget", "5"]
    assert main([*argv, "--provider", "openai", "--dry-run"]) == 2
    assert "reports tokens, not cost" in capsys.readouterr().err


def test_a_budget_needs_the_relay(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    argv = ["run", "task", "-w", str(tmp_path), "--provider", "openrouter"]
    assert main([*argv, "--budget", "5", "--mode", "open", "--dry-run"]) == 2
    assert "--mode open does not start" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_budget_that_cannot_be_spent_is_refused(value):
    with pytest.raises(AgentboxError, match="positive"):
        parse_args(["run", "task", "--budget", value])


def relay_that_spent(amount, reports_cost=True):
    provider = SimpleNamespace(cost_field="cost" if reports_cost else None)
    return SimpleNamespace(cfg=SimpleNamespace(spent=amount, provider=provider))


def test_the_agents_own_cost_goes_when_the_relay_has_a_real_one():
    """The agent prices a run from its catalogue, which never held the model id
    the relay hands it, so it reports $0.0000 under the relay's real figure."""
    stats = "6,394 in (0 cached) / 306 out, $0.0000"
    assert agent_stats(stats, relay_that_spent(0.0396)) == "6,394 in (0 cached) / 306 out"


def test_an_agents_real_cost_is_left_alone():
    stats = "23,398 in (15,381 cached) / 864 out, $0.1200"
    assert agent_stats(stats, relay_that_spent(0.0396)) == stats


@pytest.mark.parametrize(
    "relay", [None, relay_that_spent(0.0), relay_that_spent(0.5, reports_cost=False)]
)
def test_nothing_is_stripped_without_a_figure_to_prefer(relay):
    stats = "10 in / 2 out, $0.0000"
    assert agent_stats(stats, relay) == stats
