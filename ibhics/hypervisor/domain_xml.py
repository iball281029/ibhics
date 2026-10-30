"""Build libvirt domain and volume XML from a VMSpec.

Kept free of the libvirt bindings so it can be unit tested on any machine.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .base import DiskSpec, Firmware, VMSpec

DISK_DEVS = "abcdefghijklmnopqrstuvwxyz"
META_NS = "https://ibhics.org/xmlns/vm/1"  # ibHiCS's own facts about a VM, kept in its XML


ET.register_namespace("ibhics", META_NS)


def volume_name(vm_name: str, index: int, disk: DiskSpec) -> str:
    # Block volumes are raw; file images use qcow2 so they stay thin and support snapshots.
    suffix = "" if disk.backend == "block" else ".qcow2"
    return f"{vm_name}-disk{index}{suffix}"


def build_volume_xml(vm_name: str, index: int, disk: DiskSpec) -> str:
    vol = ET.Element("volume")
    ET.SubElement(vol, "name").text = volume_name(vm_name, index, disk)
    ET.SubElement(vol, "capacity", unit="GiB").text = str(disk.size_gb)
    if disk.backend == "file":
        target = ET.SubElement(vol, "target")
        ET.SubElement(target, "format", type="qcow2")
    return ET.tostring(vol, encoding="unicode")


def cdrom_element(iso: str | None) -> ET.Element:
    cdrom = ET.Element("disk", type="file", device="cdrom")
    ET.SubElement(cdrom, "driver", name="qemu", type="raw")
    if iso:
        ET.SubElement(cdrom, "source", file=iso)
    ET.SubElement(cdrom, "target", dev="sda", bus="sata")
    ET.SubElement(cdrom, "readonly")
    return cdrom


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
    if spec.os_type:
        meta = ET.SubElement(ET.SubElement(dom, "metadata"), f"{{{META_NS}}}vm")
        ET.SubElement(meta, f"{{{META_NS}}}os").text = spec.os_type
    ET.SubElement(dom, "memory", unit="MiB").text = str(spec.memory_mb)
    ET.SubElement(dom, "vcpu", placement="static").text = str(spec.vcpus)

    os_attrs = {"firmware": "efi"} if spec.firmware == Firmware.UEFI else {}
    os_el = ET.SubElement(dom, "os", **os_attrs)
    ET.SubElement(os_el, "type", arch="x86_64", machine="q35").text = "hvm"
    if spec.iso:
        ET.SubElement(os_el, "boot", dev="cdrom")
    ET.SubElement(os_el, "boot", dev="hd")

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
        disk_el = ET.SubElement(devices, "disk", type="volume", device="disk")
        ET.SubElement(
            disk_el,
            "driver",
            name="qemu",
            type="raw" if disk.backend == "block" else "qcow2",
            cache="none",
            discard="unmap",
        )
        ET.SubElement(disk_el, "source", pool=disk.pool, volume=volume_name(spec.name, index, disk))
        ET.SubElement(disk_el, "target", dev=f"vd{DISK_DEVS[index]}", bus=disk.bus)

    # A CD drive is always there (empty without an ISO), so a disc such as the guest
    # tools can go in later, even while the VM runs.
    devices.append(cdrom_element(spec.iso))

    for nic in spec.nics:
        iface = ET.SubElement(devices, "interface", type="network")
        if nic.mac:
            ET.SubElement(iface, "mac", address=nic.mac)
        ET.SubElement(iface, "source", network=nic.network)
        ET.SubElement(iface, "model", type=nic.model)

    # The console is reached through the manager's noVNC proxy, never directly.
    ET.SubElement(devices, "graphics", type="vnc", autoport="yes", listen="127.0.0.1")
    video = ET.SubElement(devices, "video")
    ET.SubElement(video, "model", type="virtio")
    ET.SubElement(devices, "serial", type="pty")
    channel = ET.SubElement(devices, "channel", type="unix")
    ET.SubElement(channel, "target", type="virtio", name="org.qemu.guest_agent.0")
    ET.SubElement(devices, "memballoon", model="virtio")

    return ET.tostring(dom, encoding="unicode")
