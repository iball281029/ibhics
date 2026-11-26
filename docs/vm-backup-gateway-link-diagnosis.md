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

## Host recovered; physical check deferred

The .103 host subsequently became reachable again at 100 Mbps, rejoined as DRBD Secondary and both replicas became UpToDate. Its EXWin2022 and test-ib-Migrate domains were Running with no reported block errors. The ubututest container could read its migration proof file. No reboot or forced speed was used. The operator explicitly deferred checking the cable/switch port.

Gateway .106 remains on .104. Repeating the same representative 96 MiB point-in-time read test on .104 gave depth-1 results of 0.132 and 0.124 seconds, and depth-8 results of 0.142 and 0.134 seconds; every byte matched. This validates the gateway/network path bottleneck, not a standalone async-read code improvement. A real PNetLab Full Backup is now being measured through the recovered topology. The physical .103 uplink still requires later correction, especially for replication-heavy workloads or a future gateway failover.

## End-to-end Full Backup acceptance

The real PNetLab-27H1-V8.12 Full Backup completed on .104 in **916.226 seconds (15m 16s)**. Its logical disk was 536,870,912,000 bytes; it read 113,229,430,784 bytes and added 73,916,304 compressed bytes. Restore point: `PNetLab-27H1-V8.12@20261125T235257Z`. The VM remained Running. Manifest geometry was valid, and 16 distributed chunks (64 MiB) were reconstructed into a temporary local file and checksum-verified. The complete disk was not restored or boot-tested in this acceptance.

The prior Full #35 read the same byte count at approximately 10 MiB/s, but wrote a different unique payload and included Manager/queue/return delays. This is not a controlled code-only wall-clock comparison. The representative point-in-time read test above and the live Full run validate the current .104 gateway path. The physical .103 uplink remains 100 Mbps and was explicitly deferred by the operator. Node-level acceptance did not create a Manager Jobs request or bypass browser authentication.

During recovery checks, EXWin2022 answered guest-ping. The separate test-ib-Migrate Windows VM showed CRITICAL_PROCESS_DIED and did not answer its guest agent. There is no pre-incident screenshot establishing when it crashed. It has not been reset; explicit recovery authorization was requested. Evidence: `deliverables/test-ib-migrate-bsod-20261126.png`.
