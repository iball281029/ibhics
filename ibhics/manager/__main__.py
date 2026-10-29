"""Run the manager: python -m ibhics.manager"""

from __future__ import annotations

import argparse
import os

import uvicorn

from ..hypervisor import load_driver
from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="ibhics-manager")
    parser.add_argument("--host", default=os.environ.get("IBHICS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IBHICS_PORT", "8006")))
    parser.add_argument(
        "--driver",
        choices=["fake", "libvirt"],
        default=os.environ.get("IBHICS_DRIVER", "fake"),
        help="'fake' runs without KVM for development; 'libvirt' manages real VMs",
    )
    parser.add_argument(
        "--libvirt-uri", default=os.environ.get("IBHICS_LIBVIRT_URI", "qemu:///system")
    )
    args = parser.parse_args()

    app = create_app(load_driver(args.driver, args.libvirt_uri))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
