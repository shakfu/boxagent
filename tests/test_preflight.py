"""Preflight tests: firewall listing parse, key validation.

No containers and no network: `run` and `urlopen` are monkeypatched.
"""

import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from sanduk import preflight
from sanduk.errors import AgentboxError

KEY = "sk-ant-api03-SECRET"


def test_firewall_entries_pair_paths_with_state(monkeypatch):
    listing = (
        "1 : /opt/homebrew/.../Python.app \n"
        "             (Block incoming connections)\n"
        "2 : /usr/bin/python3 \n"
        "             (Allow incoming connections)\n"
    )
    monkeypatch.setattr(
        preflight, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=listing)
    )
    entries = dict(preflight._firewall_entries())
    assert entries["/opt/homebrew/.../Python.app"] is True
    assert entries["/usr/bin/python3"] is False


def test_validate_key_fails_on_401(monkeypatch):
    """A bad key must fail here in milliseconds, not after ~174s of in-container
    retry backoff."""

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(AgentboxError):
        preflight.validate_key(KEY, "https://api.anthropic.com")


def test_validate_key_accepts_other_statuses(monkeypatch):
    """A 500 proves the endpoint answered; let the agent try."""

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 500, "Server Error", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert preflight.validate_key(KEY, "https://api.anthropic.com")


def test_validate_key_fails_when_unreachable(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(AgentboxError):
        preflight.validate_key(KEY, "http://192.168.128.1:9")
