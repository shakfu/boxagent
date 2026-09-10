# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `--oci-runtime NAME`, Docker only, renders as `docker run --runtime NAME`: gVisor's `runsc`, or Kata's `io.containerd.kata.v2` for a VM per container. On Linux the agent otherwise shares the host kernel, and a kernel escape lands in the host where the relay holds the key. Refused under `--runtime apple`, where each container is already a VM. A CI job runs the container suite under `runsc`; Kata is not measured. See [docs/dev/microvms.md](docs/dev/microvms.md).

- The `runs` table records why a wakeup failed, and `sanduk runs` prints it; an outbox entry with no report carries it too. The exit code alone could not tell a timeout from the agent's own error or a run with no final result. A timed-out run now writes `--stats-file` and copies `--report` like any other failed run; it used to exit before either.

### Fixed

- A run whose agent reports an error still writes `--stats-file` and copies `--report`. It returned first, so a failed wakeup recorded no token count, and its outbox entry read "(no report, exit 1)" beside a report the agent had written.

- A run with no final result exits 1, even when the agent exited 0. Every reader returns None when its terminal record never arrives, and the run treated that as success: an assistant marked its messages answered by an agent that had answered nothing.

- A run whose client is killed by a signal exits 128 plus the signal number. The raw negative status reached the shell as 247 for SIGKILL and was stored in `runs` as -9.

`scripts/sanduk.py` has the last two fixes, and the `--report` half of the first.

- Two relayed runs that both find their network missing both start. Each created it, and the second create failed on Apple's engine with "has a pending operation", stopping that run. This happens on first use and on the first runs after `destroy`. The second run now waits for the first run's network.

## [0.2.3]

### Added

- `--budget USD`, a cost ceiling for one run. OpenRouter returns `usage.cost` on every response -- the `usage: {include: true}` parameter that used to be needed is deprecated and has no effect -- so the relay adds it up and refuses the next call with a 402 once the total is reached. Refused for every other provider: they report tokens, and pricing those means a table this package would have to keep current. Refused in `--mode open` as well, where there is no relay to count. The ceiling is crossed exactly once, by the call that crosses it: what a call costs is not knowable before it is made, and an estimator would be wrong in one direction or the other -- a growing conversation makes each call dearer than the last, cache reads make them cheaper. `--max-tokens-cap` bounds the size of that last call. The cost is recorded before the client is released, so the call after it cannot slip through on a total that has not caught up. The run's spend joins the relay's teardown line.

### Changed

- A refusal says why in its body, and names its kind: `authentication_error`, `forbidden`, `budget_exceeded`, `invalid_request_error`. Every refusal used to send `{"type":"forbidden"}` whatever the reason, so an agent stopped by a budget reported "forbidden" to its user and to its logs, and its retry logic saw a permission problem. This is sanduk's own policy talking to a container that already knows it is behind a relay.

- The agent's own tally drops a zero cost when the relay has a real one. An agent prices a run from its model catalogue, which never held the id the relay hands it, so a run against OpenRouter printed `$0.0000` from the agent one line under `$0.0396 spent` from the relay.

- Every duration reads the same way: a bare number is seconds, a suffix of `s`, `m`, `h` or `d` multiplies it. `--timeout 15m` works, and so does `timeout = "15m"` in `assistant.toml`, which used to raise `ValueError: invalid literal for int()` from inside `load` -- a traceback rather than a message, in a file whose `every = "30m"` sat two lines above it. One parser in `sanduk.util` now serves both keys and the flag.

### Fixed

- The network holder is started for the run's length plus five minutes, not a flat day. vmnet only keeps the host bridge up while a container is attached, so a relayed run past 24 hours lost the bridge -- and with it the address the relay is bound to -- and failed as if the network had broken, with nothing having announced the ceiling. `--timeout` over a week is now refused, and so is a zero or negative one, which used to arm a watchdog that fired immediately.

### Added

- `--agent hermes`, Nous Research's hermes-agent, and `Reader.line` for it. It is the only shipped agent that prints prose instead of a JSON stream, so the reader contract grew an optional hook that takes every line which is not a record; every other handler ignores it. It is also the only one that cannot be pointed at the relay: measured against 0.19.0 with a stub upstream inside the container, `--base_url`, `OPENROUTER_BASE_URL` and `model.base_url` in `~/.hermes/config.yaml` each left the call going to openrouter.ai and returning its own 401. `check` refuses `key-safe` and `sealed` by name, so the run stops at the flag rather than at the first API call, and `--mode open` is what it supports: a disposable container holding your key. The image is `python:3.12-slim` with `hermes-agent==0.19.0`, the newest published; the repository is at 0.21.1. Its entry point is `python -m run_agent`, not the `hermes-agent` console script, which calls `main()` with no arguments and runs a hardcoded demo query with every flag ignored. Its stats line says API calls, because it reports no token counts.

