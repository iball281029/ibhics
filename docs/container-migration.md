# Container migration between nodes

Use **Containers → Migrate**, select a healthy compute node, and permit a temporary stop for a running container. This version requires both nodes to access the same pool and container folder on shared storage. It does not copy a private local disk or perform live memory migration.

The source stops, and normal container actions are fenced using a durable migration receipt. The destination proves it can see the same private receipt, root filesystem and required network/configuration. Only then does the source hand over ownership and the destination regenerate its local configuration. Running containers start on the destination; stopped containers remain stopped. Names, MAC addresses, root filesystem and snapshots are preserved.

Failure before ownership commit aborts back to the source. An uncertain commit remains fenced: the source must not restart after a lost response. **Recover migration** completes on the registered destination owner, or aborts on the source if ownership has not changed. Recovery uses the saved receipt and does not guess which node should own the container. If destination startup fails after ownership transfer, the destination remains the owner; inspect and start it there rather than starting the old source.

Changing the owner metadata by hand or starting a container directly through LXC during migration bypasses the Manager's protections. Normal API lifecycle and backup actions share the container task lock, and node actions reject a pending migration.

## Acceptance on 2026-11-26

Installed on both 172.16.12.103 and 172.16.12.104; both Manager and Agent services passed health checks. The exact merged .104 release passed 62 tests covering migration, shared listing, ownership, container backup, container operations and authentication. Ruff, JavaScript syntax and diff whitespace checks passed.

The dedicated `ubututest` container migrated from .103 to .104 and back. Both directions verified that the container ran at the destination, an in-container proof file matched, MAC and snapshots were preserved and inventory had exactly one owner. The container ended running on its original .103 owner. The live backend was tested through the authenticated node-agent interface; the browser session had expired, so the live browser form was not submitted during this acceptance.

Private local-storage migration and live memory migration remain unsupported in this version.
