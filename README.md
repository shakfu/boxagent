# boxagent

Run an agent inside a disposable container. The agent does its work, writes a report to a bind-mounted directory, and the container is deleted.

Today that means Claude Code on macOS through Apple's [`container`](https://github.com/apple/container). The container engine sits behind `boxagent.runtime.Runtime` and the agent CLI behind `boxagent.agent`, so Docker, Podman, and other agents are additive.

In its stronger mode the VM has no route off the host and never holds the API key: a host-side relay injects the credential, and the container gets a per-run token that is worthless anywhere else.

## Requirements

- Mac with Apple silicon, macOS 26 or later (`container` requires both)

- [`container`](https://github.com/apple/container) 1.2.0 or later

- Python 3.11 or later, and `uv`

- An Anthropic API key in `ANTHROPIC_API_KEY`

Apple's `container` runs **Linux** containers as lightweight VMs. There is no such thing as a macOS container here; anything needing Xcode or the macOS toolchain cannot be the workload.

## Quickstart

```
make sync
make image
export ANTHROPIC_API_KEY=sk-ant-...
make run TASK='Summarise every Python file here.' WORK=./work
```

Or directly, which is where all the flags live:

```
uv run boxagent 'Summarise every Python file here.' -w ./work --proxy
uv run boxagent --help
```

## Two modes

|                                   | default          | `--proxy`                        |
| --------------------------------- | ---------------- | -------------------------------- |
| Host filesystem                    | VM isolation     | VM isolation                     |
| API key location                   | inside the VM    | host only; VM holds a run token  |
| Egress                             | unrestricted     | none                             |
| Agent can POST your source anywhere| yes              | no                               |
| Which endpoints the agent may call | all              | three, matched exactly           |
| Record of what it sent upstream    | none             | `--log-bodies`                   |

The default mode is filesystem isolation and nothing more. `--proxy` is where the containment is.

## How --proxy works

1. `boxagent-net` is created with `--internal`: no route off the host.

2. vmnet only creates the host bridge while a container is attached, so a placeholder container is started first and torn down at the end.

3. The relay binds the bridge gateway only, so it is unreachable from Wi-Fi or LAN.

4. The container is given `ANTHROPIC_BASE_URL` and a per-run token as its `ANTHROPIC_API_KEY`. Both arrive through the child process environment, so neither appears in `ps`, and `container inspect` shows the token, not the key.

5. The relay checks the token, checks the path against an exact allowlist, applies any model or token policy, swaps in the real key, and streams the response back.

## Flags worth knowing

`--allow-model claude-opus-5` and `--max-tokens-cap N` are enforced on the host, where the container cannot edit them. Claude Code asks for `max_tokens: 64000` on every call, so a cap below that silently truncates every request.

`--log-bodies` records each request body: a digest line per call, full JSON under `--log-dir` (default `./boxagent-logs`, deliberately outside the bind mount so the agent cannot read or edit its own audit trail). Bodies contain the system prompt and every file the agent has read.

`--dry-run` prints the `container run` command and exits. `--keep` leaves the container for inspection, and warns that `container inspect` then exposes the token.

## Layout

```
src/boxagent/
    cli.py         flags, lifecycle, teardown
    runtime.py     container engines; ContainerSpec; only `apple` is implemented
    agent.py       the agent CLI inside the container; only Claude Code
    proxy.py       the host-side relay
    preflight.py   key validation, macOS firewall check
    resources/
        Containerfile
```

A second engine is a `Runtime` subclass and a `RUNTIMES` entry. It must supply four things: the CLI name, the verb that deletes a container (`rm`, not `delete`), how `network inspect` reports the gateway, and whether the host bridge needs a placeholder container to exist at all.

## Make targets

`make help` lists all of them. The ones you need:

```
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
make stop             Stop boxagent containers, leave them on disk
make clean            Delete them and build scratch. Keeps work/ and logs
make distclean        clean, plus .venv and tool caches
make destroy          clean, plus the image, the network, and the logs
make system-start / system-stop / system-status
```

`make system-stop` stops Apple's container service for everything on the machine, not just boxagent.

## Testing

The fast suite makes no API calls and needs no key: the relay is exercised against a local fake upstream, and the preflight is monkeypatched. The integration suite boots real VMs and proves the relay by the 401 an invalid key earns from the real endpoint, which is itself proof the request arrived.

## Measured on this setup

| | |
| --- | --- |
| Bad key, host preflight | 0.27s |
| Bad key, no preflight | 174s of in-container retry backoff |
| Agent run, 3 files, 7 turns | 35.4s wall, 23.2s of it upstream |
| Container direct egress on `boxagent-net` | `000` |
| Real key present in container environment | 0 occurrences |

## Known traps

The macOS application firewall silently drops connections to a binary set to "Block incoming connections", so the agent's first API call hangs until `--timeout` rather than failing. Homebrew's Python is shipped blocked on at least one machine; uv's interpreters are signed and auto-allowed. `--proxy` runs a preflight that names the exact `socketfilterfw --unblockapp` command when it sees an explicit block. It cannot detect an interpreter that will merely prompt.

`AF_UNIX` paths cap at 104 bytes on macOS, which matters if you point `--log-dir` somewhere deep.