- `--mode open|key-safe|sealed`, replacing `--proxy` as the way to say what a run may do. A mode is two properties: whether the host keeps the key, and whether the container has a route off it. `open` is the old default, `sealed` is what `--proxy` was, and `key-safe` is new: the same relay, the same run token, the same path allowlist and model policy, on a network created without `--internal`. It exists for runs that need `npm install` or `git clone` and must not hold the key. It buys no containment and the record stops being complete -- what the agent sends anywhere else never passes the relay -- so the run says so on stderr where the sealed message used to. The fourth combination is not a mode: a key in the container with no route to any provider cannot call anything. `--proxy` is kept, undocumented, as the old spelling of `sealed`, and contradicting it with `--mode` is an error rather than a silent winner. `assistant.toml` takes `mode` in place of `proxy`, still defaulting to sealed, and reads the boolean with a note if it finds one. `destroy` deletes every network a mode creates rather than one mode's: which mode you ran last week is not a question a cleanup verb should ask, and `--proxy-network` now adds a name to that list instead of replacing it.

- `--agent prime`, PrimeIntellect's prime-agent. It is pi's CLI in another build -- the release tarball declares `bin: prime-agent` and depends on the `@earendil-works/pi-*` packages -- so the handler subclasses pi's and inherits the reader, the argv and the protocols. Three differences, each measured against 0.9.4 rather than read: the provider block names the credential's variable bare, where `$NAME` arrived at the upstream as that literal string; there is no `--no-approve`, so the mounted directory's own `.prime/agent/settings.json` is read, which steers a run without widening the box; and its only tool is a Python REPL, so the image bakes the kernel, because without it the agent answers by trying to install `uv` and the proxy network has no route for that. The image installs the release tarball, checksummed against the release manifest, rather than an npm package.

### Fixed

- The relay closes a connection it refuses. The request body is still unread when a 401 or 403 is written, so a client that reused the connection had its body parsed as the next request line, and every request after it answered 400. Found with prime-agent, whose first call was refused for an unrelated reason and whose retries then all failed on a poisoned connection.

- The relay reads a chunked request body. A client that streams its request sends no `Content-Length`, and reading zero bytes there left the body in the socket with the same result as above. Both copies of the relay have it, and the suite runs against both.

### Changed

- The variable carrying an agent's generated config is `SANDUK_MODELS_JSON` rather than `SANDUK_PI_MODELS`: two agents write one now, and the name is sanduk's own, not either agent's.


## [0.2.2]

### Added

- `run --mount HOST:DEST[:ro]`, repeatable, for a host directory beside the one `-w` gives the agent -- a repository it may read and must not write, say. A destination at or under `/work` is refused rather than layered: it would hide part of what `-w` put there, and a run reading the wrong files fails nothing. Read-only is rendered as `--mount type=bind,source=,target=,readonly`, which both engines read the same way; `-v host:dest:ro` is Docker's alone, and was measured refusing a write under Apple's engine.

- `approval = true` in `assistant.toml`, with `sanduk approve` and `sanduk reject` deciding one outbox entry at a time and `outbox --pending` listing what waits. Delivery is where the gate belongs because delivery is the only thing that leaves the box: what an agent does, it does inside a container that is then deleted. A decision is not undone by the opposite one. Entries written before the columns existed were delivered on sight, so the migration marks them approved rather than holding a backlog nobody asked to review.

- `mounts` in `assistant.toml`, resolved from the config file's own directory, so `../repo:/repo:ro` means beside the config rather than beside whatever directory the scheduler ran from. Validation stays in `run`: one place decides what a mount may be.

- `run --stats-file PATH`, writing the run's outcome as JSON: exit code, ok, the token line, the error, the report path. `main` returns a status and prints the rest, so a caller recording what a run cost had nowhere to read it; `runs` now shows the tokens per wakeup. A database written before the column gains it on open rather than losing its history.

- A weekly CI job that builds all five agent images and asks each what it is. The images are the only thing here that rots without a commit -- four pinned npm versions and a static binary, whose flags and JSON records change under us, which is how codex's doubled trace and pi's message roles were both found. Also on `workflow_dispatch`.

