"""Local disks a node could turn into a storage pool, from lsblk, and clearing them first."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

GIB = 1024**3
LSBLK = [
    "lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,MODEL,SERIAL,TYPE,TRAN,FSTYPE,MOUNTPOINTS,RO,ROTA",
]  # fmt: skip
# Volume groups ibHiCS made: "ibhics" holds the system, "ibhics_<pool>" each LVM pool.
OWN_VG = re.compile(r"^ibhics($|_)")


@dataclass
class LocalDisk:
    path: str  # /dev/sda, or /dev/mapper/mpatha for a multipath LUN
    size_gb: float
    model: str = ""
    serial: str = ""
    transport: str = ""  # sata, sas, nvme, fc, iscsi, usb...
    empty: bool = True  # no partitions, filesystem or LVM signature
    contents: str = ""  # what is on it when not empty, e.g. "2 partitions (vmfs)"
    media: str = ""  # "ssd" or "hdd" (from the kernel's rotational flag)


def _walk(dev: dict):
    yield dev
    for child in dev.get("children") or []:
        yield from _walk(child)


def lv_vg(name: str) -> str:
    """The volume group of an LVM device-mapper name: "ceph--a1-osd--block--b2" -> "ceph-a1"."""
    name = name.rsplit("/", 1)[-1]  # lsblk -p gives /dev/mapper/...
    return re.split(r"(?<!-)-(?!-)", name, maxsplit=1)[0].replace("--", "-")


def _foreign_vgs(dev: dict) -> list[str]:
    return sorted({lv_vg(d["name"]) for d in _walk(dev) if d.get("type") == "lvm"})


def _in_use(dev: dict) -> bool:
    for d in _walk(dev):
        if any(m for m in d.get("mountpoints") or []):  # mounted, or active swap
            return True
        # A logical volume of ibHiCS's own: the system disk or a storage pool.
        if d.get("type") == "lvm" and OWN_VG.match(lv_vg(d.get("name") or "")):
            return True
    return False


def _rotational(dev: dict) -> bool:
    rota = dev.get("rota")
    return rota in (True, "1", 1)


def _describe(dev: dict) -> str:
    parts = [c for c in dev.get("children") or [] if c.get("type") == "part"]
    types = {d["fstype"] for d in _walk(dev) if d.get("fstype")} - {"mpath_member"}
    vgs = _foreign_vgs(dev)
    if any(vg.startswith("ceph-") for vg in vgs) or "ceph_bluestore" in types:
        return "Ceph OSD (LVM)"
    if vgs:
        return f"LVM volume group {', '.join(vgs)}"
    types = sorted(types)
    if parts:
        contents = f"{len(parts)} partition{'s' if len(parts) > 1 else ''}"
        return contents + (f" ({', '.join(types)})" if types else "")
    return ", ".join(types)


def parse_lsblk(text: str) -> list[LocalDisk]:
    """Whole disks (and multipath LUNs) that hold nothing the node is using. Disks that
    still carry old data (another system's partitions, a Ceph OSD) are listed too, marked,
    because adding a pool erases them."""
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
            contents = _describe(dev)
            transport = top.get("tran") or ""
            disks.append(
                LocalDisk(
                    path=dev["path"],
                    size_gb=round(int(dev.get("size") or top["size"]) / GIB, 1),
                    model=(top.get("model") or "").strip(),
                    serial=(top.get("serial") or "").strip(),
                    transport=transport,
                    empty=not contents,
                    contents=contents,
                    media="hdd" if _rotational(top) and transport != "nvme" else "ssd",
                )
            )
    return disks


def list_local_disks(run: Callable = subprocess.run) -> list[LocalDisk]:
    result = run(LSBLK, capture_output=True, text=True, check=True)
    return parse_lsblk(result.stdout)


def _out(run: Callable, cmd: list[str]) -> str:
    result = run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def clear_plan(lsblk_text: str, pvs_text: str, devices: list[str]) -> list[list[str]]:
    """The commands that empty `devices` for a new pool: old LVM volume groups (a Ceph
    OSD, another system's LVM) are switched off and removed, then every signature is
    wiped. Refuses a disk the node is using, or a volume group that also spans a disk
    not being cleared, so nothing outside the chosen disks is touched."""
    tops = {d["path"]: d for d in json.loads(lsblk_text).get("blockdevices", [])}
    pvs = [
        (r.get("pv_name", "").strip(), r.get("vg_name", "").strip())
        for rep in json.loads(pvs_text or "{}").get("report", [])
        for r in rep.get("pv", [])
    ]
    cmds: list[list[str]] = []
    for device in devices:
        dev = tops.get(device)
        if dev is None:
            raise ValueError(f"{device} is not a disk on this node")
        if _in_use(dev):
            raise ValueError(f"{device} is in use by this node (mounted, or ibHiCS's own LVM)")
    paths = {d["path"] for device in devices for d in _walk(tops[device])}
    vgs = sorted({vg for pv, vg in pvs if pv in paths and vg})
    for vg in vgs:
        if OWN_VG.match(vg):
            raise ValueError(f"volume group {vg} belongs to ibHiCS; remove that pool first")
        outside = sorted(pv for pv, g in pvs if g == vg and pv not in paths)
        if outside:
            raise ValueError(f"volume group {vg} also uses {', '.join(outside)}; pick those too")
        cmds += [["vgchange", "-an", vg], ["vgremove", "-ff", "-y", vg]]
    for device in devices:
        dev = tops[device]
        if not dev.get("children") and not dev.get("fstype"):
            continue  # already empty
        for pv, _vg in pvs:
            if pv in {d["path"] for d in _walk(dev)}:
                cmds.append(["pvremove", "-ff", "-y", pv])
        parts = [c["path"] for c in dev.get("children") or [] if c.get("type") == "part"]
        cmds += [["wipefs", "-a", p] for p in parts]
        cmds.append(["wipefs", "-a", device])
    # An md/LVM descendant can appear under several physical parents in lsblk.
    # Removing the same PV twice would fail after the first removal succeeded.
    return [list(cmd) for cmd in dict.fromkeys(tuple(cmd) for cmd in cmds)]


def clear_disks(devices: list[str], run: Callable = subprocess.run) -> None:
    """Empty the chosen disks before a pool is built on them (see clear_plan)."""
    # lsblk reports the real /dev/sdX for a /dev/disk/by-id link (but keeps /dev/mapper).
    devices = [os.path.realpath(d) if d.startswith("/dev/disk/") else d for d in devices]
    lsblk = _out(run, ["lsblk", "-J", "-p", "-o", "NAME,PATH,TYPE,FSTYPE,MOUNTPOINTS", *devices])
    try:
        pvs = _out(run, ["pvs", "--reportformat", "json", "-o", "pv_name,vg_name"])
    except (RuntimeError, OSError):
        pvs = "{}"
    for cmd in clear_plan(lsblk, pvs, devices):
        _out(run, cmd)
    for device in devices:  # let the kernel drop the old partitions; harmless if it cannot
        run(["blockdev", "--rereadpt", device], capture_output=True, text=True)
