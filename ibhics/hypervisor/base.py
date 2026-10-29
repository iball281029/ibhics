"""Hypervisor driver interface shared by the manager and node agents."""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from enum import StrEnum

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
    # "file" keeps a qcow2 image in the node's local pool; "rbd" places it in a Ceph pool.
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
            if disk.backend not in ("file", "rbd"):
                raise ValueError("disk backend must be 'file' or 'rbd'")


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