- An integration test for a wakeup: an `assistant.toml` on disk through a real container and the relay to a report, a row and an outbox entry, with only the model stubbed. Every other assistant test replaces the `run` command, which is the seam that test exists to cover.

- Assistants: `assistant add|list|show|enable|disable`, `tell`, `tick`, `serve`, `outbox`, `runs`. An assistant is a directory with an `assistant.toml`, a `workspace/` the agent keeps its memory in, and a `reports/` holding one task and one report per wakeup. A wakeup composes the brief and any queued messages into a task and calls `run`, so an assistant can do nothing a typed `sanduk run` cannot. What has to outlive a run -- the schedule, the claim that stops two processes waking one assistant, the inbox, the outbox -- is SQLite under `$XDG_STATE_HOME/sanduk/assistants.db`; what an operator edits is the TOML file. Schedules are intervals rather than cron expressions: a parser for the second is a dependency this package does not have, and `every = "30m"` is what a wakeup schedule is. `tick` is one pass and cron can call it; `serve` is that pass on a loop in the foreground, sleeping until the next assistant is due and holding no container or credential in between. SIGINT or SIGTERM stops the loop after the pass in flight, and an interrupted wakeup is not counted as a failure: three interrupts should not disable an assistant that works. Wakeups run one at a time, because `run` installs signal handlers and Python allows that only on the main thread, so a wakeup per worker thread would lose the teardown they exist for. A wakeup that fails keeps its messages, doubles its interval per consecutive failure, and disables the assistant at `max_failures`. Delivery is a command the operator names: sanduk ships no platform adapter and holds no messaging credential.

### Changed

- `stop` and `clean` leave alone a container a live run is using, and say which. Both act on the `sanduk-` prefix, which cannot tell a wakeup in flight from a leftover; the run records can, and now do. `clean --all` takes it anyway, for a run whose process is wedged rather than working. Without this a `make clean` in one terminal deletes a scheduled wakeup's container mid-run, which is the failure nobody is present to see.

- Every container drops all Linux capabilities and runs under an init process. The agent runs as the image's unprivileged user and only reads, writes and forks, so nothing it could keep is anything it needs, and a shell it leaves behind is reaped rather than held by pid 1. `--runtime docker` adds `--security-opt no-new-privileges` and `--pids-limit 1024`, which Apple's CLI has no flags for: a shared kernel is where both matter, and there the ceiling is a fork bomb's rather than a workload's. Not `--read-only`: every shipped agent writes under `$HOME`, and the tmpfs that would take its place is a path `Runtime` has no business knowing.

### Added

- `--agent pi`. pi speaks Anthropic Messages, OpenAI Chat Completions and OpenAI Responses, so it reaches every provider; the provider block names which with an `api` field, preferring Chat Completions where both are served. Its endpoint is neither a variable nor a flag but a `models.json` in its config directory, so the image's entrypoint writes that file from `SANDUK_PI_MODELS` and execs pi -- a config in the bind mount would sit in the user's repository, editable by the agent reading it. `--model` is required, since the provider block lists what pi may select. With `anthropic` the base URL is the bare root: pi appends `/v1/messages` itself, and a base ending in `/v1` reached the relay as `/v1/v1/messages`, which it refused.

## [0.2.1]

### Added

- `--agent codex` and `--agent opencode`. Both learn their endpoint differently from the two that shipped before, which is what forced `argv` to take the wiring. codex has no base-URL variable at all: sanduk names a `model_providers.sanduk` block on the command line with `-c`, and only the key travels in the environment. opencode has no variable either, but takes its whole configuration as JSON in `OPENCODE_CONFIG_CONTENT`; writing an `opencode.json` into the bind mount instead would put it in the user's repository, editable by the agent reading it. codex accepts only `wire_api = "responses"`, so `check` refuses `anthropic` and `openrouter`. opencode reaches every provider, picking an npm driver by wire protocol -- `@ai-sdk/anthropic` or `@ai-sdk/openai-compatible`, both baked into the image because the proxy network cannot fetch one -- and requires `--model`, since its config names exactly one. Both images keep codex's and opencode's own sandbox and approval gates off: the container is the boundary, and a second one inside it only blocks the work.

- `/v1/responses` on `openai-compat`, measured against llama-server build 10850. The route table is the egress allowlist, so without the entry a Responses-only agent could not reach a local model at all.

