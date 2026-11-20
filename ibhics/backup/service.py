"""Backup operations that run on the node holding the VM, next to its disks."""

from __future__ import annotations

import copy
import shutil
import tempfile
import threading
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path

from ..hypervisor import HypervisorDriver, InvalidState, VMInfo
from ..hypervisor.serialize import spec_from_json, spec_to_json
from .repository import CHUNK_SIZE, BackupInfo, Repository, RepositoryBusy
from .store import Mounter, S3Store, open_store
from .targets import BackupTarget, repository_identity


class NodeBackup:
    def __init__(
        self,
        driver: HypervisorDriver,
        mounter: Mounter | None = None,
        s3_factory=S3Store.from_target,
        chunk_size: int = CHUNK_SIZE,
        scratch_dir: str | None = None,
    ) -> None:
        self._driver = driver
        self._mounter = mounter or Mounter()
        self._s3_factory = s3_factory
        self._chunk_size = chunk_size
        self._scratch_dir = scratch_dir
        # Jobs on one target run one at a time on this node, so garbage collection never
        # races a backup started from the same node.
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    def cleanup(self) -> list[str]:
        """Detach targets left attached by a crash; call once when the node starts."""
        return self._mounter.cleanup()

    def _open(self, target: BackupTarget):
        target.validate()
        return open_store(target, self._mounter, self._s3_factory)

    def backup(
        self, uuid: str, target: BackupTarget, mode="incremental", progress=None
    ) -> BackupInfo:
        if mode not in ("incremental", "active_full"):
            raise ValueError("Backup mode must be incremental or active_full")
        vm = self._driver.get_vm(uuid)
        check_quota(target)
        with self._locks[target.name], self._open(target) as store:
            from ..hypervisor.backup_cbt import CBTUnavailable

            repo = Repository(store, self._chunk_size, compression=target.compression)
            with repo.reading():
                parent = repo.latest(uuid) if mode == "incremental" else None
                reason = "CBT is not supported by this driver; full backup used"
                if hasattr(self._driver, "export_backup"):
                    try:
                        with self._driver.export_backup(uuid, parent) as (readers, capture):
                            return repo.backup_readers(
                                vm, readers, spec_to_json(vm.spec), capture, parent, progress
                            )
                    except CBTUnavailable as exc:
                        reason = f"Full fallback: {exc}"
                with self._driver.export_disks(uuid) as images:
                    return repo.backup(vm, images, spec_to_json(vm.spec), note=reason)

    def synthetic_full(self, target, backup_id, progress=None):
        check_quota(target)
        with self._locks[target.name], self._open(target) as store:
            return Repository(
                store, self._chunk_size, compression=target.compression
            ).synthetic_full(backup_id, progress)

    def copy(self, source, destination, backup_id, archive=False, progress=None):
        if repository_identity(source) == repository_identity(destination):
            raise ValueError("Choose a different destination repository")
        check_quota(destination)
        with ExitStack() as stack:
            for name in sorted({source.name, destination.name}):
                stack.enter_context(self._locks[name])
            src = Repository(
                stack.enter_context(self._open(source)),
                self._chunk_size,
                compression=source.compression,
            )
            dst = Repository(
                stack.enter_context(self._open(destination)),
                self._chunk_size,
                compression=destination.compression,
            )
            return src.transfer(
                backup_id,
                dst,
                "archive" if archive else "copy",
                independent=archive,
                progress=progress,
            )

    def list(self, target: BackupTarget, vm_name: str | None = None) -> list[BackupInfo]:
        with self._open(target) as store:
            return Repository(store).list(vm_name)

    def restore(
        self,
        target: BackupTarget,
        backup_id: str,
        name: str | None = None,
        pool: str | None = None,
    ) -> VMInfo:
        with self._locks[target.name], self._open(target) as store, Repository(store).reading():
            repo = Repository(store)
            manifest = repo.manifest(backup_id)
            spec = spec_from_json(manifest["spec"])
            if name and name != spec.name:
                spec.name = name
                # A copy next to the original must not share its MAC addresses.
                for nic in spec.nics:
                    nic.mac = None
            if pool:
                for disk in spec.disks:
                    disk.pool = pool
            vm = self._driver.create_vm(copy.deepcopy(spec))
            scratch = Path(tempfile.mkdtemp(prefix="ibhics-restore-", dir=self._scratch_dir))
            try:
                for index in range(len(manifest["disks"])):
                    image = scratch / f"disk{index}.raw"
                    repo.restore_disk(manifest, index, image)
                    self._driver.import_disk(vm.uuid, index, image)
                    image.unlink()
            except BaseException:
                self._driver.delete_vm(vm.uuid)
                raise
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            return self._driver.get_vm(vm.uuid)

    def delete(self, target: BackupTarget, backup_id: str) -> int:
        """Remove one backup; returns how many chunks were freed."""
        with self._locks[target.name], self._open(target) as store:
            repo = Repository(store)
            repo.delete(backup_id)
            return _gc(repo)

    def prune(
        self, target: BackupTarget, vm_name: str, keep_last: int, weekly=0, monthly=0, yearly=0
    ) -> list[str]:
        with self._locks[target.name], self._open(target) as store:
            repo = Repository(store)
            removed = repo.prune(vm_name, keep_last, weekly, monthly, yearly)
            if removed:
                _gc(repo)
            return removed


def _gc(repo: Repository) -> int:
    try:
        return repo.gc()
    except RepositoryBusy:
        return 0  # another node is writing; its chunks are freed by a later run


def dir_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def check_quota(target: BackupTarget) -> None:
    """A repository with a reserved size takes no new backup once that space is used."""
    if not target.quota_gb or not target.path:
        return
    root = Path(target.path)
    used = dir_bytes(root) if root.is_dir() else 0
    if used >= target.quota_gb * 1024**3:
        raise InvalidState(
            f"backup repository {target.name} is full ({used / 1024**3:.1f} of "
            f"{target.quota_gb} GB): delete old backups or give it more space"
        )
