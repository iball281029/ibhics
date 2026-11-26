#!/usr/bin/python3
"""Validate HA NFS client tracking before starting the kernel NFS server.

The resource agent bind-mounts shared state on /var/lib/nfs. A tracker started
before that bind can retain a local SQLite descriptor, making lock reclaim fail.
This helper never restarts NFS, moves a VIP, or changes DRBD roles.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path


def shared_state_mount(run=subprocess.run):
    result = run(
        ["findmnt", "--json", "--mountpoint", "/var/lib/nfs", "-o", "TARGET,SOURCE,FSTYPE"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    mounts = json.loads(result.stdout).get("filesystems", [])
    if len(mounts) != 1:
        raise RuntimeError("NFS state must be bind-mounted from the HA DRBD volume")
    mount = mounts[0]
    source = mount.get("source", "").split("[", 1)[0]
    if (
        mount.get("target") != "/var/lib/nfs"
        or not source.startswith("/dev/drbd")
        or mount.get("fstype") not in ("ext4", "xfs")
    ):
        raise RuntimeError("Refusing NFS tracking on non-HA state storage")
    return mount


def tracking_files_match(
    proc_root=Path("/proc"), state_root=Path("/var/lib/nfs"), run=subprocess.run
):
    result = run(
        ["systemctl", "show", "nfsdcld.service", "-p", "MainPID", "--value"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    pid = int(result.stdout.strip())
    if pid <= 0:
        raise RuntimeError("NFS client tracker is not running")
    found = False
    expected = state_root / "nfsdcld" / "main.sqlite"
    for fd in (proc_root / str(pid) / "fd").iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue
        if target.removesuffix(" (deleted)") != str(expected):
            continue
        opened = fd.stat()
        current = expected.stat()
        if target.endswith(" (deleted)") or (opened.st_dev, opened.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise RuntimeError("NFS tracker retains a stale database descriptor")
        found = True
    if not found:
        raise RuntimeError("NFS tracker has not opened the shared client database")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify"))
    args = parser.parse_args()
    shared_state_mount()
    if args.action == "prepare":
        # Never create a local fallback database when the HA bind is absent.
        Path("/var/lib/nfs/nfsdcld").mkdir(mode=0o700, exist_ok=True)
    else:
        tracking_files_match()


if __name__ == "__main__":
    main()