- Run records and an orphan sweep. A run writes the containers it owns and its own pid to `$XDG_STATE_HOME/sanduk/runs`; every later run deletes the containers of records whose owner is gone. A run killed with SIGKILL runs no teardown, so it left its container alive with the run token inside it, and a `--proxy` run left the network holder as well. Nothing can reap at kill time, so the next run does it. What marks a container reapable is the record, not the `sanduk-` prefix: `--keep` releases it, and a pid whose number has been reused reads as alive, which skips the sweep rather than deleting a container another process is using. A state directory that cannot be written -- no `HOME`, no write permission -- stops the run. Warning and running on without a record would restore the leak this closes, silently.

- `RUNTIME` in the Makefile, passed as `--runtime` by every target that talks to an engine. `make run RUNTIME=docker` used to run against Apple's engine.

### Changed

- SIGTERM and SIGHUP tear the run down instead of killing the process where it stands. Both now raise `SystemExit`, which `launch` treats as `KeyboardInterrupt` does: kill the client, then delete the container. `timeout(1)` and a bare `kill` send SIGTERM, so this was the common half of the leak; SIGKILL is the other half, and the run records are what cover it.

- `Agent.argv` takes the run's `Wiring`. An agent whose endpoint is a config key rather than a variable cannot be driven otherwise. Nothing secret belongs in the result: the credential reaches the container through `Wiring.key_env`, and argv is visible to `inspect`.

- The agent gets `stdin=/dev/null` rather than the parent's. sanduk passes the task as an argument and reads the agent's stdout, so there is nothing to type; codex reads stdin when it is not a terminal, and an inherited one leaves it waiting on a stream nobody writes.

- `IMAGE` is no longer computed in the Makefile and is exported only when set. It named the image per agent through a `hax`-or-`claude` conditional, which sent `make test-container AGENT=codex` at Claude Code's image. The integration suite already falls back to the handler's own image, so the registry is now the single source of that name.

### Fixed

- The CI Docker job built its image with `make image AGENT=hax ENGINE=docker`. `ENGINE` was deleted in 0.2.0, so the build ran against the default runtime, `apple`, which does not exist on a Linux runner.

- codex traced every command twice. One command arrives as `item.started` and again as `item.completed`; the reader printed on any `item.` record. Commands now trace on `item.started`, messages on `item.completed`. An item whose type is `error` is traced as well, instead of being dropped -- codex reports a fatal turn as `turn.failed`, so an error item was the run's only warning and nothing showed it.

## [0.2.0]

### Added

- `destroy` and `system status|start|stop`, which empties the Makefile of engine names: `ENGINE` and `CONTAINERFILE` are gone rather than parameterised. `sanduk destroy` deletes the containers, the image and the network, and deliberately not the `--log-bodies` directory: that is written outside the bind mount so the agent cannot edit its own audit trail, and a cleanup verb removing it would undo the reason it is there. `make destroy` still deletes it, as an explicit `rm -rf` where it reads as the file operation it is. `service_status` is generic -- it reports what `require` finds, so it answers for an engine that is not working, which is the case it exists for -- while `service_start` and `service_stop` raise unless an engine owns them. Docker's daemon belongs to launchd, systemd or Desktop, and says so instead of pretending.

- `build`, `shell`, `ps`, `stop` and `clean`, which is what `make image`, `make shell`, `make ps`, `make stop` and `make clean` were doing by shelling out to one engine. `ps` prints sanduk's own view of name, image and state rather than passing an engine's columns through, which would have relocated the problem instead of fixing it. `stop` and `clean` find their containers through `list_containers`, so nothing outside the `sanduk-` prefix is reachable by either. `shell` goes through neither `run_argv` nor `launch`: the first builds no tty and the second reads stdout as a JSON stream, so it takes a `shell_argv` of its own and inherits the terminal.

- Subcommands. `sanduk run <task>` is what `sanduk <task>` was; `sanduk list agents|providers|runtimes` shows what is registered. `run` is required rather than inferred from a first argument matching no command, because a one-word task -- `clean`, `stop`, `build` -- would otherwise run a destructive verb; the old form errors and names the fix. `--runtime` and `--quiet` moved to a parent parser shared by every command that talks to an engine, so they follow the verb. Defining them on the main parser instead lets the subparser's default overwrite what came before it, which would make `sanduk --runtime docker run` silently use `apple`. `list` takes neither: it reads registries, and accepting `--runtime` would imply it contacts one. See `docs/dev/cli_subcommands.md` for the remaining verbs.

- CI, on `ubuntu-latest` and `macos-latest`. A Linux job builds the hax image under Docker and runs the integration suite against it, which is the only configuration that can prove `--proxy` at all: Apple's engine and Docker Desktop both keep the bridge inside a VM, so no macOS runner can bind the gateway. Lint, format and types run once rather than once per matrix cell, and the version matrix carries the declared bounds only.

