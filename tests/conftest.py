import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """A test that forgot XDG_STATE_HOME wrote run records into the real
    ~/.local/state/sanduk. A test that sets its own still overrides this."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
