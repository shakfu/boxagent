"""CLI tests: argument rules, the report instruction, teardown of stale state.

`main` returns an exit code rather than raising, so these assert on the code.
Every run here is a --dry-run: nothing is built and nothing is started.
"""

import pytest

from sanduk.agent import KEY_ENV, REPORT_NAME
from sanduk.agents.claude import ClaudeCode
from sanduk.cli import main, parse_args
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
    assert engine.deleted_networks == ["sanduk-ci"]


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
