# Podman

Status: investigated, 2026-09-10. Nothing implemented. Podman is not installed on this machine, so nothing here is measured. Claims cite Podman's or netavark's source and docs; inference is marked.

Scope: what a `Podman` runtime needs, and which of sanduk's three modes it can run.

## Which modes work where

| | `open` | `key-safe`, `sealed` |
| --- | --- | --- |
| rootful, Linux | yes | yes, with a network holder |
| rootless, Linux | yes, with a uid mapping | no |
| `podman machine` (macOS, Windows) | yes | no |

The relayed modes bind the relay to the network's bridge gateway on the host. Only rootful Podman puts that address on the host.

- Rootless bridge networks live in a separate namespace, the rootless-netns. The gateway address exists only there, so no host process can bind it. A container on a rootless `--internal` network reaches that gateway and nothing else ([discussion #23164](https://github.com/containers/podman/discussions/23164), [etchosts/ip.go](https://github.com/containers/container-libs/blob/main/common/libnetwork/etchosts/ip.go)).
- `podman machine` runs containers in a Linux VM, which is the Docker Desktop case ([podman-machine(1)](https://docs.podman.io/en/latest/markdown/podman-machine.1.html)).

Rootless is Podman's default and its main use. The host-bound relay covers the least common Podman setup.

## What a `Podman` runtime needs

It would subclass `Docker`. Six differences:

1. **A network holder.** netavark creates the bridge and its gateway address when the first container attaches, and deletes both when the last leaves ([bridge.rs](https://github.com/containers/netavark/blob/main/src/network/bridge.rs), [podman#17844](https://github.com/containers/podman/issues/17844)). That is Apple's behaviour, not Docker's. The comment at `runtime.py:92` says Podman creates the bridge with the network; it is wrong.
2. **`network_info`** reads `[0].subnets[0].gateway` and `[0].subnets[0].subnet` ([podman-network-inspect](https://docs.podman.io/en/latest/markdown/podman-network-inspect.1.html)).
3. **`require_service`** runs `podman info --format '{{.Version.Version}}'`. Docker's `{{.ServerVersion}}` is not a Podman field ([define/info.go](https://github.com/containers/podman/blob/main/libpod/define/info.go), [podman-info](https://docs.podman.io/en/latest/markdown/podman-info.1.html)).
4. **Rootless detection**, so a relayed mode is refused at the flag. Today it would fail after `wait_for_gateway`'s 30s. The field is probably `{{.Host.Security.Rootless}}`; not confirmed.
5. **A uid mapping for `/work`.** Rootless maps container root to the caller, and uid 1000 to a subordinate uid. Every shipped image runs its agent as uid 1000, so the agent cannot write the mount (inference, from [userns](https://github.com/containers/podman/blob/main/docs/source/markdown/options/userns.container.md) and [troubleshooting #34](https://github.com/containers/podman/blob/main/troubleshooting.md)). `--userns=keep-id:uid=1000,gid=1000` maps the caller to 1000; it needs Podman 4.3 ([RELEASE_NOTES](https://github.com/containers/podman/blob/main/RELEASE_NOTES.md)).
6. **SELinux labels** on Fedora and RHEL. The docs warn an unlabelled bind mount may be refused ([volume.md](https://github.com/containers/podman/blob/main/docs/source/markdown/options/volume.md)). Both fixes cost something ([mount.md](https://github.com/containers/podman/blob/main/docs/source/markdown/options/mount.md)):
   - `relabel=private` rewrites the label on the user's project directory, in place. The docs advise against relabelling home directories.
   - `--security-opt label=disable` removes SELinux confinement from the container.

   This needs a decision.

Already compatible:

- Every flag `run_argv` emits ([podman-run](https://docs.podman.io/en/latest/markdown/podman-run.1.html)).
- `ps --format` with Docker's template; `.Names` is a string ([ps.go](https://github.com/containers/podman/blob/main/cmd/podman/containers/ps.go)).
- Image names: local images are stored as `localhost/<name>`, and a lookup by bare name finds them ([libimage/runtime.go](https://github.com/containers/container-libs/blob/main/common/libimage/runtime.go)). Every `FROM` line here is fully qualified, so short-name resolution never runs.
- Two runs creating one network: netavark serialises creates under a file lock. The loser fails with "network already exists", and the retry in `ensure_network` then finds the winner's network ([network.go](https://github.com/containers/container-libs/blob/main/common/libnetwork/netavark/network.go)).

## Rootful has its own cost

Rootful Podman means sanduk itself runs as root. `runtime.cli = "sudo podman"` does not work: `sudo` resets the environment, and `-e NAME` inherits the key or the run token from sanduk's environment. The container would get neither. So the relay, and the key it holds, would live in a root process.

## Options

| | covers | cost (estimated) |
| --- | --- | --- |
| A. `open` only | rootless, machine | ~60 lines; the mode with no containment |
| B. A, plus relayed modes when rootful | adds rootful Linux | ~100 lines, and a CI job on `sudo podman` |
| C. a relay the container reaches without the host gateway | adds rootless; perhaps Docker Desktop and Colima | a design change, below |

C has three shapes. None is measured.

- **The relay inside the rootless-netns** (`podman unshare --rootless-netns`), bound to the gateway there. The key stays in a host process. Podman only.
- **A relay container** attached to the internal network and to a routable one. Works on any engine. The key moves into a container sanduk runs, so the README's claim that the container never holds the key narrows to the agent's container.
- **`--network none` plus a unix socket** bind-mounted into the container, with a TCP forwarder in the image. Having no network is stronger than `--internal`. Unix sockets probably do not cross the file sharing of Docker Desktop or Apple's VMs (inference), so this helps native Linux only.

## CI

`ubuntu-24.04` ships Podman 4.9.3 ([Ubuntu2404-Readme](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)), and `sudo podman` works there. Rootless probably works too, though untested. Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor, but ships unconfined profiles for `podman`, `crun` and `slirp4netns` ([runner-images#10443](https://github.com/actions/runner-images/issues/10443), [apparmor filelist](https://packages.ubuntu.com/noble/amd64/apparmor/filelist)). A CI job is the only place any of this can be measured.

## What I would do

Nothing yet.

- A covers most Podman users, but only in the mode with no containment.
- B adds only rootful Podman, and only by running sanduk as root.

If Podman is wanted, C is the version worth building. It is a decision about where the relay lives, not an engine subclass.

## Side finding: Docker and uid 1000

Rootful Docker has the same mapping problem, unverified. The agent runs as uid 1000, and `run` creates `/work` as mode 0755, owned by whoever started sanduk. If that user is not uid 1000, the agent cannot write `REPORT.md`. GitHub's runner user is probably uid 1001 (inference). The CI wakeup test does not assert that a report exists.
