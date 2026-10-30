"""Proxmox VE and plain KVM/libvirt hosts as Migrate sources, read over SSH.

The node lets its root key into the host with the source's root password once (like
for ESXi), lists the VMs with `qm` on Proxmox or `virsh` elsewhere, and later pulls the
disks over SSH before virt-v2v converts them.
"""

from __future__ import annotations

import json
import re
import shlex
import xml.etree.ElementTree as ET

from ..hypervisor.base import Firmware
from .sources import GIB, Source, SourceVM, interface_networks

# Proxmox disk keys in `qm config` (the CD-ROM and EFI vars are not disks to copy).
QM_DISK = re.compile(r"^(scsi|virtio|sata|ide)\d+$")
QM_SIZE = re.compile(r"size=(\d+(?:\.\d+)?)([KMGT])")
UNIT = {"K": 1 / 1024**2, "M": 1 / 1024, "G": 1, "T": 1024}


def parse_qm_config(vmid: str, text: str) -> tuple[SourceVM, list[str]]:
    """A VM from `qm config VMID`, and its disks' storage volume ids (local-lvm:vm-100-disk-0)."""
    conf = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            conf[key.strip()] = value.strip()
    volumes, sizes = [], []
    for key in sorted(conf, key=lambda k: (k.rstrip("0123456789"), int(re.sub(r"\D", "", k) or 0))):
        value = conf[key]
        if not QM_DISK.match(key) or "media=cdrom" in value or value.startswith("none"):
            continue
        volumes.append(value.split(",", 1)[0])
        m = QM_SIZE.search(value)
        sizes.append(max(1, round(float(m[1]) * UNIT[m[2]])) if m else 0)
    sockets, cores = int(conf.get("sockets", 1)), int(conf.get("cores", 1))
    vm = SourceVM(
        ref=vmid,
        name=re.sub(r"[^A-Za-z0-9_.-]", "-", conf.get("name", f"vm-{vmid}"))[:63],
        vcpus=int(conf.get("vcpus", sockets * cores)),
        memory_mb=int(conf.get("memory", 2048)),
        firmware=Firmware.UEFI if conf.get("bios") == "ovmf" else Firmware.BIOS,
        disks_gb=sizes,
        nics=sum(1 for k in conf if re.match(r"^net\d+$", k)) or 1,
        networks=[
            _qm_network(conf[k]) for k in sorted(conf, key=_net_index) if re.match(r"^net\d+$", k)
        ],
        guest_os=QM_OSTYPE.get(conf.get("ostype", ""), ""),
        notes=conf.get("description", "").replace("%0A", " ").strip()[:200],
    )
    return vm, volumes


QM_OSTYPE = {
    "l26": "Linux",
    "win11": "Windows 11 / Server 2022+",
    "win10": "Windows 10 / Server 2016-2019",
    "win8": "Windows 8 / Server 2012",
    "win7": "Windows 7 / Server 2008 R2",
    "other": "",
}


def parse_libvirt_domain(xml: str) -> tuple[SourceVM, list[str]]:
    """A VM from `virsh dumpxml`, and the paths of its disks on the host."""
    root = ET.fromstring(xml)
    memory = root.find("memory")
    unit = memory.get("unit", "KiB")
    mib = int(memory.text) // (1024 if unit == "KiB" else 1) if unit in ("KiB", "MiB") else 2048
    os_el = root.find("os")
    efi = os_el is not None and (os_el.get("firmware") == "efi" or os_el.find("loader") is not None)
    paths = []
    for disk in root.findall("./devices/disk[@device='disk']"):
        source = disk.find("source")
        if source is not None:
            paths.append(source.get("file") or source.get("dev") or "")
    name = root.findtext("name", "vm")
    vm = SourceVM(
        ref=name,
        name=re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:63],
        vcpus=int(root.findtext("vcpu", "1")),
        memory_mb=mib,
        firmware=Firmware.UEFI if efi else Firmware.BIOS,
        disks_gb=[0] * len(paths),
        nics=len(root.findall("./devices/interface")) or 1,
        networks=interface_networks(root),
    )
    return vm, [p for p in paths if p]


def list_kvm_vms(source: Source, ssh) -> list[SourceVM]:
    """VMs on a Proxmox VE or libvirt host. `ssh` is a HostSSH; disk paths go in vm.disk_paths."""
    ssh.ensure_access(source)
    if ssh.command(source, "command -v qm || true").strip():
        return _proxmox_vms(source, ssh)
    found = []
    for name in ssh.command(source, "virsh list --all --name").split():
        vm, paths = parse_libvirt_domain(ssh.command(source, f"virsh dumpxml {shlex.quote(name)}"))
        state = ssh.command(source, f"virsh domstate {shlex.quote(name)}").strip()
        vm.state = "running" if state == "running" else "stopped"
        vm.disk_paths = paths
        vm.disks_gb = [_size_gb(source, ssh, p) for p in paths]
        found.append(vm)
    return sorted(found, key=lambda v: v.name.lower())


def _proxmox_vms(source: Source, ssh) -> list[SourceVM]:
    found = []
    for line in ssh.command(source, "qm list").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        vmid, status = parts[0], parts[2]
        vm, volumes = parse_qm_config(vmid, ssh.command(source, f"qm config {vmid}"))
        vm.state = "running" if status == "running" else "stopped"
        vm.disk_paths = [
            ssh.command(source, f"pvesm path {shlex.quote(v)}").strip() for v in volumes
        ]
        found.append(vm)
    return sorted(found, key=lambda v: v.name.lower())


def _size_gb(source: Source, ssh, path: str) -> int:
    """Virtual size of a disk image (or size of a block device) on the host."""
    q = shlex.quote(path)
    try:
        info = ssh.command(source, f"qemu-img info --output=json -U {q} 2>/dev/null || true")
        size = json.loads(info)["virtual-size"] if info.strip() else None
        if size is None:
            size = int(ssh.command(source, f"blockdev --getsize64 {q}").split()[0])
        return -(-int(size) // GIB)
    except (ValueError, IndexError, KeyError, RuntimeError):
        return 0


def libvirt_xml(vm: SourceVM, disks: list[tuple[str, str]]) -> str:
    """A libvirt domain for virt-v2v -i libvirtxml: the VM with its disks copied locally.
    `disks` is (path, format) in boot order."""
    dom = ET.Element("domain", type="kvm")
    ET.SubElement(dom, "name").text = vm.name
    ET.SubElement(dom, "memory", unit="MiB").text = str(vm.memory_mb)
    ET.SubElement(dom, "vcpu").text = str(vm.vcpus)
    os_el = ET.SubElement(
        dom, "os", **({"firmware": "efi"} if vm.firmware == Firmware.UEFI else {})
    )
    ET.SubElement(os_el, "type").text = "hvm"
    devices = ET.SubElement(dom, "devices")
    for index, (path, fmt) in enumerate(disks):
        disk = ET.SubElement(devices, "disk", type="file", device="disk")
        ET.SubElement(disk, "driver", name="qemu", type=fmt)
        ET.SubElement(disk, "source", file=path)
        ET.SubElement(disk, "target", dev=f"sd{chr(ord('a') + index)}", bus="scsi")
    return ET.tostring(dom, encoding="unicode")


def _net_index(key: str) -> int:
    return int(key[3:]) if key[3:].isdigit() else 0


def _qm_network(value: str) -> str:
    """'virtio=BC:24:11:..,bridge=vmbr0,tag=100' -> 'vmbr0 VLAN 100'."""
    opts = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
    bridge = opts.get("bridge", "")
    return f"{bridge} VLAN {opts['tag']}" if opts.get("tag") else bridge
