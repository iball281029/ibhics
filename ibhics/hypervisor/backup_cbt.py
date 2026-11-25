"""Point-in-time readers over libvirt pull backup; dirty ranges come from QEMU, not holes.

Only running qcow2 guests support persistent checkpoints. Other cases fall back
to full export. Never infer changed blocks from sparse-file allocation.
"""

import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4


class CBTUnavailable(RuntimeError):
    pass


class NBDReader:
    def __init__(self, handle, incremental):
        self.handle = handle
        self.size = handle.get_size()
        self.incremental = incremental

    def read(self, offset, count):
        block = self.handle.pread(count, offset)
        if len(block) != count:
            raise RuntimeError("Short read from point-in-time backup")
        return block

    def changed_chunks(self, chunk_size):
        if not self.incremental:
            if hasattr(self.handle, "can_meta_context") and self.handle.can_meta_context(
                "base:allocation"
            ):
                yield from self._nonzero_chunks(chunk_size)
            else:
                yield from range((self.size + chunk_size - 1) // chunk_size)
            return
        context = "qemu:dirty-bitmap:changes"
        if not self.handle.can_meta_context(context):
            raise CBTUnavailable("Dirty bitmap metadata was not negotiated")
        offset, last = 0, -1
        while offset < self.size:
            ranges = []

            def extent(meta, start, entries, error, ranges=ranges):
                if meta == context:
                    if len(entries) % 2:
                        raise RuntimeError("Malformed dirty bitmap response")
                    for length, flags in zip(entries[::2], entries[1::2], strict=True):
                        if length <= 0 or flags & ~1:
                            raise RuntimeError("Invalid dirty bitmap extent")
                        ranges.append((start, length, flags))
                        start += length
                return 0

            self.handle.block_status(min(self.size - offset, 1 << 30), offset, extent)
            cursor = offset
            for start, length, dirty in ranges:
                if start != cursor:
                    raise RuntimeError("Dirty bitmap response contains a gap")
                end = min(start + length, self.size)
                if dirty:
                    for index in range(start // chunk_size, (end + chunk_size - 1) // chunk_size):
                        if index > last:
                            yield index
                            last = index
                cursor = end
            if cursor <= offset:
                raise CBTUnavailable("No dirty bitmap progress; refusing incomplete backup")
            offset = cursor

    def _nonzero_chunks(self, chunk_size):
        """Skip only NBD STATE_ZERO extents; a hole alone does not imply zero data."""
        offset, last = 0, -1
        while offset < self.size:
            ranges = []

            def extent(meta, start, entries, error, ranges=ranges):
                if meta == "base:allocation":
                    if len(entries) % 2:
                        raise RuntimeError("Malformed allocation metadata response")
                    for length, flags in zip(entries[::2], entries[1::2], strict=True):
                        if length <= 0:
                            raise RuntimeError("Invalid allocation metadata extent")
                        ranges.append((start, length, flags))
                        start += length
                return 0

            self.handle.block_status(min(self.size - offset, 1 << 30), offset, extent)
            cursor = offset
            for start, length, flags in ranges:
                if start != cursor:
                    raise RuntimeError("Allocation metadata response contains a gap")
                end = min(start + length, self.size)
                # NBD_STATE_ZERO is bit 1. Ignore all other flags, including HOLE.
                if not flags & 2:
                    for index in range(start // chunk_size, (end + chunk_size - 1) // chunk_size):
                        if index > last:
                            yield index
                            last = index
                cursor = end
            if cursor <= offset:
                raise RuntimeError("No allocation metadata progress")
            offset = cursor


def _sweep_scratch(base: Path, older_than: float = 3600) -> None:
    """Remove scratch folders left by earlier backups (best effort)."""
    import shutil
    import time

    for leftover in base.glob("cbt-*"):
        try:
            if time.time() - leftover.stat().st_mtime > older_than:
                shutil.rmtree(leftover, ignore_errors=True)
        except OSError:
            pass


@contextmanager
def capture(driver, uuid, parent=None):
    try:
        import nbd
    except ImportError:
        raise CBTUnavailable("Install python3-libnbd on compute nodes for CBT") from None
    dom = driver._lookup(uuid)
    disks = ET.fromstring(dom.XMLDesc(0)).findall("./devices/disk[@device='disk']")
    if (
        not dom.isActive()
        or not disks
        or any(d.find("driver") is None or d.find("driver").get("type") != "qcow2" for d in disks)
    ):
        raise CBTUnavailable("CBT requires a running VM with qcow2 disks")
    topology = [
        (d.find("target").get("dev"), driver._disk_path(d), driver._volume(d).info()[1])
        for d in disks
    ]
    fingerprint = hashlib.sha256(json.dumps(topology).encode()).hexdigest()
    checkpoint = None
    if parent and parent.get("disk_fingerprint") == fingerprint and parent.get("checkpoint"):
        try:
            dom.checkpointLookupByName(parent["checkpoint"], 0)
            checkpoint = parent["checkpoint"]
        except Exception:
            checkpoint = None
    new = "ibhics-" + uuid4().hex
    # The scratch files hold what the guest overwrites during the backup; where they
    # run out of room QEMU pauses the guest, so they go where the disks have room.
    staging = getattr(driver, "_backup_staging", None)
    base = staging(disks) if staging else Path(driver.scratch_dir)
    base.mkdir(parents=True, exist_ok=True)
    _sweep_scratch(base)
    handles, started = [], False
    # On NFS, QEMU may still hold a deleted scratch file for a moment after the job ends
    # (.nfs* silly-rename), so the folder cannot be removed yet. That must not fail a
    # finished backup; the next run sweeps the leftover folder.
    with TemporaryDirectory(prefix="cbt-", dir=base, ignore_cleanup_errors=True) as directory:
        from .libvirt_driver import _qemu_may_write

        _qemu_may_write(Path(directory))
        root = ET.Element("domainbackup", mode="pull")
        if checkpoint:
            ET.SubElement(root, "incremental").text = checkpoint
        socket = str(Path(directory) / "nbd.sock")
        ET.SubElement(root, "server", transport="unix", socket=socket)
        group = ET.SubElement(root, "disks")
        cp = ET.Element("domaincheckpoint")
        ET.SubElement(cp, "name").text = new
        cpdisks = ET.SubElement(cp, "disks")
        for index, (dev, _, _) in enumerate(topology):
            disk = ET.SubElement(
                group,
                "disk",
                name=dev,
                type="file",
                exportname=f"disk{index}",
                exportbitmap="changes",
            )
            ET.SubElement(disk, "scratch", file=str(Path(directory) / f"disk{index}.qcow2"))
            ET.SubElement(cpdisks, "disk", name=dev, checkpoint="bitmap")
        try:
            try:
                dom.backupBegin(
                    ET.tostring(root, encoding="unicode"), ET.tostring(cp, encoding="unicode")
                )
                started = True
            except Exception as exc:
                raise CBTUnavailable(f"Checkpoint backup unavailable: {exc}") from exc
            readers = []
            for index in range(len(disks)):
                handle = nbd.NBD()
                handles.append(handle)
                handle.set_export_name(f"disk{index}")
                if hasattr(handle, "add_meta_context"):
                    handle.add_meta_context("base:allocation")
                if checkpoint:
                    handle.add_meta_context("qemu:dirty-bitmap:changes")
                handle.connect_unix(socket)
                readers.append(NBDReader(handle, bool(checkpoint)))
            yield (
                readers,
                {
                    "checkpoint": new,
                    "socket": socket,
                    "disk_fingerprint": fingerprint,
                    "incremental": bool(checkpoint),
                },
            )
        finally:
            original_error = sys.exc_info()[1]
            cleanup_errors = []
            for handle in handles:
                try:
                    handle.close()
                except Exception as exc:
                    cleanup_errors.append(exc)
            if started:
                # Always end the pull job, including when a client fails to close.
                try:
                    dom.abortJob()
                except Exception as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                message = "Pull backup cleanup failed: " + "; ".join(map(str, cleanup_errors))
                if original_error is not None:
                    original_error.add_note(message)
                else:
                    raise RuntimeError(message) from cleanup_errors[0]
