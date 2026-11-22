"""Immutable ESXi replica layers: read and write only CBT-reported chunks.

The original raw baseline stays untouched. New qcow2 layers refer to backing
images by relative name, so shared pools may mount at different node paths.
An invalid CBT baseline is an error, never a silent full scan or full copy.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from .store import ReplicaStore, durable_json, fsync_dir

MAX_DEPTH = 32


class CBTRequired(ValueError):
    def __init__(self):
        super().__init__("CBT unavailable or reset; baseline preserved, full scan not started")


class ReplicaMaintenanceRequired(ValueError):
    def __init__(self):
        super().__init__("Replica chain needs maintenance before the next incremental sync")


class CowReplicaStore(ReplicaStore):
    def _command(self, argv, parent):
        try:
            subprocess.run(argv, cwd=parent, capture_output=True, check=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Replica copy-on-write image operation failed") from None

    def _layer(self, old, parent, generation):
        if old.get("depth", 0) >= MAX_DEPTH:
            raise ReplicaMaintenanceRequired
        target = parent / f"{generation}.qcow2"
        if target.exists():
            raise ValueError("Replica layer already exists")
        self._command(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                old.get("format", "raw"),
                "-b",
                Path(old["path"]).name,
                target.name,
            ],
            parent,
        )
        os.chmod(target, 0o600)
        fsync_dir(parent)
        return target

    def _write(self, target, offset, data):
        # qemu-io's source is a local temporary pattern file, not shell input.
        # Its generated basename contains no spaces or command metacharacters.
        with tempfile.NamedTemporaryFile(prefix=".replica-write-", dir=target.parent) as patch:
            patch.write(data)
            patch.flush()
            command = f"write -q -s {Path(patch.name).name} {offset} {len(data)}"
            self._command(
                ["qemu-io", "-f", "qcow2", "-c", command, "-c", "flush", target.name],
                target.parent,
            )

    def _sync(self, provider, progress):
        previous = self.current()
        if not previous:
            # A first baseline must read the source once. Subsequent cycles never
            # enter the raw full-copy implementation.
            return super()._sync(provider, progress)
        generation = uuid.uuid4().hex
        prepared = []
        committed = False
        try:
            try:
                progress(0, 0)  # also observes an operator stop before snapshot work
                provider.prepare(previous.get("token"))
                point = provider.begin()
                old_disks = previous["disks"]
                if (
                    len(point.disks) != len(old_disks)
                    or len(old_disks) != len(self.disk_roots)
                    or previous.get("chunk") != self.chunk
                ):
                    raise ValueError("Replica layout changed; an explicit new baseline is required")
                ranges = provider.list_changed(previous.get("token"))
                if ranges is None or provider.tracking != "cbt":
                    raise CBTRequired
                plans = []
                for disk, old in zip(point.disks, old_disks, strict=True):
                    if disk.key != old["key"] or disk.size != old["size"] or disk.key not in ranges:
                        raise ValueError("Replica disk identity changed; new baseline required")
                    indices = set()
                    for start, length in ranges[disk.key]:
                        if (
                            type(start) is not int
                            or type(length) is not int
                            or start < 0
                            or length <= 0
                            or start + length > disk.size
                        ):
                            raise ValueError("Invalid CBT change range")
                        indices.update(
                            range(start // self.chunk, (start + length - 1) // self.chunk + 1)
                        )
                    plans.append(sorted(indices))
                total = sum(
                    min(self.chunk, disk.size - n * self.chunk)
                    for disk, indices in zip(point.disks, plans, strict=True)
                    for n in indices
                )
                progress(0, total)
                done = changed = transferred = 0
                records = []
                for disk, old, parent, indices in zip(
                    point.disks, old_disks, self.disk_roots, plans, strict=True
                ):
                    record = dict(old)
                    record["generation"] = old.get("generation", previous["generation"])
                    record["hashes"] = list(old["hashes"])
                    target = None
                    for n in indices:
                        progress(done, total)
                        offset = n * self.chunk
                        length = min(self.chunk, disk.size - offset)
                        data = provider.read(disk, offset, length)
                        if len(data) != length:
                            raise ValueError("Short source disk read")
                        digest = hashlib.sha256(data).hexdigest()
                        transferred += length
                        if digest != old["hashes"][n]:
                            if target is None:
                                # Track the candidate before creation so a partial
                                # qemu-img failure cannot leave an orphan layer.
                                target = parent / f"{generation}.qcow2"
                                prepared.append(target)
                                self._layer(old, parent, generation)
                                record.update(
                                    path=str(target),
                                    generation=generation,
                                    format="qcow2",
                                    depth=old.get("depth", 0) + 1,
                                )
                            self._write(target, offset, data)
                            record["hashes"][n] = digest
                            changed += length
                        done += length
                        progress(done, total)
                    if target is not None:
                        with target.open("rb") as image:
                            os.fsync(image.fileno())
                    records.append(record)
            finally:
                provider.end()
            manifest = {
                "generation": generation,
                "token": point.token,
                "chunk": self.chunk,
                "disks": records,
                "bytes_transferred": transferred,
                "changed_bytes": changed,
                "total_bytes": sum(d.size for d in point.disks),
                "source_read_bytes": transferred,
                "work_bytes": total,
                "storage": "qcow2-cow",
                "change_tracking": "cbt",
                "fallback_reason": None,
            }
            durable_json(self.root / "current.json", manifest)
            committed = True
            return manifest
        finally:
            # replace() may have published the manifest before a directory fsync error.
            committed = committed or self.current().get("generation") == generation
            if not committed:
                for path in prepared:
                    path.unlink(missing_ok=True)
            provider.finalize(committed)
