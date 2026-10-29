"""Run the manager: python -m ibhics.manager"""

from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path

import uvicorn

from ..cluster import Cluster
from ..hypervisor import load_driver
from .app import create_app


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
    parser.add_argument("--heartbeat", type=float, default=5.0, help="seconds between checks")
    args = parser.parse_args()

    cluster = Cluster(
        token=os.environ.get("IBHICS_CLUSTER_TOKEN", ""),
        heartbeat_timeout=args.heartbeat * 3,
        state_path=args.state_file,
    )
    cluster.add_local(args.node_name, load_driver(args.driver, args.libvirt_uri))
    cluster.load()

    app = create_app(cluster, heartbeat_interval=args.heartbeat)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
