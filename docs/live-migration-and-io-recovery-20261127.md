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

## NFS grace 240 s applied on .104 (17:02, user approved)

Applied without `systemctl restart nfs-server` (its `exportfs -r` would drop the exports
that drbd-reactor's OCF exportfs resources add, since /etc/exports is empty) and with the
OCF server scope kept: `nfsdctl threads 0`, write `nfsv4gracetime=240`, `nfsv4leasetime=90`,
then `nfsdctl threads 16` inside `unshare -u` with hostname 172.16.12.106. nfsd was down
about 7 s. Result: gracetime 240, leasetime 90, scope 172.16.12.106, exports unchanged.
Do not use `nfsdctl autostart` here: it failed with "lockd configuration failure".

Effect: the kernel ended grace after 3 s ("all clients done reclaiming"), yet the .104
client (it mounts its own VIP) logged "lost 4 locks" and its three VMs paused with an
I/O error; the two VMs on .103 were not affected. The agent resumed PNetLab-27H1-V8.12,
a manual run of the same code resumed npm-vm. OpenP9 was refused ("disk outside managed
storage") because its UEFI firmware, NVRAM and seed ISO are local files; the recovery
now skips local files and reopens only managed-storage nodes (new test), deployed on both
nodes, agent restarted, OpenP9 resumed. All 5 VMs: running, guest agent answers.

Next: the early grace lift lets a slow client lose its locks. Consider keeping the full
grace period (disable early lift in nfsdcld) at the next maintenance window.

## Later on 27 Nov (all pushed)

- Container live migration in the UI by default; systemd guests fall back to a short stop and
  restart on the destination (ubututest .103 -> .104 in 8.5 to 9 s).
- Container HA with an NFS run lock. First lab run found a double start (lock holder lived in
  the agent cgroup; 70 s overlap, fsck clean). Fixed (holder in its own systemd scope, kills its
  container on lock loss). Re-test: agent stopped 3 min -> no takeover; simulated crash ->
  restarted on the other node in 30 s. Reboot-based self-fencing: declined by the user.
- Backup: zstd-3, read-ahead, two concurrent backups per shared repository. test-ib-Migrate
  full 12 GiB: 6:00 -> 3:44 -> 2:08 while npm-vm backed up at the same time.
- Stale QEMU io-status cleared on all five VMs by live migration out and back (no downtime).
- NFSv4 early grace lift: no supported nfs.conf switch for v4 (lift-grace is sm-notify/v3);
  left as is. The I/O-error auto-recovery covers the pause it can cause.
