"""Build libvirt domain and volume XML from a VMSpec.

Kept free of the libvirt bindings so it can be unit tested on any machine.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .base import ADVANCED_FIELDS, DiskSpec, Firmware, NicSpec, VMSpec
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


def disk_element(disk: DiskSpec, volume: str, dev: str, iothread: str | None = None) -> ET.Element:
    # Block disks name the LV itself: Debian's virt-aa-helper does not resolve
    # <source pool volume> for logical pools, so AppArmor would deny QEMU the device.
    # The domain metadata keeps which pool and volume the disk is.
    block = disk.backend == "block"
    disk_el = ET.Element("disk", type="block" if block else "volume", device="disk")
    driver = ET.SubElement(
        disk_el,
        "driver",
        name="qemu",
        type="raw" if block else "qcow2",
        cache="none",
        discard="unmap",
        error_policy="stop",
        rerror_policy="stop",
    )
    if disk.bus == "virtio":
        driver.set("io", "native")
        if iothread is not None:
            driver.set("iothread", iothread)
    if block:
        ET.SubElement(disk_el, "source", dev=f"/dev/{vg_name(disk.pool)}/{volume}")
    else:
        ET.SubElement(disk_el, "source", pool=disk.pool, volume=volume)
    ET.SubElement(disk_el, "target", dev=dev, bus=disk.bus)
    return disk_el


def disk_iothread_ids(root: ET.Element) -> set[str]:
    """Include explicit IDs and the IDs libvirt fills in up to the thread count."""
    count = int(root.findtext("iothreads", "0"))
    ids = {int(el.get("id")) for el in root.findall("iothreadids/iothread")}
    candidate = 1
    while len(ids) < count:
        ids.add(candidate)
        candidate += 1
    return {str(i) for i in ids} if count > 0 else set()


def normalize_disk_error_policy(root: ET.Element) -> bool:
    """Pause managed guests on disk errors instead of injecting failure into the OS."""
    changed = False
    for disk in root.findall("devices/disk[@device='disk']"):
        driver = disk.find("driver")
        if driver is None or driver.get("name") != "qemu":
            continue
        for key in ("error_policy", "rerror_policy"):
            if driver.get(key) != "stop":
                driver.set(key, "stop")
                changed = True
    return changed


def disk_error_protected(root: ET.Element) -> bool:
    drivers = [disk.find("driver") for disk in root.findall("devices/disk[@device='disk']")]
    return bool(drivers) and all(
        d is not None and d.get("error_policy") == "stop" and d.get("rerror_policy") == "stop"
        for d in drivers
    )


def normalize_disk_io(root: ET.Element) -> bool:
    """Fill missing virtio disk tuning in an offline definition; keep user choices."""
    changed = False
    for disk in root.findall("devices/disk[@device='disk']"):
        target, driver = disk.find("target"), disk.find("driver")
        if (
            target is None
            or target.get("bus") != "virtio"
            or driver is None
            or driver.get("cache") != "none"
            or driver.get("name") != "qemu"
        ):
            continue
        for key, value in (("io", "native"), ("discard", "unmap")):
            if driver.get(key) is None:
                driver.set(key, value)
                changed = True
        if driver.get("iothread") is not None or driver.find("iothreads") is not None:
            continue
        if root.find("iothreads") is None:
            ET.SubElement(root, "iothreads").text = "1"
            changed = True
        ids = disk_iothread_ids(root)
        if ids:
            driver.set("iothread", min(ids, key=int))
            changed = True
    return changed


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


MAX_VCPUS = 32  # vCPU slots a VM can grow to while running
MAX_MEMORY_MB = 512 * 1024  # memory a VM can grow to while running
MEMORY_SLOTS = 16  # DIMMs that can be added while running
DIMM_MB = 128  # Linux onlines memory in 128 MiB blocks


def hotplug_ready(root) -> bool:
    """The definition leaves room to add CPUs and memory while the VM runs."""
    vcpu = root.find("vcpu")
    return (
        root.find("maxMemory") is not None
        and root.find("cpu/numa/cell") is not None
        and vcpu is not None
        and vcpu.get("current") is not None
    )


def make_hotplug_ready(root, vcpus: int, memory_mb: int) -> None:
    """Give an older definition hot-add room (takes effect at its next start)."""
    vcpu = root.find("vcpu")
    vcpu.set("current", str(vcpus))
    vcpu.text = str(max(MAX_VCPUS, vcpus))
    if root.find("maxMemory") is None:
        el = ET.Element("maxMemory", slots=str(MEMORY_SLOTS), unit="MiB")
        el.text = str(max(MAX_MEMORY_MB, memory_mb))
        root.insert(list(root).index(root.find("memory")), el)
    cpu = root.find("cpu")
    if cpu is None:
        cpu = ET.SubElement(root, "cpu", mode="host-model")
    numa = cpu.find("numa")
    if numa is None:
        numa = ET.SubElement(cpu, "numa")
    for cell in numa.findall("cell"):
        numa.remove(cell)
    ET.SubElement(
        numa,
        "cell",
        id="0",
        cpus=f"0-{max(MAX_VCPUS, vcpus) - 1}",
        memory=str(memory_mb),
        unit="MiB",
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
    # Room to add CPUs and memory while the VM runs (hot-add).
    ET.SubElement(dom, "maxMemory", slots=str(MEMORY_SLOTS), unit="MiB").text = str(
        max(MAX_MEMORY_MB, spec.memory_mb)
    )
    ET.SubElement(dom, "memory", unit="MiB").text = str(spec.memory_mb)
    ET.SubElement(dom, "vcpu", placement="static", current=str(spec.vcpus)).text = str(
        max(MAX_VCPUS, spec.vcpus)
    )

    if any(disk.bus == "virtio" for disk in spec.disks):
        ET.SubElement(dom, "iothreads").text = "1"

    os_attrs = {"firmware": "efi"} if spec.firmware == Firmware.UEFI else {}
    os_el = ET.SubElement(dom, "os", **os_attrs)
    ET.SubElement(
        os_el, "type", arch="x86_64", machine="q35" if spec.machine_type == "q35" else "pc"
    ).text = "hvm"
    for dev in spec.boot or (["cdrom", "hd"] if spec.iso else ["hd"]):
        ET.SubElement(os_el, "boot", dev=dev)

    if spec.secure_boot:
        firmware = ET.SubElement(os_el, "firmware")
        ET.SubElement(firmware, "feature", enabled="yes", name="secure-boot")
        ET.SubElement(firmware, "feature", enabled="yes", name="enrolled-keys")
    features = ET.SubElement(dom, "features")
    if spec.secure_boot:
        ET.SubElement(features, "smm", state="on")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    # host-model keeps live migration possible between nodes with different CPU generations.
    cpu = ET.SubElement(dom, "cpu", mode="host-model")
    numa = ET.SubElement(cpu, "numa")  # memory hot-add needs a NUMA cell to add DIMMs to
    ET.SubElement(
        numa,
        "cell",
        id="0",
        cpus=f"0-{max(MAX_VCPUS, spec.vcpus) - 1}",
        memory=str(spec.memory_mb),
        unit="MiB",
    )
    ET.SubElement(dom, "clock", offset="utc")
    ET.SubElement(dom, "on_poweroff").text = "destroy"
    ET.SubElement(dom, "on_reboot").text = "restart"
    ET.SubElement(dom, "on_crash").text = "restart"

    devices = ET.SubElement(dom, "devices")
    for index, disk in enumerate(spec.disks):
        volume = volume_name(spec.name, index, disk)
        # SATA disks (older Windows with no virtio driver) come after the two CD drives.
        dev = f"vd{DISK_DEVS[index]}" if disk.bus == "virtio" else f"sd{DISK_DEVS[index + 2]}"
        devices.append(disk_element(disk, volume, dev, iothread="1"))
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

    apply_hardware_xml(dom, spec)
    return ET.tostring(dom, encoding="unicode")


def apply_hardware_xml(
    root: ET.Element, spec: VMSpec, fields: tuple[str, ...] = ADVANCED_FIELDS
) -> None:
    """Apply managed hardware without replacing device addresses or storage XML."""
    cpu = root.find("cpu")
    if cpu is None:
        cpu = ET.SubElement(root, "cpu")
    if "cpu_mode" in fields:
        # "default" keeps the CPU model the VM had before, which live-migrates between
        # nodes with different CPUs; host-passthrough pins it to this host's CPU.
        if spec.cpu_mode == "default":
            if cpu.get("mode") == "host-passthrough":
                cpu.attrib.pop("mode")
        else:
            cpu.set("mode", spec.cpu_mode)
            for attr in ("match", "check"):
                cpu.attrib.pop(attr, None)
            for tag in ("model", "vendor", "feature"):
                for el in cpu.findall(tag):
                    cpu.remove(el)
    if "cpu_sockets" in fields or "cpu_cores" in fields:
        for el in cpu.findall("topology"):
            cpu.remove(el)
        if spec.cpu_sockets is not None:
            # A fixed topology describes the full CPU capacity. Growing it is an offline edit.
            ET.SubElement(
                cpu,
                "topology",
                sockets=str(spec.cpu_sockets),
                cores=str(spec.cpu_cores),
                threads="1",
            )
            vcpu = root.find("vcpu")
            vcpu.text = str(spec.vcpus)
            vcpu.set("current", str(spec.vcpus))
            cell = cpu.find("numa/cell")
            if cell is not None:
                cell.set("cpus", "0" if spec.vcpus == 1 else f"0-{spec.vcpus - 1}")
    os_el = root.find("os")
    if "boot_menu_seconds" in fields:
        for el in os_el.findall("bootmenu"):
            os_el.remove(el)
        if spec.boot_menu_seconds:
            ET.SubElement(
                os_el, "bootmenu", enable="yes", timeout=str(spec.boot_menu_seconds * 1000)
            )
    if "restart_on_crash" in fields:
        crash = root.find("on_crash")
        if crash is None:
            crash = ET.SubElement(root, "on_crash")
        crash.text = "restart" if spec.restart_on_crash else "destroy"
    devices = root.find("devices")
    if "keymap" in fields:
        graphics = devices.find("graphics[@type='vnc']")
        if graphics is not None:
            graphics.attrib.pop("keymap", None)
            if spec.keymap != "default":
                graphics.set("keymap", spec.keymap)
    if "mouse" in fields:
        for el in devices.findall("input"):
            if el.get("type") in ("tablet", "mouse"):
                devices.remove(el)
        ET.SubElement(
            devices,
            "input",
            type="tablet" if spec.mouse == "tablet" else "mouse",
            bus="usb" if spec.mouse == "tablet" else "ps2",
        )
    if "ballooning" in fields:
        for el in devices.findall("memballoon"):
            devices.remove(el)
        ET.SubElement(devices, "memballoon", model="virtio" if spec.ballooning else "none")
    if "serial_port" in fields:
        if not spec.serial_port:
            for tag in ("serial", "console"):
                for el in devices.findall(tag):
                    devices.remove(el)
        elif devices.find("serial") is None:
            serial = ET.SubElement(devices, "serial", type="pty")
            ET.SubElement(serial, "target", port="0")
        if spec.serial_port and devices.find("console") is None:
            console = ET.SubElement(devices, "console", type="pty")
            ET.SubElement(console, "target", type="serial", port="0")
    if "usb_controller" in fields and spec.usb_controller:
        # A USB controller is normally auto-added by libvirt; request one explicitly.
        if devices.find("controller[@type='usb'][@model='qemu-xhci']") is None:
            controllers = devices.findall("controller[@type='usb']")
            index = max((int(el.get("index", "0")) for el in controllers), default=-1) + 1
            ET.SubElement(devices, "controller", type="usb", model="qemu-xhci", index=str(index))
    elif "usb_controller" in fields:
        for el in devices.findall("controller[@type='usb'][@model='qemu-xhci']"):
            devices.remove(el)
    if "tpm" in fields:
        for el in devices.findall("tpm"):
            devices.remove(el)
        if spec.tpm:
            tpm = ET.SubElement(
                devices, "tpm", model="tpm-crb" if spec.machine_type == "q35" else "tpm-tis"
            )
            ET.SubElement(tpm, "backend", type="emulator", version="2.0")
