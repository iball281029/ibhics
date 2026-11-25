# Full VM Backup: verified gateway uplink bottleneck

The remaining Full Backup problem is a 100 Mbps link on the active NFS gateway, not a shortage of outstanding NBD reads.

On 2026-11-26, `ethtool eno1` on gateway 172.16.12.106 (the same physical host as 172.16.12.103, `ibhics-node2`) reported:

- Supported and advertised: 10/100/1000 Mbps.
- Actual speed: **100 Mbps**, Full duplex, automatic negotiation enabled.
- Peer compute node .104: **1000 Mbps**.
- DRBD: gateway Primary, local and peer disks UpToDate; exported filesystem XFS.

100 Mbps permits at most 12.5 decimal MB/s before network/protocol overhead. This matches the observed Full Backup read ceiling around 10–12 MB/s.

A dedicated paused VM with no network was filled with 96 MiB of representative backup data. The same point-in-time data was read and byte-compared in ABBA order: queue depth 1, 8, 8, 1. Results were 8.418, 8.356, 8.199 and 8.456 seconds. Parallel requests did not materially improve throughput. The test VM and its files were removed. No unproven async-read change was installed.

## Required next step

Check the cable, connectors and switch-port configuration to obtain a 1 Gbps uplink. Both ends advertising gigabit does not prove the cable and switch configuration can currently establish that link. The host data alone does not prove which physical component needs replacement.

A software attempt can restart the current automatic negotiation with `ethtool -r eno1`, without forcing 1000 Mbps. This may briefly interrupt NFS and cluster traffic. It initially required explicit operator approval; the subsequent authorized attempt is recorded below. Forcing speed, reloading the driver, changing the bridge or moving the storage gateway are not included in this proposal.

After restoring the link, repeat the same 96 MiB data test and an end-to-end Full Backup, verify the restored bytes, and report measured time and throughput. Do not mark the remaining Full Backup issue complete while the gateway still negotiates at 100 Mbps.

Previous application improvements remain installed: bounded compression/durable-write concurrency, checkpoint recovery, allocation-aware zero skipping and inexpensive native-immutability capability checks. Those do not remove the physical uplink ceiling.

## Authorized renegotiation and recovery status

The operator subsequently authorized restarting automatic negotiation. A one-shot systemd timer requested `ethtool -r eno1` on .103; no advertised-speed mask, forced speed or bridge configuration was changed. The host then became unreachable and did not return during initial checks. The command completion journal cannot yet be read remotely. Do not repeat a port reset blindly.

The .104 node assumed DRBD Primary, mounted the exported filesystem and brought up NFS VIP .106 and Manager VIP .111. After the NFS grace/recovery period, repository metadata was readable. Its npm-vm, OpenP9 and PNetLab domains were Running; Manager health returned OK. The .103 peer remains disconnected. This is degraded operation, not completion of the Backup/network repair. Console/BMC access or a physical cable/port check was requested to recover .103 and subsequently check its guests.
