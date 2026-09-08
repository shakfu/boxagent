# TODO

Ordered by how much they would change a decision, not by effort.

## Untested

- **Concurrent runs.** Each run gets its own token and ephemeral port but shares `sanduk-net`. Two agents at once is untested; so is one run's placeholder container being torn down while another run still needs the bridge.

- **A long run.** Everything measured so far finishes in ~35s. Nothing has hit `--timeout`, exhausted `--max-turns`, or run long enough to trigger context compaction.

- **A failing agent.** No test covers the agent erroring out, being killed mid-stream, or writing no `REPORT.md`. The code paths exist and are read, not run.

## Correctness

- **Each provider's route table is the paths one workload was seen to use.** A different task (web search, subagents, MCP) may call something else and get a 403. The failure is legible in the proxy log, but the fix is manual: `--proxy-allow-path`.

- **OpenAI's `/v1/responses` is declared but never exercised.** No agent here speaks it. The cap field and usage names come from the documentation, not from a response.

- **OpenAI and OpenRouter completions are untested.** The bad-key path is covered live for both; a real completion needs a key and a model id, and neither has been run.

- **`stream_usage_option` is a guess for any openai-compatible server but llama.cpp.** Measured there, both directions: no field means no streamed usage, the field means full counts. A stricter server could reject it outright, and nothing has tried one.

- **The relay buffers request bodies fully in memory** before forwarding. Fine at 60KB; unexamined for file uploads.

- **The firewall preflight only detects an explicit Block entry.** An interpreter that would merely prompt is not caught, and the symptom is identical: a hang.

- **`--log-dir` is not checked against the `AF_UNIX` 104-byte limit** or against being inside the bind mount, which would hand the agent its own audit trail.

## Design

- **The engine seam has one implementation.** `Runtime` was shaped against Apple `container` alone, so its assumptions are unfalsified: that `stop` then delete is the teardown, that `-e NAME` inherits, that `network create --internal` blocks egress, that `--cpus`/`--memory` are accepted. Docker and Podman are not installed on this machine, so none of it is measured. The first real second engine will move the seam.

- **The agent seam has one implementation.** `agent.py` speaks Claude Code's stream-json and its flag names. Pointing a different agent at the relay is now two flags, `--agent-key-env` and `--agent-base-url-env`, but nothing parses another agent's event stream or builds its argv, so the trace and the usage line would be empty.

- **`--model`, `--effort`, `--bare` and the rest are Claude Code's flag names on the top-level parser.** A second agent either reuses them wrongly or needs its own group. A `--` passthrough is the cheaper shape.

- **No subscription auth path.** `CLAUDE_CODE_OAUTH_TOKEN` works in the container but not through the relay, which authenticates with the provider's own header and expects an API key behind it. Codex CLI defaults to ChatGPT sign-in for the same reason, so this blocks it too. Deliberate: an account credential in a VM is worse than a scoped one.

- **The placeholder container costs a VM boot and 256MB** for the duration of every `--proxy` run, purely so the bridge exists before the relay binds. Worth checking whether a shorter-lived container or a retrying bind would do.

- **`sanduk-logs` grows without bound.** No rotation, no cap.

- **No container reuse.** Every run pays a fresh VM boot. Fine for the experiment; wrong if this ever runs in a loop.

## Nice to have

- `--report` copies `REPORT.md` out, but nothing collects other artifacts the agent writes outside the mount.

- The stream trace prints tool names only. Tool inputs would make a failed run easier to read, at the cost of terminal noise.

- No way to resume or re-attach to a `--keep` container from the runner.