- `Runtime.list_containers(prefix)`, returning `Container` rows rather than the engine's text. Apple's CLI has no `--format`, so its implementation reads columns; Docker's asks for named fields, where a value containing a space cannot shift a column and an unknown field fails at the template instead of silently.

- `--runtime docker`. Two verbs differ from Apple's engine and nothing else does: `rm` deletes a container, and the gateway comes out of `IPAM.Config` rather than a status object. `run_argv` is shared, because Apple's engine adopted Docker's flag surface; a test renders one spec through both and fails the day that stops holding. No placeholder container is started, since Docker creates the bridge with the network rather than only while one is attached. `--proxy` needs the bridge gateway to be an address on this host, which a daemon inside a VM (Docker Desktop, Colima, Lima) does not give: the bind fails and says so, rather than the relay listening where the container cannot reach it. Written against the documented CLI and tested against recorded output; not yet run against a daemon.

- `--agent`, selecting the CLI that runs in the container. A handler answers four questions and nothing else: which image carries the program, what flags drive it headlessly, which variables it reads its endpoint and credential from, and how to read the JSON it streams back. Handlers are looked up in the `sanduk.agents` entry-point group, so one in a separate distribution is found the way the shipped two are, and `--agent mypkg:MyAgent` runs one that is not packaged at all. The shipped handlers are also seeded directly, because a source checkout has no entry-point metadata and losing `claude` there is worse than the inconsistency. A plugin that fails to import is reported and skipped; one claiming a shipped name is refused, since replacing `claude` would change what runs in the container without changing the command line. See `docs/agents.md`.

- `--agent hax`, which is what makes `--provider openai`, `openrouter` and `openai-compat` reachable. The relay forwards but does not translate, so those three needed an agent speaking OpenAI Chat Completions and there was none. hax also removes the node runtime from the image: the release binary is static musl. Both endpoints hax would reach a first-party provider on are pinned, so a run always selects `openai-compatible` or `anthropic-compatible`, whose base URL and key come from the environment. `HAX_CATALOG_URL` is emptied inside the container: the model-metadata fetch has no route off `sanduk-net` and could only time out.

- `Provider.api_prefix`, the path segment a handed-over base URL must end in for requests to land on `routes`. `/v1` everywhere but OpenRouter's `/api/v1`. Claude Code did not need it because it appends `/v1/messages` to a bare root itself; an agent given a complete base URL posts off the allowlist without it.

- Four providers behind `--provider`: `anthropic`, `openai`, `openrouter`, `openai-compat`. A `Provider` record carries the upstream, the auth header and scheme, the environment variable its key comes from, the preflight path, and a route table. The route table maps each allowed path to the wire protocol spoken there, so one structure is the egress allowlist, the field `--max-tokens-cap` clamps, and the names the usage line reads. Protocol hangs off the route rather than the provider because OpenAI serves Responses and Chat Completions on two paths of one host. It is declared rather than detected because Anthropic Messages and OpenAI Responses both report `input_tokens` and `output_tokens`, so a body cannot tell them apart.

- `--upstream scheme://host:port`, which points the relay at a local llama-server or any OpenAI-compatible endpoint. Plaintext to anything but a loopback address is refused: the relay writes the real key into every forwarded request, so `http://` off-machine puts the credential on the wire. `--insecure-upstream` overrides. A path in the upstream is an error rather than a silent truncation, since prefixes belong in the allowlist and the agent's base URL, and OpenRouter's `/api/v1` is exactly the case that would otherwise send every request to the wrong place.

- `--agent-key-env` and `--agent-base-url-env`, naming the variables the agent reads inside the container. They default to the handler's, and are separate from the provider's because the host reads the key under one name and the container may want another: against a relay, hax reads `HAX_OPENAI_BASE_URL` where the host read `OPENAI_API_KEY`.

- `make test-live` and the `provider_live` marker, deselected by default alongside `container`. `LLAMA_SERVER` runs the openai-compat suite against a local llama-server for nothing; the bad-key tests reach the real Anthropic, OpenAI and OpenRouter endpoints with no credential at all, because refusing an invalid key needs no valid one. Of the two completion tests, OpenRouter needs only a key and defaults to `openrouter/free`, a router over free models, chosen over a pinned `:free` id because those rotate out of the catalogue and take the test with them. OpenAI has no free tier and stays opt-in through `OPENAI_MODEL`, so nothing is spent unless a model is named.

