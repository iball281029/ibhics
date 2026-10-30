"""Hypervisor driver interface shared by the manager and node agents."""

from __future__ import annotations

import abc
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")


BOOT_DEVICES = ("hd", "cdrom", "network")


class VMState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    PAUSED = "paused"
    UNKNOWN = "unknown"


class Firmware(StrEnum):
    BIOS = "bios"
    UEFI = "uefi"


@dataclass
class DiskSpec:
    size_gb: int
    # "file": a qcow2 image in a directory pool (local dir, NFS).
    # "block": a raw volume in a block pool (LVM on a local disk, FC or iSCSI LUN; Ceph RBD).
    # Drivers set this from the pool's type when the VM is created.
    backend: str = "file"
    pool: str = "default"
    bus: str = "virtio"
    # Reported, not requested: space the disk takes in its pool. Below size_gb for thin
    # disks (qcow2); on thick LVM it equals the size.
    used_gb: float | None = None


@dataclass
class NicSpec:
    network: str = "default"
    model: str = "virtio"
    mac: str | None = None


@dataclass
class VMSpec:
    name: str
    vcpus: int = 1
    memory_mb: int = 1024
    firmware: Firmware = Firmware.BIOS
    disks: list[DiskSpec] = field(default_factory=list)
    nics: list[NicSpec] = field(default_factory=list)
    iso: str | None = None
    description: str = ""
    os_type: str = ""  # what the admin says the guest runs, e.g. "Windows Server 2022"
    # Boot devices in order: "hd", "cdrom", "network". None: the CD first when an ISO is
    # set at creation, then the disk.
    boot: list[str] | None = None
    autostart: bool = False  # start with the node

    def validate(self) -> None:
        if len(self.os_type) > 64:
            raise ValueError("os_type is at most 64 characters")
        if not NAME_RE.match(self.name):
            raise ValueError(
                "name must start with a letter or digit and use only letters, digits, '_', '.', '-'"
            )
        if self.vcpus < 1:
            raise ValueError("vcpus must be at least 1")
        if self.memory_mb < 128:
            raise ValueError("memory_mb must be at least 128")
        if self.boot is not None:
            if not self.boot or any(b not in BOOT_DEVICES for b in self.boot):
                raise ValueError(f"boot devices must be from {', '.join(BOOT_DEVICES)}")
            if len(set(self.boot)) != len(self.boot):
                raise ValueError("each boot device can appear once")
        for disk in self.disks:
            if disk.size_gb < 1:
                raise ValueError("disk size_gb must be at least 1")
            if disk.backend not in ("file", "block"):
                raise ValueError("disk backend must be 'file' or 'block'")


class PoolType(StrEnum):
    DIR = "dir"  # directory on a local filesystem
    NFS = "nfs"
    ISCSI = "iscsi"  # log in to a target, then LVM on the chosen LUN
    LVM = "lvm"  # LVM volume group on a block device: local disk or FC/multipath LUN
    # A whole local disk formatted XFS, holding thin qcow2 images: disks take only the
    # space the guest writes, and imports skip empty areas instead of writing zeros.
    DISK = "disk"


BLOCK_POOL_TYPES = (PoolType.ISCSI, PoolType.LVM)
WIPE_POOL_TYPES = (*BLOCK_POOL_TYPES, PoolType.DISK)
DEV_RE = re.compile(r"^/dev/[A-Za-z0-9_./:-]+$")
IQN_RE = re.compile(r"^(iqn\.\d{4}-\d{2}\.[^\s]+|eui\.[0-9A-Fa-f]{16})$")
HOST_RE = re.compile(r"^[A-Za-z0-9_.:\[\]-]+$")


