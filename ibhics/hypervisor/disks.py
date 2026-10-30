"""Local disks a node could turn into an LVM storage pool, from lsblk."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

GIB = 1024**3
LSBLK = ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,MODEL,SERIAL,TYPE,TRAN,FSTYPE,MOUNTPOINTS,RO"]


@dataclass
class LocalDisk:
    path: str  # /dev/sda, or /dev/mapper/mpatha for a multipath LUN
    size_gb: float
    model: str = ""
    serial: str = ""
    transport: str = ""  # sata, sas, nvme, fc, iscsi, usb...
    empty: bool = True  # no partitions, filesystem or LVM signature
    contents: str = ""  # what is on it when not empty, e.g. "2 partitions (vmfs)"


def _walk(dev: dict):
    yield dev
    for child in dev.get("children") or []:
        yield from _walk(child)


def _in_use(dev: dict) -> bool:
    for d in _walk(dev):
        if any(m for m in d.get("mountpoints") or []):  # mounted, or active swap
            return True
        # An LVM physical volume with logical volumes on it: the system disk or a pool.
        if d.get("fstype") == "LVM2_member" and d.get("children"):
            return True
    return False


def parse_lsblk(text: str) -> list[LocalDisk]:
    """Whole disks (and multipath LUNs) that hold nothing the node is using. Disks that
    still carry old data are listed too, marked, because adding a pool erases them."""
    disks, seen = [], set()
    for top in json.loads(text).get("blockdevices", []):
        if top.get("type") != "disk" or top.get("ro") or int(top.get("size") or 0) < GIB:
            continue
        children = top.get("children") or []
        mpaths = [c for c in children if c.get("type") == "mpath"]
        for dev in mpaths or [top]:  # a multipath member is offered as its /dev/mapper LUN
            if dev["path"] in seen or _in_use(dev):
                continue
            seen.add(dev["path"])
            parts = [c for c in dev.get("children") or [] if c.get("type") == "part"]
            types = sorted({d["fstype"] for d in _walk(dev) if d.get("fstype")} - {"mpath_member"})
            if parts:
                contents = f"{len(parts)} partition{'s' if len(parts) > 1 else ''}"
                contents += f" ({', '.join(types)})" if types else ""
            else:
                contents = ", ".join(types)
            disks.append(
                LocalDisk(
                    path=dev["path"],
                    size_gb=round(int(dev.get("size") or top["size"]) / GIB, 1),
                    model=(top.get("model") or "").strip(),
                    serial=(top.get("serial") or "").strip(),
                    transport=top.get("tran") or "",
                    empty=not contents,
                    contents=contents,
                )
            )
    return disks


def list_local_disks(run: Callable = subprocess.run) -> list[LocalDisk]:
    result = run(LSBLK, capture_output=True, text=True, check=True)
    return parse_lsblk(result.stdout)
