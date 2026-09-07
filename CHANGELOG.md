# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Nothing is released yet; everything below is unreleased initial work.

## [Unreleased]

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
