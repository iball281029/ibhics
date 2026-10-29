"""Request and response models for the ibHiCS REST API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..hypervisor import DiskSpec, Firmware, HostInfo, NicSpec, VMInfo, VMSpec, VMState


class DiskIn(BaseModel):
    size_gb: int = Field(ge=1, le=65536)
    backend: str = Field(default="file", pattern="^(file|rbd)$")
    pool: str = "default"


class NicIn(BaseModel):
    network: str = "default"
    model: str = "virtio"
    mac: str | None = Field(default=None, pattern="^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


class DiskOut(BaseModel):
    size_gb: int
    backend: str
    pool: str


class NicOut(BaseModel):
    network: str
    model: str
    mac: str | None = None


class VMCreate(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
    vcpus: int = Field(default=1, ge=1, le=256)
    memory_mb: int = Field(default=1024, ge=128)
    firmware: Firmware = Firmware.BIOS
    disks: list[DiskIn] = Field(default_factory=lambda: [DiskIn(size_gb=20)])
    nics: list[NicIn] = Field(default_factory=lambda: [NicIn()])
    iso: str | None = None
    description: str = Field(default="", max_length=1024)

    def to_spec(self) -> VMSpec:
        return VMSpec(
            name=self.name,
            vcpus=self.vcpus,
            memory_mb=self.memory_mb,
            firmware=self.firmware,
            disks=[DiskSpec(**d.model_dump()) for d in self.disks],
            nics=[NicSpec(**n.model_dump()) for n in self.nics],
            iso=self.iso,
            description=self.description,
        )


class VMOut(BaseModel):
    uuid: str
    name: str
    state: VMState
    vcpus: int
    memory_mb: int
    firmware: Firmware
    disks: list[DiskOut]
    nics: list[NicOut]
    iso: str | None
    description: str

    @classmethod
    def from_info(cls, info: VMInfo) -> VMOut:
        spec = info.spec
        return cls(
            uuid=info.uuid,
            name=spec.name,
            state=info.state,
            vcpus=spec.vcpus,
            memory_mb=spec.memory_mb,
            firmware=spec.firmware,
            disks=[DiskOut(size_gb=d.size_gb, backend=d.backend, pool=d.pool) for d in spec.disks],
            nics=[NicOut(network=n.network, model=n.model, mac=n.mac) for n in spec.nics],
            iso=spec.iso,
            description=spec.description,
        )


class HostOut(BaseModel):
    hostname: str
    cpus: int
    memory_mb: int
    hypervisor: str

    @classmethod
    def from_info(cls, info: HostInfo) -> HostOut:
        return cls(**info.__dict__)


class StopRequest(BaseModel):
    force: bool = False
