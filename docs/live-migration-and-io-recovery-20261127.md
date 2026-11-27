# Container live migration (CRIU) and VM I/O-error recovery: lab results, 27 Nov 2026

## VM paused by a storage I/O error: isolated test on ibhics-node2 (.103)

Throwaway VM `ibhics-iotest` (64 MiB, no network, no OS) with its raw disk on XFS over a
device-mapper target. The disk file's data extent was switched to a dm `error` table, the
VM was started and SeaBIOS's boot read failed: libvirt reported `paused (I/O error)`.
The table was switched back to linear and the installed `IORecovery.run_once()` was run:

    recovered: True, reason: resumed after reopening disk files, 3.0 s
    final state: running (unpaused)

Everything (VM, dm target, loop device, mount) was removed by the script. No production
VM, link, gateway or NFS service was touched. Script: run from /root/ibhics-iotest.sh.

Open points, stated plainly:
- QEMU `io-status: failed` / `virsh domblkerror: unspecified error` stays on EXWin2022 and
  test-ib-Migrate after the 26 Nov recovery. QEMU's `cont` resets io-status only for
  named (monitor-owned) block backends, not for `-blockdev` devices, so the flag is stale.
  It clears with a new QEMU process: power cycle or a live migration. The web console does
  not read this flag.
- NFSv4 grace 240 s takes effect on the next NFS server restart (maintenance window).

## Container live migration with CRIU

criu 4.1.1 installed on .103 and .104 (`criu check`: Looks good), with
`/etc/criu/default.conf`: `skip-file-rwx-check`, `manage-cgroups=full`.
Tested only on `ubututest` (unprivileged LXC 6.0.4, Ubuntu with systemd 259, cgroup v2).

Fixes needed before a checkpoint succeeds:
1. Nested UTS namespaces (systemd-udevd, -logind, rsyslog with ProtectHostname):
   drop-in `ProtectHostname=no` inside the guest.
2. LXC ttys bind-mounted as /dev/tty1..4: `lxc.tty.max = 0`, `lxc.console.path = none`.
3. `memory.pressure` file descriptors of services: `DefaultMemoryPressureWatch=off`.

Result: checkpoint (dump + stop) works, 1.1 to 1.6 s, 13 MB image.
Restore fails on both the other node and the same node:
`fsnotify: Unable to open /sys/fs/cgroup/init.scope/memory.events` (systemd PID 1 keeps an
inotify watch on its cgroup2 files; CRIU resolves it outside the container's cgroup
namespace while restoring). This is a CRIU/LXC limit for systemd guests on cgroup v2,
not an ibHiCS bug. After each failed restore the container was cold-started on its owner
(.103); it runs there now with its own IP.
