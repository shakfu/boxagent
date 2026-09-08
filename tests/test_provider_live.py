"""Live provider tests: a real upstream, over the real network path.

Marked `provider_live` and deselected by default, because each needs something
the fast suite does not: a running server, or a key.

openai-compat is the one that costs nothing. Point LLAMA_SERVER at a local
llama-server and the whole relay path is exercised for free:

    llama-server -hf ggml-org/Qwen3-0.6B-GGUF --port 8080
    LLAMA_SERVER=http://127.0.0.1:8080 make test-live
"""

import json
import os
import urllib.error
import urllib.request

import pytest

from sanduk.errors import AgentboxError
from sanduk.preflight import validate_key
from sanduk.providers import (
    ANTHROPIC_PROVIDER,
    OPENAI_COMPAT_PROVIDER,
    OPENAI_PROVIDER,
    OPENROUTER_PROVIDER,
    parse_upstream,
)
from sanduk.proxy import start_proxy

pytestmark = pytest.mark.provider_live

LLAMA_SERVER = os.environ.get("LLAMA_SERVER", "")
TOKEN = "run-token-for-live-tests"


def _reachable(url):
    try:
        with urllib.request.urlopen(f"{url}/v1/models", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


needs_llama = pytest.mark.skipif(
    not LLAMA_SERVER or not _reachable(LLAMA_SERVER),
    reason="set LLAMA_SERVER to a running llama-server, e.g. http://127.0.0.1:8080",
)


@pytest.fixture
def relay():
    scheme, host = parse_upstream(LLAMA_SERVER)
    provider = OPENAI_COMPAT_PROVIDER
    assert provider.scheme == scheme, "openai-compat expects a plaintext upstream"
    srv, port = start_proxy("", TOKEN, "127.0.0.1", upstream=host, provider=provider)
    yield port
    srv.shutdown()


def post(port, path, payload, token=TOKEN):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def completion(model="local-model", **extra):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        **extra,
    }


@needs_llama
def test_a_completion_round_trips(relay):
    """The whole path: bearer token checked, plaintext upstream, real model."""
    status, body = post(relay, "/v1/chat/completions", completion())
    assert status == 200, body
    assert body["choices"][0]["message"]["content"].strip()


@needs_llama
def test_usage_comes_back(relay):
    """A protocol whose counters the relay cannot read logs usage=?, so this is
    what proves openai-chat's field names are right against a real server."""
    status, body = post(relay, "/v1/chat/completions", completion())
    assert status == 200, body
    assert body["usage"]["prompt_tokens"] > 0
    assert body["usage"]["completion_tokens"] > 0


@needs_llama
def test_a_wrong_token_is_rejected_before_the_upstream(relay):
    status, _ = post(relay, "/v1/chat/completions", completion(), token="guessed")
    assert status == 401


@needs_llama
def test_a_path_outside_the_allowlist_is_rejected(relay):
    """llama-server serves /completion too; the allowlist must not."""
    status, _ = post(relay, "/completion", {"prompt": "hi"})
    assert status == 403


@needs_llama
def test_the_model_allowlist_is_enforced_against_a_real_server(relay):
    _, host = parse_upstream(LLAMA_SERVER)
    srv, port = start_proxy(
        "",
        TOKEN,
        "127.0.0.1",
        upstream=host,
        provider=OPENAI_COMPAT_PROVIDER,
        allow_models={"only-this-one"},
    )
    try:
        status, _ = post(port, "/v1/chat/completions", completion("something-else"))
        assert status == 403
    finally:
        srv.shutdown()


@needs_llama
def test_the_cap_reaches_a_real_server(relay):
    """--max-tokens-cap is enforced on the host. This proves the clamped field
    is the one the server actually honours."""
    _, host = parse_upstream(LLAMA_SERVER)
    srv, port = start_proxy(
        "",
        TOKEN,
        "127.0.0.1",
        upstream=host,
        provider=OPENAI_COMPAT_PROVIDER,
        max_tokens_cap=8,
    )
    try:
        status, body = post(
            port,
            "/v1/chat/completions",
            completion(max_completion_tokens=4096)
            | {"messages": [{"role": "user", "content": "Count slowly to fifty."}]},
        )
        assert status == 200, body
        assert body["usage"]["completion_tokens"] <= 8, body["usage"]
    finally:
        srv.shutdown()


# --- validate paths, no credential required ---------------------------------
#
# These reach the real endpoints with a deliberately invalid key. They cost
# nothing, need nothing exported, and prove the one thing a wrong validate_path
# breaks: a bad key sailing through the preflight.

