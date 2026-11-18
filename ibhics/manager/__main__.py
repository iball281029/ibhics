"""Run the manager: python -m ibhics.manager"""

from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path

import uvicorn

from ..backup import NodeBackup
from ..cluster import Cluster
from ..hypervisor import load_driver
from ..hypervisor.edge import restore_edge
from .app import create_app
from .auth import Auth
from .backups import BackupManager
from .imports import ImportManager
from .migrations import TRANSPORTS, MigrationManager
from .usage import UsageHistory
from .vmgroups import VMGroups


def main() -> None:
    parser = argparse.ArgumentParser(prog="ibhics-manager")
    parser.add_argument("--host", default=os.environ.get("IBHICS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IBHICS_PORT", "8888")))
    parser.add_argument(
        "--node-name",
        default=os.environ.get("IBHICS_NODE_NAME", socket.gethostname()),
        help="name of the compute node the manager runs on",
    )
    parser.add_argument(
        "--driver",
        choices=["fake", "libvirt"],
        default=os.environ.get("IBHICS_DRIVER", "fake"),
        help="'fake' runs without KVM for development; 'libvirt' manages real VMs",
    )
    parser.add_argument(
        "--libvirt-uri", default=os.environ.get("IBHICS_LIBVIRT_URI", "qemu:///system")
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ.get("IBHICS_STATE_FILE", "/var/lib/ibhics/cluster.json")),
        help="where cluster membership is saved",
    )
    parser.add_argument(
        "--backup-state-file",
        type=Path,
        default=Path(os.environ.get("IBHICS_BACKUP_STATE_FILE", "/var/lib/ibhics/backup.json")),
        help="where backup targets (with their credentials) and policies are saved",
    )
    parser.add_argument(
        "--migration-transport",
        choices=sorted(TRANSPORTS),
        default=os.environ.get("IBHICS_MIGRATION_TRANSPORT", "tls"),
        help="how nodes' libvirt daemons reach each other during live migration",
    )
    parser.add_argument(
        "--migration-host",
        default=os.environ.get("IBHICS_MIGRATION_HOST"),
        help="name other nodes use to reach this node's libvirtd (default: its FQDN)",
    )
    parser.add_argument(
        "--users-file",
        type=Path,
        default=Path(os.environ.get("IBHICS_USERS_FILE", "/var/lib/ibhics/users.json")),
        help="where console users and password hashes are saved",
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=Path(os.environ.get("IBHICS_AUDIT_LOG", "/var/log/ibhics/audit.log")),
        help="append-only record of every change and sign-in",
    )
    parser.add_argument(
        "--groups-file",
        type=Path,
        default=Path(os.environ.get("IBHICS_GROUPS_FILE", "/var/lib/ibhics/vm-groups.json")),
        help="where the VM groups (the folders of the VM list) are saved",
    )
    parser.add_argument(
        "--recycle-days",
        type=int,
        default=int(os.environ.get("IBHICS_RECYCLE_DAYS", "7")),
        help="keep deleted VMs (stopped, with their disks) this many days; 0 deletes at once",
    )
    parser.add_argument(
        "--migrate-state-file",
        type=Path,
        default=Path(os.environ.get("IBHICS_MIGRATE_STATE_FILE", "/var/lib/ibhics/migrate.json")),
        help="where ibHiCS Migrate sources (with their credentials) are saved",
    )
    parser.add_argument("--tls-cert", default=os.environ.get("IBHICS_TLS_CERT"), help="HTTPS")
    parser.add_argument("--tls-key", default=os.environ.get("IBHICS_TLS_KEY"))
    parser.add_argument("--heartbeat", type=float, default=5.0, help="seconds between checks")
    args = parser.parse_args()
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key go together")

    if args.tls_cert and args.driver == "libvirt":
        from . import console_cert

        vip = os.environ.get("IBHICS_MANAGER_VIP", "").split("/")[0]
        try:
            if console_cert.ensure(
                Path(args.tls_cert),
                Path(args.tls_key),
                [args.node_name],
                [args.migration_host or "", vip],
            ):
                print("Console certificate re-signed for this node and the manager VIP", flush=True)
        except Exception as exc:  # the old certificate still works
            print(f"Console certificate unchanged: {exc}", flush=True)

    leader = _lead(args)

    cluster = Cluster(
        token=os.environ.get("IBHICS_CLUSTER_TOKEN", ""),
        heartbeat_timeout=args.heartbeat * 3,
        state_path=args.state_file,
    )
    driver = load_driver(args.driver, args.libvirt_uri)
    node_backup = NodeBackup(driver)
    node_backup.cleanup()  # detach backup targets a crash may have left attached
    restore_edge(driver)
    cluster.add_local(args.node_name, driver, node_backup)
    if args.migration_host:
        # Saved with the members, so a standby manager can reach this node's agent.
        cluster.local_agent = f"http://{args.migration_host}:8007"
    cluster.load()
    if cluster.local_agent and args.state_file.exists():
        cluster.save()  # record this node's own agent for whichever manager comes next

    backups = BackupManager(cluster, state_path=args.backup_state_file)
    backups.load()

    auth = Auth(state_path=args.users_file, audit_path=args.audit_log)
    auth.load()
    generated = auth.bootstrap(os.environ.get("IBHICS_ADMIN_PASSWORD"))
    if generated:
        # Kept out of the service log. The admin must replace it at first sign-in.
        secret = args.users_file.with_name("initial-admin-password")
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(generated + "\n")
        print(f"Created user 'admin'; its initial password is in {secret}", flush=True)

    migrations = MigrationManager(
        cluster,
        transport=args.migration_transport,
        local_host=args.migration_host,
        busy=lambda vm: any(
            j.kind == "backup" and j.vm == vm and j.state in ("queued", "running")
            for j in backups.jobs()
        ),
    )

    from .activity import ActivityLog
    from .ha import HighAvailability
    from .p2v import P2VSessions

    migration_tls = None
    if args.driver == "libvirt" and args.migration_transport == "tls" and args.migration_host:
        from .migration_tls import MigrationTLS

        migration_tls = MigrationTLS(cluster, local_address=args.migration_host)

    imports = ImportManager(cluster, state_path=args.migrate_state_file)
    imports.load()

    groups = VMGroups(state_path=args.groups_file)
    groups.load()

    app = create_app(
        cluster,
        heartbeat_interval=args.heartbeat,
        backups=backups,
        scheduler_interval=30,
        migrations=migrations,
        auth=auth,
        imports=imports,
        groups=groups,
        recycle_days=args.recycle_days,
        edge_state=Path("/var/lib/ibhics/edge.json") if args.driver == "libvirt" else None,
        pool_labels_path=args.state_file.with_name("pool-labels.json"),
        usage=UsageHistory(
            Path(os.environ.get("IBHICS_USAGE_FILE", "/var/lib/ibhics/usage.sqlite3"))
        ),
        migration_tls=migration_tls,
        ha=HighAvailability(cluster, state_path=args.state_file.with_name("ha.json")),
        activity=ActivityLog(args.state_file.with_name("activity.sqlite3")),
        p2v=P2VSessions(args.state_file.with_name("p2v.json")),
        p2v_pin=_public_key_pin(args.tls_cert),
    )
    if leader is not None:
        from .leader import Replicator, manager_vip

        vip = manager_vip(args.migration_host or "")
        if vip is not None:
            vip.claim()  # released by ExecStopPost (installer drop-in) when it stops
        Replicator(leader, _state_files(args), vip=vip).start()
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=args.tls_cert,
        ssl_keyfile=args.tls_key,
    )


