"""Provider contracts; capabilities describe implemented transports, never guessed CBT."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..importer.sources import Source, SourceType


@dataclass(frozen=True)
class Disk:
    key: str
    size: int


@dataclass(frozen=True)
class Point:
    token: str
    disks: tuple[Disk, ...]


class ChangeProvider(ABC):
    """All reads belong to begin's immutable point. end must tolerate partial begin.

    Ranges, when supplied, must include every changed byte since prev_token, keyed
    by disk. None means unknown: hash every chunk. Tokens advance only at commit.
    """

    @abstractmethod
    def begin(self) -> Point: ...

    def list_changed(self, prev_token: str | None) -> dict[str, list[tuple[int, int]]] | None:
        return None

    def hash(self, disk: Disk, offset: int, length: int) -> str | None:
        """Optional SHA256 at the source; None uses streamed local comparison."""
        return None

    @abstractmethod
    def read(self, disk: Disk, offset: int, length: int) -> bytes: ...

    @abstractmethod
    def end(self) -> None: ...


def capability(source: Source) -> dict:
    kind = source.type
    supported = kind in (SourceType.KVM, SourceType.VMWARE)
    reason = {
        SourceType.KVM: "SSH: file-backed qcow2 internal snapshots only; block storage pending",
        SourceType.VMWARE: "ESXi SSH: base flat VMDK only; pre-existing snapshots refused",
        SourceType.P2V: "Live P2V delta is not yet supported by the agent",
        SourceType.NUTANIX: "Full re-sync required; consistent snapshot provider not yet supported",
        SourceType.SANGFOR: "Full re-sync required; consistent live export not yet supported",
    }.get(kind, "Replication is not supported for static OVA, disk or upload sources")
    if kind == SourceType.VMWARE and (source.transport != "ssh" or source.path):
        supported = False
        reason = "Replication requires a direct ESXi source with SSH transport"
    return {
        "supported": supported,
        "change_tracking": "hash-delta" if kind in (SourceType.KVM, SourceType.VMWARE) else "full",
        "live": supported,
        "reason": reason,
    }
