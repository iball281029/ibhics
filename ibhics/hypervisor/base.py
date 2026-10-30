"""Hypervisor driver interface shared by the manager and node agents."""

from __future__ import annotations

import abc
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")


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

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ValueError(
                "name must start with a letter or digit and use only letters, digits, '_', '.', '-'"
            )
        if self.vcpus < 1:
            raise ValueError("vcpus must be at least 1")
        if self.memory_mb < 128:
            raise ValueError("memory_mb must be at least 128")
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


BLOCK_POOL_TYPES = (PoolType.ISCSI, PoolType.LVM)
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
        elif self.type == PoolType.LVM:
            if not self.device or not DEV_RE.match(self.device):
                raise ValueError("lvm pool needs a block device path under /dev/")
        if self.type in BLOCK_POOL_TYPES and not self.confirm_wipe:
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