- `stream_options.include_usage` is added to streamed requests for providers that need it. Measured against llama-server: without the field a stream reports no usage at all, with it the counts arrive including cached tokens. The flag sits on the provider rather than the protocol because OpenAI and OpenRouter both speak Chat Completions and only OpenAI needs it; OpenRouter sends usage in the final chunk unasked, in a chunk whose `choices` array is not empty, unlike OpenAI's.

### Changed

- `make image` and `make run` take `AGENT=hax`, and `IMAGE` and `CONTAINERFILE` follow it. Building one agent's image under the other's tag was the failure this prevents: `image_exists` would find it and skip the build.

- `resources/Containerfile` is `Containerfile.claude`, beside `Containerfile.hax`. `--containerfile` follows `--agent` and the base handler declares none, so a handler that names no Containerfile is an error naming the handler rather than a build of Claude Code's image under someone else's agent.

- A provider the agent cannot speak to is refused before the image is built. `--provider openai` with Claude Code was accepted and then 404'd by the relay mid-run, since Claude Code speaks Anthropic Messages only. `Agent.check` also refuses a flag the agent has no equivalent for: hax has no approval gate, so `--allowed-tools` and `--permission-mode` are errors rather than silently dropped restrictions.

- Reading the agent's stream moved from `launch` into a per-run `Reader`, and the usage line with it. Token counts are not comparable across agents: Claude Code reports cache reads beside `input_tokens` and hax reports them inside it. Measured against llama-server, a hax round-trip carrying 2252 input tokens reports 2192 of them as cached, so summing the two would count the cache twice. `Outcome.stats` is therefore a formatted line, not a struct with no shared meaning to normalize to. A fresh reader per run also keeps one run's tally out of the next.

- OpenRouter is validated at `/api/v1/key`, not `/api/v1/models`. Measured without a credential: models answers 200 to an anonymous request, so a preflight pointed there would accept any key, including a garbage one, and the 0.27s-against-174s saving it exists for would be imaginary. A live test asserts both halves, the rejection and the miss it replaces.

- The usage line omits a counter its protocol does not report instead of printing zero for it. OpenAI-shaped responses carry no cache-write count, and `cache_write=0` cannot be told apart from a real zero. Cached tokens are read through nested keys for the same reason: OpenAI reports them at `prompt_tokens_details.cached_tokens`, two levels down, where a flat scan dropped them and left `cache_read=0`.

- Model policy, the token cap, and usage injection share one pass over the request body. The old guard returned before parsing unless `--allow-model` or `--max-tokens-cap` was set, which was correct while policing was the only thing that happened there. Left as it was, usage injection would have fired only on runs that also set a policy flag, and silently never on a plain run.

- `start_proxy` takes its allowlist and upstream from the provider rather than from the Anthropic constants. Naming a provider without also naming an allowlist kept Anthropic's, so every OpenAI path 403'd.

- `tests/test_script.py` no longer compares the relay between the package and `scripts/sanduk.py` as syntax. The package's relay is now parameterised by provider and the script's is Anthropic-only by design, so six shared methods diverge on purpose and an AST comparison could only be widened until it meant nothing. `tests/test_proxy.py` runs all twenty relay tests against both copies instead, which compares behaviour. Two guards hold it in place: one asserts the excluded names still exist on both sides, because an intersection drops a deleted name without complaint, and one asserts the parameterisation itself is still there.

- `scripts/sanduk.py` states its scope. It stays Anthropic, Claude Code and Apple `container` only, so it keeps running with nothing beside it.

### Fixed

- `tests/test_container.py` skipped every non-Apple run. It took the engine's readiness from `<cli> system status`, which Docker has no equivalent for, so a Docker run skipped with a message about a service that does not exist. It now calls `Runtime.require`, which is each engine's own check. The suite also asserted `Claude Code` in `--version` and read leftovers out of `container list -a`, both of which the `AGENT` and `RUNTIME` variables were supposed to have made neutral.

- The relay forwarded a container-supplied `api-key` header upstream. It dropped `x-api-key` and `authorization`, which were the only two credential headers it used, and passed anything else through. The header is inert against Anthropic and is the credential for Azure OpenAI, so the general shape of the bug is that a header meaning nothing to one provider is the key for the next. All four known credential headers are now dropped whatever the provider. Fixed in `scripts/sanduk.py` too.

## [0.1.0]

### Added

- `sanduk`: build a Linux VM through Apple `container`, run Claude Code headless in it against a bind-mounted directory, collect `REPORT.md`, delete the container. `--dry-run` prints the command instead.

