# Multi-provider implementation plan

Status: proposed, 2026-09-07. Target: 0.2.0.

Scope: the relay learns four providers. The container engine stays Apple `container` only. No agent registry (see "Any agent", below).

Acceptance bar, set by the project owner: no provider ships without a live integration test.

## Why provider and protocol are not the same thing

OpenAI serves two wire protocols on different paths. Usage field names follow the protocol, not the provider, so a provider alone does not say how to read a response.

| Protocol | Input | Output | Cached |
| --- | --- | --- | --- |
| Anthropic Messages | `input_tokens` | `output_tokens` | `cache_read_input_tokens` |
| OpenAI Responses | `input_tokens` | `output_tokens` | `input_tokens_details.cached_tokens` |
| OpenAI Chat | `prompt_tokens` | `completion_tokens` | `prompt_tokens_details.cached_tokens` |

Anthropic Messages and OpenAI Responses use identical top-level names. Detecting the protocol from a response body is therefore impossible. It must be declared.

Reference: <https://developers.openai.com/api/docs/guides/reasoning>

## The four providers

| | anthropic | openai | openrouter | openai-compat |
| --- | --- | --- | --- | --- |
| Upstream | `api.anthropic.com` | `api.openai.com` | `openrouter.ai` | `--upstream` |
| Scheme | https | https | https | http if loopback |
| Auth header | `x-api-key` | `Authorization: Bearer` | `Authorization: Bearer` | Bearer, or none |
| Host key env | `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` | `OPENROUTER_API_KEY` | optional |
| Validate path | `GET /v1/models` | `GET /v1/models` | `GET /api/v1/models` | skip if no key |

OpenRouter's base URL is `https://openrouter.ai/api/v1`. Its paths carry the `/api/v1` prefix: `/api/v1/chat/completions`, not `/v1/chat/completions`.

Reference: <https://openrouter.ai/docs/api-reference/overview>

## Design: fold the protocol into the allowlist

`DEFAULT_ALLOW` (`src/sanduk/proxy.py:29`) becomes `dict[path, protocol | None]`
instead of `frozenset[path]`. One structure answers three questions:

- `Handler.authorized` (`proxy.py:183`): membership. Exact match, unchanged.

- `Handler.apply_policy` (`proxy.py:192`): which field to clamp.

- `UsageSniffer`: which field names to read.

`None` marks a path with no request body and no usage, such as `/v1/models`.

```python
"openai": {
    "/v1/responses":        "openai-responses",
    "/v1/chat/completions": "openai-chat",
    "/v1/models":           None,
}
```

Exact matching is retained. `/v1/models` as a prefix would also admit `/v1/models-internal-secret`.

## Changes

### 1. Upstream becomes scheme, host and port

`proxy.py:277` hardcodes `http.client.HTTPSConnection`. A local llama-server is `http://127.0.0.1:8080`.

Refuse plaintext unless the host is a loopback address. A remote `http://` upstream sends the API key in cleartext. An explicit `--insecure-upstream` overrides.

The upstream carries no path. Prefixes belong in the allowlist and in the container's base URL. llama-server example: upstream `http://127.0.0.1:8080`, allowlist `/v1/chat/completions`, container base URL `http://<gateway>:<port>/v1`.

### 2. `authorized` reads the provider's auth header

`proxy.py:184` reads `x-api-key` only. `STRIP_REQ` (`proxy.py:47`) already strips both `x-api-key` and `authorization`, so the outbound side is correct. The gap is inbound: an OpenAI-shaped agent sends the run token as `Authorization: Bearer`.

Check the declared header only. Reject with 401 naming the expected header. An agent configured into the wrong header must fail legibly.

### 3. Per-protocol cap field

| Protocol | Field |
| --- | --- |
| Anthropic Messages | `max_tokens` |
| OpenAI Responses | `max_output_tokens` |
| OpenAI Chat | `max_completion_tokens`, legacy `max_tokens` |

Keep the existing semantic: a cap inserts the field when it is absent.

### 4. `UsageSniffer` takes a field map and reads nested keys

`proxy.py:145` keeps top-level ints only:

```python
self.usage.update({k: v for k, v in found.items() if isinstance(v, int)})
```

`prompt_tokens_details.cached_tokens` is nested, so it is dropped silently.

### 5. Inject `stream_options.include_usage` for OpenAI-shaped requests

A streamed OpenAI call reports no usage unless the request sets `stream_options: {"include_usage": true}`. Usage then arrives on the final chunk, whose `choices` array is empty. Inject it in `apply_policy`, beside the existing cap rewrite.

OpenRouter does not need the injection. It returns usage in the final chunk unprompted, and puts a non-empty `choices` array in that chunk, which the parser must tolerate.

References: <https://community.openai.com/t/usage-stats-now-available-when-using-streaming-with-the-chat-completions-api-or-completions-api/738156>, <https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events>

### 6. `validate_key` per provider, and skippable

`preflight.py:75` hardcodes `/v1/models`, `x-api-key` and `anthropic-version`. A llama-server has no meaningful auth and answers 200 regardless, so 401 detection there is noise. The provider record declares whether it has auth and which path validates it.

## Tests

Three keys live in the owner's shell at once, so two of these are new requirements rather than restatements.

1. **No credential crosses the relay.** Parameterised over all four providers: a container-supplied `x-api-key`, `authorization`, or `api-key` never reaches upstream. This is the highest-risk edit in the plan; the failure is silent.

2. **Only the selected provider's key is read.** With `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `OPENROUTER_API_KEY` all exported, the container env holds the run token and zero real keys. The README currently claims "Real key present in container environment: 0 occurrences"; this widens it to all three.

