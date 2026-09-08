# sanduk

'sanduk', pronounced SAN-dook, means 'box' in Arabic.

sanduk is a Python CLI tool and package that makes it easy to run an agent inside a disposable container. The agent does its work, writes a report to a bind-mounted directory, and when it’s finished, the container is deleted.

Two agents ship: Claude Code and [hax](https://github.com/OleksandrChekhovskyi/hax). Two container engines: Apple's [`container`](https://github.com/apple/container) on macOS, and `docker`. Each sits behind a registry -- an agent behind `sanduk.agent.Agent`, an engine behind `sanduk.runtime.Runtime` -- so a third of either is one class. An agent can live in your own package and be found by entry point; see [docs/agents.md](docs/agents.md). Podman is not implemented.

Four providers are supported: Anthropic, OpenAI, OpenRouter, and any OpenAI-compatible server, which includes a local `llama-server`. See [Providers](#providers).

In its stronger mode the container has no route off the host and never holds the API key: a host-side relay injects the credential, and the container gets a per-run token that is worthless anywhere else. Against a local model there is no key to hold, and nothing leaves the machine at all.

## Requirements

- A container engine, one of:

  - Apple's [`container`](https://github.com/apple/container) 1.2.0 or later, which needs Apple silicon and macOS 26 or later

  - `docker`, with a daemon on this kernel. `--proxy` needs the bridge gateway to be an address this host can bind, which Docker Desktop, Colima and Lima do not give.

- Python 3.11 or later, and `uv`

- An API key for the provider you pick, in that provider's variable: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`. `--provider openai-compat` needs none.

Apple's `container` runs **Linux** containers as lightweight VMs. There is no such thing as a macOS container here; anything needing Xcode or the macOS toolchain cannot be the workload.

Each agent has its own image. Claude Code's is `node:22-slim`; hax's is `debian:trixie-slim` with no language runtime, since the binary is static. Both add `git`, `ripgrep`, `curl`, `jq`, and `python3`. The agent can only run what is in it. Without an interpreter it falls back to hand-tracing and still writes a confident report, so check whether the findings say they were reproduced. There is no C, Go, or Rust toolchain: point `--image` at your own, or `--containerfile` at one to build.

## Quickstart

```text
make sync
make image
export ANTHROPIC_API_KEY=sk-ant-...
make run TASK='Summarise every Python file here.' WORK=./work
```

Or directly, which is where all the flags live:

```text
uv run sanduk run 'Summarise every Python file here.' -w ./work --proxy
uv run sanduk --help
```

## Two modes

|                                   | default          | `--proxy`                        | `--proxy` + local model |
| --------------------------------- | ---------------- | -------------------------------- | ----------------------- |
| Host filesystem                    | container only   | container only                   | container only          |
| API key location                   | in the container | host only; the container holds a run token | there is no key |
| Egress                             | unrestricted     | none                             | none                    |
| Agent can POST your source anywhere| yes              | no                               | no                      |
| Which endpoints the agent may call | all              | the provider's, matched exactly   | the provider's, matched exactly |
| Leaves the machine                 | yes              | the prompt and every file read   | nothing                 |
| Record of what it sent upstream    | none             | `--log-bodies`                   | `--log-bodies`          |

The default mode is filesystem isolation and nothing more. `--proxy` is where the containment is.

With a local model the relay is no longer protecting a credential, because there is not one. What it still does is hold the agent to an exact path allowlist, enforce the model and token policy where the container cannot edit it, and record what was sent.

## Commands

```text
sanduk run <task>          run an agent in a disposable container
sanduk build               build the agent's image
sanduk shell               interactive shell in that image
sanduk ps                  list sanduk containers
sanduk stop                stop them, leaving them on disk
sanduk clean               stop and delete them
sanduk destroy             clean, plus the image and the network
sanduk system status       whether the engine is ready
sanduk list agents         what each registered handler speaks
sanduk list providers      the URL an agent must be given, per provider
sanduk list runtimes       engines, and whether each is installed
```

Every container-engine call sanduk makes goes through `Runtime`, so the Makefile names no engine and `--runtime` selects one for any of these.

## Providers

```text
--provider anthropic       api.anthropic.com          ANTHROPIC_API_KEY
--provider openai          api.openai.com             OPENAI_API_KEY
--provider openrouter      openrouter.ai/api/v1       OPENROUTER_API_KEY
--provider openai-compat   --upstream, no key needed  OPENAI_API_KEY if set
```

A provider record names the upstream, the auth header, the variable its key comes from, the path the preflight checks, and a route table. The route table maps each allowed path to the wire protocol spoken there, so one structure is the egress allowlist, the field `--max-tokens-cap` clamps, and the names the usage line reads.

The protocol hangs off the route, not the provider, because OpenAI serves Responses and Chat Completions on two paths of one host. It is declared rather than detected because Anthropic Messages and OpenAI Responses both report `input_tokens` and `output_tokens`; a response body cannot tell them apart.

A local model:

```text
llama-server -m ~/.models/some-model.gguf --port 8080 --alias local-model
sanduk run 'Review this.' -w ./repo --proxy \
    --provider openai-compat --upstream http://127.0.0.1:8080 --model local-model
```

`--upstream` takes `scheme://host:port` and no path. Plaintext `http://` to anything but a loopback address is refused, because the relay writes the real key into every forwarded request; `--insecure-upstream` overrides. OpenRouter's `/api/v1` prefix lives in the allowlist, not the upstream.

The relay only forwards. It does not translate between protocols, so the agent has to speak the provider's own API. Claude Code speaks Anthropic Messages only, so `--provider openai|openrouter|openai-compat` needs `--agent hax`; the pairing is refused before anything is built rather than 404'd by the relay later.

`--agent-key-env` and `--agent-base-url-env` name the variables the agent reads inside the container. They default to the provider's. They are separate because sanduk reads the key on the host under one name and the container may want another, which is what makes an arbitrary agent a matter of two flags rather than a new module.

## Agents

```text
--agent claude   Claude Code       anthropic only
--agent hax      hax               every provider
```

A handler says which image carries the agent, what flags drive it headlessly, which variables it reads its endpoint from, and how to read its JSON stream. Nothing else about a run differs, so a third agent is a class in your own package, advertised in the `sanduk.agents` entry-point group or named directly as `--agent mypkg.handlers:MyAgent`. See [docs/agents.md](docs/agents.md).

```text
sanduk run 'Review this.' -w ./repo --proxy --agent hax \
    --provider openrouter --model anthropic/claude-sonnet-5
```

hax is a static C binary with no approval gate, which suits a container that is already the boundary. The image carries no language runtime. `--allowed-tools` and `--permission-mode` are Claude Code flags and are refused rather than dropped.

## How --proxy works

1. `sanduk-net` is created with `--internal`: no route off the host.

2. vmnet only creates the host bridge while a container is attached, so a placeholder container is started first and torn down at the end.

3. The relay binds the bridge gateway only, so it is unreachable from Wi-Fi or LAN.

4. The container is given the provider's base-URL variable and a per-run token as its key variable. Both arrive through the child process environment, so neither appears in `ps`, and `container inspect` shows the token, not the key.

5. The relay checks the token in the header that provider authenticates with, checks the path against an exact allowlist, applies any model or token policy, swaps in the real key, and streams the response back. Every credential header the container sent is dropped, not only the one this provider uses: a header that means nothing to one API is the key for another.

6. Every relayed call logs its token counts: `in=`, `cache_write=`, `cache_read=`, `out=`. A counter the protocol does not report is left out rather than printed as zero, and a completion that reports none at all logs `usage=?` rather than a line that looks ordinary. The relay narrows the client's `Accept-Encoding` to `gzip` to read them, because the API answers in brotli whenever a client offers it and nothing in the standard library decodes brotli. For OpenAI-shaped providers it also adds `stream_options.include_usage`, without which a streamed response carries no counts at all.

## Flags worth knowing

`--allow-model claude-opus-5` and `--max-tokens-cap N` are enforced on the host, where the container cannot edit them. The cap clamps whichever field the protocol uses: `max_tokens`, `max_completion_tokens`, or `max_output_tokens`. Claude Code asks for `max_tokens: 64000` on every call, so a cap below that silently truncates every request.

`--log-bodies` records each request body: a digest line per call, full JSON under `--log-dir` (default `./sanduk-logs`, deliberately outside the bind mount so the agent cannot read or edit its own audit trail). Bodies contain the system prompt and every file the agent has read.

`--dry-run` prints the `container run` command and exits. `--keep` leaves the container for inspection, and warns that `container inspect` then exposes the token.

## Layout

```text
src/sanduk/
    cli.py         flags, lifecycle, teardown
    runtime.py     container engines; ContainerSpec; `apple` and `docker`
    providers.py   provider records, wire protocols, route tables
    agent.py       the agent strategy: interface, registry, plugin loading
    agents/        the shipped handlers: claude.py, hax.py
    proxy.py       the host-side relay
    preflight.py   key validation, macOS firewall check
    resources/
        Containerfile.claude
        Containerfile.hax
```

A second agent is an `Agent` subclass in any package; see [docs/agents.md](docs/agents.md). A third engine is a `Runtime` subclass and a `RUNTIMES` entry. It must supply four things: the CLI name, the verb that deletes a container (`rm`, not `delete`), how `network inspect` reports the gateway, and whether the host bridge needs a placeholder container to exist at all.

`--runtime docker` needs a daemon on this kernel, not one in a VM. Docker Desktop, Colima and Lima keep the bridge inside the VM, so the relay cannot bind the gateway; the run stops at the bind with that reason rather than listening somewhere the container cannot reach. `--runtime apple` is the macOS path.

## Make targets

`make help` lists all of them. The ones you need:

```text
make sync             Resolve and install the environment
make test             Fast suite: no containers, no API calls, no key needed
make test-container   Integration suite: boots real containers
make test-all         Both
make qa               lint-check, format-check, typecheck, test
make image            Build the agent image if missing
make image-rebuild    Force a rebuild
make run              TASK='...' WORK=./dir ARGS='--effort max'
make run-proxy        Same, with no egress and the key held on the host
make shell            Interactive shell in the image
make ps / make logs   Containers / recorded request bodies
make stop             Stop sanduk containers, leave them on disk
make clean            Delete them and build scratch. Keeps work/ and logs
make distclean        clean, plus .venv and tool caches
make destroy          clean, plus the image, the network, and the logs
make system-start / system-stop / system-status
```

`make system-stop` stops Apple's container service for everything on the machine, not just sanduk.

## CI

`.github/workflows/ci.yml` runs lint, format and types once, the fast suite across `ubuntu-latest` and `macos-latest` on the declared Python bounds, and the integration suite on Linux against a real Docker daemon. That last job is not redundancy: a native daemon puts the bridge on the host kernel, so it is the only place `--proxy` can be proved. Neither Apple's engine nor Docker Desktop can, and both are what you have locally.

## Testing

The fast suite makes no API calls and needs no key: the relay is exercised against a local fake upstream, and the preflight is monkeypatched.

`scripts/sanduk.py` carries its own copy of the relay, so every relay test runs twice, once against each copy. That compares behaviour rather than source, which an AST comparison could no longer do once the package's relay grew providers the script does not have.

The integration suite boots real VMs and proves the relay by the 401 an invalid key earns from the real endpoint, which is itself proof the request arrived.

`make test-live` talks to real providers, and most of it costs nothing. The bad-key tests reach Anthropic, OpenAI, and OpenRouter with no credential at all, since refusing an invalid key needs no valid one. Point `LLAMA_SERVER` at a local `llama-server` and the whole openai-compat path runs for free.

Of the two completion tests, OpenRouter needs only a key: it defaults to `openrouter/free`, a router that picks a free model at random. A pinned `:free` id works too, but those rotate out of the catalogue, which fails the test for a reason unrelated to sanduk. OpenAI has no free tier, so it stays opt-in through `OPENAI_MODEL` and nothing is spent unless a model is named.

```text
LLAMA_SERVER=http://127.0.0.1:8080 make test-live
OPENAI_MODEL=<a cheap model> make test-live
```

Nothing runs on its own. There is no CI here, and `pyproject.toml` deselects both the container and the live suites, so `make test` is the only one that runs unasked.

## Measured on this setup

| | |
| --- | --- |
| Bad key, host preflight | 0.27s |
| Bad key, no preflight | 174s of in-container retry backoff |
| Agent run, 3 files, 7 turns | 35.4s wall, 23.2s of it upstream |
| Review of one 59-line file, 10 turns | $0.55, 148s wall |
| Claude Code's system prompt and tool schemas | 22,993 tokens |
| That prefix written cold, as a share of one run | 25% of its cost |
| The same prefix on a second run inside the cache TTL | read, not written: 23% cheaper |
| Container direct egress on `sanduk-net` | `000` |
| Real keys in the container, with all three exported | 0 of 3 |
| OpenRouter `/api/v1/models`, no credential | `200` |
| OpenRouter `/api/v1/key`, no credential | `401` |
| Streamed llama-server usage, no `stream_options` | none reported |
| The same, with `include_usage` injected | full counts, cached included |

## Known traps

The macOS application firewall silently drops connections to a binary set to "Block incoming connections", so the agent's first API call hangs until `--timeout` rather than failing. Homebrew's Python is shipped blocked on at least one machine; uv's interpreters are signed and auto-allowed. `--proxy` runs a preflight that names the exact `socketfilterfw --unblockapp` command when it sees an explicit block. It cannot detect an interpreter that will merely prompt.

`AF_UNIX` paths cap at 104 bytes on macOS, which matters if you point `--log-dir` somewhere deep.

A path allowlist is per provider and matched exactly. OpenRouter's endpoint is `/api/v1/chat/completions`; the `/v1/chat/completions` in OpenAI's documentation will 403, and the rejection is legible only in the proxy log.
