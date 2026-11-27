"""Backup operations that run on the node holding the VM, next to its disks."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from ..hypervisor import HypervisorDriver, InvalidState, VMInfo, VMSpec
from ..hypervisor.containers import ContainerRestartFailed, ContainerSpec
from ..hypervisor.serialize import spec_from_json, spec_to_json
from .console import console_png
from .container_image import RawImageReader
from .repository import CHUNK_SIZE, BackupInfo, Repository, RepositoryBusy
from .store import Mounter, RepositoryLock, S3Store, open_store
from .targets import BackupTarget, repository_identity
from .upgrade_guard import protected


class BackupCancelled(RuntimeError):
    """An operator stopped a backup before the restore-point commit."""


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
        # Maintenance is exclusive; backups share two admission slots.
        self._locks: defaultdict[str, RepositoryLock] = defaultdict(RepositoryLock)
        self._progress_lock = threading.RLock()
        self._progress: dict[tuple[str, str], dict] = {}
        self._cancel: dict[tuple[str, str], threading.Event] = {}
        self._usage_lock = threading.Lock()
        self._usage_cache: dict[str, tuple[float, int]] = {}

    def backup_progress(self, uuid: str, target: str = "") -> dict:
        with self._progress_lock:
            matches = [
                v
                for (vm, name), v in self._progress.items()
                if vm == uuid and (not target or name == target)
            ]
            if not matches:
                return {}
            result = dict(max(matches, key=lambda item: item["started_at"]))
        now = time.time()
        result["elapsed_seconds"] = max(0, now - result["started_at"])
        result["idle_seconds"] = max(0, now - result["updated_at"])
        result["read_mib_per_second"] = (
            result.get("read_bytes", 0) / 1024**2 / max(1, result["elapsed_seconds"])
        )
        return result

    def cancel_backup(self, uuid: str, target: str) -> bool:
        key = (uuid, target)
        with self._progress_lock:
            current = self._progress.get(key, {})
            if current.get("state") != "running" or current.get("phase") == "committing":
                return False
            self._cancel[key].set()
            current.update(
                cancel_requested=True, step="Cancellation requested; waiting for safe cleanup"
            )
        # Only the backup job may be aborted, never VM power or another domain job.
        if current.get("backup_socket") and hasattr(self._driver, "abort_backup"):
            self._driver.abort_backup(uuid, current["backup_socket"])
        return True

    @contextmanager
    def _repository_turn(self, target, check_cancel):
        lock = self._locks[target.name].backup
        while not lock.acquire(timeout=0.25):
            check_cancel()
        try:
            check_cancel()
            yield
        finally:
            lock.release()

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
        key = (uuid, target.name)
        now = time.time()
        with self._progress_lock:
            if any(vm == uuid and status["state"] == "running"
                   for (vm, _), status in self._progress.items()):
                raise RepositoryBusy("A backup is already running for this VM")
            cancel = self._cancel[key] = threading.Event()
            self._progress[key] = {
                "state": "running",
                "phase": "waiting",
                "step": "Waiting for repository lock",
                "percent": None,
                "started_at": now,
                "updated_at": now,
                "target": target.name,
            }
            # Bound retained progress independently of the task history.
            for old in list(self._progress):
                if len(self._progress) <= 200:
                    break
                if self._progress[old]["state"] != "running":
                    del self._progress[old]
                    self._cancel.pop(old, None)

        def check_cancel():
            if cancel.is_set():
                raise BackupCancelled("Cancelled by operator")

        def metrics(data):
            with self._progress_lock:
                check_cancel()
                self._progress[key].update(data, updated_at=time.time())

        def report(step, percent):
            metrics({"step": step, "percent": percent})
            if progress:
                progress(step, percent)

        try:
            info = self._backup(uuid, target, mode, report, metrics, check_cancel)
            # A completed manifest wins a late cancel request; it cannot be unpublished.
            with self._progress_lock:
                self._progress[key].update(
                    state="done",
                    phase="complete",
                    step="Complete",
                    percent=100,
                    read_bytes=info.read_bytes,
                    stored_bytes=info.stored_bytes,
                    updated_at=time.time(),
                )
            return info
        except BaseException as exc:
            stopped = (cancel.is_set() or isinstance(exc, BackupCancelled)) and not isinstance(
                exc, ContainerRestartFailed
            )
            with self._progress_lock:
                self._progress[key].update(
                    state="cancelled" if stopped else "failed",
                    phase="cancelled" if stopped else "failed",
                    step="Cancelled by operator" if stopped else "Backup failed",
                    updated_at=time.time(),
                )
            if stopped:
                raise BackupCancelled("Cancelled by operator") from exc
            raise

    def _backup(self, uuid, target, mode, progress, metrics, check_cancel) -> BackupInfo:
        if uuid.startswith("ct:"):
            return self._backup_container(uuid[3:], target, mode, progress, metrics, check_cancel)
        if mode not in ("incremental", "active_full"):
            raise ValueError("Backup mode must be incremental or active_full")
        vm = self._driver.get_vm(uuid)
        check_quota(target)
        with self._repository_turn(target, check_cancel), self._open(target) as store:
            progress("Inspecting previous restore point and CBT checkpoint", None)
            metrics({"phase": "preparing"})
            from ..hypervisor.backup_cbt import CBTUnavailable

            repo = Repository(
                store,
                self._chunk_size,
                compression=target.compression,
                immutability_days=target.immutability_days,
            )
            with repo.reading(shared=True, check_cancel=check_cancel):
                parent = repo.latest(uuid) if mode == "incremental" else None
                reason = "CBT is not supported by this driver; full backup used"
                if hasattr(self._driver, "export_backup"):
                    try:
                        with self._driver.export_backup(uuid, parent) as (readers, capture):
                            metrics({"backup_socket": capture.get("socket")})
                            return repo.backup_readers(
                                vm,
                                readers,
                                spec_to_json(vm.spec),
                                capture,
                                parent,
                                progress,
                                metrics=metrics,
                            )
                    except CBTUnavailable as exc:
                        reason = f"Full fallback: {exc}"
                        # An unusable incremental bitmap does not make full pull
                        # backup unavailable. Establish a fresh checkpoint first,
                        # retaining allocation-aware reads and observable progress.
                        if parent is not None:
                            progress(
                                "Incremental checkpoint unavailable; starting a fresh full backup",
                                None,
                            )
                            metrics(
                                {"phase": "preparing", "mode": "full", "fallback_reason": reason}
                            )
                            try:
                                with self._driver.export_backup(uuid, None) as (readers, capture):
                                    metrics({"backup_socket": capture.get("socket")})
                                    return repo.backup_readers(
                                        vm,
                                        readers,
                                        spec_to_json(vm.spec),
                                        capture,
                                        None,
                                        progress,
                                        metrics=metrics,
                                    )
                            except CBTUnavailable as full_exc:
                                reason += f"; full pull unavailable: {full_exc}"
                progress(reason + "; exporting a full point-in-time image", None)
                metrics({"phase": "exporting", "mode": "full", "fallback_reason": reason})
                with self._driver.export_disks(uuid) as images:
                    progress("Export complete; reading full backup into repository", None)
                    metrics({"phase": "writing"})
                    return repo.backup(
                        vm,
                        images,
                        spec_to_json(vm.spec),
                        note=reason,
                        progress=progress,
                        metrics=metrics,
                    )

    def _backup_container(self, name, target, mode, progress, metrics, check_cancel):
        if mode not in ("incremental", "active_full", "container_stop"):
            raise ValueError("Invalid container backup mode")
        check_quota(target)
        with self._repository_turn(target, check_cancel), self._open(target) as store:
            repo = Repository(
                store,
                self._chunk_size,
                compression=target.compression,
                immutability_days=target.immutability_days,
            )
            with repo.reading(shared=True, check_cancel=check_cancel):
                progress("Preparing stopped container filesystem", None)
                with self._driver.containers().backup_source(
                    name, allow_stop=mode == "container_stop"
                ) as (image, definition):
                    reader = RawImageReader(image)
                    try:
                        entity = SimpleNamespace(uuid=f"ct:{name}", spec=VMSpec(name=name))
                        return repo.backup_readers(
                            entity,
                            [reader],
                            definition,
                            {
                                "incremental": False,
                                "note": "Consistent stopped container filesystem",
                            },
                            progress=progress,
                            metrics=metrics,
                        )
                    finally:
                        reader.close()

    @protected
    def restore_container(self, target, backup_id, name=None, pool=None):
        with (
            self._locks[target.name],
            self._open(target) as store,
            Repository(store).reading(check_cancel=lambda: None),
        ):
            repo = Repository(store)
            manifest = repo.manifest(backup_id)
            definition = manifest["spec"]
            if definition.get("entity_type") != "container" or len(manifest["disks"]) != 1:
                raise InvalidState("This restore point is not a container backup")
            spec = ContainerSpec(**definition["container"])
            if name and name != spec.name:
                spec.name = name
                cards = [{**card, "mac": ""} for card in spec.interfaces()]
                spec = spec.with_interfaces(cards)
            if pool:
                spec.pool = pool
            return self._driver.containers().import_rootfs(
                spec,
                lambda path: repo.restore_disk(manifest, 0, path),
                definition.get("includes", []),
            )

    @protected
    def synthetic_full(self, target, backup_id, progress=None):
        check_quota(target)
        with self._locks[target.name], self._open(target) as store, store.waiting():
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
            for repo in sorted((src, dst), key=lambda repo: (
                repository_identity(source) if repo is src else repository_identity(destination)
            )):
                stack.enter_context(repo.store.waiting())
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
        with (
            self._locks[target.name],
            self._open(target) as store,
            Repository(store).reading(check_cancel=lambda: None),
        ):
            return self._restore(Repository(store), backup_id, name, pool, isolated)

    def _restore(self, repo, backup_id, name, pool, isolated=False):
        """Materialize a VM under an already-held repository lease."""
        manifest = repo.manifest(backup_id)
        if manifest["spec"].get("entity_type") == "container":
            raise InvalidState("Use container restore for this restore point")
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
        if manifest["spec"].get("entity_type") == "container":
            raise VerificationSkipped("VM boot verification does not apply to container backups")
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
        with (
            self._verify_lock,
            self._locks[target.name],
            self._open(target) as store,
            store.waiting(),
        ):
            try:
                if pool:
                    result.update(verified="failed", reason="No guest heartbeat before timeout")
                    self._verify_capacity(target, backup_id, pool)
                    vm = self._restore(
                        Repository(store), backup_id, f"ibhics-verify-{uuid4().hex[:16]}",
                        pool, isolated=True,
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
                key = str(store.root)
                # Single-flight and cached accounting avoid repeated full NFS tree walks.
                # A live backup must not compete with a capacity scanner.
                with self._usage_lock:
                    with self._progress_lock:
                        busy = any(
                            v["state"] == "running" and v["target"] == target.name
                            for v in self._progress.values()
                        )
                    cached = self._usage_cache.get(key)
                    if not busy and (not cached or time.monotonic() - cached[0] >= 60):
                        cached = (
                            time.monotonic(),
                            dir_bytes(store.root) if store.root.is_dir() else 0,
                        )
                        self._usage_cache[key] = cached
                    used = cached[1] if cached else None
                    age = max(0, time.monotonic() - cached[0]) if cached else None
                existing = store.root
                while not existing.exists():
                    existing = existing.parent
                disk_usage = shutil.disk_usage(existing)
                physical_free = disk_usage.free
                free = physical_free
                if target.quota_gb:
                    free = (
                        min(free, max(0, target.quota_gb * 1024**3 - used))
                        if used is not None
                        else None
                    )
                return {
                    "used_bytes": used,
                    "free_bytes": free,
                    "basis": "filesystem",
                    "used_bytes_age_seconds": age,
                    "used_bytes_cached": bool(cached),
                    "accounting_deferred": busy,
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
        with self._locks[target.name], self._open(target) as store, store.waiting():
            repo = Repository(store)
            repo.delete(backup_id)
            return _gc(repo)

    def prune(
        self, target: BackupTarget, vm_name: str, keep_last: int, weekly=0, monthly=0, yearly=0
    ) -> list[str]:
        with self._locks[target.name], self._open(target) as store, store.waiting():
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
