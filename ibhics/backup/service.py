"""Backup operations that run on the node holding the VM, next to its disks."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..hypervisor import HypervisorDriver, InvalidState, VMInfo
from ..hypervisor.serialize import spec_from_json, spec_to_json
from .console import console_png
from .repository import CHUNK_SIZE, BackupInfo, Repository, RepositoryBusy
from .store import Mounter, S3Store, open_store
from .targets import BackupTarget, repository_identity
from .upgrade_guard import protected


class VerificationSkipped(RuntimeError):
    """Boot validation cannot safely reserve the resources it needs."""


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
        self._verify_lock = threading.Lock()
        # Jobs on one target run one at a time on this node, so garbage collection never
        # races a backup started from the same node.
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    def cleanup(self) -> list[str]:
        """Detach targets left attached by a crash; call once when the node starts."""
        return self._mounter.cleanup()

    def _open(self, target: BackupTarget):
        target.validate()
        return open_store(target, self._mounter, self._s3_factory)

    @protected
    def backup(
        self, uuid: str, target: BackupTarget, mode="incremental", progress=None
    ) -> BackupInfo:
        if mode not in ("incremental", "active_full"):
            raise ValueError("Backup mode must be incremental or active_full")
        vm = self._driver.get_vm(uuid)
        check_quota(target)
        with self._locks[target.name], self._open(target) as store:
            from ..hypervisor.backup_cbt import CBTUnavailable

            repo = Repository(
                store,
                self._chunk_size,
                compression=target.compression,
                immutability_days=target.immutability_days,
            )
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

    @protected
    def synthetic_full(self, target, backup_id, progress=None):
        check_quota(target)
        with self._locks[target.name], self._open(target) as store:
            return Repository(
                store,
                self._chunk_size,
                compression=target.compression,
                immutability_days=target.immutability_days,
            ).synthetic_full(backup_id, progress)

    @protected
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
                immutability_days=destination.immutability_days,
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

    @protected
    def restore(
        self,
        target: BackupTarget,
        backup_id: str,
        name: str | None = None,
        pool: str | None = None,
        *,
        isolated: bool = False,
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
            if isolated:
                spec.nics = []
                spec.autostart = False
                spec.iso = spec.driver_iso = None
                spec.boot = ["hd"]
                spec.linked_to = ""
                spec.template = False
                spec.recycled = ""
            if pool:
                for disk in spec.disks:
                    disk.pool = pool
            scratch = Path(tempfile.mkdtemp(prefix="ibhics-restore-", dir=self._scratch_dir))
            vm = None
            try:
                vm = self._driver.create_vm(copy.deepcopy(spec))
                for index in range(len(manifest["disks"])):
                    image = scratch / f"disk{index}.raw"
                    repo.restore_disk(manifest, index, image)
                    self._driver.import_disk(vm.uuid, index, image)
                    image.unlink()
            except BaseException:
                if vm is not None:
                    self._driver.delete_vm(vm.uuid)
                raise
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            return self._driver.get_vm(vm.uuid)

    def _verify_capacity(self, target, backup_id, pool):
        with self._open(target) as store:
            manifest = Repository(store).manifest(backup_id)
        spec = spec_from_json(manifest["spec"])
        host = self._driver.host_info()
        reserved = sum(vm.spec.memory_mb for vm in self._driver.list_vms())
        if spec.memory_mb + reserved + max(1024, host.memory_mb // 10) > host.memory_mb:
            raise VerificationSkipped("Insufficient unallocated memory for isolated verification")
        destination = next(
            (p for p in self._driver.list_pools() if p.name == pool and p.active), None
        )
        logical = sum(d["size_bytes"] for d in manifest["disks"])
        largest = max((d["size_bytes"] for d in manifest["disks"]), default=0)
        if destination is None or destination.available_gb * 1024**3 < logical + largest:
            raise VerificationSkipped(
                "Verification pool is unavailable or has insufficient free space"
            )
        scratch = self._scratch_dir or tempfile.gettempdir()
        if shutil.disk_usage(scratch).free < largest + 1024**3:
            raise VerificationSkipped("Insufficient scratch space for verification restore")

    @protected
    def verify(self, target, backup_id, pool, timeout=300):
        """One isolated boot per node. Manager also serializes across the cluster/VM."""
        if not 1 <= timeout <= 3600:
            raise ValueError("Verification timeout must be 1 through 3600 seconds")
        result = {
            "verified": "skipped",
            "time": datetime.now(UTC).isoformat(),
            "reason": "Verification pool is required",
            "screenshot_path": None,
        }
        vm = None
        with self._verify_lock:
            try:
                if pool:
                    result.update(verified="failed", reason="No guest heartbeat before timeout")
                    self._verify_capacity(target, backup_id, pool)
                    vm = self.restore(
                        target, backup_id, f"ibhics-verify-{uuid4().hex[:16]}", pool, isolated=True
                    )
                    self._driver.start_vm(vm.uuid)
                    deadline = time.monotonic() + timeout
                    while True:
                        if self._driver.backup_boot_ready(vm.uuid):
                            result.update(verified="passed", reason="Guest agent responded to ping")
                            break
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(min(2, max(0, deadline - time.monotonic())))
                    try:
                        data, mime = self._driver.backup_screenshot(vm.uuid)
                        key = f"evidence/{backup_id}/console.png"
                        png = console_png(data, mime)
                        with self._open(target) as store:
                            store.put(key, png)
                        result["screenshot_path"] = key
                    except Exception as exc:
                        result["screenshot_error"] = str(exc)
            except VerificationSkipped as exc:
                result.update(verified="skipped", reason=str(exc))
            except NotImplementedError:
                result.update(verified="skipped", reason="Driver has no boot verification probe")
            except Exception as exc:
                result.update(verified="failed", reason=str(exc))
            finally:
                if vm is not None:
                    try:
                        try:
                            self._driver.stop_vm(vm.uuid, force=True)
                        finally:
                            self._driver.delete_vm(vm.uuid)
                    except Exception as exc:
                        result.update(
                            verified="failed",
                            reason=f"Temporary VM cleanup failed: {exc}",
                            cleanup_vm_uuid=vm.uuid,
                        )
                result["finished"] = datetime.now(UTC).isoformat()
                with self._open(target) as store:
                    # Validate the ID and existence before publishing a sidecar.
                    Repository(store).manifest(backup_id)
                    store.put(f"evidence/{backup_id}/result.json", json.dumps(result).encode())
        return result

    def usage(self, target):
        if target.on_demand:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "basis": "On-demand target is not attached for capacity monitoring",
            }
        with self._open(target) as store:
            if hasattr(store, "root"):
                used = dir_bytes(store.root) if store.root.is_dir() else 0
                existing = store.root
                while not existing.exists():
                    existing = existing.parent
                disk_usage = shutil.disk_usage(existing)
                physical_free = disk_usage.free
                free = physical_free
                if target.quota_gb:
                    free = min(free, max(0, target.quota_gb * 1024**3 - used))
                return {
                    "used_bytes": used,
                    "free_bytes": free,
                    "basis": "filesystem",
                    "capacity_scope": str(existing.stat().st_dev),
                    "filesystem_free_bytes": physical_free,
                    "filesystem_total_bytes": disk_usage.total,
                    "quota_bytes": target.quota_gb * 1024**3 if target.quota_gb else None,
                }
            return {
                "used_bytes": None,
                "free_bytes": None,
                "basis": "S3 capacity is not reported by this API",
            }

    def evidence(self, target, backup_id):
        with self._open(target) as store:
            repo = Repository(store)
            info = repo.info(backup_id)
            key = (info.verification or {}).get("screenshot_path")
            if not key or key not in (
                f"evidence/{backup_id}/console.png",
                f"evidence/{backup_id}/console.ppm",
            ):
                raise InvalidState("No verification screenshot is available")
            import base64

            mime = "image/png" if key.endswith(".png") else "image/x-portable-pixmap"
            return {"mime": mime, "data": base64.b64encode(store.get(key)).decode()}

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
