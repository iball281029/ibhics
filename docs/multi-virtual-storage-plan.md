# Plan: a second Virtual Storage set (handoff item B)

Written 2026-11-11 after read-only checks on Node1 (.104) and Node2 (.103). Nothing on
the cluster was changed for this plan.

## What blocks it today

1. **No new disks are attached yet.** Each node shows only the disks of the current set
   (2 × 3.6 TB HDD, 1 × 894 GB SSD) and the SATADOM system disk.
2. **LINSTOR Gateway 2.3.0 with kernel NFS allows one NFS resource per cluster.** Its own
   help text says so and points to `--implementation=ganesha` for several resources.
   Kernel `nfsd` listens on `0.0.0.0:2049` on both nodes today.
3. **ibHiCS stores one set.** `created.json`, the status API, cache mode, the UI card and
   the create flow all assume a single set.

## Options

| | A. Second set on NFS-Ganesha | B. Move everything to Ganesha | C. One NFS resource, several DRBD volumes |
|---|---|---|---|
| Current set | stays on kernel NFS, untouched | must be recreated or migrated | stays, but its gateway config is hand-edited |
| New VIP | yes, one per set | yes, one per set | no, shares .106 |
| Risk to running VMs | low (bind kernel nfsd to .106 only, one restart) | high (data move, downtime) | medium (unsupported by the gateway tool) |
| Failover | each set fails over on its own | each set on its own | both sets always move together |
| Reflink full clones | must be re-tested on Ganesha (NFS 4.2 CLONE) | same | kept |

## Recommendation: A, proven in the isolated lab first

1. In the isolated lab on Node2 (kept from earlier tests), run kernel NFS bound to one IP
   next to a Ganesha resource on a second IP; check failover, NFSv4 locks
   (`qemu-img` lock test across nodes) and `FICLONE` reflink through Ganesha.
2. Code: per-set catalog (`created/<name>.json`) with a migration of today's
   `created.json`; status, cache mode and UI list every set; VIP conflict checks across
   sets; create flow allows a second set only with the Ganesha backend.
3. Live: set `[nfsd] host=172.16.12.106` in `/etc/nfs.conf` and restart `nfs-server` on
   the node that holds the export (VMs keep running; NFS clients retry), then the owner
   selects the new disks and presses **Create**.

## Needed from the owner

- Install the new disks in both nodes (same size class on each side), then tell Claude.
- Pick a free IP for the second set's VIP (not .103–.108).
- Approve the short `nfs-server` restart on the node that serves the current set.
