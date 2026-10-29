"""Backup operations that run on the node holding the VM, next to its disks."""

from __future__ import annotations

import copy
import shutil
import tempfile
import threading
from collections import defaultdict
from pathlib import Path

from ..hypervisor import HypervisorDriver, VMInfo
from ..hypervisor.serialize import spec_from_json, spec_to_json
from .repository import CHUNK_SIZE, BackupInfo, Repository, RepositoryBusy
from .store import Mounter, S3Store, open_store
from .targets import BackupTarget


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

    def _open(self, target: BackupTarget):
        target.validate()
        return open_store(target, self._mounter, self._s3_factory)

    def backup(self, uuid: str, target: BackupTarget) -> BackupInfo:
        vm = self._driver.get_vm(uuid)
        with self._locks[target.name], self._open(target) as store:
            with self._driver.export_disks(uuid) as images:
                repo = Repository(store, self._chunk_size)
                return repo.backup(vm, images, spec_to_json(vm.spec))

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
        with self._open(target) as store:
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

    def prune(self, target: BackupTarget, vm_name: str, keep_last: int) -> list[str]:
        with self._locks[target.name], self._open(target) as store:
            repo = Repository(store)
            removed = repo.prune(vm_name, keep_last)
            if removed:
                _gc(repo)
            return removed


def _gc(repo: Repository) -> int:
    try:
        return repo.gc()
    except RepositoryBusy:
        return 0  # another node is writing; its chunks are freed by a later run
