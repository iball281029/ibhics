"""Run a node agent: python -m ibhics.agent --role compute|witness"""

from __future__ import annotations

import argparse
import os
import socket

import uvicorn

from ..cluster.topology import NodeRole
from ..hypervisor import load_driver
from .app import create_agent_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="ibhics-agent")
    parser.add_argument("--name", default=os.environ.get("IBHICS_NODE_NAME", socket.gethostname()))
    parser.add_argument(
        "--role",
        choices=[r.value for r in NodeRole],
        default=os.environ.get("IBHICS_ROLE", NodeRole.COMPUTE.value),
    )
    parser.add_argument("--host", default=os.environ.get("IBHICS_AGENT_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("IBHICS_AGENT_PORT", "8007"))
    )
    parser.add_argument(
        "--driver", choices=["fake", "libvirt"], default=os.environ.get("IBHICS_DRIVER", "libvirt")
    )
    parser.add_argument(
        "--libvirt-uri", default=os.environ.get("IBHICS_LIBVIRT_URI", "qemu:///system")
    )
    args = parser.parse_args()

    token = os.environ.get("IBHICS_CLUSTER_TOKEN", "")
    if not token:
        parser.error("set IBHICS_CLUSTER_TOKEN to the cluster's shared token")

    role = NodeRole(args.role)
    driver = load_driver(args.driver, args.libvirt_uri) if role == NodeRole.COMPUTE else None
    uvicorn.run(create_agent_app(args.name, role, token, driver), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
