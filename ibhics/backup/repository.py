"""Deduplicating backup repository on top of a Store.

Layout:
  chunks/<aa>/<sha256>          zlib-compressed fixed-size pieces of disk images
  backups/<vm>/<stamp>.json     one manifest per backup: the VM's spec and its disks' chunk lists
  locks/<vm>@<stamp>            present while a backup is being written

Each backup is a complete image of the VM, but a chunk already in the repository is not stored
again, so after the first backup only changed data takes up space (incremental forever).
All-zero chunks are not stored at all. Removing a backup drops its manifest; garbage
collection then deletes chunks no manifest refers to.
"""

from __future__ import annotations

import hashlib
import json
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..hypervisor.base import NAME_RE, VMInfo
from .store import Store

CHUNK_SIZE = 4 * 1024 * 1024
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
ID_RE = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9_.-]{0,62})@(\d{8}T\d{6}Z(?:-\d+)?)$")
STALE_LOCK = timedelta(hours=12)


class BackupNotFound(LookupError):
    pass


class RepositoryBusy(RuntimeError):
    pass


@dataclass
class BackupInfo:
    id: str
    vm_name: str
    vm_uuid: str
    created: str  # ISO 8601, UTC
    size_bytes: int  # logical size of all disks
    stored_bytes: int  # new, compressed data this backup added to the repository
    disks: int


def parse_id(backup_id: str) -> tuple[str, str]:
    match = ID_RE.match(backup_id)
    if not match:
        raise BackupNotFound(backup_id)
    return match.group(1), match.group(2)


def _manifest_key(backup_id: str) -> str:
    vm, stamp = parse_id(backup_id)
    return f"backups/{vm}/{stamp}.json"


def _chunk_key(digest: str) -> str:
    return f"chunks/{digest[:2]}/{digest}"


class Repository:
    def __init__(
        self,
        store: Store,
        chunk_size: int = CHUNK_SIZE,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.chunk_size = chunk_size
        self._clock = clock

    # Writing

    def backup(self, vm: VMInfo, disk_paths: list[Path], spec_json: dict) -> BackupInfo:
        if not NAME_RE.match(vm.spec.name):
            raise ValueError(f"cannot back up VM with name {vm.spec.name!r}")
        backup_id = self._new_id(vm.spec.name)
        lock_key = f"locks/{backup_id}"
        self.store.put(lock_key, self._clock().isoformat().encode())
        try:
            disks, stored = [], 0
            for path in disk_paths:
                chunks, size, added = self._write_disk(Path(path))
                disks.append({"size_bytes": size, "chunks": chunks})
                stored += added
            manifest = {
                "format": 1,
                "id": backup_id,
                "vm_name": vm.spec.name,
                "vm_uuid": vm.uuid,
                "created": self._clock().isoformat(),
                "chunk_size": self.chunk_size,
                "stored_bytes": stored,
                "spec": spec_json,
                "disks": disks,
            }
            # The manifest goes last: a backup that dies halfway leaves only unreferenced
            # chunks, which the next garbage collection removes.
            self.store.put(_manifest_key(backup_id), json.dumps(manifest, indent=1).encode())
        finally:
            self.store.delete(lock_key)
        return _info(manifest)

    def _write_disk(self, path: Path) -> tuple[list[str | None], int, int]:
        chunks: list[str | None] = []
        size = added = 0
        with open(path, "rb") as f:
            while block := f.read(self.chunk_size):
                size += len(block)
                if not block.strip(b"\0"):
                    chunks.append(None)
                    continue
                digest = hashlib.sha256(block).hexdigest()
                key = _chunk_key(digest)
                if not self.store.exists(key):
                    data = zlib.compress(block, 1)
                    self.store.put(key, data)
                    added += len(data)
                chunks.append(digest)
        return chunks, size, added

    def _new_id(self, vm_name: str) -> str:
        stamp = self._clock().strftime(STAMP_FORMAT)
        existing = set(self.store.list(f"backups/{vm_name}/"))
        candidate, n = stamp, 1
        while f"backups/{vm_name}/{candidate}.json" in existing:
            n += 1
            candidate = f"{stamp}-{n}"
        return f"{vm_name}@{candidate}"

    # Reading

    def list(self, vm_name: str | None = None) -> list[BackupInfo]:
        prefix = f"backups/{vm_name}/" if vm_name else "backups/"
        result = []
        for key in self.store.list(prefix):
            if key.endswith(".json"):
                result.append(_info(json.loads(self.store.get(key))))
        return sorted(result, key=lambda b: (b.vm_name, b.created))

    def manifest(self, backup_id: str) -> dict:
        try:
            return json.loads(self.store.get(_manifest_key(backup_id)))
        except KeyError:
            raise BackupNotFound(backup_id) from None

    def restore_disk(self, manifest: dict, index: int, out: Path) -> None:
        disk = manifest["disks"][index]
        chunk_size = manifest["chunk_size"]
        with open(out, "wb") as f:
            for digest in disk["chunks"]:
                if digest is None:
                    f.seek(chunk_size, 1)  # leave a hole; the file stays sparse
                    continue
                block = zlib.decompress(self.store.get(_chunk_key(digest)))
                if hashlib.sha256(block).hexdigest() != digest:
                    raise RuntimeError(f"chunk {digest} is corrupt")
                f.write(block)
            f.truncate(disk["size_bytes"])

    # Removing

    def delete(self, backup_id: str) -> None:
        key = _manifest_key(backup_id)
        if not self.store.exists(key):
            raise BackupNotFound(backup_id)
        self.store.delete(key)

    def prune(self, vm_name: str, keep_last: int) -> list[str]:
        """Keep the newest keep_last backups of a VM; return the ids removed."""
        if keep_last < 1:
            raise ValueError("keep_last must be at least 1")
        backups = self.list(vm_name)
        removed = [b.id for b in backups[:-keep_last]]
        for backup_id in removed:
            self.delete(backup_id)
        return removed

    def gc(self) -> int:
        """Delete chunks that no backup uses. Refuses while another backup is being written."""
        now = self._clock()
        for key in self.store.list("locks/"):
            try:
                started = datetime.fromisoformat(self.store.get(key).decode())
            except (KeyError, ValueError):
                continue
            if now - started < STALE_LOCK:
                raise RepositoryBusy("a backup is in progress on this target; try again later")
        used = set()
        for key in self.store.list("backups/"):
            if key.endswith(".json"):
                for disk in json.loads(self.store.get(key))["disks"]:
                    used.update(d for d in disk["chunks"] if d)
        removed = 0
        for key in self.store.list("chunks/"):
            if key.rsplit("/", 1)[-1] not in used:
                self.store.delete(key)
                removed += 1
        return removed


def _info(manifest: dict) -> BackupInfo:
    return BackupInfo(
        id=manifest["id"],
        vm_name=manifest["vm_name"],
        vm_uuid=manifest["vm_uuid"],
        created=manifest["created"],
        size_bytes=sum(d["size_bytes"] for d in manifest["disks"]),
        stored_bytes=manifest["stored_bytes"],
        disks=len(manifest["disks"]),
    )
