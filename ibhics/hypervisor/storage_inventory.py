"""Read-only physical disk inventory for the Virtual Storage creation review.

Unlike the ordinary pool picker, this also describes mounted, reclaimable pools.
Nothing in this module releases a mount or changes a block device.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import subprocess
import xml.etree.ElementTree as ET

from .disks import GIB, LSBLK, OWN_VG, _describe, _rotational, _walk, lv_vg


def _under(path: str, directory: str) -> bool:
    directory = posixpath.normpath(directory)
    path = posixpath.normpath(path)
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def parse_inventory(
    lsblk: dict, pools: list[dict], references: list[dict], pvs: dict | None = None
) -> dict:
    """Classify whole disks and record all disks sharing a RAID or LVM device.

    Pools contain name/target; references contain owner/path. Both active and
    persistent VM definitions, snapshots and backing stores must be supplied.
    An unreadable definition must fail the caller, never become an empty list.
    """
    tops = [
        d
        for d in lsblk.get("blockdevices", [])
        if d.get("type") == "disk" and int(d.get("size") or 0) >= GIB
    ]
    descendants = {d["path"]: list(_walk(d)) for d in tops}
    vg_pvs: dict[str, set[str]] = {}
    for report in (pvs or {}).get("report", []):
        for pv in report.get("pv", []):
            vg = pv.get("vg_name", "").strip()
            if vg:
                vg_pvs.setdefault(vg, set()).add(pv["pv_name"].strip())
    rows = []
    for disk in tops:
        path = disk["path"]
        children = descendants[path]
        paths = {d["path"] for d in children}
        mounts = sorted({m for d in children for m in d.get("mountpoints") or [] if m})
        pool_names = set()
        reasons = set()
        affected = set()
        if disk.get("ro"):
            reasons.add("Read-only disk")
        if disk.get("tran") in ("usb", "iscsi", "fc"):
            reasons.add("Virtual Storage requires local SATA, SAS or NVMe disks")
        if any(d.get("type") == "mpath" for d in children):
            reasons.add("Multipath storage is not a local data disk")
        for d in children:
            if d.get("type") == "lvm" and OWN_VG.match(lv_vg(d.get("name", ""))):
                reasons.add("System or existing ibHiCS LVM volume")
        for mount in mounts:
            candidates = [p for p in pools if p.get("target") == mount]
            if (
                mount
                in ("/", "/boot", "/boot/efi", "/var", "/var/lib", "/var/lib/ibhics", "[SWAP]")
                or not candidates
            ):
                reasons.add(f"Protected mount: {mount}")
            else:
                pool_names.update(p["name"] for p in candidates)
        for ref in references:
            source = ref["path"]
            if source in paths or any(_under(source, m) for m in mounts if m != "[SWAP]"):
                affected.add(ref["owner"])
        if affected:
            reasons.add("In use by " + ", ".join(sorted(affected)))
        # Shared device-mapper descendants also catch VGs spanning several disks,
        # md RAID members and an SSD cache attached to a data LV.
        shared = {d["path"] for d in children if d.get("type") not in ("part", "disk")}
        required = {path}
        for other, subtree in descendants.items():
            if shared & {d["path"] for d in subtree}:
                required.add(other)
        for vg, physical in vg_pvs.items():
            if not paths & physical:
                continue
            represented = set()
            for other, subtree in descendants.items():
                found = physical & {d["path"] for d in subtree}
                if found:
                    required.add(other)
                    represented.update(found)
            if physical - represented:
                reasons.add(f"Volume group {vg} also uses undiscovered physical volumes")
        rows.append(
            {
                "path": path,
                "size_gib": round(int(disk["size"]) / GIB, 1),
                "model": (disk.get("model") or "").strip(),
                "serial": (disk.get("serial") or "").strip(),
                "media": "hdd" if _rotational(disk) else "ssd",
                "contents": _describe(disk),
                "mounts": mounts,
                "reclaim_pools": sorted(pool_names),
                "required_disks": sorted(required),
                "selectable": not reasons,
                "reasons": sorted(reasons),
            }
        )
    # A shared group is an indivisible selection. A protected member protects all.
    by_path = {d["path"]: d for d in rows}
    changed = True
    while changed:
        changed = False
        for row in rows:
            group = set(row["required_disks"])
            for path in list(group):
                group.update(by_path[path]["required_disks"])
            blocked = sorted(p for p in group if not by_path[p]["selectable"])
            if blocked and row["selectable"]:
                row["selectable"] = False
                row["reasons"].append("Shares storage with protected disk: " + ", ".join(blocked))
                changed = True
            row["required_disks"] = sorted(group)
    snapshot = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {"disks": rows, "fingerprint": hashlib.sha256(snapshot).hexdigest()}


def inventory(driver, run=subprocess.run) -> dict:
    """Read the live node; failures propagate so a broken probe cannot permit a wipe."""
    conn = getattr(driver, "_conn", None)
    if conn is None:
        raise ValueError("Physical disk inventory requires the libvirt driver")
    result = run(LSBLK, capture_output=True, text=True, check=True, timeout=15)
    pools = []
    for pool in conn.listAllStoragePools():
        root = ET.fromstring(pool.XMLDesc(0))
        target = root.findtext("target/path")
        if target:
            pools.append({"name": pool.name(), "target": target})
    references = []
    for dom in conn.listAllDomains():
        xmls = [dom.XMLDesc(2)]  # VIR_DOMAIN_XML_INACTIVE: configured disks too
        if dom.isActive():
            xmls.append(dom.XMLDesc(0))
        xmls.extend(s.XMLDesc(0) for s in dom.listAllSnapshots(0))
        for text in xmls:
            root = ET.fromstring(text)
            for source in root.findall(".//source"):
                for attr in ("file", "dev"):
                    if source.get(attr):
                        references.append({"owner": dom.name(), "path": source.get(attr)})
                if source.get("pool"):
                    matches = [p for p in pools if p["name"] == source.get("pool")]
                    if not matches:
                        raise ValueError(f"Cannot resolve storage pool for {dom.name()}")
                    references.extend({"owner": dom.name(), "path": p["target"]} for p in matches)
    physical = run(
        ["pvs", "--reportformat", "json", "-o", "pv_name,vg_name"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    result = parse_inventory(
        json.loads(result.stdout), pools, references, json.loads(physical.stdout)
    )
    interfaces = run(["ip", "-j", "-4", "address", "show"], capture_output=True,
                     text=True, check=True, timeout=15)
    result["addresses"] = [
        {"interface": interface["ifname"], "address": address["local"],
         "prefix": address["prefixlen"]}
        for interface in json.loads(interfaces.stdout)
        for address in interface.get("addr_info", []) if address.get("scope") == "global"
    ]
    return result
