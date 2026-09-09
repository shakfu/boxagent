# sanduk

'sanduk', pronounced SAN-dook, means 'box' in Arabic.

sanduk is a Python CLI tool and package that makes it easy to run an agent inside a disposable container. The agent does its work, writes a report to a bind-mounted directory, and when it’s finished, the container is deleted.

Six agents ship: Claude Code, [codex](https://github.com/openai/codex), [hax](https://github.com/OleksandrChekhovskyi/hax), [opencode](https://github.com/sst/opencode), [pi](https://github.com/earendil-works/pi), and [prime-agent](https://github.com/PrimeIntellect-ai/prime-agent). Two container engines: Apple's [`container`](https://github.com/apple/container) on macOS, and `docker`. Each sits behind a registry -- an agent behind `sanduk.agent.Agent`, an engine behind `sanduk.runtime.Runtime` -- so another of either is one class. An agent can live in your own package and be found by entry point; see [docs/agents.md](docs/agents.md). Podman is not implemented.

Four providers are supported: Anthropic, OpenAI, OpenRouter, and any OpenAI-compatible server, which includes a local `llama-server`. See [Providers](#providers).

In its stronger mode the container has no route off the host and never holds the API key: a host-side relay injects the credential, and the container gets a per-run token that is worthless anywhere else. Against a local model there is no key to hold, and nothing leaves the machine at all.

## Requirements

- A container engine, one of:

  - Apple's [`container`](https://github.com/apple/container) 1.2.0 or later, which needs Apple silicon and macOS 26 or later

  - `docker`, with a daemon on this kernel. `--proxy` needs the bridge gateway to be an address this host can bind, which Docker Desktop, Colima and Lima do not give.

- Python 3.11 or later. `uv` as well, for a source checkout: the Makefile targets run through it

- An API key for the provider you pick, in that provider's variable: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`. `--provider openai-compat` needs none.

Apple's `container` runs **Linux** containers as lightweight VMs. There is no such thing as a macOS container here; anything needing Xcode or the macOS toolchain cannot be the workload.

Each agent has its own image. Claude Code, codex, opencode and pi run on `node:22-slim`; hax's is `debian:trixie-slim` with no language runtime, since the binary is static. All add `git`, `ripgrep`, `curl`, `jq`, and `python3`. The agent can only run what is in it. Without an interpreter it falls back to hand-tracing and still writes a confident report, so check whether the findings say they were reproduced. There is no C, Go, or Rust toolchain: point `--image` at your own, or `--containerfile` at one to build.

## Install

```text
pip install sanduk
```

Note that `uv tool install sanduk` and `pipx install sanduk` do the same thing into their own environment, which is what you want for a globally available command line tool. There are no Python dependencies to resolve either way; a container engine remains as a requirement.

The wheel carries a `Containerfile` per agent, so `sanduk build` works from a plain install with no checkout.

## Quickstart

```text
pip install sanduk
export ANTHROPIC_API_KEY=sk-ant-...
sanduk build                                              # the claude image
sanduk run 'Summarise every Python file here.' -w ./work --mode sealed
sanduk --help
```

From a checkout, where the Makefile wraps the same commands:

```text
git clone https://github.com/shakfu/sanduk.git && cd sanduk
make sync
make image
export ANTHROPIC_API_KEY=sk-ant-...
make run TASK='Summarise every Python file here.' WORK=./work
```

`make` targets call `uv run sanduk`, so an editable checkout and an installed copy take the same flags.

## Three modes

`--mode` sets two properties: whether the host keeps the key, and whether the container has a route off it.

|                                   | `open` (default) | `key-safe`                       | `sealed`                        |
| --------------------------------- | ---------------- | -------------------------------- | ------------------------------- |
| Host filesystem                    | container only   | container only                   | container only                  |
| API key location                   | in the container | host only; the container holds a run token | host only; the container holds a run token |
| Egress                             | unrestricted     | unrestricted                     | none                            |
| Agent can POST your source anywhere| yes              | yes                              | no                              |
| Which model endpoints it may call  | all              | the provider's, matched exactly   | the provider's, matched exactly |
| Model policy (`--allow-model`, `--max-tokens-cap`) | none | enforced on the host     | enforced on the host            |
| Record of what it sent upstream    | none             | `--log-bodies`, model calls only | `--log-bodies`, every call      |

`open` is filesystem isolation and nothing more. `sealed` is where the containment is: the network is created with `--internal`, so the relay on the bridge gateway is the only address the container can reach.

`key-safe` is the same relay on a routable network. It exists for runs that need `npm install`, `pip install` or `git clone` and must not hold your key. It buys credential protection and keeps the model policy; it buys no containment, and the audit trail stops being complete, because what the agent sends anywhere else never passes the relay.

`--proxy` still works as the old spelling of `--mode sealed`.

Against a local model the relay is no longer protecting a credential, because there is not one. What it still does is hold the agent to an exact path allowlist, enforce the model and token policy where the container cannot edit it, and record what was sent.

## Commands

```text
sanduk run <task>          run an agent in a disposable container
sanduk build               build the agent's image
sanduk shell               interactive shell in that image
sanduk ps                  list sanduk containers
sanduk stop                stop them, leaving them on disk
sanduk clean               stop and delete them
sanduk destroy             clean, plus the image and every mode's network
sanduk system status       whether the engine is ready
sanduk list agents         what each registered handler speaks
sanduk list providers      the URL an agent must be given, per provider
sanduk list runtimes       engines, and whether each is installed

sanduk assistant add <dir> register a scheduled assistant
sanduk tell <name> <text>  queue a message for its next wakeup
sanduk tick                run every assistant that is due, once
sanduk serve               tick on a loop, in the foreground
sanduk outbox              what they have produced
sanduk runs                wakeup history
```

The first group is one container and no memory of it. The second is [Assistants](#assistants): the same run, on a schedule, with state that outlives it.

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
sanduk run 'Review this.' -w ./repo --mode sealed \
    --provider openai-compat --upstream http://127.0.0.1:8080 --model local-model
```

`--upstream` takes `scheme://host:port` and no path. Plaintext `http://` to anything but a loopback address is refused, because the relay writes the real key into every forwarded request; `--insecure-upstream` overrides. OpenRouter's `/api/v1` prefix lives in the allowlist, not the upstream.

The relay only forwards. It does not translate between protocols, so the agent has to speak the provider's own API. Claude Code speaks Anthropic Messages only; codex speaks OpenAI Responses only. `sanduk list agents` prints what each handler speaks, and a pairing no protocol supports is refused before anything is built rather than 404'd by the relay later.

`--agent-key-env` and `--agent-base-url-env` name the variables the agent reads inside the container. They default to the provider's. They are separate because sanduk reads the key on the host under one name and the container may want another, which is what makes an arbitrary agent a matter of two flags rather than a new module.

## Agents

```text
--agent claude     Claude Code   anthropic
--agent codex      codex         openai, openai-compat
--agent hax        hax           every provider
--agent opencode   opencode      every provider
--agent pi         pi            every provider
--agent prime      prime-agent   every provider
```

A handler says which image carries the agent, what flags drive it headlessly, which variables it reads its endpoint from, and how to read its JSON stream. Nothing else about a run differs, so a seventh agent is a class in your own package, advertised in the `sanduk.agents` entry-point group or named directly as `--agent mypkg.handlers:MyAgent`. See [docs/agents.md](docs/agents.md).

```text
sanduk run 'Review this.' -w ./repo --mode sealed --agent hax \
    --provider openrouter --model anthropic/claude-sonnet-5
```

hax is a static C binary with no approval gate, which suits a container that is already the boundary. The image carries no language runtime. `--allowed-tools` and `--permission-mode` are Claude Code flags and are refused rather than dropped.

codex accepts only `wire_api = "responses"`, so it pairs with `openai` or with an `openai-compat` server that answers `/v1/responses`. It has no base-URL variable: sanduk passes the endpoint as a `-c model_providers...` override, which is why `argv` is handed the run's wiring. Its own sandbox is disabled with `--sandbox danger-full-access`, since the container is the boundary and codex's sandbox would only stop the work; `--skip-git-repo-check` is passed because the bind mount is usually not a repository.

opencode takes its whole configuration from `OPENCODE_CONFIG_CONTENT`, so no `opencode.json` is written into the bind mount, where it would sit in your repository and be editable by the agent reading it. The provider block picks an npm driver by wire protocol: `@ai-sdk/anthropic` for Messages, `@ai-sdk/openai-compatible` for Chat Completions. Both are installed in the image, because the proxy network has no route to fetch one at runtime. `--model` is required: the config names one model and there is nothing to put in it otherwise.

prime-agent is pi's CLI in PrimeIntellect's build: the release tarball declares `bin: prime-agent` and depends on the `@earendil-works/pi-*` packages, so its handler is a subclass of pi's and inherits the reader, the argv and the protocols. Three things differ, each measured rather than read. Its provider block names the credential's variable bare where pi writes `$NAME`. It has no `--no-approve`, so a `.prime/agent/settings.json` in the mounted directory is read: that steers the run without widening the box, which is the container and the relay either way. And its only tool is a Python REPL, so the image carries the kernel; without it the agent answers by trying to install `uv`, which the proxy network has no route for. The image installs a checksummed release tarball rather than an npm package.

pi speaks all three protocols the relay carries, and its provider block names which one with an `api` field. It reads providers from `models.json` in its config directory, not from a variable or a flag, so the image's entrypoint writes that file from `SANDUK_PI_MODELS` and pi is never given the bind mount as a place to find one. `--model` is required, and `--no-approve` is passed so a `.pi/settings.json` in the mounted repository cannot steer the run. With `--provider anthropic` the base URL is the bare root: pi appends `/v1/messages` itself.

## How the relay works

1. `sanduk-net` is created with `--internal`: no route off the host.

2. vmnet only creates the host bridge while a container is attached, so a placeholder container is started first and torn down at the end.

3. The relay binds the bridge gateway only, so it is unreachable from Wi-Fi or LAN.

4. The container is given the provider's base-URL variable and a per-run token as its key variable. Both arrive through the child process environment, so neither appears in `ps`, and `container inspect` shows the token, not the key.

5. The relay checks the token in the header that provider authenticates with, checks the path against an exact allowlist, applies any model or token policy, swaps in the real key, and streams the response back. Every credential header the container sent is dropped, not only the one this provider uses: a header that means nothing to one API is the key for another.

6. Every relayed call logs its token counts: `in=`, `cache_write=`, `cache_read=`, `out=`. A counter the protocol does not report is left out rather than printed as zero, and a completion that reports none at all logs `usage=?` rather than a line that looks ordinary. The relay narrows the client's `Accept-Encoding` to `gzip` to read them, because the API answers in brotli whenever a client offers it and nothing in the standard library decodes brotli. For OpenAI-shaped providers it also adds `stream_options.include_usage`, without which a streamed response carries no counts at all.

## Flags worth knowing

`--allow-model claude-opus-5` and `--max-tokens-cap N` are enforced on the host, where the container cannot edit them. The cap clamps whichever field the protocol uses: `max_tokens`, `max_completion_tokens`, or `max_output_tokens`. Claude Code asks for `max_tokens: 64000` on every call, so a cap below that silently truncates every request.

`--log-bodies` records each request body: a digest line per call, full JSON under `--log-dir` (default `./sanduk-logs`, deliberately outside the bind mount so the agent cannot read or edit its own audit trail). Bodies contain the system prompt and every file the agent has read.

`--mount HOST:DEST[:ro]` puts another host directory in the container, beside the one `-w` gives it. Repeatable, read-write unless `:ro`. A destination at or under `/work` is refused: it would shadow part of what `-w` put there, which is a run reading the wrong files rather than one that fails. Read-only is rendered as `--mount type=bind,...,readonly` because `-v host:dest:ro` is Docker's spelling alone.

`--dry-run` prints the `container run` command and exits. `--keep` leaves the container for inspection, and warns that `container inspect` then exposes the token.

A run records the containers it owns, and its own pid, under `$XDG_STATE_HOME/sanduk/runs` (`~/.local/state` by default). Every run first deletes the containers of records whose owner process is gone. SIGKILL cannot be caught, so a killed run cannot delete its own container -- the next run does it, and until then the container is alive holding the run token. SIGTERM and SIGHUP are caught and tear down in place. `--keep` releases the record, so a container you asked to keep is never swept. A state directory that cannot be written stops the run before it starts.

## Assistants

`run` is one container and keeps nothing. An assistant is a directory, a schedule and a mailbox around that same run.

```text
~/assistants/triage/
    assistant.toml     what to run, and how often
    brief.md           the standing instruction
    workspace/         bind-mounted at /work; where the agent keeps its memory
    reports/           one task and one report per wakeup
```

```toml
# assistant.toml
agent    = "pi"
provider = "anthropic"
model    = "claude-sonnet-5"
every    = "30m"          # an interval, not a cron expression
brief    = "brief.md"
gate     = "gate.sh"      # optional: a non-zero exit skips the wakeup, unpaid
timeout  = 900
max_failures = 3
mode     = "sealed"       # open | key-safe | sealed, as above
approval = false          # true: results wait for `sanduk approve` before delivery
mounts   = ["../repo:/repo:ro"]   # host paths are relative to this file
args     = []             # extra `sanduk run` flags, verbatim
```

```text
sanduk assistant add ~/assistants/triage   register the directory
sanduk assistant list | show <name>        schedule, failures, queue depth
sanduk assistant enable | disable <name>   the operator's switch
sanduk tell <name> 'look at PR 12'         queue a message for the next wakeup
sanduk tick                                run everything that is due, once
sanduk serve [--interval 60]               the same, on a loop
sanduk outbox [--deliver CMD]              read results, or pipe them somewhere
sanduk outbox --pending                    what is waiting for a decision
sanduk approve <id> | reject <id>          decide it
sanduk runs                                wakeup history with exit codes
```

`tick` is what a scheduler calls:

```text
*/10 * * * * cd ~/assistants && /opt/homebrew/bin/uv run sanduk tick
```

`serve` is the same pass on a loop, in the foreground, for when you would rather run one process than a cron entry -- under launchd, systemd, or a terminal. It sleeps until the next assistant is due, capped at `--interval`, and holds no container and no credential in between: it is a timer. SIGINT or SIGTERM stops it after the pass in flight; a signal during a wakeup tears that wakeup down first, and the interrupted wakeup does not count against the assistant's failures.

A wakeup is one `sanduk run`: the brief plus any queued messages become the task, `workspace/` is the bind mount, and the report is copied to `reports/<timestamp>.md`. The container is deleted at the end like any other run, and the relay holds the key for exactly as long as the wakeup lasts.

What outlives a wakeup lives in SQLite at `$XDG_STATE_HOME/sanduk/assistants.db` (`~/.local/state` by default): the schedule, the claim that stops two processes running one assistant, the inbox and the outbox. Messages are consumed only by a wakeup that finished, so a failed one still has them. A failure backs the schedule off, doubling per consecutive failure, and disables the assistant at `max_failures` until you enable it again.

`--deliver` hands each undelivered result to a command on stdin, with `SANDUK_ASSISTANT` and `SANDUK_RUN_ID` in its environment. sanduk ships no platform adapters and holds no messaging credential.

`approval = true` holds every result until a person runs `sanduk approve <id>`; `sanduk reject <id>` keeps it from ever being sent, and neither decision undoes the other. Delivery is the gate because delivery is the only thing that leaves the box: what the agent does, it does inside a container that is deleted at the end of the wakeup.

`mounts` puts other host directories in the container, resolved from the config file's own directory, so an assistant can read a repository it must not write: `mounts = ["../repo:/repo:ro"]`.

Two things to hold onto. `mode` defaults to `sealed` and should stay there: an unattended run is the one nobody is watching. And whatever the agent writes into `workspace/` is read as instruction on the next wakeup, which is memory and also a channel between runs -- keep it somewhere you read the diffs.

## Layout

```text
src/sanduk/
    cli.py         flags, lifecycle, teardown
    runtime.py     container engines; ContainerSpec; `apple` and `docker`
    providers.py   provider records, wire protocols, route tables
    agent.py       the agent strategy: interface, registry, plugin loading
    runs.py        which process owns which container; the orphan sweep
    assistants.py  identity, schedule, mailbox: the assistant commands
    agents/        the shipped handlers: claude.py, codex.py, hax.py,
                   opencode.py, pi.py, prime.py
    proxy.py       the host-side relay
    preflight.py   key validation, macOS firewall check
    resources/
        Containerfile.claude
        Containerfile.codex
        Containerfile.hax
        Containerfile.opencode
        Containerfile.pi
        Containerfile.prime
```

Another agent is an `Agent` subclass in any package; see [docs/agents.md](docs/agents.md). A third engine is a `Runtime` subclass and a `RUNTIMES` entry. It must supply four things: the CLI name, the verb that deletes a container (`rm`, not `delete`), how `network inspect` reports the gateway, and whether the host bridge needs a placeholder container to exist at all.

Every container drops all Linux capabilities and runs under an init process. `--runtime docker` adds `--security-opt no-new-privileges` and `--pids-limit 1024`, which Apple's CLI has no flags for and which matter on a shared kernel; there each container is its own VM. The root filesystem stays writable, because every shipped agent writes under `$HOME`.

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
make run-proxy        Same, sealed: no egress, key held on the host
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

The per-agent rows are one task and one local model, counted by the relay through `--proxy`. What they compare is each agent's fixed prompt overhead, not the quality of its answer.

| | |
| --- | --- |
| Bad key, host preflight | 0.27s |
| Bad key, no preflight | 174s of in-container retry backoff |
| Agent run, 3 files, 7 turns | 35.4s wall, 23.2s of it upstream |
| Review of one 59-line file, 10 turns | $0.55, 148s wall |
| Claude Code's system prompt and tool schemas | 22,993 tokens |
| That prefix written cold, as a share of one run | 25% of its cost |
| The same prefix on a second run inside the cache TTL | read, not written: 23% cheaper |
| One task, one model, input tokens per run: pi | 4.8-5.1k |
| The same task: codex | 12.8-20.8k |
| The same task: opencode | 23.4k |
| The same task: prime-agent | 14.8k |
| Container direct egress on `sanduk-net` (sealed) | `000` |
| The same on `sanduk-open` (key-safe) | `401`: it reached the provider |
| Real keys in the container, with all three exported | 0 of 3 |
| OpenRouter `/api/v1/models`, no credential | `200` |
| OpenRouter `/api/v1/key`, no credential | `401` |
| Streamed llama-server usage, no `stream_options` | none reported |
| The same, with `include_usage` injected | full counts, cached included |

## Known traps

The macOS application firewall silently drops connections to a binary set to "Block incoming connections", so the agent's first API call hangs until `--timeout` rather than failing. Homebrew's Python is shipped blocked on at least one machine; uv's interpreters are signed and auto-allowed. `--proxy` runs a preflight that names the exact `socketfilterfw --unblockapp` command when it sees an explicit block. It cannot detect an interpreter that will merely prompt.

`AF_UNIX` paths cap at 104 bytes on macOS, which matters if you point `--log-dir` somewhere deep.

A path allowlist is per provider and matched exactly. OpenRouter's endpoint is `/api/v1/chat/completions`; the `/v1/chat/completions` in OpenAI's documentation will 403, and the rejection is legible only in the proxy log.
