"""Local disk preparation used only after the operator confirms the creation review."""

from __future__ import annotations

import fcntl
import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from .hypervisor.disks import _walk, clear_plan
from .hypervisor.hybrid import chunk_size
from .hypervisor.storage_inventory import inventory
from .storage_create import NAME, NodeDisks, review_node, thin_geometry


class NodeProvisionIn(BaseModel):
    name: str = Field(pattern=NAME)
    selection: NodeDisks
    size_gib: int = Field(ge=1, le=1048576)
    confirm_wipe: bool = False


def provision(
    driver,
    body: NodeProvisionIn,
    *,
    node: str,
    root: Path = Path("/var/lib/ibhics/virtual-storage"),
    run=subprocess.run,
    report=lambda _step: None,
) -> dict:
    """Revalidate this node immediately before releasing any existing pool.

    A failed attempt is retained for inspection. It is never automatically replayed
    and it never deletes the partially created storage as a rollback.
    """
    if not body.confirm_wipe or body.selection.node != node:
        raise ValueError("Explicit disk erasure confirmation and matching node are required")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "provision.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _provision(driver, body, root, run, report)


def _provision(driver, body, root, run, report):
    def command(args, timeout=180):
        done = run(args, capture_output=True, text=True, check=False, timeout=timeout)
        if done.returncode:
            raise RuntimeError(f"{args[0]} failed: {(done.stderr or done.stdout)[-2000:]}")
        return done.stdout

    # All validation, including pool references, precedes the first mutation.
    current = inventory(driver, run=run)
    reviewed = review_node(body.selection, current, body.size_gib)
    thin_mib, metadata_mib = thin_geometry([d["size_gib"] for d in reviewed["data"]])
    vg = "ibhv-" + body.name
    existing = command(["vgs", "--noheadings", "-o", "vg_name"]).split()
    if vg in existing:
        raise ValueError(f"{vg} already exists; inspect the previous operation before retrying")
    audit = root / (body.name + "-disks.json")
    if audit.exists():
        raise ValueError("A previous disk operation exists; inspect it before retrying")
    audit.write_text(json.dumps({"selection": body.model_dump(), "review": reviewed}, indent=2))
    audit.chmod(0o600)
    for pool_name in reviewed["reclaim_pools"]:
        report(f"Releasing {pool_name} on {body.selection.node}")
        pool = driver._conn.storagePoolLookupByName(pool_name)
        backup = root / f"{body.name}-{pool_name}-pool.xml"
        backup.write_text(pool.XMLDesc(0))
        backup.chmod(0o600)
        if pool.isActive():
            pool.destroy()
        pool.undefine()
    paths = body.selection.data + body.selection.cache
    block = json.loads(
        command(["lsblk", "-J", "-p", "-o", "NAME,PATH,TYPE,FSTYPE,MOUNTPOINTS", *paths])
    )
    physical = command(["pvs", "--reportformat", "json", "-o", "pv_name,vg_name"])
    clear = clear_plan(json.dumps(block), physical, paths)
    arrays = {
        d["path"]
        for top in block["blockdevices"]
        for d in _walk(top)
        if d.get("type", "").startswith("raid")
    }
    report(f"Clearing confirmed disks on {body.selection.node}")
    for cmd in clear:
        if cmd[0] != "wipefs":
            command(cmd)
    for array in sorted(arrays):
        command(["mdadm", "--stop", array])
    for cmd in clear:
        if cmd[0] == "wipefs":
            command(cmd)
    for path in paths:
        command(["blockdev", "--rereadpt", path])
    command(["udevadm", "settle"])
    data = list(body.selection.data)
    report(f"Creating thin storage on {body.selection.node}")
    command(["pvcreate", "-ff", "-y", *data, *body.selection.cache])
    command(["vgcreate", vg, *data, *body.selection.cache])
    command(
        [
            "lvcreate",
            "-y",
            "--type",
            "thin-pool",
            "--chunksize",
            "256K",
            "--poolmetadatasize",
            f"{metadata_mib}M",
            "-L",
            f"{thin_mib}M",
            "-n",
            "thin",
            vg,
            *data,
        ]
    )
    if body.selection.cache:
        command(["lvcreate", "-y", "-l", "95%PVS", "-n", "cache", vg, *body.selection.cache])
        command(
            [
                "lvconvert",
                "-y",
                "--type",
                "cache",
                "--cachevol",
                f"{vg}/cache",
                "--cachemode",
                "writethrough",
                "--chunksize",
                chunk_size(command, vg),
                f"{vg}/thin",
            ]
        )
    command(["vgcfgbackup", vg])
    return {
        "node": body.selection.node,
        "volume_group": vg,
        "thin_pool": "thin",
        "provider": f"{vg}/thin",
        "cache_mode": "writethrough" if body.selection.cache else None,
    }
