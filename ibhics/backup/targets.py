"""Where backups are kept: a directory on the node, a NAS, a SAN LUN, a USB disk or S3."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from enum import StrEnum

from ..hypervisor.base import DEV_RE, HOST_RE, IQN_RE, NAME_RE


class TargetType(StrEnum):
    LOCAL = "local"  # a directory on each node (/var/lib/ibhics/backup/<name> by default)
    NFS = "nfs"
    SMB = "smb"  # Windows share or NAS over SMB/CIFS
    USB = "usb"  # removable disk, mounted for each job and unmounted after
    ISCSI = "iscsi"  # a LUN on a SAN or NAS: log in, mount for the job, log out
    FC = "fc"  # a Fibre Channel (multipath) LUN or any other block device
    S3 = "s3"  # AWS S3, MinIO, Ceph RGW or any S3-compatible service


# A LUN or USB disk carries an ordinary filesystem: only one node may mount it at a time.
EXCLUSIVE_TYPES = (TargetType.USB, TargetType.ISCSI, TargetType.FC)


SHARE_RE = re.compile(r"^[A-Za-z0-9_.$ -]{1,80}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9_./-]*$")
SECRET_FIELDS = ("password", "secret_key")
LOCAL_ROOT = "/var/lib/ibhics/backup"


@dataclass
class BackupTarget:
    name: str
    type: TargetType
    path: str | None = None  # local: directory
    host: str | None = None  # nfs / smb server, iSCSI portal
    export: str | None = None  # nfs export path
    share: str | None = None  # smb share name
    username: str | None = None  # smb
    password: str | None = None  # smb
    domain: str | None = None  # smb
    port: int = 3260  # iSCSI portal port
    iqn: str | None = None  # iSCSI target
    lun: int = 0
    device: str | None = None  # usb: /dev/disk/by-label/...; fc: /dev/mapper/mpathb
    # iSCSI/FC: a LUN with no filesystem is formatted (XFS) only when this is set.
    confirm_wipe: bool = False
    endpoint: str | None = None  # s3: https://s3.example.com
    bucket: str | None = None
    region: str = "us-east-1"
    access_key: str | None = None
    secret_key: str | None = None
    prefix: str = "ibhics"  # nfs/smb/usb/iscsi/fc: subdirectory; s3: key prefix

    @property
    def shared(self) -> bool:
        """True when every node sees the same backups. Local directories and USB disks
        belong to one node."""
        return self.type not in (TargetType.LOCAL, TargetType.USB)

    @property
    def on_demand(self) -> bool:
        """True when the target is attached only while a job runs (isolated otherwise)."""
        return self.type not in (TargetType.LOCAL, TargetType.S3)

    @property
    def exclusive(self) -> bool:
        """True when jobs on this target must not overlap across nodes."""
        return self.type in EXCLUSIVE_TYPES

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ValueError("target name must use only letters, digits, '_', '.', '-'")
        if not PREFIX_RE.match(self.prefix) or ".." in self.prefix.split("/"):
            raise ValueError("prefix may use letters, digits, '_', '.', '-', '/' and no '..'")
        if self.type == TargetType.LOCAL:
            if self.path is not None and (not self.path.startswith("/") or self.path == "/"):
                raise ValueError("local target path must be an absolute directory other than /")
        elif self.type == TargetType.NFS:
            self._need_host()
            if not self.export or not self.export.startswith("/"):
                raise ValueError("nfs target needs an absolute export path")
        elif self.type == TargetType.SMB:
            self._need_host()
            if not self.share or not SHARE_RE.match(self.share):
                raise ValueError("smb target needs a share name")
            for value in (self.username, self.password, self.domain):
                if value and any(c in value for c in ",\n\r"):
                    raise ValueError("smb credentials must not contain commas or newlines")
        elif self.type in (TargetType.USB, TargetType.FC):
            if not self.device or not DEV_RE.match(self.device):
                raise ValueError(f"{self.type} target needs a device path under /dev/")
        elif self.type == TargetType.ISCSI:
            self._need_host()
            if not self.iqn or not IQN_RE.match(self.iqn):
                raise ValueError("iscsi target needs a target IQN (iqn.YYYY-MM.... or eui....)")
            if self.lun < 0 or not 0 < self.port < 65536:
                raise ValueError("invalid iSCSI LUN or port")
        elif self.type == TargetType.S3:
            if not self.endpoint or not re.match(
                r"^https?://[A-Za-z0-9_.:\[\]-]+/?$", self.endpoint
            ):
                raise ValueError("s3 target needs an endpoint URL such as https://s3.example.com")
            if not self.bucket or not BUCKET_RE.match(self.bucket):
                raise ValueError("s3 target needs a valid bucket name")
            if not self.access_key or not self.secret_key:
                raise ValueError("s3 target needs an access key and a secret key")

    def _need_host(self) -> None:
        if not self.host or not HOST_RE.match(self.host):
            raise ValueError(f"{self.type} target needs a server host")

    def to_dict(self) -> dict:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["type"] = str(self.type)
        return data

    def public_dict(self) -> dict:
        """What the API shows: secrets replaced by whether they are set."""
        data = self.to_dict()
        for key in SECRET_FIELDS:
            data[key] = "********" if data[key] else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> BackupTarget:
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        values["type"] = TargetType(values["type"])
        return cls(**values)

    def describe(self) -> str:
        return {
            TargetType.LOCAL: self.path or f"{LOCAL_ROOT}/{self.name}",
            TargetType.NFS: f"{self.host}:{self.export}",
            TargetType.SMB: f"//{self.host}/{self.share}",
            TargetType.USB: self.device or "",
            TargetType.ISCSI: f"{self.host}:{self.port} {self.iqn} lun {self.lun}",
            TargetType.FC: self.device or "",
            TargetType.S3: f"{self.endpoint} {self.bucket}",
        }[self.type]
