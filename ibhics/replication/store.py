"""Raw replicas with a single durable manifest commit across all VM disks.

Each cycle prepares independent sparse files beside each disk's pool. Publishing the
manifest is the only commit point. Failed cycles cannot mutate the prior generation
or its hashes. Space usage can temporarily be twice the VM's allocated disk space.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

from .providers import ChangeProvider

CHUNK = 4 << 20


def durable_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sparse_copy(source: Path, target: Path, chunk: int = CHUNK) -> None:
    """Independent copy, including zero holes; never hardlink a writable replica."""
    # A reflink keeps generation creation cheap on XFS/btrfs; sparse copy is universal.
    try:
        result = subprocess.run(
            ["cp", "--reflink=always", "--sparse=always", "--", str(source), str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            os.chmod(target, 0o600)
            with target.open("rb") as cloned:
                os.fsync(cloned.fileno())
            return
        target.unlink(missing_ok=True)
    except OSError:
        target.unlink(missing_ok=True)
    with source.open("rb") as src, target.open("xb") as dst:
        os.chmod(target, 0o600)
        while data := src.read(chunk):
            if any(data):
                dst.write(data)
            else:
                dst.seek(len(data), 1)
        dst.truncate(src.tell())
        dst.flush()
        os.fsync(dst.fileno())


def clear_range(out, offset: int, length: int) -> None:
    """Punch stale nonzero data out of a cloned generation; zero write fallback."""
    import ctypes

    out.flush()
    libc = ctypes.CDLL(None, use_errno=True)
    fallocate = getattr(libc, "fallocate", None)
    if fallocate is not None:
        fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
        if fallocate(out.fileno(), 3, offset, length) == 0:  # KEEP_SIZE | PUNCH_HOLE
            return
    out.seek(offset)
    out.write(bytes(length))


@contextmanager
def operation_lock(path: Path, *, shared: bool = False):
    import fcntl

    from ..hypervisor import InvalidState

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InvalidState("Replication or application upgrade is already active") from None
        yield


class ReplicaStore:
    def __init__(self, root: Path, disk_roots: list[Path], chunk: int = CHUNK):
        if chunk < 1:
            raise ValueError("chunk must be positive")
        self.root, self.disk_roots, self.chunk = Path(root), list(map(Path, disk_roots)), chunk

    def current(self) -> dict:
        path = self.root / "current.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        # Pool mount points can differ on relay and destination nodes. A generation's
        # relative name, never a path from another node, identifies its disk files.
        for disk, root in zip(data.get("disks", []), self.disk_roots, strict=False):
            disk["path"] = str(root / f"{data['generation']}.raw")
        return data

    def sync(self, provider: ChangeProvider, progress=lambda done, total: None) -> dict:
        with operation_lock(self.disk_roots[0].parent / "replication.lock"):
            return self._sync(provider, progress)

    def _sync(self, provider, progress):
        previous = self.current()
        old_disks = previous.get("disks", [])
        generation = uuid.uuid4().hex
        prepared: list[Path] = []
        records = []
        transferred = changed = total = done = 0
        try:
            try:
                point = provider.begin()
                if not point.disks or len(point.disks) != len(self.disk_roots):
                    raise ValueError("source disk count differs from replica targets")
                if len({d.key for d in point.disks}) != len(point.disks):
                    raise ValueError("duplicate source disk identity")
                ranges = provider.list_changed(previous.get("token")) if previous else None
                total = sum(d.size for d in point.disks)
                for index, disk in enumerate(point.disks):
                    if disk.size < 1:
                        raise ValueError("source disk size must be known and positive")
                    parent = self.disk_roots[index]
                    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target = parent / f"{generation}.raw"
                    prepared.append(target)
                    old = old_disks[index] if index < len(old_disks) else {}
                    compatible = (
                        old.get("key") == disk.key
                        and old.get("size") == disk.size
                        and previous.get("chunk") == self.chunk
                    )
                    hints = ranges.get(disk.key) if ranges is not None and compatible else None
                    if hints is not None and any(
                        start < 0 or length <= 0 or start + length > disk.size
                        for start, length in hints
                    ):
                        raise ValueError("invalid provider change range")
                    hashes = []
                    old_file = open(old["path"], "rb") if compatible else None
                    try:
                        if compatible:
                            sparse_copy(Path(old["path"]), target, self.chunk)
                            fd = os.open(target, os.O_WRONLY)
                        else:
                            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        with os.fdopen(fd, "r+b") as out:
                            for n, offset in enumerate(range(0, disk.size, self.chunk)):
                                length = min(self.chunk, disk.size - offset)
                                prior = old.get("hashes", [])[n] if compatible else None
                                untouched = hints is not None and not any(
                                    start < offset + length and start + size > offset
                                    for start, size in hints
                                )
                                digest = prior if untouched else provider.hash(disk, offset, length)
                                zero_hash = hashlib.sha256(bytes(length)).hexdigest()
                                data = None
                                if digest is None or (digest != prior and digest != zero_hash):
                                    data = provider.read(disk, offset, length)
                                    transferred += len(data)
                                    if len(data) != length:
                                        raise ValueError("short source disk read")
                                    actual = hashlib.sha256(data).hexdigest()
                                    if digest is not None and actual != digest:
                                        raise ValueError("source point changed during read")
                                    digest = actual
                                if digest != prior:
                                    changed += length
                                if digest == prior:
                                    old_file.seek(offset)
                                    if hashlib.sha256(old_file.read(length)).hexdigest() != digest:
                                        raise ValueError("replica integrity check failed")
                                elif digest != zero_hash:
                                    out.seek(offset)
                                    out.write(data)
                                elif compatible:
                                    clear_range(out, offset, length)
                                hashes.append(digest)
                                done += length
                                progress(done, total)
                            out.truncate(disk.size)
                            out.flush()
                            os.fsync(out.fileno())
                        fsync_dir(parent)
                    finally:
                        if old_file:
                            old_file.close()
                    records.append(
                        {
                            "key": disk.key,
                            "size": disk.size,
                            "path": str(target),
                            "hashes": hashes,
                        }
                    )
            finally:
                # Cleanup is part of success, even if begin or a later disk failed.
                provider.end()
            manifest = {
                "generation": generation,
                "token": point.token,
                "chunk": self.chunk,
                "disks": records,
                "bytes_transferred": transferred,
                "changed_bytes": changed,
                "total_bytes": total,
                "change_tracking": provider.tracking,
                "fallback_reason": provider.fallback_reason,
            }
            durable_json(self.root / "current.json", manifest)
        except BaseException:
            # A manifest already published by replace is authoritative even if fsync failed.
            if self.current().get("generation") != generation:
                for path in prepared:
                    path.unlink(missing_ok=True)
            raise
        # Reclaim only files belonging to the superseded generation after commit.
        for old in old_disks:
            try:
                Path(old["path"]).unlink(missing_ok=True)
            except OSError:
                pass  # the committed generation is valid; stale local data can be reclaimed later

        return manifest

    def clone(self, folder: Path) -> list[Path]:
        with operation_lock(self.disk_roots[0].parent / "replication.lock"):
            return self._clone(folder)

    def _clone(self, folder):
        current = self.current()
        if not current:
            raise ValueError("replica has not synced yet")
        folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        result = []
        try:
            for index, disk in enumerate(current["disks"]):
                path = folder / f"disk-{index}.raw"
                sparse_copy(Path(disk["path"]), path, self.chunk)
                result.append(path)
            return result
        except BaseException:
            shutil.rmtree(folder)
            raise
