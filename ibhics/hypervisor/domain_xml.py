"""Build libvirt domain and volume XML from a VMSpec.

Kept free of the libvirt bindings so it can be unit tested on any machine.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .base import DiskSpec, Firmware, NicSpec, VMSpec
from .pool_xml import vg_name

DISK_DEVS = "abcdefghijklmnopqrstuvwxyz"
NAME_NS = "https://ibhics.org/xmlns/name/1"  # a running VM's new name (rename while on)
META_NS = "https://ibhics.org/xmlns/vm/1"  # ibHiCS's own facts about a VM, kept in its XML


ET.register_namespace("ibhics", META_NS)


def video_element(model: str) -> ET.Element:
    video = ET.Element("video")
    attrs = {"type": model}
    if model in ("vga", "qxl", "cirrus"):
        attrs["vram"] = "16384"
    ET.SubElement(video, "model", **attrs)
    return video


def file_name(vm_name: str) -> str:
    """The VM's name for files and LVM volumes: as is when ASCII; a Thai name becomes
    vm-<hash> (LVM, QEMU job IDs and other tools want ASCII)."""
    if vm_name.isascii():
        return vm_name
    import hashlib

    return "vm-" + hashlib.sha1(vm_name.encode()).hexdigest()[:10]


def volume_name(vm_name: str, index: int, disk: DiskSpec) -> str:
    # Block volumes are raw; file images use qcow2 so they stay thin and support snapshots.
    suffix = "" if disk.backend == "block" else ".qcow2"
    return f"{file_name(vm_name)}-disk{index}{suffix}"


def build_volume_xml(vm_name: str, index: int, disk: DiskSpec, backing: str | None = None) -> str:
    """Volume XML for a new disk; with `backing`, a qcow2 overlay on that image (a linked
    clone's disk: reads fall through to the template's, writes stay in the overlay)."""
    vol = ET.Element("volume")
    ET.SubElement(vol, "name").text = volume_name(vm_name, index, disk)
    ET.SubElement(vol, "capacity", unit="GiB").text = str(disk.size_gb)
    if disk.backend == "file":
        # Thin: libvirt would otherwise preallocate qcow2 metadata for the full size.
        ET.SubElement(vol, "allocation").text = "0"
        target = ET.SubElement(vol, "target")
        ET.SubElement(target, "format", type="qcow2")
        if backing:
            store = ET.SubElement(vol, "backingStore")
            ET.SubElement(store, "path").text = backing
            ET.SubElement(store, "format", type="qcow2")
    elif backing:
        raise ValueError("linked clones need qcow2 disks (a folder, thin disk or NFS pool)")
    return ET.tostring(vol, encoding="unicode")


def cdrom_element(iso: str | None, dev: str = "sda") -> ET.Element:
    cdrom = ET.Element("disk", type="file", device="cdrom")
    ET.SubElement(cdrom, "driver", name="qemu", type="raw")
    if iso:
        ET.SubElement(cdrom, "source", file=iso)
    ET.SubElement(cdrom, "target", dev=dev, bus="sata")
    ET.SubElement(cdrom, "readonly")
    return cdrom


def disk_element(disk: DiskSpec, volume: str, dev: str) -> ET.Element:
    # Block disks name the LV itself: Debian's virt-aa-helper does not resolve
    # <source pool volume> for logical pools, so AppArmor would deny QEMU the device.
    # The domain metadata keeps which pool and volume the disk is.
    block = disk.backend == "block"
    disk_el = ET.Element("disk", type="block" if block else "volume", device="disk")
    ET.SubElement(
        disk_el,
        "driver",
        name="qemu",
        type="raw" if block else "qcow2",
        cache="none",
        discard="unmap",
    )
    if block:
        ET.SubElement(disk_el, "source", dev=f"/dev/{vg_name(disk.pool)}/{volume}")
    else:
        ET.SubElement(disk_el, "source", pool=disk.pool, volume=volume)
    ET.SubElement(disk_el, "target", dev=dev, bus=disk.bus)
    return disk_el


def nic_element(nic: NicSpec) -> ET.Element:
    iface = ET.Element("interface", type="network")
    if nic.mac:
        ET.SubElement(iface, "mac", address=nic.mac)
    ET.SubElement(iface, "source", network=nic.network)
    ET.SubElement(iface, "model", type=nic.model or "virtio")
    return iface


def vm_meta(root: ET.Element) -> ET.Element:
    """The domain's <ibhics:vm> metadata element, created when missing."""
    metadata = root.find("metadata")
    if metadata is None:
        metadata = ET.SubElement(root, "metadata")
    meta = metadata.find(f"{{{META_NS}}}vm")
    if meta is None:
        meta = ET.SubElement(metadata, f"{{{META_NS}}}vm")
    return meta