def _state_files(args) -> list[Path]:
    """The manager's own state: what a standby needs to take over."""
    state = args.state_file
    return [
        state,
        args.backup_state_file,
        args.users_file,
        args.users_file.with_name("sessions.json"),
        args.groups_file,
        args.migrate_state_file,
        state.with_name("pool-labels.json"),
        state.with_name("ha.json"),
        state.with_name("p2v.json"),
        state.with_name("activity.sqlite3"),
    ]


def _lead(args):
    """With IBHICS_SHARED_STATE set: become the active manager, or wait as standby
    (this call then never returns: the process re-execs itself as active)."""
    shared = os.environ.get("IBHICS_SHARED_STATE")
    if not shared or args.driver != "libvirt":
        return None
    import sys
    import threading

    from .leader import Leadership, restore, standby_app

    address = args.migration_host or args.host
    leader = Leadership(Path(shared), args.node_name, address)
    if not leader.available:
        print(f"Shared state {shared} unavailable: running on this node's own state", flush=True)
        return None
    files = _state_files(args)

    previous = leader.current().get("node")
    if os.environ.get("IBHICS_LEADER_FD") or leader.try_acquire():
        if previous and previous != args.node_name and not os.environ.get("IBHICS_LEADER_FD"):
            names = restore(files, leader.shared, Path("/var/lib/ibhics"))
            print(f"Taking over from {previous}: restored {', '.join(names)}", flush=True)
        print(f"Active manager ({args.node_name})", flush=True)
        return leader

    print(f"Standby manager; active is {previous or 'unknown'}", flush=True)
    from .leader import manager_vip

    vip = manager_vip(address)
    if vip is not None:
        vip.release()  # only the active manager answers on the console address

    def watch() -> None:
        while True:
            threading.Event().wait(5)
            try:
                before = leader.current().get("node")  # read before we announce ourselves
                if leader.available and leader.try_acquire():
                    if before and before != args.node_name:
                        names = restore(files, leader.shared, Path("/var/lib/ibhics"))
                        print(f"Taking over from {before}: restored {', '.join(names)}", flush=True)
                    argv = [sys.executable, "-m", "ibhics.manager", *sys.argv[1:]]
                    os.execve(sys.executable, argv, leader.inheritable())
            except Exception as exc:
                print(f"standby: {exc}", flush=True)

    threading.Thread(target=watch, daemon=True).start()
    uvicorn.run(
        standby_app(leader),
        host=args.host,
        port=args.port,
        ssl_certfile=args.tls_cert,
        ssl_keyfile=args.tls_key,
    )
    raise SystemExit(0)


def _public_key_pin(cert: str | None) -> str:
    """curl --pinnedpubkey value of the console's certificate, so the P2V agent talks
    to this manager only. Empty when it cannot be read."""
    if not cert:
        return ""
    import subprocess

    try:
        pub = subprocess.run(
            ["openssl", "x509", "-in", cert, "-pubkey", "-noout"],
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
        der = subprocess.run(
            ["openssl", "pkey", "-pubin", "-outform", "DER"],
            input=pub,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    import base64
    import hashlib

    return base64.b64encode(hashlib.sha256(der).digest()).decode()


if __name__ == "__main__":
    main()
