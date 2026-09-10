# TODO

Ordered by how much they would change a decision, not by effort.

## Untested

- **Kata Containers under `--oci-runtime`.** A VM per container on Linux, where Docker otherwise shares the host kernel. Measure `sealed` mode (the relay on the host gateway), `/work` under Cloud Hypervisor or QEMU, and Firecracker's lack of filesystem sharing. Needs KVM; standard GitHub runners may not expose it. See [docs/dev/microvms.md](docs/dev/microvms.md).

- **A long run.** Everything measured so far finishes in ~35s. No real agent has hit `--timeout`, exhausted `--max-turns`, or run long enough to trigger context compaction.

## Correctness

- **Each provider's route table is the paths one workload was seen to use.** A different task (web search, subagents, MCP) may call something else and get a 403. The failure is legible in the proxy log, but the fix is manual: `--proxy-allow-path`.

- **OpenAI's `/v1/responses` has not been checked against a real response.** codex speaks it, but the cap field and usage names come from the documentation.

- **OpenAI completions are untested.** The bad-key path is covered live; a real completion needs a key and a model id, and none has been run. OpenRouter's have, through the 0.2.3 budget work.

- **`stream_usage_option` is a guess for any openai-compatible server but llama.cpp.** Measured there, both directions: no field means no streamed usage, the field means full counts. A stricter server could reject it outright, and nothing has tried one.

- **The relay buffers request bodies fully in memory** before forwarding. Fine at 60KB; unexamined for file uploads.

- **The firewall preflight only detects an explicit Block entry.** An interpreter that would merely prompt is not caught, and the symptom is identical: a hang.

- **`--log-dir` is not checked against being inside the bind mount**, which would hand the agent its own audit trail.

- **A timed-out run records no token count.** The reader's partial tally is discarded with the kill.

## Design

- **Docker is measured only in CI.** This machine has Apple's engine alone; the CI job runs Docker on a native Linux daemon. Docker Desktop, Colima and Lima cannot run the relayed modes, because the bridge gateway is not bindable from the host. Podman is not implemented; see [docs/dev/podman.md](docs/dev/podman.md).

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