@dataclass
class PoolSpec:
    name: str
    type: PoolType
    path: str | None = None  # dir: where images live
    host: str | None = None  # nfs server, or iSCSI portal host
    port: int = 3260  # iSCSI portal port
    export: str | None = None  # nfs export path
    iqn: str | None = None  # iSCSI target IQN
    lun: int = 0
    device: str | None = None  # lvm: /dev/mapper/mpatha, /dev/disk/by-id/..., /dev/sdb
    # Creating an LVM volume group erases the device; callers must say they mean it.
    confirm_wipe: bool = False

    @property
    def disk_backend(self) -> str:
        return "block" if self.type in BLOCK_POOL_TYPES else "file"

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ValueError("pool name must use only letters, digits, '_', '.', '-'")
        if self.type == PoolType.DIR:
            if self.path is not None and not self.path.startswith("/"):
                raise ValueError("dir pool path must be absolute")
        elif self.type == PoolType.NFS:
            if not self.host or not HOST_RE.match(self.host):
                raise ValueError("nfs pool needs a server host")
            if not self.export or not self.export.startswith("/"):
                raise ValueError("nfs pool needs an absolute export path")
        elif self.type == PoolType.ISCSI:
            if not self.host or not HOST_RE.match(self.host):
                raise ValueError("iscsi pool needs a portal host")
            if not self.iqn or not IQN_RE.match(self.iqn):
                raise ValueError("iscsi pool needs a target IQN (iqn.YYYY-MM.... or eui....)")
            if self.lun < 0 or not 0 < self.port < 65536:
                raise ValueError("invalid iSCSI LUN or port")
        elif self.type in (PoolType.LVM, PoolType.DISK):
            if not self.device or not DEV_RE.match(self.device):
                raise ValueError(f"{self.type} pool needs a block device path under /dev/")
        if self.type in WIPE_POOL_TYPES and not self.confirm_wipe:
            raise ValueError("this pool erases its device; set confirm_wipe to true")


@dataclass
class PoolInfo:
    name: str
    type: PoolType
    active: bool
    capacity_gb: float
    allocation_gb: float
    available_gb: float
    source: str = ""  # human-readable origin: path, host:/export, iqn/lun, device


class PoolNotFound(LookupError):
    pass


@dataclass
class VMInfo:
    uuid: str
    spec: VMSpec
    state: VMState
    # Running with settings saved that only apply after the next power off and on.
    restart_needed: bool = False


@dataclass
class DiskEdit:
    """One disk in a VM edit: index of an existing disk (to keep or grow), or None for a
    new disk of size_gb in pool."""

    size_gb: int
    index: int | None = None
    pool: str = "default"


@dataclass
class NicEdit:
    """One NIC in a VM edit: mac of an existing NIC (to keep or move), or None for new."""

    network: str = "default"
    mac: str | None = None
    model: str = "virtio"


@dataclass
class VMEdit:
    """A VM's new settings. Fields left None stay as they are. disks and nics, when given,
    are the whole new list: existing entries left out are removed."""

    description: str | None = None
    os_type: str | None = None
    vcpus: int | None = None
    memory_mb: int | None = None
    boot: list[str] | None = None
    autostart: bool | None = None
    disks: list[DiskEdit] | None = None
    nics: list[NicEdit] | None = None
    # Removing a disk detaches it; this also deletes its volume and data.
    delete_removed_disks: bool = False

    def apply_to(self, spec: VMSpec) -> VMSpec:
        """The VMSpec this edit asks for, validated; disks and NICs are checked apart."""
        new = VMSpec(**{**spec.__dict__})
        for name in ("description", "os_type", "vcpus", "memory_mb", "boot", "autostart"):
            if getattr(self, name) is not None:
                setattr(new, name, getattr(self, name))
        new.validate()
        if self.disks is not None:
            seen = [d.index for d in self.disks if d.index is not None]
            if len(set(seen)) != len(seen) or any(not 0 <= i < len(spec.disks) for i in seen):
                raise ValueError("unknown or repeated disk index")
            for d in self.disks:
                if d.size_gb < 1:
                    raise ValueError("disk size_gb must be at least 1")
                if d.index is not None and d.size_gb < spec.disks[d.index].size_gb:
                    raise ValueError(f"disk {d.index + 1} cannot shrink, only grow")
            if not self.disks:
                raise ValueError("a VM needs at least one disk")
        if self.nics is not None:
            macs = [n.mac for n in self.nics if n.mac]
            known = {n.mac for n in spec.nics}
            if len(set(macs)) != len(macs) or not set(macs) <= known:
                raise ValueError("unknown or repeated NIC")
        return new


@dataclass
class GuestInterface:
    name: str
    mac: str
    ips: list[str] = field(default_factory=list)  # with prefix, e.g. 10.0.0.21/24


@dataclass
class GuestInfo:
    """What the guest itself reports through qemu-guest-agent. Without an agent only
    `agent=False` (and, for NAT networks, DHCP lease addresses) is known."""

    agent: bool = False
    os_name: str = ""  # pretty name, e.g. "Ubuntu 24.04.1 LTS"
    os_version: str = ""
    kernel: str = ""
    hostname: str = ""
    interfaces: list[GuestInterface] = field(default_factory=list)
    # Filesystems as the guest sees them: what is really used, whatever the disk format.
    fs_used_gb: float | None = None
    fs_total_gb: float | None = None

    @property
    def ips(self) -> list[str]:
        return [
            ip.split("/")[0]
            for i in self.interfaces
            for ip in i.ips
            if not ip.startswith(("127.", "::1", "fe80"))
        ]


