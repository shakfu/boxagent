# MicroVMs

Status: investigated, 2026-09-10. `--oci-runtime` is implemented; gVisor is measured, Kata is not. Claims cite primary sources; inference is marked.

Scope: whether sanduk should run agents in microVMs, and how.

## Where sanduk already has a VM boundary

- **macOS, Apple's `container`:** every container is its own lightweight VM. No change needed.
- **Linux, Docker:** the agent shares the host kernel. A kernel escape reaches the host, and in `sealed` mode the host is where the key lives: in the sanduk process, not the container.

So the gap is Linux only, and the threat it closes is kernel escape. Boot time does not matter: a measured run takes ~35s, so a microVM's faster start changes nothing.

## Driving a VMM directly: no

Firecracker, Cloud Hypervisor, QEMU microvm and crosvm are virtual machine monitors, not container engines. A sanduk engine on one would have to rebuild what Docker does now:

- turn an OCI image into a root filesystem, and supply a kernel;
- create tap devices and a bridge (root) to rebuild the internal network the relay depends on;
- share `/work`. Firecracker has no filesystem sharing: its devices are virtio-net, virtio-block, a serial console and legacy controllers, and block devices are backed by pre-formatted files ([design.md](https://github.com/firecracker-microvm/firecracker/blob/main/docs/design.md)). `/work` would be a disk image copied in and out;
- stream the agent's stdout over a serial console or vsock;
- need KVM, which exists only on Linux, so macOS gains nothing.

That is a container runtime, written again.

## An OCI runtime under Docker: yes

Docker picks the program that starts each container with `--runtime`. Two candidates:

- **Kata Containers:** a VM per container. "Docker supports Kata Containers since 22.06: `sudo docker run --runtime io.containerd.kata.v2`" ([Limitations.md](https://github.com/kata-containers/kata-containers/blob/main/docs/Limitations.md)).
  - It can use Cloud Hypervisor, Firecracker, QEMU, Dragonball or StratoVirt ([hypervisors.md](https://github.com/kata-containers/kata-containers/blob/main/docs/hypervisors.md)).
  - Outside TEE setups, bind mounts use filesystem sharing, so `/work` should work.
  - Host networking and Podman are unsupported. sanduk uses neither.
- **gVisor (`runsc`):** not a VM. It serves the container's system calls from a user-space kernel. Installed with `sudo runsc install`, run with `docker run --runtime=runsc` ([quick start](https://gvisor.dev/docs/user_guide/quick_start/docker/)). Its default platform needs no KVM (inference), so a standard CI runner can test it.

`--oci-runtime NAME` passes the name through as `docker run --runtime NAME`. Apple's engine refuses it. Docker's `default-runtime` in `daemon.json` does the same without sanduk, but for every container on the host.

## What is measured

| | `open` | `sealed` | `/work` |
| --- | --- | --- | --- |
| gVisor | CI job `gvisor`; by hand | CI job `gvisor`; by hand | CI job `gvisor`; by hand |
| Kata, Cloud Hypervisor or QEMU | no | no | no |
| Kata, Firecracker | no | no | probably unsupported (inference, from Firecracker's device list) |

The relay works under gVisor. On 2026-09-11 the container suite passed under runsc 20260831 on docker-ce 29.8.0, for hax and claude, including two concurrent `sealed` runs and a round trip through `/work`. gVisor uses the container's network namespace, which sits on the Docker bridge, so the host gateway stays bindable and reachable.

Kata attaches its VM to the same namespace, so the relay should work there too. That is inference; Kata is in `TODO.md`.

## Alternative

Rootless Docker or Podman limits what a kernel escape reaches, with no VM. It narrows the impact rather than adding a boundary, and Podman's rootless networking rules out the relayed modes ([podman.md](podman.md)).

## Correction

A widely copied summary says Firecracker cannot pause. It can: `PATCH /vm` pauses and resumes it, and snapshots are supported ([snapshot-support.md](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md)).
