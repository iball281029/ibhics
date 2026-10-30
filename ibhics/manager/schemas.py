"""Request and response models for the ibHiCS REST API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..backup import BackupInfo, BackupTarget, TargetType
from ..cluster.topology import NodeRole, Topology
from ..hypervisor import (
    DiskSpec,
    Firmware,
    HostInfo,
    NicSpec,
    PoolInfo,
    PoolSpec,
    PoolType,
    VMInfo,
    VMSpec,
    VMState,
)


class DiskIn(BaseModel):
    size_gb: int = Field(ge=1, le=65536)
    backend: str = Field(default="file", pattern="^(file|block)$")
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
    os_type: str = Field(default="", max_length=64)
    # Manager only: which compute node to place the VM on. Empty picks automatically.
    node: str | None = None

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
            os_type=self.os_type,
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
    os_type: str = ""
    node: str | None = None

    @classmethod
    def from_info(cls, info: VMInfo, node: str | None = None) -> VMOut:
        spec = info.spec
        return cls(
            node=node,
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
            os_type=spec.os_type,
        )


class GuestInterfaceOut(BaseModel):
    name: str
    mac: str
    ips: list[str]


class GuestOut(BaseModel):
    uuid: str = ""
    agent: bool
    os_name: str
    os_version: str
    kernel: str
    hostname: str
    interfaces: list[GuestInterfaceOut]
    ips: list[str]


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


class MigrateRequest(BaseModel):
    node: str | None = Field(default=None, description="destination; omit to pick one")
    copy_storage: bool | None = Field(
        default=None,
        description="copy disks that are not on shared storage; omit to decide automatically",
    )
    bandwidth_mib: int | None = Field(default=None, ge=1, description="limit in MiB/s")


class MigrationOut(BaseModel):
    id: str
    vm: str
    uuid: str
    source: str
    target: str | None
    copy_storage: bool
    live: bool
    state: str
    created: str
    finished: str | None
    error: str | None


class AgentMigrate(BaseModel):
    dest_uri: str = Field(pattern=r"^qemu\+(tls|ssh|tcp)://[^\s/]+/system$")
    copy_storage: bool = False
    bandwidth_mib: int | None = Field(default=None, ge=1)


class NodeJoin(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
    role: NodeRole
    address: str = Field(pattern=r"^https?://", description="Agent URL, e.g. http://10.0.0.12:8007")


class MemberOut(BaseModel):
    name: str
    role: NodeRole
    address: str | None
    local: bool
    healthy: bool
    last_error: str | None


class ClusterOut(BaseModel):
    topology: Topology
    quorum: bool
    ha_capable: bool
    members: list[MemberOut]


class PoolCreate(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
    type: PoolType
    path: str | None = Field(default=None, description="dir: image directory")
    host: str | None = Field(default=None, description="nfs server or iSCSI portal")
    port: int = Field(default=3260, ge=1, le=65535)
    export: str | None = Field(default=None, description="nfs export path")
    iqn: str | None = None
    lun: int = Field(default=0, ge=0)
    device: str | None = Field(default=None, description="lvm: e.g. /dev/mapper/mpatha")
    confirm_wipe: bool = Field(
        default=False, description="required for iscsi and lvm: the device is erased"
    )

    def to_spec(self) -> PoolSpec:
        return PoolSpec(**self.model_dump())


class LocalDiskOut(BaseModel):
    path: str
    size_gb: float
    model: str
    serial: str
    transport: str
    empty: bool
    contents: str


class PoolOut(BaseModel):
    name: str
    type: PoolType
    active: bool
    capacity_gb: float
    allocation_gb: float
    available_gb: float
    source: str
    node: str | None = None

    @classmethod
    def from_info(cls, info: PoolInfo, node: str | None = None) -> PoolOut:
        return cls(node=node, **info.__dict__)


NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$"


class BackupTargetIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    type: TargetType
    path: str | None = Field(default=None, description="local: backup directory on each node")
    host: str | None = Field(default=None, description="nfs or smb server")
    export: str | None = Field(default=None, description="nfs export path")
    share: str | None = Field(default=None, description="smb share name")
    username: str | None = None
    password: str | None = None
    domain: str | None = None
    port: int = Field(default=3260, ge=1, le=65535, description="iscsi portal port")
    iqn: str | None = Field(default=None, description="iscsi target IQN")
    lun: int = Field(default=0, ge=0)
    device: str | None = Field(
        default=None, description="usb: /dev/disk/by-label/BACKUP; fc: /dev/mapper/mpathb"
    )
    confirm_wipe: bool = Field(
        default=False, description="iscsi/fc: format the LUN if it has no filesystem"
    )
    endpoint: str | None = Field(default=None, description="s3: e.g. https://minio.local:9000")
    bucket: str | None = None
    region: str = "us-east-1"
    access_key: str | None = None
    secret_key: str | None = None
    prefix: str = "ibhics"

    def to_target(self) -> BackupTarget:
        return BackupTarget(**self.model_dump())


TARGET_FIELDS = {
    TargetType.LOCAL: ("path",),
    TargetType.NFS: ("host", "export", "prefix"),
    TargetType.SMB: ("host", "share", "username", "password", "domain", "prefix"),
    TargetType.USB: ("device", "prefix"),
    TargetType.ISCSI: ("host", "port", "iqn", "lun", "confirm_wipe", "prefix"),
    TargetType.FC: ("device", "confirm_wipe", "prefix"),
    TargetType.S3: ("endpoint", "bucket", "region", "access_key", "secret_key", "prefix"),
}


class BackupTargetOut(BaseModel):
    name: str
    type: TargetType
    source: str
    shared: bool
    on_demand: bool  # attached only while a job runs, detached (isolated) otherwise
    settings: dict  # the target's fields, secrets masked

    @classmethod
    def from_target(cls, target: BackupTarget) -> BackupTargetOut:
        fields = TARGET_FIELDS[target.type]
        settings = {k: v for k, v in target.public_dict().items() if k in fields and v}
        return cls(
            name=target.name,
            type=target.type,
            source=target.describe(),
            shared=target.shared,
            on_demand=target.on_demand,
            settings=settings,
        )


class BackupOut(BaseModel):
    id: str
    vm_name: str
    vm_uuid: str
    created: str
    size_bytes: int
    stored_bytes: int
    disks: int
    node: str | None = None  # set for local targets: the node that holds the backup

    @classmethod
    def from_info(cls, info: BackupInfo, node: str | None = None) -> BackupOut:
        return cls(node=node, **info.__dict__)


class BackupRequest(BaseModel):
    target: str
    keep_last: int | None = Field(default=None, ge=1, description="prune older backups after")


class RestoreRequest(BaseModel):
    name: str | None = Field(default=None, pattern=NAME_PATTERN, description="default: original")
    node: str | None = Field(default=None, description="shared targets: where to restore")
    pool: str | None = Field(default=None, description="put every disk in this pool")
    source_node: str | None = Field(default=None, description="local targets: node holding it")


class PolicyIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    target: str
    vms: list[str] = Field(default_factory=list, description="VM names; empty means every VM")
    at: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="daily")
    every_hours: int | None = Field(default=None, ge=1, le=168)
    keep_last: int = Field(default=7, ge=1, le=1000)
    enabled: bool = True


class PolicyOut(PolicyIn):
    last_run: str | None = None


class JobOut(BaseModel):
    id: str
    kind: str
    state: str
    target: str
    vm: str
    node: str | None
    policy: str | None
    created: str
    finished: str | None
    result: str | None
    error: str | None


# Agent-only request bodies: the manager sends the whole target, credentials included.


class AgentBackupRun(BaseModel):
    uuid: str
    target: dict


class AgentBackupList(BaseModel):
    target: dict
    vm_name: str | None = None


class AgentRestore(BaseModel):
    target: dict
    backup_id: str
    name: str | None = Field(default=None, pattern=NAME_PATTERN)
    pool: str | None = None


class AgentBackupDelete(BaseModel):
    target: dict
    backup_id: str


class AgentPrune(BaseModel):
    target: dict
    vm_name: str
    keep_last: int = Field(ge=1)


# Sign-in and users


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class UserOut(BaseModel):
    name: str
    role: str
    enabled: bool
    must_change_password: bool
    created: str
    last_login: str | None

    @classmethod
    def from_user(cls, user) -> UserOut:
        return cls(
            name=user.name,
            role=str(user.role),
            enabled=user.enabled,
            must_change_password=user.must_change_password,
            created=user.created,
            last_login=user.last_login,
        )


class LoginOut(BaseModel):
    token: str  # for scripts: send as "Authorization: Bearer <token>"; browsers use the cookie
    idle_timeout: int
    user: UserOut


class UserIn(BaseModel):
    name: str = Field(max_length=32)
    role: str = Field(pattern=r"^(viewer|operator|admin)$")
    password: str = Field(max_length=256)


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern=r"^(viewer|operator|admin)$")
    enabled: bool | None = None
    password: str | None = Field(default=None, max_length=256)


class AuditOut(BaseModel):
    time: str
    user: str
    action: str
    target: str
    result: int
    address: str


# ibHiCS Migrate


class SourceIn(BaseModel):
    name: str
    type: str = Field(pattern=r"^(vmware|ova|disk)$")
    host: str | None = None
    username: str | None = None
    password: str | None = Field(default=None, max_length=256)
    path: str | None = None
    verify_tls: bool = False

    def to_source(self):
        from ..importer import Source, SourceType

        return Source(**{**self.model_dump(), "type": SourceType(self.type)})


class SourceOut(BaseModel):
    name: str
    type: str
    settings: dict  # password masked


class SourceVMOut(BaseModel):
    ref: str
    name: str
    vcpus: int
    memory_mb: int
    firmware: str
    disks_gb: list[int]
    nics: int
    state: str
    guest_os: str
    notes: str = ""


class ImportRequest(BaseModel):
    source: str
    vm: str = Field(description="the ref from GET /migrate/sources/{name}/vms")
    name: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
    node: str | None = None
    pool: str = "default"
    network: str = "default"
    vcpus: int | None = Field(default=None, ge=1, le=256)
    memory_mb: int | None = Field(default=None, ge=128)
    start: bool = False


class ImportJobOut(BaseModel):
    id: str
    source: str
    vm: str
    name: str
    node: str
    state: str
    created: str
    finished: str | None
    result: str | None
    error: str | None


class AgentImport(BaseModel):
    source: dict
    vm: dict
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
    pool: str = "default"
    network: str = "default"
    vcpus: int | None = None
    memory_mb: int | None = None
    start: bool = False