- `--proxy`: run the agent on an `--internal` network with no route off the host, and relay its API calls through a host-side proxy that holds the key. The container gets a per-run token. Chosen over passing the key in as an environment variable because the container otherwise has a live credential and unrestricted egress, which makes "sandbox" true of the filesystem only. The relay is not optional overhead: on an egress-blocked network it is the container's only path to the API, so it must exist regardless, and injecting the key there costs two lines.

- `--allow-model` and `--max-tokens-cap`: model allowlist and token ceiling applied to request bodies at the relay. Enforced on the host, where the container cannot edit them, which is the point of doing it here rather than in the agent's flags.

- `tests/test_script.py` compares every function, method, and constant the standalone script and the package share, normalized for annotations and docstrings. Pinning the Containerfile alone was not enough: three relay fixes had landed in the package and not in the script, one of them a bodyless GET being dropped with no response. Eight names differ by design -- `die` against `AgentboxError`, `util.note`, and typing -- and are listed in the test; anything else fails it.

- `python3` in the agent image. Two runs in a row reached their verdict by hand-tracing rather than execution -- once against C with no compiler, once against a Python file with no interpreter -- and each spent turns discovering the absence before working around it. With an interpreter present the agent ports the file, runs it, and marks findings reproduced. Shipping a toolchain for every language the agent might meet is unbounded; one interpreter covers the common workload. It is not free: execution buys more turns, not fewer.

- Per-request token counts on the relay's log line: `in=`, `cache_write=`, `cache_read=`, `out=`. Both response shapes are read, because Claude Code sends `stream=false` and the API also streams: server-sent events carry usage in `message_start` and `message_delta`, a JSON body carries it once at the top level. Only SSE lines holding `"usage"` are parsed, so a stream is still forwarded chunk by chunk; a JSON body is buffered to 256KB and parsed at the end, since nothing in it can be read until it is whole. The request digest under `--log-bodies` gained `stream=` so the two are told apart without guessing. A `/v1/messages` response that yields no counts logs `usage=?` rather than a line that looks ordinary. Answers what the end-of-run total cannot: how much of each turn was a cache read, and so whether a second container reuses the first one's cached prefix.

- `--log-bodies`: record every request body the agent sends upstream. A digest line per call plus full JSON under `--log-dir`, which defaults outside the bind mount so the agent cannot read or edit its own audit trail. Bodies carry the system prompt and the contents of every file read, so they are written to files rather than to the terminal.

- Preflight validation of `ANTHROPIC_API_KEY` against the real endpoint before any container is created. A bad key inside the container costs 174s of SDK retry backoff before failing; the preflight rejects it in 0.27s.

- Preflight warning when the macOS application firewall has the running interpreter set to "Block incoming connections". That configuration drops the container's connection to the relay with no error, so the first API call hangs until `--timeout`. The check reads `socketfilterfw --listapps`, matches the framework `Resources/Python.app` path that `sys.executable` does not resolve to, and prints the exact `--unblockapp` command.

- `Makefile` and a pytest suite: 54 fast tests using a local fake upstream, 7 integration tests that boot real VMs. Neither makes an API call, so `make test` costs nothing and needs no key.

### Changed

- `sanduk.py` is now the `sanduk` package under `src/`, installed as an `sanduk` console script. The 762-line script had one module for the CLI, the relay, the Apple `container` calls, and the Claude Code flags, which is exactly the shape that makes a second container engine or a second agent an edit through the middle of it. The pre-package script is kept at `scripts/sanduk.py`, which still runs standalone through its PEP 723 header. It now embeds the Containerfile as a raw string and writes it to a temporary build context when `--containerfile` is absent, so a copied script needs nothing beside it; an explicit `--containerfile` that is missing is still an error rather than a silent fall back. `tests/test_script.py` keeps the embedded copy byte-identical to `sanduk/resources/Containerfile`.

- Every call to a container engine moved behind `runtime.Runtime`, with `AppleContainer` the only implementation. `ContainerSpec` describes a container to run and `run_argv` renders it, so the placeholder container and the agent container go through the same code. A Docker or Podman subclass has to supply four things: the CLI name, the delete verb (`rm`, not `delete`), how `network inspect` reports the gateway, and whether the host bridge needs a placeholder container at all. Neither engine is installed here, so neither is written -- an untested backend is worse than an absent one.

- The Containerfile ships as package data at `sanduk/resources/Containerfile`, and `--containerfile` defaults to it. Previously the default was the string `"Containerfile"`, resolved against the working directory, so the tool only built an image when run from a checkout.

