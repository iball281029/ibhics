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

    tracking = "hash-delta"
    fallback_reason: str | None = None

    def prepare(self, prev_token: str | None) -> None:
        """Receive the committed baseline under the replica operation lock."""
        return None

    def finalize(self, committed: bool) -> None:
        """Release tracking metadata after the manifest commit decision is known."""
        return None

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
    supported = kind in (SourceType.KVM, SourceType.VMWARE, SourceType.NUTANIX)
    reason = {
        SourceType.KVM: (
            "Libvirt NBD pull: dirty-bitmap with checkpoints; otherwise full hash scan. "
            "raw/LVM/ZFS/RBD via full pull when supported by the host. "
            "Proxmox without libvirt: file-backed qcow2 internal snapshots only"
        ),
        SourceType.VMWARE: (
            "Direct ESXi: CBT when API and disk changeIds are ready, otherwise hash-delta; "
            "base flat VMDK only; pre-existing snapshots refused"
        ),
        SourceType.P2V: (
            "Agent --replicate push delta is available via P2V sessions; no scheduled pull"
        ),
        SourceType.NUTANIX: (
            "Prism v3 VM snapshot and stopped isolated clone export: full-diff. "
            "Requires v3 vm_snapshots and snapshot parent cloning on the source; "
            "CRT needs a retained reference snapshot, which per-round cleanup removes"
        ),
        SourceType.SANGFOR: "Existing Sangfor client exports OVA but provides no snapshot API",
    }.get(kind, "Replication is not supported for static OVA, disk or upload sources")
    if kind == SourceType.VMWARE and (source.transport != "ssh" or source.path):
        supported = False
        reason = "Replication requires a direct ESXi source with SSH transport"
    return {
        "supported": supported,
        "change_tracking": "hash-delta"
        if kind in (SourceType.KVM, SourceType.VMWARE)
        else "full-diff"
        if kind == SourceType.NUTANIX
        else "full",
        "live": supported,
        "reason": reason,
    }