def set_template_flag(meta: ET.Element, template: bool) -> None:
    for el in meta.findall(f"{{{META_NS}}}template"):
        meta.remove(el)
    if template:
        ET.SubElement(meta, f"{{{META_NS}}}template")


def set_recycled_mark(meta: ET.Element, at: str) -> None:
    for el in meta.findall(f"{{{META_NS}}}recycled"):
        meta.remove(el)
    if at:
        ET.SubElement(meta, f"{{{META_NS}}}recycled", at=at)


def recycled_at(root: ET.Element) -> str:
    el = root.find(f"metadata/{{{META_NS}}}vm/{{{META_NS}}}recycled")
    return el.get("at", "") if el is not None else ""


def template_facts(root: ET.Element) -> tuple[bool, str]:
    """(is a template, uuid of the template a linked clone is based on) from domain XML."""
    meta = root.find(f"metadata/{{{META_NS}}}vm")
    if meta is None:
        return False, ""
    linked = meta.find(f"{{{META_NS}}}linked")
    return (
        meta.find(f"{{{META_NS}}}template") is not None,
        linked.get("template", "") if linked is not None else "",
    )


def build_domain_xml(spec: VMSpec, uuid: str | None = None) -> str:
    spec.validate()
    if len(spec.disks) > len(DISK_DEVS):
        raise ValueError(f"at most {len(DISK_DEVS)} disks are supported")

    dom = ET.Element("domain", type="kvm")
    ET.SubElement(dom, "name").text = spec.name
    if uuid:
        ET.SubElement(dom, "uuid").text = uuid
    if spec.description:
        ET.SubElement(dom, "description").text = spec.description
    meta = ET.SubElement(ET.SubElement(dom, "metadata"), f"{{{META_NS}}}vm")
    if spec.os_type:
        ET.SubElement(meta, f"{{{META_NS}}}os").text = spec.os_type
    set_template_flag(meta, spec.template)
    if spec.linked_to:
        ET.SubElement(meta, f"{{{META_NS}}}linked", template=spec.linked_to)
    set_recycled_mark(meta, spec.recycled)
    ET.SubElement(dom, "memory", unit="MiB").text = str(spec.memory_mb)
    ET.SubElement(dom, "vcpu", placement="static").text = str(spec.vcpus)

    os_attrs = {"firmware": "efi"} if spec.firmware == Firmware.UEFI else {}
    os_el = ET.SubElement(dom, "os", **os_attrs)
    ET.SubElement(os_el, "type", arch="x86_64", machine="q35").text = "hvm"
    for dev in spec.boot or (["cdrom", "hd"] if spec.iso else ["hd"]):
        ET.SubElement(os_el, "boot", dev=dev)

    features = ET.SubElement(dom, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    # host-model keeps live migration possible between nodes with different CPU generations.
    ET.SubElement(dom, "cpu", mode="host-model")
    ET.SubElement(dom, "clock", offset="utc")
    ET.SubElement(dom, "on_poweroff").text = "destroy"
    ET.SubElement(dom, "on_reboot").text = "restart"
    ET.SubElement(dom, "on_crash").text = "restart"

    devices = ET.SubElement(dom, "devices")
    for index, disk in enumerate(spec.disks):
        volume = volume_name(spec.name, index, disk)
        # SATA disks (older Windows with no virtio driver) come after the two CD drives.
        dev = f"vd{DISK_DEVS[index]}" if disk.bus == "virtio" else f"sd{DISK_DEVS[index + 2]}"
        devices.append(disk_element(disk, volume, dev))
        ET.SubElement(meta, f"{{{META_NS}}}disk", dev=dev, pool=disk.pool, volume=volume)

    # A CD drive is always there (empty without an ISO), so a disc such as the guest
    # tools can go in later, even while the VM runs.
    devices.append(cdrom_element(spec.iso))
    if spec.driver_iso:
        devices.append(cdrom_element(spec.driver_iso, dev="sdb"))

    for nic in spec.nics:
        devices.append(nic_element(nic))

    # The console is reached through the manager's noVNC proxy, never directly.
    ET.SubElement(devices, "graphics", type="vnc", autoport="yes", listen="127.0.0.1")
    devices.append(video_element(spec.video))
    # An absolute pointer: the mouse in the console follows the cursor exactly (a PS/2
    # mouse alone drifts away from it).
    ET.SubElement(devices, "input", type="tablet", bus="usb")
    ET.SubElement(devices, "serial", type="pty")
    channel = ET.SubElement(devices, "channel", type="unix")
    ET.SubElement(channel, "target", type="virtio", name="org.qemu.guest_agent.0")
    ET.SubElement(devices, "memballoon", model="virtio")

    return ET.tostring(dom, encoding="unicode")
