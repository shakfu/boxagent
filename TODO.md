# TODO

Ordered by how much they would change a decision, not by effort.

## Untested

- **Kata Containers under `--oci-runtime`.** A VM per container on Linux, where Docker otherwise shares the host kernel. Measure `sealed` mode (the relay on the host gateway), `/work` under Cloud Hypervisor or QEMU, and Firecracker's lack of filesystem sharing. Needs KVM; standard GitHub runners may not expose it. See [docs/dev/microvms.md](docs/dev/microvms.md).

- **A long run.** Everything measured so far finishes in ~35s. No real agent has hit `--timeout`, exhausted `--max-turns`, or run long enough to trigger context compaction.

## Correctness

- **`--budget` does not hold across concurrent calls.** The relay checks `spent` before forwarding and updates it only after a response completes, so any number of calls pass while earlier ones are in flight. Five concurrent calls billed $2.00 against a $1.00 ceiling with nothing rejected, against a README that promises the ceiling is crossed by exactly one call. A client that hangs up mid-stream skips the accounting block, so billed work goes uncounted. Three decisions, not one: admission, the disconnect path, and what a response carrying no usage block means for further admission -- currently a silent zero. Serializing budgeted calls keeps the documented guarantee and throttles a parallel agent to one call at a time; reserving `--max-tokens-cap` worth of credit at admission keeps concurrency and bounds the overrun by the number in flight instead.

- **`--log-dir` is not checked against being inside the bind mount.** The default `./sanduk-logs` lands in `/work` under `-w .`, which hands the agent its own audit trail, and `--help` claims the opposite unconditionally. Body files are created with an ordinary `open(..., "wb")`, so a predictable future log name can also be pointed at a host path through a symlink. Resolve the directory against the workspace and every read-write `--mount`, and create entries `O_EXCL|O_NOFOLLOW`.

- **`sweep()` discards ownership when the engine is merely unreachable.** Both engines return an empty container list when their list command fails, so a stopped daemon looks like an engine with no containers and the records naming real ones are deleted. Reproduced with docker installed and its daemon down; the missing-binary case is already handled. `Runtime.destroy()` only logs a failed delete, and ordinary teardown releases its record without confirming the container is gone. In `--mode open` the container holds the real key, so one that is never reaped holds a credential for as long as it exists.

- **Concurrent wakeups share one stats file.** Every wakeup writes `$XDG_STATE_HOME/sanduk/wakeup.json`, deleting it before the run and after reading it. Claims are per assistant, so two scheduler processes running different assistants collide: one deletes another's stats, reads its token totals, or has its result recorded against the wrong name. The documented cron entry runs `tick` every 10 minutes against a 900s default timeout, so overlapping passes are the default shape. Key the path on the database run id.

- **The relay and holder are acquired outside the lifecycle cleanup block.** Both start before the `try/finally` around `launch()`, so a failure in mount validation, log-directory creation or relay binding leaves a holder container running and a relay listening. Mounts are validated only while building the container command, after all of it. `log_dir.mkdir` and the bind raise `OSError`, which is not `AgentboxError`, so it escapes `main()` past `tick` and `serve` and kills a long-lived scheduler outright. Validate static options first, then enclose every acquisition in one scope.

- **Nothing checks that a network is actually internal.** `ensure_network` accepts any existing network with a gateway and subnet, and neither runtime's `network_info` returns the `internal` flag. A routable network left by a `key-safe` run is reused by a `sealed` run through the same `--proxy-network` name, and the CLI then prints "no route off the host" having checked nothing. Operator-triggered rather than agent-triggered, which is why it sits below the rest; the false assertion is the defect. Verifying a fix needs a real engine, so it lands in CI rather than `make test`.

- **Each provider's route table is the paths one workload was seen to use.** A different task (web search, subagents, MCP) may call something else and get a 403. The failure is legible in the proxy log, but the fix is manual: `--proxy-allow-path`.

- **OpenAI's `/v1/responses` has not been checked against a real response.** codex speaks it, but the cap field and usage names come from the documentation.

- **OpenAI completions are untested.** The bad-key path is covered live; a real completion needs a key and a model id, and none has been run. OpenRouter's have, through the 0.2.3 budget work.

- **`stream_usage_option` is a guess for any openai-compatible server but llama.cpp.** Measured there, both directions: no field means no streamed usage, the field means full counts. A stricter server could reject it outright, and nothing has tried one.

- **The relay buffers request bodies fully in memory** before forwarding. Fine at 60KB; unexamined for file uploads.

- **The firewall preflight only detects an explicit Block entry.** An interpreter that would merely prompt is not caught, and the symptom is identical: a hang.

- **A timed-out run records no token count.** The reader's partial tally is discarded with the kill.

- **The README contradicts itself on CI.** One section describes `.github/workflows/ci.yml`; a later one says there is no CI. `providers.py` still opens "Only Anthropic is implemented" above four `Provider` rows.

## Design

- **Docker is measured in CI and once by hand.** On 2026-09-11, docker-ce 29.8.0 on Ubuntu 24.04 passed the container suite for hax and claude, under runc and under gVisor's runsc 20260831. Ubuntu's snap package cannot run a container under `no-new-privileges`, so `run` refuses it. Docker Desktop, Colima and Lima cannot run the relayed modes, because the bridge gateway is not bindable from the host. Podman is not implemented; see [docs/dev/podman.md](docs/dev/podman.md).

- **`--effort`, `--bare` and `--permission-mode` are Claude Code's flags on the shared parser.** claude and hax read them; the other five do not. A `--` passthrough is the cheaper shape.

- **No subscription auth path.** `CLAUDE_CODE_OAUTH_TOKEN` works in the container but not through the relay, which authenticates with the provider's own header and expects an API key behind it. Codex CLI defaults to ChatGPT sign-in for the same reason, so this blocks it too. Deliberate: an account credential in a VM is worse than a scoped one.

- **The placeholder container costs a VM boot and 256MB** for the duration of every relayed run (`key-safe`, `sealed`), purely so the bridge exists before the relay binds. Worth checking whether a shorter-lived container or a retrying bind would do.

- **`sanduk-logs` grows without bound.** No rotation, no cap.

- **No container reuse.** Every run pays a fresh VM boot. Fine for the experiment; wrong if this ever runs in a loop.

- **Someday: re-implement in Go.** A static binary removes the Python 3.11 install step, which macOS does not provide. Go's `net/http` would also stream relay bodies. Go over Rust: the stdlib covers the relay and subprocess work, where Rust needs `tokio`, `hyper` and `rustls`. Costs: the entry-point plugin model, the importable package, and zero runtime dependencies (SQLite, TOML). Wait until the `Runtime` and `Agent` seams settle. Until then, ship through `uv tool` or PyApp.

## Nice to have

- `--report` copies `REPORT.md` out, but nothing collects other artifacts the agent writes outside the mount.

- The stream trace prints tool names only. Tool inputs would make a failed run easier to read, at the cost of terminal noise.

- No way to resume or re-attach to a `--keep` container from the runner. See [docs/dev/adoption.md](docs/dev/adoption.md).
