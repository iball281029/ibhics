"""Deduplicating backup repository on top of a Store.

Layout:
  chunks/<aa>/<sha256>          zlib/zstd-compressed fixed-size pieces of disk images
  backups/<vm>/<stamp>.json     one manifest per backup: the VM's spec and its disks' chunk lists
  locks/<vm>@<stamp>            present while a backup is being written

Each backup is a complete image of the VM, but a chunk already in the repository is not stored
again, so after the first backup only changed data takes up space (incremental forever).
All-zero chunks are not stored at all. Removing a backup drops its manifest; garbage
collection then deletes chunks no manifest refers to.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from statistics import median
from uuid import uuid4

from ..hypervisor.base import NAME_RE, InvalidState, VMInfo
from .compression import codec, compress, decompress
from .store import DirStore, RepositoryBusy, Store

CHUNK_SIZE = 4 * 1024 * 1024
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
ID_RE = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9_.-]{0,62})@(\d{8}T\d{6}Z(?:-\d+)?)$")


class BackupNotFound(LookupError):
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
    kind: str = "full"
    source_id: str | None = None
    read_bytes: int = 0
    note: str = ""
    new_data_bytes: int = 0
    dedup_saved_bytes: int = 0
    compression: str = "legacy-zlib"
    verified: str = "skipped"
    verification: dict | None = None
    immutable_until: str | None = None
    lock_status: str = "none"
    suspicious: bool = False
    anomaly_reason: str = ""
    entity_type: str = "vm"


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


def _exclusive(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with self.store.exclusive():
            return method(self, *args, **kwargs)

    return run


class Repository:
    def __init__(
        self,
        store: Store,
        chunk_size: int = CHUNK_SIZE,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        compression: str = "balanced",
        immutability_days: int = 0,
    ) -> None:
        self.store = store
        self.chunk_size = chunk_size
        self._clock = clock
        self.compression = compression
        self.immutability_days = immutability_days
        codec(compression)

    @contextmanager
    def reading(self):
        """Keep GC away from source chunks while verifying, copying or restoring."""
        with self.store.exclusive():
            key = f"locks/operation-{uuid4().hex}"
            self.store.put(key, self._clock().isoformat().encode())
            try:
                yield
            finally:
                self.store.delete(key)

    def _chunk(self, manifest, digest):
        prefix = manifest.get("chunk_prefix", "chunks")
        data = self.store.get(f"{prefix}/{digest[:2]}/{digest}")
        block = decompress(data, manifest["chunk_size"])
        if len(block) > manifest["chunk_size"] or hashlib.sha256(block).hexdigest() != digest:
            raise RuntimeError(f"chunk {digest} is corrupt")
        return data, block

    def latest(self, vm_uuid):
        matches = [
            b
            for b in self.list()
            if b.vm_uuid == vm_uuid and b.kind in ("full", "incremental", "synthetic_full")
        ]
        return self.manifest(max(matches, key=lambda b: (b.created, b.id)).id) if matches else None

    @_exclusive
    def backup_readers(
        self, vm, readers, spec_json, capture, parent=None, progress=None, metrics=None
    ):
        """Publish a complete block map; only dirty chunks are read from an incremental source."""
        incremental = bool(capture.get("incremental"))
        if incremental and (
            not parent
            or len(readers) != len(parent["disks"])
            or parent["chunk_size"] != self.chunk_size
        ):
            raise ValueError("Incremental base geometry is incompatible")
        backup_id = self._new_id(vm.spec.name)
        stored = read_bytes = new_data = nonzero = 0
        disks = []
        total_bytes = sum(reader.size for reader in readers)
        scanned_bytes = 0

        def update(phase, step, disk=None):
            percent = (
                min(99.9, 100 * scanned_bytes / total_bytes)
                if total_bytes and not incremental
                else None
            )
            if progress:
                progress(step, percent)
            if metrics:
                metrics(
                    {
                        "phase": phase,
                        "step": step,
                        "percent": percent,
                        "mode": "incremental" if incremental else "full",
                        "read_bytes": read_bytes,
                        "scanned_bytes": scanned_bytes,
                        "stored_bytes": stored,
                        "total_bytes": total_bytes,
                        "disk": disk,
                        "disks": len(readers),
                    }
                )

        update("reading", "Reading point-in-time backup data")
        # Keep NBD reads on their owning thread. Only independent, atomic local
        # chunk writes run concurrently. At most 16 default-sized chunks buffer
        # 64 MiB of input, plus compressed output and per-worker codec workspace.
        workers = min(8, os.cpu_count() or 1)
        max_pending = workers * 2
        pending, submitted = deque(), set()

        def write_chunk(key, block):
            data = compress(block, self.compression)
            self.store.put(key, data)
            return len(block), len(data)

        def finish_one():
            nonlocal stored, new_data
            logical, physical = pending.popleft().result()
            new_data += logical
            stored += physical

        with self.reading(), ThreadPoolExecutor(max_workers=workers) as writers:
            for index, reader in enumerate(readers):
                count = (reader.size + self.chunk_size - 1) // self.chunk_size
                old = parent["disks"][index] if incremental else None
                if old and old["size_bytes"] != reader.size:
                    raise ValueError("Disk size changed; perform an active full backup")
                chunks = list(old["chunks"]) if old else [None] * count
                if len(chunks) != count:
                    raise ValueError("Invalid incremental base block map")
                # A synthetic full has its own chunk set. Promote its unchanged blocks
                # into the normal pool before publishing a new incremental map.
                if old and parent.get("chunk_prefix", "chunks") != "chunks":
                    for digest in set(d for d in chunks if d):
                        key = _chunk_key(digest)
                        if not self.store.exists(key):
                            _, block = self._chunk(parent, digest)
                            data = compress(block, self.compression)
                            self.store.put(key, data)
                            stored += len(data)
                            new_data += len(block)
                            nonzero += len(block)
                for offset in reader.changed_chunks(self.chunk_size):
                    if len(pending) >= max_pending:
                        finish_one()
                    if offset < 0 or offset >= count:
                        raise ValueError("Changed block lies outside disk")
                    length = min(self.chunk_size, reader.size - offset * self.chunk_size)
                    update("reading", f"Reading disk {index + 1}/{len(readers)}", index + 1)
                    block = reader.read(offset * self.chunk_size, length)
                    if len(block) != length:
                        raise RuntimeError(
                            "Short read from backup source; restore point not published"
                        )
                    read_bytes += len(block)
                    has_data = bool(block.strip(b"\0"))
                    if has_data:
                        nonzero += len(block)
                    digest = hashlib.sha256(block).hexdigest() if has_data else None
                    if (
                        digest
                        and digest not in submitted
                        and not self.store.exists(_chunk_key(digest))
                    ):
                        update(
                            "writing",
                            f"Compressing and writing disk {index + 1}/{len(readers)}",
                            index + 1,
                        )
                        if isinstance(self.store, DirStore):
                            pending.append(writers.submit(write_chunk, _chunk_key(digest), block))
                        else:
                            logical, physical = write_chunk(_chunk_key(digest), block)
                            new_data += logical
                            stored += physical
                        submitted.add(digest)
                    chunks[offset] = digest
                    scanned_bytes = sum(r.size for r in readers[:index]) + min(
                        reader.size, (offset + 1) * self.chunk_size
                    )
                    update("reading", f"Reading disk {index + 1}/{len(readers)}", index + 1)
                scanned_bytes = sum(r.size for r in readers[: index + 1])
                update("reading", f"Scanned disk {index + 1}/{len(readers)}", index + 1)
                disks.append({"size_bytes": reader.size, "chunks": chunks})
            while pending:
                update("writing", "Finishing compressed chunk writes")
                finish_one()
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
                "read_bytes": read_bytes,
                "kind": "incremental" if incremental else "full",
                "checkpoint": capture.get("checkpoint"),
                "disk_fingerprint": capture.get("disk_fingerprint"),
                "note": capture.get("note", ""),
            }
            manifest.update(
                new_data_bytes=new_data,
                dedup_saved_bytes=max(0, nonzero - new_data),
                compression=codec(self.compression),
            )
            update("committing", "Saving restore point manifest; confirming durable backup")
            self._publish(manifest)
        return _info(manifest)

    def transfer(self, backup_id, destination, kind="copy", independent=False, progress=None):
        """Verified, source-free synthesis or copy. Publish the manifest only after all chunks.

        Synthetic full writes an independent compressed chunk set. It can be restored
        even after every original backup and its shared chunks have been removed.
        """
        if kind not in ("copy", "archive", "synthetic_full"):
            raise ValueError("Unknown repository operation")
        with self.reading(), destination.reading():
            original = self.manifest(backup_id)
            manifest = copy.deepcopy(original)
            new_id = destination._new_id(original["vm_name"])
            prefix = f"fulls/{new_id}" if independent else "chunks"
            digests = sorted({d for disk in original["disks"] for d in disk["chunks"] if d})
            references = Counter(d for disk in original["disks"] for d in disk["chunks"] if d)
            stored = new_data = nonzero = 0
            for index, digest in enumerate(digests):
                data, block = self._chunk(original, digest)
                nonzero += len(block) * references[digest]
                key = f"{prefix}/{digest[:2]}/{digest}"
                if destination.store.exists(key):
                    # Do not trust an already present destination chunk without verifying.
                    destination._chunk(
                        {"chunk_prefix": prefix, "chunk_size": original["chunk_size"]}, digest
                    )
                else:
                    data = compress(block, destination.compression)
                    destination.store.put(key, data)
                    stored += len(data)
                    new_data += len(block)
                if progress:
                    progress(
                        "Verifying and writing backup chunks",
                        100 * (index + 1) / max(1, len(digests)),
                    )
            manifest.update(
                id=new_id,
                kind=kind,
                source_id=backup_id,
                format=2 if independent else 1,
                chunk_prefix=prefix,
                stored_bytes=stored,
                read_bytes=0,
                data_created=original.get("data_created", original["created"]),
                created=destination._clock().isoformat(),
            )
            manifest.update(
                new_data_bytes=new_data,
                dedup_saved_bytes=nonzero - new_data,
                compression=codec(destination.compression),
                note="Repository-side operation; no VM disk reads. Ratios describe this operation.",
            )
            # Copies are not a new checkpoint of the VM and must never become CBT bases.
            if kind != "synthetic_full":
                manifest.pop("checkpoint", None)
                manifest.pop("disk_fingerprint", None)
            destination._publish(manifest)
        return _info(manifest)

    def synthetic_full(self, backup_id, progress=None):
        return self.transfer(backup_id, self, "synthetic_full", True, progress)

    # Writing

    @_exclusive
    def backup(
        self,
        vm: VMInfo,
        disk_paths: list[Path],
        spec_json: dict,
        note="",
        progress=None,
        metrics=None,
    ) -> BackupInfo:
        if not NAME_RE.match(vm.spec.name):
            raise ValueError(f"cannot back up VM with name {vm.spec.name!r}")
        backup_id = self._new_id(vm.spec.name)
        lock_key = f"locks/{backup_id}"
        self.store.put(lock_key, self._clock().isoformat().encode())
        try:
            disks, stored = [], 0
            self._new_data = self._nonzero = 0
            total = sum(Path(path).stat().st_size for path in disk_paths)
            completed = 0

            def update(read, added, phase="reading"):
                current = completed + read
                percent = min(99.9, current / total * 100) if total else None
                step = (
                    "Saving restore point manifest"
                    if phase == "committing"
                    else "Reading full backup into repository"
                )
                if progress:
                    progress(step, percent)
                if metrics:
                    metrics(
                        {
                            "phase": phase,
                            "step": step,
                            "percent": percent,
                            "mode": "full",
                            "read_bytes": current,
                            "scanned_bytes": current,
                            "stored_bytes": stored + added,
                            "total_bytes": total,
                        }
                    )

            for path in disk_paths:
                chunks, size, added = self._write_disk(Path(path), update)
                completed += size
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
                "kind": "full",
                "read_bytes": sum(d["size_bytes"] for d in disks),
                "note": note,
                "new_data_bytes": self._new_data,
                "dedup_saved_bytes": self._nonzero - self._new_data,
                "compression": codec(self.compression),
            }
            # The manifest goes last: a backup that dies halfway leaves only unreferenced
            # chunks, which the next garbage collection removes.
            update(0, 0, "committing")
            self._publish(manifest)
        finally:
            self.store.delete(lock_key)
        return _info(manifest)

    def is_locked(self, manifest):
        until = manifest.get("immutable_until")
        return bool(until and datetime.fromisoformat(until) > self._clock())

    def info(self, backup_id):
        manifest = self.manifest(backup_id)
        info = _info(manifest)
        key = f"evidence/{backup_id}/result.json"
        if self.store.exists(key):
            info.verification = json.loads(self.store.get(key))
            info.verified = info.verification.get("verified", "skipped")
        key = f"protection/{backup_id}.json"
        if self.store.exists(key):
            info.lock_status = json.loads(self.store.get(key))["status"]
        return info

    def _publish(self, manifest):
        # Derivative copies do not inherit a source's expired lock or boot evidence.
        manifest.pop("immutable_until", None)
        manifest.pop("lock_status", None)
        history = [
            b
            for b in self.list()
            if b.vm_uuid == manifest["vm_uuid"] and b.kind in ("full", "incremental")
        ]
        history.sort(key=lambda b: (b.created, b.id))
        samples = [b.new_data_bytes for b in history[-14:]]
        changed = manifest.get("new_data_bytes", 0)
        logical = sum(d["size_bytes"] for d in manifest["disks"])
        suspicious = bool(
            len(samples) >= 3 and changed > 3 * median(samples) and changed > logical * 0.2
        )
        if manifest["kind"] in ("full", "incremental"):
            manifest["suspicious"] = suspicious
            manifest["anomaly_reason"] = (
                "New unique data exceeds 3× the median of previous runs and 20% of disk size"
                if suspicious
                else ""
            )
        until = self._clock() + timedelta(days=self.immutability_days)
        if self.immutability_days:
            manifest["immutable_until"] = until.isoformat()
            manifest["lock_status"] = "soft"
        key = _manifest_key(manifest["id"])
        self.store.put(key, json.dumps(manifest).encode())
        if self.immutability_days:
            prefix = manifest.get("chunk_prefix", "chunks")
            keys = {
                f"{prefix}/{d[:2]}/{d}" for disk in manifest["disks"] for d in disk["chunks"] if d
            }
            results = [self.store.protect(k, until) for k in sorted(keys)]
            results.append(self.store.protect(key, until))
            status = "native" if all(results) else "soft"
            self.store.put(
                f"protection/{manifest['id']}.json", json.dumps({"status": status}).encode()
            )
            manifest["lock_status"] = status

    def _write_disk(self, path: Path, progress=None) -> tuple[list[str | None], int, int]:
        chunks: list[str | None] = []
        size = added = 0
        with open(path, "rb") as f:
            while block := f.read(self.chunk_size):
                if progress:
                    progress(size, added)
                size += len(block)
                if not block.strip(b"\0"):
                    chunks.append(None)
                    continue
                digest = hashlib.sha256(block).hexdigest()
                self._nonzero += len(block)
                key = _chunk_key(digest)
                if not self.store.exists(key):
                    data = compress(block, self.compression)
                    self._new_data += len(block)
                    if progress:
                        progress(size, added, "writing")
                    self.store.put(key, data)
                    added += len(data)
                chunks.append(digest)
        if progress:
            progress(size, added)
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
                result.append(self.info(json.loads(self.store.get(key))["id"]))
        return sorted(result, key=lambda b: (b.vm_name, b.created))

    def manifest(self, backup_id: str) -> dict:
        try:
            return json.loads(self.store.get(_manifest_key(backup_id)))
        except KeyError:
            raise BackupNotFound(backup_id) from None

    @_exclusive
    def restore_disk(self, manifest: dict, index: int, out: Path) -> None:
        disk = manifest["disks"][index]
        chunk_size = manifest["chunk_size"]
        with open(out, "wb") as f:
            for digest in disk["chunks"]:
                if digest is None:
                    f.seek(chunk_size, 1)  # leave a hole; the file stays sparse
                    continue
                _, block = self._chunk(manifest, digest)
                f.write(block)
            f.truncate(disk["size_bytes"])

    # Removing

    @_exclusive
    def delete(self, backup_id: str) -> None:
        key = _manifest_key(backup_id)
        if not self.store.exists(key):
            raise BackupNotFound(backup_id)
        manifest = self.manifest(backup_id)
        if self.is_locked(manifest):
            raise InvalidState(f"Restore point is immutable until {manifest['immutable_until']}")
        self.store.release(key)
        self.store.delete(key)

    @_exclusive
    def prune(self, vm_name: str, keep_last: int, weekly=0, monthly=0, yearly=0) -> list[str]:
        """Keep the newest keep_last backups of a VM; return the ids removed."""
        if keep_last < 1:
            raise ValueError("keep_last must be at least 1")
        from .retention import retained_ids

        backups = self.list(vm_name)
        keep = retained_ids(
            [self.manifest(b.id) for b in backups], keep_last, weekly, monthly, yearly
        )
        removed = [
            b.id for b in backups if b.id not in keep and not self.is_locked(self.manifest(b.id))
        ]
        for backup_id in removed:
            self.delete(backup_id)
        return removed

    @_exclusive
    def gc(self) -> int:
        """Delete chunks that no backup uses. Refuses while another backup is being written."""
        # Elapsed time does not prove a writer is dead: large backups can run for days.
        # Crash leftovers require explicit operator review before removing their locks.
        if self.store.list("locks/"):
            raise RepositoryBusy("a repository operation or uncleared lock exists; try again later")
        used = set()
        for key in self.store.list("backups/"):
            if key.endswith(".json"):
                manifest = json.loads(self.store.get(key))
                prefix = manifest.get("chunk_prefix", "chunks")
                for disk in manifest["disks"]:
                    used.update(f"{prefix}/{d[:2]}/{d}" for d in disk["chunks"] if d)
        removed = 0
        for key in self.store.list("chunks/") + self.store.list("fulls/"):
            if key not in used:
                self.store.release(key)
                self.store.delete(key)
                removed += 1
        return removed


def _info(manifest: dict) -> BackupInfo:
    return BackupInfo(
        id=manifest["id"],
        vm_name=manifest["vm_name"],
        vm_uuid=manifest["vm_uuid"],
        entity_type=manifest.get("spec", {}).get("entity_type", "vm"),
        created=manifest["created"],
        size_bytes=sum(d["size_bytes"] for d in manifest["disks"]),
        stored_bytes=manifest["stored_bytes"],
        disks=len(manifest["disks"]),
        kind=manifest.get("kind", "full"),
        source_id=manifest.get("source_id"),
        read_bytes=manifest.get("read_bytes", 0),
        note=manifest.get("note", ""),
        new_data_bytes=manifest.get("new_data_bytes", 0),
        dedup_saved_bytes=manifest.get("dedup_saved_bytes", 0),
        compression=manifest.get("compression", "legacy-zlib"),
        immutable_until=manifest.get("immutable_until"),
        lock_status=manifest.get("lock_status", "none"),
        suspicious=manifest.get("suspicious", False),
        anomaly_reason=manifest.get("anomaly_reason", ""),
    )
