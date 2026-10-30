"""Where ibHiCS Migrate takes VMs from, and how it lists what is there.

- vmware: an ESXi host or a vCenter, read agentlessly through libvirt's ESX driver.
- ova: a folder of OVA files (VMware, VirtualBox or other exports).
- disk: a folder of disk images (VMDK, VHDX/VHD from Hyper-V, QCOW2, raw), one VM each.

Folders are paths on the compute nodes, typically an NFS or SMB share mounted there.
"""

from __future__ import annotations

import base64
import json
import math
import re
import socket
import ssl
import subprocess
import tarfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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
    notes: str = ""  # vmware: the VM's annotation

    def to_dict(self) -> dict:
        return {**asdict(self), "firmware": str(self.firmware)}

    @classmethod
    def from_dict(cls, data: dict) -> SourceVM:
        return cls(**{**data, "firmware": Firmware(data["firmware"])})


# Discovery

Opener = Callable[[Source], object]  # returns a libvirt connection


def check_reachable(host: str, port: int = 443, timeout: float = 5.0) -> None:
    """Fail fast with a clear message: libvirt waits minutes on an unreachable host."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except OSError as exc:
        reason = "timed out" if isinstance(exc, TimeoutError) else exc.strerror or str(exc)
        raise SourceUnavailable(
            f"cannot reach {host} on port {port} ({reason}). Check that this node can route "
            f"to it and that no firewall blocks HTTPS between them."
        ) from None


def open_vmware(source: Source):
    check_reachable(source.host)
    try:
        import libvirt
    except ImportError:
        raise SourceUnavailable("libvirt-python is needed to read VMware sources") from None

    def credentials(creds, _data):
        for cred in creds:
            if cred[0] == libvirt.VIR_CRED_AUTHNAME:
                cred[4] = source.username
            elif cred[0] in (libvirt.VIR_CRED_PASSPHRASE, libvirt.VIR_CRED_NOECHOPROMPT):
                cred[4] = source.password
        return 0

    # The ESX driver asks for the password as a no-echo prompt on some libvirt versions.
    kinds = [libvirt.VIR_CRED_AUTHNAME, libvirt.VIR_CRED_PASSPHRASE, libvirt.VIR_CRED_NOECHOPROMPT]
    auth = [kinds, credentials, None]
    try:
        return libvirt.openAuth(source.libvirt_uri, auth, 0)
    except libvirt.libvirtError as exc:
        raise SourceUnavailable(f"cannot connect to {source.host}: {exc}") from None


def list_vms(
    source: Source,
    opener: Opener = open_vmware,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    fetch: Callable[[Source, str], str] | None = None,
) -> list[SourceVM]:
    if source.type == SourceType.VMWARE:
        conn = opener(source)
        try:
            found = []
            for dom in conn.listAllDomains():
                xml = dom.XMLDesc(0)
                vm = parse_vmware_xml(xml)
                vm.state = "running" if dom.isActive() else "stopped"
                found.append((vm, vmware_disk_files(xml)))
        finally:
            conn.close()
        # Disk sizes and guest OS are in the VM's files on the datastore, not in what
        # libvirt reports; read them in parallel and leave blanks where that fails.
        with ThreadPoolExecutor(max_workers=8) as pool:
            reader = fetch or read_datastore_file
            list(pool.map(lambda item: add_vmware_details(source, *item, fetch=reader), found))
        return sorted((vm for vm, _ in found), key=lambda v: v.name.lower())
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
        notes=(root.findtext("description", "") or "").strip(),
    )


DATASTORE_PATH = re.compile(r"^\[(?P<ds>[^\]]+)\]\s*(?P<path>.+)$")
VMDK_EXTENT = re.compile(r"^\s*(?:RW|RDONLY|NOACCESS)\s+(\d+)\s", re.MULTILINE)
VMX_GUEST = re.compile(r'^\s*guestOS\s*=\s*"([^"]*)"', re.MULTILINE)
GUEST_NAMES = [  # VMware guestOS ids, by prefix
    ("windows2022srvnext", "Windows Server 2025"),
    ("windows2019srvnext", "Windows Server 2022"),
    ("windows2019srv", "Windows Server 2019"),
    ("windows9srv", "Windows Server 2016"),
    ("windows8srv", "Windows Server 2012"),
    ("windows11", "Windows 11"),
    ("windows9", "Windows 10"),
    ("windows", "Windows"),
    ("ubuntu", "Ubuntu Linux"),
    ("debian", "Debian GNU/Linux"),
    ("rhel", "Red Hat Enterprise Linux"),
    ("centos", "CentOS"),
    ("rocky", "Rocky Linux"),
    ("almalinux", "AlmaLinux"),
    ("oraclelinux", "Oracle Linux"),
    ("sles", "SUSE Linux Enterprise"),
    ("freebsd", "FreeBSD"),
    ("other", "Other Linux"),
]


def guest_os_name(guest_id: str) -> str:
    gid = guest_id.lower()
    for prefix, name in GUEST_NAMES:
        if gid.startswith(prefix):
            bits = " (64-bit)" if gid.endswith("-64") or gid.endswith("64guest") else ""
            version = re.match(rf"{prefix}(\d+)", gid) if not prefix.startswith("windows") else None
            return f"{name}{' ' + version.group(1) if version else ''}{bits}"
    return guest_id


def vmware_disk_files(xml: str) -> list[str]:
    root = ET.fromstring(xml)
    return [
        d.find("source").get("file", "")
        for d in root.findall("./devices/disk[@device='disk']")
        if d.find("source") is not None
    ]


def datastore_url(source: Source, path: str) -> str | None:
    """HTTPS URL of a file given as '[datastore] folder/file' (ESXi's /folder service)."""
    m = DATASTORE_PATH.match(path)
    if not m:
        return None
    dc = source.path.strip("/").split("/")[0] if source.path else "ha-datacenter"
    query = urllib.parse.urlencode({"dcPath": dc, "dsName": m["ds"]})
    return f"https://{source.host}/folder/{urllib.parse.quote(m['path'])}?{query}"


def read_datastore_file(source: Source, url: str, limit: int = 65536) -> str:
    ctx = ssl.create_default_context()
    if not source.verify_tls:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    req = urllib.request.Request(url)
    token = base64.b64encode(f"{source.username}:{source.password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Range", f"bytes=0-{limit - 1}")  # descriptors are small; never pull a disk
    with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
        return resp.read(limit).decode("utf-8", "replace")


def add_vmware_details(
    source: Source,
    vm: SourceVM,
    disks: list[str],
    fetch: Callable[[Source, str], str] = read_datastore_file,
) -> None:
    for i, disk in enumerate(disks[: len(vm.disks_gb)]):
        url = datastore_url(source, disk)
        try:
            sectors = sum(int(n) for n in VMDK_EXTENT.findall(fetch(source, url))) if url else 0
        except (OSError, ValueError):
            continue
        vm.disks_gb[i] = math.ceil(sectors * 512 / 2**30)
    m = DATASTORE_PATH.match(disks[0]) if disks else None
    if m:  # the .vmx is normally next to the first disk and named after its folder
        folder = m["path"].rsplit("/", 1)[0]
        vmx = f"[{m['ds']}] {folder}/{folder.rsplit('/', 1)[-1]}.vmx"
        try:
            found = VMX_GUEST.search(fetch(source, datastore_url(source, vmx)))
        except (OSError, ValueError):
            found = None
        if found:
            vm.guest_os = guest_os_name(found.group(1))


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