BAD = "definitely-not-a-real-key"


@pytest.mark.parametrize(
    "provider",
    [ANTHROPIC_PROVIDER, OPENAI_PROVIDER, OPENROUTER_PROVIDER],
    ids=lambda p: p.name,
)
def test_a_bad_key_is_rejected_by_the_real_endpoint(provider):
    """The preflight exists because a bad key costs ~174s of in-container retry
    backoff. It only pays off if the endpoint actually refuses one."""
    url = f"{provider.scheme}://{provider.host}"
    with pytest.raises(AgentboxError, match=provider.key_env):
        validate_key(BAD, url, provider)


def test_openrouter_models_would_have_passed_the_bad_key():
    """Why validate_path is /api/v1/key. /api/v1/models answers 200 with no
    credential, so validating there would accept anything and the preflight
    would be decoration."""
    import dataclasses

    on_models = dataclasses.replace(OPENROUTER_PROVIDER, validate_path="/api/v1/models")
    assert validate_key(BAD, "https://openrouter.ai", on_models) is True


# --- paid smoke tests -------------------------------------------------------
#
# One tiny completion each, through the relay, against the real API.
#
# OpenRouter defaults to openrouter/free, a router that picks a free model at
# random, so that half runs on a key alone and costs nothing. A pinned :free id
# would work too but they rotate out, which fails the test for a reason that
# has nothing to do with sanduk. OpenAI has no free tier, so it stays opt-in
# through OPENAI_MODEL and nothing is spent by accident.
#
#     OPENAI_MODEL=<a cheap model> make test-live


def _live_relay(provider, key):
    srv, port = start_proxy(key, TOKEN, "127.0.0.1", provider=provider)
    return srv, port


@pytest.mark.parametrize(
    ("provider", "model_env", "path", "default_model"),
    [
        (OPENAI_PROVIDER, "OPENAI_MODEL", "/v1/chat/completions", ""),
        (
            OPENROUTER_PROVIDER,
            "OPENROUTER_MODEL",
            "/api/v1/chat/completions",
            "openrouter/free",
        ),
    ],
    ids=["openai", "openrouter"],
)
def test_a_real_completion_round_trips(provider, model_env, path, default_model):
    key = os.environ.get(provider.key_env, "")
    model = os.environ.get(model_env, "") or default_model
    if not key:
        pytest.skip(f"set {provider.key_env}")
    if not model:
        pytest.skip(f"set {model_env}; this provider has no free default")
    srv, port = _live_relay(provider, key)
    try:
        status, body = post(
            port,
            path,
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with: ok"}],
                "max_completion_tokens": 16,
            },
        )
        assert status == 200, body
        assert body["usage"]["prompt_tokens"] > 0
        assert body["choices"][0]["message"]["content"].strip()
    finally:
        srv.shutdown()


@pytest.mark.parametrize(
    ("provider", "path"),
    [
        (OPENAI_PROVIDER, "/v1/chat/completions"),
        (OPENROUTER_PROVIDER, "/api/v1/chat/completions"),
    ],
    ids=["openai", "openrouter"],
)
def test_the_real_key_reaches_the_api_and_the_token_does_not(provider, path):
    """A 401 from the real endpoint when the relay holds no key is what proves
    the request arrived carrying ours in the working case."""
    key = os.environ.get(provider.key_env, "")
    if not key:
        pytest.skip(f"set {provider.key_env}")
    srv, port = _live_relay(provider, BAD)
    try:
        status, _ = post(port, path, {"model": "x", "messages": []})
        assert status in (401, 403), status
    finally:
        srv.shutdown()


@needs_llama
def test_a_streamed_completion_reports_usage(relay):
    """Without stream_options.include_usage an OpenAI-shaped stream carries no
    counts at all, and the relay's log line reads usage=? on every call."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{relay}/v1/chat/completions",
        data=json.dumps(completion() | {"stream": True}).encode(),
        headers={
            "authorization": f"Bearer {TOKEN}",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        assert r.status == 200
        body = r.read().decode()
    counted = [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ") and '"usage"' in line and "[DONE]" not in line
    ]
    usage = [e["usage"] for e in counted if isinstance(e.get("usage"), dict)]
    assert usage, f"no usage in the stream: {body[:400]}"
    assert usage[-1]["prompt_tokens"] > 0
    assert usage[-1]["completion_tokens"] > 0
