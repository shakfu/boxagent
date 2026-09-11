# Docker Sandboxes (`sbx`)

Status: investigated from documentation, 2026-09-11. Not installed or measured. Decision: do not integrate; pursue Kata under `--oci-runtime` instead ([microvms.md](microvms.md)). Claims cite primary sources; inference is marked.

Scope: whether sanduk should run agents through Docker Sandboxes ([docs](https://docs.docker.com/ai/sandboxes)).

## What it is

- **Isolation:** one microVM per sandbox, each with its own Docker daemon ([architecture](https://docs.docker.com/ai/sandboxes/architecture/)).
- **Credentials:** a host-side proxy injects the key. The VM sees a sentinel such as `proxy-managed`: "the real credential never enters the sandbox" ([credentials](https://docs.docker.com/ai/sandboxes/configuration/credentials/)). Built-in services include `anthropic`, `openai` and `openrouter`; kits can declare others.
- **Egress:** all outbound TCP goes through the host proxy. Default is deny; UDP and ICMP are blocked. The default allowlist includes "broad wildcards" such as `*.googleapis.com` ([security](https://docs.docker.com/ai/sandboxes/security)).
- **Host services:** reached as `host.docker.internal` after `sbx policy allow network localhost:PORT` ([development](https://docs.docker.com/ai/sandboxes/workflows/development/)).
- **Lifecycle:** sandboxes persist until `sbx rm`. Unattended use is `sbx create`, `sbx exec`, `sbx rm` ([automation](https://docs.docker.com/ai/sandboxes/workflows/automation/)).
- **Images:** templates must use `FROM docker/sandbox-templates:<variant>` and are pulled from a registry ([templates](https://docs.docker.com/ai/sandboxes/customize/templates/)).
- **Agents:** Claude Code, Codex, Copilot, Cursor, Devin, Docker Agent, Droid, Gemini, Kiro, OpenCode, and a bare shell ([agents](https://docs.docker.com/ai/sandboxes/agents)). hax, hermes, pi and prime would each need a kit ([build an agent](https://docs.docker.com/ai/sandboxes/customize/build-an-agent/)).
- **Requirements** ([install](https://docs.docker.com/ai/sandboxes/install/), [FAQ](https://docs.docker.com/ai/sandboxes/faq/)):
  - macOS 14+ on Apple silicon, Windows 11, or Ubuntu 24.04+. Ubuntu derivatives are unsupported.
  - Linux needs KVM and the user in the `kvm` group.
  - A Docker account: `sbx login` (OAuth) or a PAT.
  - Telemetry on by default; `SBX_NO_TELEMETRY=1` disables it.
  - Docker Desktop and Docker Engine are not needed.

## Against sanduk

| | sanduk, docker | sanduk, apple | sbx |
|-|-|-|-|
| Kernel boundary on Linux | shared (gVisor optional) | n/a | VM |
| Key off the container | `key-safe`, `sealed` | same | yes |
| Egress rule granularity | exact path | exact path | domain:port (inference: no path rules documented) |
| `--allow-model`, `--max-tokens-cap` | yes | yes | no |
| Body log | `--log-bodies` | same | not documented |
| Deleted after the run | yes | yes | only on `sbx rm` |
| Arbitrary image | yes | yes | no |
| Offline with a local model | yes | yes | unknown; login and Hub pulls need Docker's domains |
| Account | no | no | yes |

sbx adds a boundary only on Linux. On macOS, Apple's `container` already gives a VM per container.

## Fit with `Runtime`

Poor. `Runtime.run_argv` renders the Docker CLI's `--network`, `--cap-drop`, `-v`, `--entrypoint`, `--cpus` and `--memory`. `ensure_network` relies on `network create --internal` and a bindable gateway. sbx documents none of these. An `Sbx` subclass would override nearly every method, and the relay wiring would not apply.

## Options considered

1. **sbx as the boundary, relay kept.** Bind the relay to `127.0.0.1:PORT`, allow only `localhost:PORT`, and remove the default rules. Keeps path, model and token policy. Costs: all seven Containerfiles rebased onto `sandbox-templates` and pushed to a registry; a Docker account; a relay any host process can reach, guarded only by the run token; a second run path.
2. **sbx policy, relay dropped.** Loses the path allowlist, model and token policy, body logs, and arbitrary `openai-compat` upstreams. sanduk would become a wrapper.
3. **Not integrated.** Kata through `--oci-runtime` gives a VM per container under Docker and keeps the internal network, relay and images, with no account or telemetry.

Chosen: 3. Revisit option 1 only if Kata fails.

## Unresolved

- **HTTPS injection.** The isolation page says the proxy does not intercept TLS; the credentials page says it "overwrites the auth header" on HTTPS requests. Both hold only if the client sends plaintext to the forward proxy, or trusts a CA in the VM. Not determined.
- **Path rules.** The [CLI reference](https://docs.docker.com/reference/cli/sbx/) returned no content, so policy syntax beyond host and port is unconfirmed.
- **Streaming.** Whether `sbx exec` passes an agent's JSON stream through unaltered.
- **Offline.** Whether a sandbox starts without Docker's domains once images are cached.