3. **The route table is exact and provider-scoped.** `/v1/chat/completions` must 403 under `openrouter`, whose real path is `/api/v1/chat/completions`.

Live smoke tests, one per provider, marked `provider_live`:

| Provider | Test | Cost |
| --- | --- | --- |
| openai-compat | local llama-server | 0 |
| openrouter | free model | 0 |
| openai | `GET /v1/models` plus a 10-token completion | cents |
| anthropic | existing integration suite | 0 |

## Phases

| Phase | Work | Done when | Status |
| --- | --- | --- | --- |
| P0 | `Provider` record and route table, anthropic as the only row | fast tests green, no behaviour change | done, 84 tests |
| P1 | http and loopback upstream, `openai-compat`, llama-server live test | a second provider passes live | done, 7 live tests pass against llama-server |
| P2 | `openai` and `openrouter` records, live smoke behind `provider_live` | all four pass live | records and validate paths done and live; paid completion smoke skips pending a model id |
| P3 | per-protocol usage sniffing, `include_usage` | token counts correct on all four | done and verified live: llama-server reports no streamed usage without the field, full counts with it |
| P4 | README two-mode table, CHANGELOG, TODO, `scripts/sanduk.py` freeze note | docs match behaviour | done; script scope note landed early, in P0 |

P1 precedes P2 deliberately. The free provider forces the plaintext-upstream and no-auth cases that the paid providers would let us skip.

### Deviations, and why

- **The script freeze moved from P4 into P0.** P0 necessarily changes `Config.__init__`, `Handler.authorized`, `Handler.apply_policy`, `Handler.relay` and two `UsageSniffer` methods, so the AST comparison in `tests/test_script.py` could not survive it. It is replaced by a behavioural check: `tests/test_proxy.py` parameterises its `relay` fixture over the package and `scripts/sanduk.py`, so all 20 relay tests run against both copies. Behaviour rather than syntax, and the stronger check of the two. Two guards keep the exclusion honest. One asserts the excluded names still exist on both sides, because `set(script) & set(package)` drops a deleted name silently. The other asserts the parameterisation is still in place.

- **Nested cached tokens moved from P3 into P1.** P1 introduces `openai-chat`, whose `cache_read` sits at `prompt_tokens_details.cached_tokens`. Shipping the protocol without the nested read would print `cache_read=0` on every call, which reads the same as a real cache miss. `digest` now also omits a counter the protocol does not declare rather than printing 0 for it, so OpenAI-shaped lines carry no `cache_write` field at all.

- **`start_proxy` now defaults `allow_paths` and `upstream` from the provider.** Both defaulted to the Anthropic constants, so naming a provider without also naming an allowlist silently kept Anthropic's. The first `openai-compat` relay test caught it with a 403 on `/v1/chat/completions`.

- **`STRIP_REQ` now drops every known credential header, not just the two in use.** It stripped `x-api-key` and `authorization` only, so a container-supplied `api-key` reached the upstream verbatim. Inert against Anthropic; it is the credential for Azure OpenAI. A header that means nothing to one provider is the key for the next, so the set is now named `CREDENTIAL_HEADERS` and covers `api-key` and `x-goog-api-key` as well. Applied to `scripts/sanduk.py` too, since a security fix in one copy and not the other is what the drift machinery exists to catch. Found by the per-provider stripping test, which is why that test is parameterised over every provider rather than the active one.

- **OpenRouter validates at `/api/v1/key`, not `/api/v1/models`.** Measured, without a credential: `/api/v1/models` answers 200 to an anonymous request, so a preflight pointed there would accept any key including a garbage one, and the 0.27s-versus-174s payoff would be imaginary. `/api/v1/key` answers 401. A live test asserts both halves.

- **`stream_usage_option` sits on the provider, not the protocol.** The plan put `include_usage` injection with the wire protocol. OpenAI and OpenRouter both speak `openai-chat` and only OpenAI needs the flag, so protocol is the wrong owner.

- **`apply_policy` returned early unless a policy flag was set.** The guard was `allow_models is None and max_tokens_cap is None`, which predates it having anything to do besides police. Injection would have fired only on runs using `--allow-model` or `--max-tokens-cap`, and silently never on a plain run.

## Any agent, without an agent registry

Landed in P1 as `--agent-key-env` and `--agent-base-url-env`.

`agent.py:17-18` hardcodes `KEY_ENV = "ANTHROPIC_API_KEY"` and `BASE_URL_ENV = "ANTHROPIC_BASE_URL"`. Make both provider defaults, with `--agent-key-env` and `--agent-base-url-env` overrides. Pointing an arbitrary agent at the relay is then at most two flags. `-e K=V` covers anything else. Roughly 20 lines in `cli.py`, no new abstraction.

The relay makes a provider reachable. The agent must still speak that provider's protocol. Claude Code will not talk to OpenRouter regardless of relay support.

## Out of scope

- **A second container engine.** Frozen at Apple `container`. Docker and Podman on macOS run containers inside a Linux VM, so the bridge gateway is not bindable from the host and the relay would have to bind `0.0.0.0`. That reverses the change in 582ee69 and weakens the two-mode table.

- **Protocol translation.** Claude Code against a non-Anthropic provider needs a translator, which is different software from a header-swapping relay. The escape hatch is an Anthropic-Messages-shaped gateway such as LiteLLM run on the host as the upstream.

- **An agent registry.** See "Any agent", above.

## Open questions

- Does OpenRouter serve the Responses API, or Chat Completions only? Decides whether Codex CLI can reach it. Codex removed `wire_api = "chat"` in February 2026 and now accepts `responses` only.

- Does `--max-tokens-cap` still mean one number when three protocols name the field differently, or does it become per-protocol?
