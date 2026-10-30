"""Where ibHiCS Migrate takes VMs from, and how it lists what is there.

- vmware: an ESXi host or a vCenter, read agentlessly through libvirt's ESX driver.
- ova: a folder of OVA files (VMware, VirtualBox or other exports).
- disk: a folder of disk images (VMDK, VHDX/VHD from Hyper-V, QCOW2, raw), one VM each.

Folders are paths on the compute nodes, typically an NFS or SMB share mounted there.
"""

from __future__ import annotations

import json
import re
import subprocess
import tarfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from ..hypervisor.base import HOST_RE, NAME_RE, Firmware

GIB = 1024**3
IMAGE_SUFFIXES = (".vmdk", ".vhdx", ".vhd", ".qcow2", ".img", ".raw")
VCENTER_PATH_RE = re.compile(r"^[A-Za-z0-9 _.%/-]+$")


class SourceType(StrEnum):
    VMWARE = "vmware"
    OVA = "ova"
    DISK = "disk"


class SourceNotFound(LookupError):
    pass


class SourceUnavailable(RuntimeError):
    pass


@dataclass
class Source:
    name: str
    type: SourceType
    host: str | None = None  # vmware: ESXi host or vCenter
    username: str | None = None
    password: str | None = None
    # vmware via vCenter: inventory path to the ESXi host, e.g. "DC1/host/cluster1/esx1".
    # ova/disk: the folder on the compute nodes.
    path: str | None = None
    verify_tls: bool = False  # most ESXi hosts have self-signed certificates

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ValueError("source name must use only letters, digits, '_', '.', '-'")
        if self.type == SourceType.VMWARE:
            if not self.host or not HOST_RE.match(self.host):
                raise ValueError("a VMware source needs the ESXi or vCenter host")
            if not self.username or not self.password:
                raise ValueError("a VMware source needs a username and password")
            if self.path and not VCENTER_PATH_RE.match(self.path):
                raise ValueError("vCenter path looks like Datacenter/host/Cluster/esxi-host")
        elif not self.path or not self.path.startswith("/"):
            raise ValueError(f"an {self.type} source needs an absolute folder path")

    @property
    def libvirt_uri(self) -> str:
        user = quote(self.username or "", safe="")
        query = "" if self.verify_tls else "?no_verify=1"
        if self.path:  # through vCenter
            return f"vpx://{user}@{self.host}/{self.path.strip('/')}{query}"
        return f"esx://{user}@{self.host}/{query}"

    def to_dict(self) -> dict:
        return {**asdict(self), "type": str(self.type)}

    @classmethod
    def from_dict(cls, data: dict) -> Source:
        return cls(**{**data, "type": SourceType(data["type"])})

    def masked(self) -> dict:
        data = self.to_dict()
        if data.get("password"):
            data["password"] = "********"
        return data


@dataclass
class SourceVM:
    ref: str  # vmware: the VM's name; ova/disk: the file path
    name: str
    vcpus: int = 2
    memory_mb: int = 4096
    firmware: Firmware = Firmware.BIOS
    disks_gb: list[int] = field(default_factory=list)  # 0 where the size is not known yet
    nics: int = 1
    state: str = ""
    guest_os: str = ""

    def to_dict(self) -> dict:
        return {**asdict(self), "firmware": str(self.firmware)}

    @classmethod
    def from_dict(cls, data: dict) -> SourceVM:
        return cls(**{**data, "firmware": Firmware(data["firmware"])})


# Discovery

Opener = Callable[[Source], object]  # returns a libvirt connection


def open_vmware(source: Source):
    try:
        import libvirt
    except ImportError:
        raise SourceUnavailable("libvirt-python is needed to read VMware sources") from None

    def credentials(creds, _data):
        for cred in creds:
            if cred[0] == libvirt.VIR_CRED_AUTHNAME:
                cred[4] = source.username
            elif cred[0] == libvirt.VIR_CRED_PASSPHRASE:
                cred[4] = source.password
        return 0

    auth = [[libvirt.VIR_CRED_AUTHNAME, libvirt.VIR_CRED_PASSPHRASE], credentials, None]
    try:
        return libvirt.openAuth(source.libvirt_uri, auth, 0)
    except libvirt.libvirtError as exc:
        raise SourceUnavailable(f"cannot connect to {source.host}: {exc}") from None


