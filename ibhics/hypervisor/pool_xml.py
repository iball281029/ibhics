"""Build libvirt storage pool XML for each ibHiCS pool type.

An iSCSI pool is two libvirt pools: an `iscsi` pool that logs in to the target
(named `<name>-iscsi`), and a `logical` pool holding an LVM volume group on the
chosen LUN. VMs only ever see the logical pool.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .base import PoolSpec, PoolType

DIR_ROOT = "/var/lib/ibhics/pools"
NFS_ROOT = "/mnt/ibhics"


def iscsi_session_name(pool_name: str) -> str:
    return f"{pool_name}-iscsi"


def vg_name(pool_name: str) -> str:
    # LVM names cannot contain '.', and a prefix keeps ibHiCS VGs recognisable.
    return "ibhics_" + pool_name.replace(".", "_").replace("-", "_")


def build_dir_pool_xml(spec: PoolSpec) -> str:
    pool = ET.Element("pool", type="dir")
    ET.SubElement(pool, "name").text = spec.name
    target = ET.SubElement(pool, "target")
    ET.SubElement(target, "path").text = spec.path or f"{DIR_ROOT}/{spec.name}"
    return ET.tostring(pool, encoding="unicode")


def build_nfs_pool_xml(spec: PoolSpec) -> str:
    pool = ET.Element("pool", type="netfs")
    ET.SubElement(pool, "name").text = spec.name
    source = ET.SubElement(pool, "source")
    ET.SubElement(source, "host", name=spec.host)
    ET.SubElement(source, "dir", path=spec.export)
    ET.SubElement(source, "format", type="nfs")
    target = ET.SubElement(pool, "target")
    ET.SubElement(target, "path").text = f"{NFS_ROOT}/{spec.name}"
    return ET.tostring(pool, encoding="unicode")


def build_iscsi_session_xml(spec: PoolSpec) -> str:
    pool = ET.Element("pool", type="iscsi")
    ET.SubElement(pool, "name").text = iscsi_session_name(spec.name)
    source = ET.SubElement(pool, "source")
    ET.SubElement(source, "host", name=spec.host, port=str(spec.port))
    ET.SubElement(source, "device", path=spec.iqn)
    target = ET.SubElement(pool, "target")
    # by-path names stay the same across reboots, unlike /dev/sdX.
    ET.SubElement(target, "path").text = "/dev/disk/by-path"
    return ET.tostring(pool, encoding="unicode")


def build_lvm_pool_xml(name: str, device: str) -> str:
    pool = ET.Element("pool", type="logical")
    ET.SubElement(pool, "name").text = name
    source = ET.SubElement(pool, "source")
    ET.SubElement(source, "device", path=device)
    ET.SubElement(source, "name").text = vg_name(name)
    ET.SubElement(source, "format", type="lvm2")
    target = ET.SubElement(pool, "target")
    ET.SubElement(target, "path").text = f"/dev/{vg_name(name)}"
    return ET.tostring(pool, encoding="unicode")


def build_pool_xml(spec: PoolSpec) -> str:
    """XML for the pool VMs use. iSCSI also needs build_iscsi_session_xml first."""
    spec.validate()
    if spec.type == PoolType.DIR:
        return build_dir_pool_xml(spec)
    if spec.type == PoolType.NFS:
        return build_nfs_pool_xml(spec)
    if spec.type == PoolType.LVM:
        return build_lvm_pool_xml(spec.name, spec.device)
    raise ValueError("iSCSI pools are built from the LUN device; use build_lvm_pool_xml")


def describe_pool_xml(xml: str, iscsi_sessions: dict[str, str]) -> tuple[PoolType, str] | None:
    """Map a libvirt pool back to (ibHiCS type, source). None for pools ibHiCS hides.

    iscsi_sessions maps a session pool's name to "host:port iqn" for every iSCSI
    session pool on the host, so a logical pool on one of its LUNs reports as iSCSI.
    """
    root = ET.fromstring(xml)
    kind = root.get("type")
    name = root.findtext("name", "")
    if kind == "dir":
        return PoolType.DIR, root.findtext("target/path", "")
    if kind == "netfs":
        host = root.find("source/host")
        export = root.find("source/dir")
        return PoolType.NFS, f"{host.get('name') if host is not None else ''}:" + (
            export.get("path") if export is not None else ""
        )
    if kind == "logical":
        device = root.find("source/device")
        dev = device.get("path", "") if device is not None else ""
        session = iscsi_sessions.get(iscsi_session_name(name))
        if session:
            return PoolType.ISCSI, f"{session} ({dev})"
        return PoolType.LVM, dev
    return None  # iscsi session pools and anything else are internal