- `die()` became `AgentboxError`, and `main` returns an exit code instead of raising `SystemExit`. Library code that calls `sys.exit` cannot be embedded. The timeout path benefits directly: teardown caught `except SystemExit` and so also caught any unrelated `sys.exit` on the way out; it now catches the one exception it means.

- One Makefile. The packaging frontend and the container frontend both defined `build`, `rebuild`, `test`, and `clean` with different meanings. Image targets are now `image` and `image-rebuild`; `build` is the Python one; `clean` deletes containers and build artifacts; `distclean` adds the resolved environment, `destroy` adds the image, network, and logs. Help is generated from `##` comments rather than a hand-maintained echo list that drifts.

- pytest, ruff, and mypy configuration consolidated into `pyproject.toml`; `pytest.ini` deleted. Both files declared `testpaths`, and `pytest.ini` silently won.

- `requires-python` raised to 3.11, matching what the PEP 723 header already declared.

- Merged `keyproxy.py` into `sanduk.py`. The relay had no second consumer and no CLI of its own, and the split made `sanduk.py` fail with `ModuleNotFoundError` the moment it was copied anywhere without its sibling. Absolute-path and symlink invocation both happened to work, which is what made the failure easy to miss.

- The relay binds the network's bridge gateway rather than `0.0.0.0`. The wildcard bind put it on Wi-Fi and LAN as well. Because vmnet only creates the bridge while a container is attached, a placeholder container now holds the network up long enough to bind, and is torn down with the run.

- Dropped the relay's peer-subnet check. Once bound to the gateway it admitted the only caller class the bind does not already exclude: a host process reaching `192.168.128.1` presents source IP `192.168.128.1`, which is inside the subnet. Access control is the run token alone.

- The task prompt is no longer written to `TASK.md` on the mount. The agent found its own instructions there as a file and spent two of six turns identifying them, and pointing `-w` at a real repository dropped a file into it. The prompt already arrives via `-p`.

- Both scripts declare their interpreter with PEP 723 and `uv run --script`.

### Fixed

- `scripts/agentbox.py` is `scripts/sanduk.py`. The rename to sanduk changed the file's contents but not its name, and `tests/test_script.py` finds it by path, so both drift tests skipped with the reason `scripts/ is not in this tree` -- which was false. The two tests that exist to catch a stale embedded Containerfile were themselves silently disabled.

- `--proxy`: the relay now offers only `gzip` upstream, for clients that already accept it. The API answers in brotli whenever a client lists it, Claude Code's does, and no standard-library module decodes brotli -- so the token counts above read compressed bytes and silently found nothing. Narrowing the offer keeps the response compressed and decodable; adding a brotli dependency to read a log line was the alternative. A client that asked for `identity`, or for something else entirely, still gets what it asked for.

- `--proxy`: a request with no body -- any GET, including `/v1/models`, which is on the default allowlist -- was dropped with the connection closed and no response. `apply_policy` returned the body it was given and `relay` read `None` back as "refused", so "there is nothing to check" and "this was rejected" were the same value. The refusal is now a separate flag. Every GET the suite covered stopped at a 401 or 403, so nothing reached the path that conflated them.

- Path allowlist matches `urlsplit(path).path` exactly instead of by prefix. Prefix matching admitted `/v1/models-internal-secret`; matching the raw path would have rejected `/v1/messages?beta=true`, which is what Claude Code actually calls. Both cases now have tests.

- The relay reads upstream with `read1`. `read(n)` blocks until `n` bytes arrive, which stalled every server-sent event behind a 64KB buffer.

- `--timeout` is enforced by a timer, not by a deadline checked inside the response loop. An agent that hangs without printing produces no lines, so the loop-checked deadline never fired.

- Teardown catches `SystemExit` as well as `KeyboardInterrupt`. A timeout kill exits through `die()` and previously skipped the delete, leaving a container alive holding the key.

- `validate_key` runs before the placeholder container is started. A rejected key used to leak that container.

- The token summary counts `cache_creation_input_tokens` and `cache_read_input_tokens`. A run billed at $0.23 was reported as 10 input tokens; 74% of its input was cache reads.

- `make clean` no longer deletes `sanduk-logs`. Recorded request bodies are evidence, not scratch; they move to `make destroy`, which reports the file count.

- `make destroy` is idempotent and no longer prints `Error 1 (ignored)` when the image or network is already gone.

- `make run` quotes `$(TASK)`. The default task is five words and was being split into five positional arguments.