@dataclass
class HostInfo:
    hostname: str
    cpus: int
    memory_mb: int
    hypervisor: str


class VMNotFound(LookupError):
    pass


class VMExists(ValueError):
    pass


class InvalidState(RuntimeError):
    pass


class MigrationFailed(RuntimeError):
    pass


class HypervisorDriver(abc.ABC):
    """Operations every node must support. The manager talks only to this interface."""

    @abc.abstractmethod
    def host_info(self) -> HostInfo: ...

    @abc.abstractmethod
    def list_vms(self) -> list[VMInfo]: ...

    @abc.abstractmethod
    def get_vm(self, uuid: str) -> VMInfo: ...

    @abc.abstractmethod
    def create_vm(self, spec: VMSpec) -> VMInfo: ...

    @abc.abstractmethod
    def start_vm(self, uuid: str) -> VMInfo: ...

    @abc.abstractmethod
    def stop_vm(self, uuid: str, force: bool = False) -> VMInfo: ...

    @abc.abstractmethod
    def delete_vm(self, uuid: str) -> None: ...

    @abc.abstractmethod
    def list_pools(self) -> list[PoolInfo]: ...

    @abc.abstractmethod
    def create_pool(self, spec: PoolSpec) -> PoolInfo: ...

    @abc.abstractmethod
    def delete_pool(self, name: str) -> None:
        """Stop managing the pool. Data on it (images, LVM volumes) is left in place."""

    def migrate_vm(
        self,
        uuid: str,
        dest_uri: str,
        copy_storage: bool = False,
        bandwidth_mib: int | None = None,
    ) -> None:
        """Move the VM to the node whose libvirt daemon is at `dest_uri`.

        A running VM keeps running (live migration); a stopped one is moved by definition
        only, which needs shared storage. With `copy_storage` the disks are copied to the
        same-named pool on the destination while the VM runs, then removed here.
        """
        raise NotImplementedError

    def insert_cdrom(self, uuid: str, iso: str | None) -> VMInfo:
        """Put an ISO in the VM's CD drive, or empty it with None; works while it runs."""
        raise NotImplementedError

    def update_vm(self, uuid: str, edit: VMEdit) -> VMInfo:
        """Change a VM's settings. On a running VM disks and NICs change at once; CPU,
        memory and boot order are saved for its next start (restart_needed)."""
        raise NotImplementedError

    def list_local_disks(self) -> list:
        """Whole disks on this node that could become an LVM pool (disks.LocalDisk)."""
        return []

    @contextmanager
    def scratch_space(self, pool: str, size_gb: int) -> Iterator[Path]:
        """A temporary directory with room for size_gb of disk images, on storage pool
        `pool` where the driver can do that, so a large import does not fill the system
        disk. Removed afterwards."""
        path = Path(tempfile.mkdtemp(prefix="ibhics-scratch-"))
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def guest_info(self, uuid: str) -> GuestInfo:
        """OS, hostname and addresses as the running guest reports them."""
        return GuestInfo()

    def list_guest_info(self) -> dict[str, GuestInfo]:
        """guest_info for every running VM on this node, keyed by uuid."""
        result = {}
        for vm in self.list_vms():
            if vm.state == VMState.RUNNING:
                try:
                    result[vm.uuid] = self.guest_info(vm.uuid)
                except (VMNotFound, InvalidState):
                    continue
        return result

    def console_address(self, uuid: str) -> tuple[str, int]:
        """Host and TCP port of the running VM's VNC display, reachable from this node."""
        raise NotImplementedError

    # Backup primitives. Agents and the manager's own node implement them; the backup
    # engine (ibhics.backup) does the rest.

    def export_disks(self, uuid: str) -> AbstractContextManager[list[Path]]:
        """Raw images of the VM's disks, consistent with each other, for the length of the
        with-block. Running VMs keep running."""
        raise NotImplementedError

    def import_disk(self, uuid: str, index: int, raw_image: Path) -> None:
        """Overwrite disk `index` of a stopped VM with a raw image."""
        raise NotImplementedError