def list_vms(
    source: Source,
    opener: Opener = open_vmware,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[SourceVM]:
    if source.type == SourceType.VMWARE:
        conn = opener(source)
        try:
            vms = []
            for dom in conn.listAllDomains():
                vm = parse_vmware_xml(dom.XMLDesc(0))
                vm.state = "running" if dom.isActive() else "stopped"
                vms.append(vm)
            return sorted(vms, key=lambda v: v.name.lower())
        finally:
            conn.close()
    folder = Path(source.path)
    if not folder.is_dir():
        raise SourceUnavailable(f"{folder} is not a folder on this node; is the share mounted?")
    if source.type == SourceType.OVA:
        return [parse_ova(p) for p in sorted(folder.glob("*.ova"))]
    return [
        describe_image(p, run)
        for p in sorted(folder.iterdir())
        if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file()
    ]


def parse_vmware_xml(xml: str) -> SourceVM:
    """A VM as libvirt's ESX driver describes it."""
    root = ET.fromstring(xml)
    memory = root.find("memory")
    kib = int(memory.text) * (1024 if memory.get("unit", "KiB") == "MiB" else 1)
    os_el = root.find("os")
    efi = os_el is not None and (os_el.get("firmware") == "efi" or os_el.find("loader") is not None)
    disks = root.findall("./devices/disk[@device='disk']")
    name = root.findtext("name")
    return SourceVM(
        ref=name,
        name=name,
        vcpus=int(root.findtext("vcpu", "1")),
        memory_mb=kib // 1024,
        firmware=Firmware.UEFI if efi else Firmware.BIOS,
        disks_gb=[0] * len(disks),  # the ESX driver doesn't report sizes
        nics=len(root.findall("./devices/interface")),
        guest_os=root.findtext("description", "") or "",
    )


OVF_NS = {
    "ovf": "http://schemas.dmtf.org/ovf/envelope/1",
    "rasd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData",
    "vmw": "http://www.vmware.com/schema/ovf",
}
UNITS = {"byte": 1, "byte * 2^10": 1024, "byte * 2^20": 1024**2, "byte * 2^30": 1024**3}


def parse_ova(path: Path) -> SourceVM:
    with tarfile.open(path) as tar:
        member = next((m for m in tar.getmembers() if m.name.endswith(".ovf")), None)
        if member is None:
            raise ValueError(f"{path.name} has no OVF descriptor")
        vm = parse_ovf(tar.extractfile(member).read().decode())
    vm.ref = str(path)
    return vm


def parse_ovf(text: str) -> SourceVM:
    root = ET.fromstring(text)
    ovf = f"{{{OVF_NS['ovf']}}}"
    system = root.find(".//ovf:VirtualSystem", OVF_NS)
    name = system.findtext("ovf:Name", None, OVF_NS) or system.get(f"{ovf}id")
    vcpus, memory_mb, nics = 1, 1024, 0
    for item in system.findall(".//ovf:VirtualHardwareSection/ovf:Item", OVF_NS):
        kind = item.findtext("rasd:ResourceType", "", OVF_NS)
        quantity = item.findtext("rasd:VirtualQuantity", "0", OVF_NS)
        if kind == "3":
            vcpus = int(quantity)
        elif kind == "4":
            unit = item.findtext("rasd:AllocationUnits", "byte * 2^20", OVF_NS)
            memory_mb = int(quantity) * UNITS.get(unit.strip(), 1024**2) // 1024**2
        elif kind == "10":
            nics += 1
    disks = []
    for disk in root.findall(".//ovf:DiskSection/ovf:Disk", OVF_NS):
        unit = disk.get(f"{ovf}capacityAllocationUnits", "byte")
        size = int(disk.get(f"{ovf}capacity", "0")) * UNITS.get(unit.strip(), 1)
        disks.append(-(-size // GIB))
    firmware = Firmware.BIOS
    for config in system.findall(".//vmw:Config", OVF_NS):
        if config.get(f"{{{OVF_NS['vmw']}}}key") == "firmware":
            if config.get(f"{{{OVF_NS['vmw']}}}value") == "efi":
                firmware = Firmware.UEFI
    guest = system.findtext(".//ovf:OperatingSystemSection/ovf:Description", "", OVF_NS)
    return SourceVM(
        ref="",
        name=name,
        vcpus=vcpus,
        memory_mb=memory_mb,
        firmware=firmware,
        disks_gb=disks,
        nics=max(nics, 1),
        guest_os=guest,
    )


def describe_image(path: Path, run=subprocess.run) -> SourceVM:
    """One disk image as a VM. CPU and memory are guesses the user can change."""
    size_gb = 0
    try:
        out = run(
            ["qemu-img", "info", "--output=json", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        size_gb = -(-json.loads(out)["virtual-size"] // GIB)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError):
        pass
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", path.stem)[:63] or "imported"
    return SourceVM(ref=str(path), name=name, disks_gb=[size_gb])
