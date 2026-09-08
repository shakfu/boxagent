# Subcommand CLI implementation plan

Status: done, 2026-09-08. Target: 0.2.0. All three commits landed; the Makefile names no engine.

Scope: `sanduk` grows subcommands, and every container-engine call the Makefile
makes in shell moves behind `Runtime`. No change to what `run` does or to any
flag it takes.

Acceptance bar: the Makefile contains no engine name. `ENGINE` and
`CONTAINERFILE` are deleted, not parameterised.

## Why

`runtime.py` exists so that every subprocess call to an engine lives in one
place. The Makefile makes seven of them anyway:

| target | engine call | what it duplicates |
| --- | --- | --- |
| `image` | `container image list \| awk \| grep -qx`, then `build -t -f` | `Runtime.image_exists`, `Runtime.build_image` |
| `shell` | `run --rm -it --entrypoint sh` | `Runtime.run_argv` |
| `ps` | `list -a` | nothing; no hook exists |
| `stop` | `list \| awk '$1 ~ /^sanduk-/'`, then `stop` | `Runtime.destroy`, half of it |
| `clean` | `list -a \| awk`, then `delete --force` | `Runtime.destroy` |
| `destroy` | `image delete`, `network delete` | nothing; no hook exists |
| `system-*` | `system start\|stop\|status` | `Runtime.require_service`, partly |

`ENGINE=docker` cannot fix any of them: `container delete` is `docker rm` and
`container list` is `docker ps`, which is the difference `Runtime.delete_verb`
already encodes and the Makefile re-hardcodes in awk.

## Commands

```
sanduk <command> [options]

  run       run an agent in a disposable container
  build     build the agent's image
  shell     interactive shell in the agent image
  ps        list sanduk containers
  stop      stop running sanduk containers, leaving them on disk
  clean     stop and delete sanduk containers
  destroy   clean, plus the agent's image and the network
  system    show or change the engine's service
  list      show registered agents, providers, runtimes
```

| command | flags beyond the common two |
| --- | --- |
| `run` | the current set, unchanged |
| `build` | `--agent --image --containerfile --force` |
| `shell` | `--agent --image` |
| `ps` `stop` `clean` | none |
| `destroy` | `--agent --image --proxy-network` |
| `system` | positional `status\|start\|stop` |
| `list` | positional `agents\|providers\|runtimes` |

## Decisions

**`run` is required.** `sanduk 'do the thing'` errors and names the fix. The
alternative infers `run` from a first argument that matches no verb, and a
one-word task -- `clean`, `stop`, `build` -- would then run a destructive verb
instead. Nothing is released at 0.1.0, so there is no installed base to weigh
against that.

**Common flags sit after the verb.** `--runtime` and `--quiet` come from an
`add_help=False` parent parser applied to each subparser, not from the main
parser. A flag defined in both places has the subparser's default overwrite the
main parser's value, so `sanduk --runtime docker ps` would silently use
`apple`. The cost is `--runtime` appearing in nine help texts.

**`system` moves in.** `require_service` already prints `container system
start` in an error, so the command is known here already. Docker's daemon is
managed by launchd, systemd, or Desktop, and its implementation says so rather
than pretending.

**`destroy` leaves `sanduk-logs` alone.** `make destroy` deletes it today.
`--log-bodies` output is placed outside the bind mount so the agent cannot edit
its own audit trail; removing it as a side effect of a container-cleanup verb
is the wrong default. It stays a `rm -rf` line in the Makefile, where it reads
as the file operation it is.

**`list` is one verb with a positional.** Three flat verbs (`sanduk agents`)
read shorter but put nouns among verbs, and add a top-level name every time an
axis is added.

## Runtime grows from four hooks to eight

| hook | needed by | why it cannot be generic |
| --- | --- | --- |
| `list_containers(prefix)` | `ps` `stop` `clean` | today it is awk over one engine's column layout |
| `delete_image(image)` | `destroy` | `image delete` against `image rm` |
| `delete_network(name)` | `destroy` | same verb split |
| `service_status/start/stop` | `system` | Apple ships a service command; Docker does not |

The three `service_*` hooks get base implementations that raise "this engine's
service is managed outside sanduk", so a third engine implements five methods,
not eight. `list_containers` returns rows, not the engine's text: `ps` prints
sanduk's own view of name, image and state. Passing the engine's columns
through would relocate the portability problem rather than fix it.

## Sequence

Three commits, because nine verbs in one is unreviewable.

1. Done. Subparser skeleton, `run` and `list` only, no new `Runtime` hook.
2. Done. `build`, `shell`, `ps`, `stop`, `clean`, and the five Makefile targets
   that duplicated them. `list_containers` landed early, with CI: the Docker job
   needed it to find leftover containers. `shell` also needed `shell_argv`, a
   fifth hook the plan had not counted.
3. Done. `destroy`, `system`, and the Makefile rewrite. Two `service_*` hooks,
   not three: `service_status` turned out to be generic, since `require` already
   knows whether an engine can take a container.

## Risks

`Docker` is written against the documented CLI and tested against recorded
output; no daemon has answered it. `list_containers` for Docker will parse
`docker ps --format`, which is a second unverified parser on top of the first.
Prefer `--format '{{.Names}}\t{{.Image}}\t{{.State}}'` over column positions, so
a wrong guess fails loudly rather than shifting a field.

`shell` needs a tty. `run_argv` builds no `-it`, and `launch` reads stdout as a
JSON stream, so `shell` cannot go through either. It runs the engine directly
with `subprocess.call` and inherits the terminal.
