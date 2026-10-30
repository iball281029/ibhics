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
from .app import create_app
from .auth import Auth
from .backups import BackupManager
from .imports import ImportManager
from .migrations import TRANSPORTS, MigrationManager


def main() -> None:
    parser = argparse.ArgumentParser(prog="ibhics-manager")
    parser.add_argument("--host", default=os.environ.get("IBHICS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IBHICS_PORT", "8006")))
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
        "--migrate-state-file",
        type=Path,
        default=Path(os.environ.get("IBHICS_MIGRATE_STATE_FILE", "/var/lib/ibhics/migrate.json")),
        help="where ibHiCS Migrate sources (with their credentials) are saved",
    )
    parser.add_argument("--tls-cert", default=os.environ.get("IBHICS_TLS_CERT"), help="HTTPS")
    parser.add_argument("--tls-key", default=os.environ.get("IBHICS_TLS_KEY"))
    parser.add_argument("--heartbeat", type=float, default=5.0, help="seconds between checks")
    args = parser.parse_args()

    cluster = Cluster(
        token=os.environ.get("IBHICS_CLUSTER_TOKEN", ""),
        heartbeat_timeout=args.heartbeat * 3,
        state_path=args.state_file,
    )
    driver = load_driver(args.driver, args.libvirt_uri)
    node_backup = NodeBackup(driver)
    node_backup.cleanup()  # detach backup targets a crash may have left attached
    cluster.add_local(args.node_name, driver, node_backup)
    cluster.load()

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

    imports = ImportManager(cluster, state_path=args.migrate_state_file)
    imports.load()

    app = create_app(
        cluster,
        heartbeat_interval=args.heartbeat,
        backups=backups,
        scheduler_interval=30,
        migrations=migrations,
        auth=auth,
        imports=imports,
    )
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key go together")
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=args.tls_cert,
        ssl_keyfile=args.tls_key,
    )


if __name__ == "__main__":
    main()
