# TODO

Ordered by how much they would change a decision, not by effort.

## Untested

- **`--allow-model` against live traffic.** Only ever exercised with synthetic requests. The bodies show `"model":"claude-opus-5"`, so it should pass, but that is inference from a log, not a measurement.

- **Concurrent runs.** Each run gets its own token and ephemeral port but shares `boxagent-net`. Two agents at once is untested; so is one run's placeholder container being torn down while another run still needs the bridge.

- **A long run.** Everything measured so far finishes in ~35s. Nothing has hit `--timeout`, exhausted `--max-turns`, or run long enough to trigger context compaction.

- **A failing agent.** No test covers the agent erroring out, being killed mid-stream, or writing no `REPORT.md`. The code paths exist and are read, not run.

## Correctness

- **`DEFAULT_ALLOW` is three paths observed in one workload.** A different task (web search, subagents, MCP) may call something else and get a 403. The failure is legible in the proxy log, but the fix is manual: `--proxy-allow-path`.

- **The relay buffers request bodies fully in memory** before forwarding. Fine at 60KB; unexamined for file uploads.

- **The firewall preflight only detects an explicit Block entry.** An interpreter that would merely prompt is not caught, and the symptom is identical: a hang.

- **`--log-dir` is not checked against the `AF_UNIX` 104-byte limit** or against being inside the bind mount, which would hand the agent its own audit trail.

## Design

- **Provider coupling is shallow but real.** `UPSTREAM`, `DEFAULT_ALLOW`, the `x-api-key` header, and `anthropic-version` are the only Anthropic-specific parts of the relay. Worth a decision: keep it Anthropic-only and say so, or make those four things configurable, the way `runtime.Runtime` now handles the engine.

- **The engine seam has one implementation.** `Runtime` was shaped against Apple `container` alone, so its assumptions are unfalsified: that `stop` then delete is the teardown, that `-e NAME` inherits, that `network create --internal` blocks egress, that `--cpus`/`--memory` are accepted. Docker and Podman are not installed on this machine, so none of it is measured. The first real second engine will move the seam.

- **The agent seam has one implementation.** `agent.py` speaks Claude Code's stream-json and its flag names. A second agent needs the same treatment as the engine: a base class, or a decision that this stays Claude-only.

- **No subscription auth path.** `CLAUDE_CODE_OAUTH_TOKEN` works in the container but not through the relay, which speaks `x-api-key`. Deliberate: an account credential in a VM is worse than a scoped one.

- **The placeholder container costs a VM boot and 256MB** for the duration of every `--proxy` run, purely so the bridge exists before the relay binds. Worth checking whether a shorter-lived container or a retrying bind would do.

- **`boxagent-logs` grows without bound.** No rotation, no cap.

- **No container reuse.** Every run pays a fresh VM boot. Fine for the experiment; wrong if this ever runs in a loop.

## Nice to have

- `--report` copies `REPORT.md` out, but nothing collects other artifacts the agent writes outside the mount.

- The stream trace prints tool names only. Tool inputs would make a failed run easier to read, at the cost of terminal noise.

- No way to resume or re-attach to a `--keep` container from the runner.
