"""Replication lifecycle and interval/daily schedules on the active manager.

Metadata follows the manager shared-state mirror. Runtime replica manifests are
separate, immutable generations on the execution node. Interrupted jobs require
operator recovery; they are never automatically replayed after manager takeover.
"""

from __future__ import annotations

import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..hypervisor import InvalidState, PoolNotFound, VMExists
from ..importer import SourceNotFound, SourceVM
from ..importer.nics import validate_nics
from ..replication.providers import capability
from ..replication.runtime import ClusterRuntime
from ..replication.store import durable_json

BUSY = {"seeding", "syncing", "cutover-pending", "cutting-over"}
SYNCABLE = {"new", "idle", "failed"}


def validate_schedule(schedule: dict) -> dict:
    allowed = {"interval_minutes", "at"}
    if not isinstance(schedule, dict) or set(schedule) - allowed:
        raise ValueError("schedule accepts interval_minutes or at (HH:MM)")
    interval, at = schedule.get("interval_minutes"), schedule.get("at")
    if (interval is None) == (at is None):
        raise ValueError("Choose an interval or daily HH:MM")
    if interval is not None and (type(interval) is not int or not 1 <= interval <= 525600):
        raise ValueError("interval_minutes must be a positive integer up to 525600")
    if at is not None:
        import re

        if not isinstance(at, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
            raise ValueError("at must be HH:MM")
    return {"interval_minutes": interval} if interval is not None else {"at": at}


@dataclass
class ReplicationJob:
    id: str
    source: str
    vm: str
    name: str
    node: str
    destination: dict
    schedule: dict
    created: str
    source_vm: dict
    capability: dict
    via: str = ""
    state: str = "new"
    last_sync: str | None = None
    last_point: str | None = None
    last_attempt: str | None = None
    percent: int = 0
    bytes_transferred: int = 0
    changed_pct: float = 0
    error: str | None = None
    result: str | None = None
    test_vm: str | None = None
    operation: str | None = None
    history: list[dict] = field(default_factory=list)


class ReplicationManager:
    def __init__(
        self,
        imports,
        state_path: Path | None = None,
        runtime=None,
        executor=None,
        clock=lambda: datetime.now().astimezone(),
    ):
        self.imports, self.cluster = imports, imports._cluster
        self.state_path = state_path
        root = (
            state_path.parent / "replica-manifests"
            if state_path
            else Path("/var/lib/ibhics/replica-manifests")
        )
        self.runtime = runtime or ClusterRuntime(self.cluster, root)
        self.executor = executor or ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="replication"
        )
        self.clock = clock
        self._jobs: dict[str, ReplicationJob] = {}
        self._lock = threading.RLock()
        # Retained for the life of an operation; installer takes an exclusive lock.
        self.guard_path = Path("/run/lock/ibhics-replication.lock")

    def _save(self):
        if self.state_path:
            durable_json(self.state_path, {"jobs": [asdict(j) for j in self._jobs.values()]})

    def load(self):
        import json

        if self.state_path and self.state_path.exists():
            for entry in json.loads(self.state_path.read_text()).get("jobs", []):
                job = ReplicationJob(**entry)
                if job.state in BUSY or job.operation:
                    job.state, job.operation = "failed", None
                    job.error = (
                        "Interrupted by manager restart; inspect source snapshots before retry"
                    )
                # Ownership is durable beside the replica, so a failed API response or
                # manager takeover must not make a test VM invisible to cleanup.
                if hasattr(self.runtime, "ownership"):
                    try:
                        owned = self.runtime.ownership(job)
                        job.test_vm = owned.get("test_vm") or job.test_vm
                        job.result = owned.get("result") or job.result
                    except Exception:
                        pass  # node may be offline; its operation still cannot be replayed
                self._jobs[job.id] = job
            self._save()

    def job(self, job_id):
        try:
            return self._jobs[job_id]
        except KeyError:
            raise SourceNotFound(f"replication {job_id}") from None

    def view(self, job):
        with self._lock:
            data = asdict(job)
            data["rpo_seconds"] = (
                max(
                    0,
                    int(
                        (
                            self.clock() - datetime.fromisoformat(job.last_point or job.last_sync)
                        ).total_seconds()
                    ),
                )
                if job.last_sync
                else None
            )
            return data

    def jobs(self):
        with self._lock:
            return [self.view(job) for job in self._jobs.values()]

    def create(self, body: dict):
        self.cluster.require_quorum()
        source = (
            self.imports.uploads_source
            if body["source"] == "uploads"
            else self.imports.source(body["source"])
        )
        cap = capability(source)
        if not cap["supported"]:
            raise ValueError(cap["reason"])
        schedule = validate_schedule(body.pop("schedule", {"interval_minutes": 15}))
        vm = next((v for v in self.imports.source_vms(source.name) if v.ref == body["vm"]), None)
        if vm is None:
            raise SourceNotFound("source VM")
        target = (
            self.cluster.member(body["node"])
            if body.get("node")
            else self.cluster.pick_node(body.get("memory_mb") or vm.memory_mb)
        )
        if target.maintenance or target not in self.cluster.compute_members():
            raise ValueError("Choose a healthy node outside maintenance")
        pools = body.get("disks") or [{"pool": body.get("pool", "default")} for _ in vm.disks_gb]
        if not pools or len(pools) != len(vm.disks_gb):
            raise ValueError("disks must have one entry per source disk")
        body["disks"] = pools
        available = {p.name: p for p in target.driver.list_pools() if p.active}
        for disk in pools:
            if (disk.get("pool") or body["pool"]) not in available:
                raise PoolNotFound(disk.get("pool") or body["pool"])
        # Relay sync only through a reachable node sharing every selected pool.
        via = target
        if not self.imports._reaches(target, source)[0]:
            via = self.imports._relay(
                target, source, {d.get("pool") or body["pool"] for d in pools}, "source unreachable"
            )
        validate_nics(body.get("nics"), vm)
        name = body.get("name") or vm.name
        network = body.get("network", "default")
        if isinstance(network, list) and len(network) != vm.nics:
            raise ValueError("Choose one destination network per NIC")
        with self._lock:
            if any(j.name == name and j.state != "done" for j in self._jobs.values()):
                raise VMExists("Replication target name already in use")
            if any(v.spec.name == name for v in target.driver.list_vms()):
                raise VMExists(name)
            body["name"], body["node"] = name, target.name
            job = ReplicationJob(
                uuid.uuid4().hex,
                source.name,
                vm.ref,
                name,
                target.name,
                copy.deepcopy(body),
                schedule,
                self.clock().isoformat(),
                vm.to_dict(),
                cap,
            )
            job.via = via.name if via.name != target.name else ""
            # Validate persistent pool support before accepting a job.
            self.runtime.store(job)
            self._jobs[job.id] = job
            self._save()
        self.sync(job.id)
        return job

    def update(self, job_id, *, schedule=None, paused=None):
        self.cluster.require_quorum()
        with self._lock:
            job = self.job(job_id)
            if job.state in BUSY or job.operation:
                raise InvalidState("Replication has an active operation")
            if job.state == "done":
                raise InvalidState("Completed replication cannot be resumed")
            new_schedule = validate_schedule(schedule) if schedule is not None else job.schedule
            if paused is not None:
                if paused:
                    job.state = "paused"
                elif job.state == "paused":
                    job.state = "idle" if job.last_sync else "new"
                else:
                    raise InvalidState("Only a paused replication can be resumed")
            job.schedule = new_schedule
            self._save()
            return job

    def _submit(self, job_id, operation, work, *, allowed, state=None):
        import fcntl

        self.cluster.require_quorum()
        with self._lock:
            job = self.job(job_id)
            if job.state not in allowed or job.operation:
                raise InvalidState("Replication is busy or this action is unavailable in its state")
            self.guard_path.parent.mkdir(parents=True, exist_ok=True)
            guard = self.guard_path.open("a+")
            try:
                fcntl.flock(guard, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                guard.close()
                raise InvalidState(
                    "Application upgrade is running; retry after it completes"
                ) from None
            previous_state = job.state
            job.operation = operation
            job.error = None
            job.percent = 0
            if state:
                job.state = state
            if operation in ("sync", "cutover"):
                job.last_attempt = self.clock().isoformat()
            self._save()

        def run():
            try:
                work(job)
            except Exception:
                with self._lock:
                    job.state = "failed"
                    # Provider/SSH output is intentionally excluded, including passwords.
                    job.error = "Replication operation failed; inspect node and source"
            finally:
                with self._lock:
                    job.operation = None
                    job.history.append(
                        {
                            "at": self.clock().isoformat(),
                            "operation": operation,
                            "state": job.state,
                            "bytes_transferred": job.bytes_transferred,
                            "error": job.error,
                        }
                    )
                    job.history = job.history[-100:]
                    try:
                        self._save()
                    finally:
                        guard.close()

        try:
            self.executor.submit(run)
        except Exception:
            with self._lock:
                job.operation, job.state = None, previous_state
                self._save()
            guard.close()
            raise
        return job

    def _sync(self, job):
        source = self.imports.source(job.source)
        vm = SourceVM.from_dict(job.source_vm)

        def progress(done, total):
            with self._lock:
                job.percent = int(done * 100 / total) if total else 0

        # The recoverable point precedes the transfer; completion time would
        # understate RPO by the duration of a long seed/sync.
        point_started = self.clock().isoformat()
        result = self.runtime.sync(job, source, vm, progress)
        with self._lock:
            job.last_sync = self.clock().isoformat()
            job.last_point = point_started
            job.bytes_transferred = result["bytes_transferred"]
            job.changed_pct = round(100 * result["changed_bytes"] / result["total_bytes"], 2)
            job.percent = 100
            self._save()

    def sync(self, job_id):
        def work(job):
            self._sync(job)
            job.state = "idle"

        return self._submit(
            job_id,
            "sync",
            work,
            allowed=SYNCABLE,
            state="syncing" if self.job(job_id).last_sync else "seeding",
        )

    def cutover(self, job_id, source_powered_off: bool):
        if source_powered_off is not True:
            raise ValueError("source_powered_off: true is required; shut down the source VM first")

        def work(job):
            self._sync(job)
            job.state = "cutting-over"
            with self._lock:
                self._save()
            vm = SourceVM.from_dict(job.source_vm)
            job.result = self.runtime.materialize(
                job, self.imports.source(job.source), vm, test=False, networks=None
            )
            job.state = "done"

        job = self.job(job_id)
        if job.test_vm or job.result:
            raise InvalidState("Clean up the test or inspect an existing cutover VM before cutover")
        return self._submit(
            job_id,
            "cutover",
            work,
            allowed={"idle", "cutover-pending", "failed"},
            state="cutover-pending",
        )

    def test_failover(self, job_id, networks=None):
        job = self.job(job_id)
        if job.test_vm or not job.last_sync:
            raise InvalidState("Sync first and clean up any existing test")

        def work(job):
            job.test_vm = self.runtime.materialize(
                job,
                self.imports.source(job.source),
                SourceVM.from_dict(job.source_vm),
                test=True,
                networks=networks or [],
            )

        return self._submit(job_id, "test-failover", work, allowed={"idle", "paused"})

    def cleanup_test(self, job_id):
        def work(job):
            self.runtime.cleanup_test(job)
            job.test_vm = None
            if job.state == "failed":
                job.state = "paused"

        return self._submit(job_id, "test-cleanup", work, allowed={"idle", "paused", "failed"})

    def delete(self, job_id, confirm: bool):
        self.cluster.require_quorum()
        if not confirm:
            raise ValueError("confirm=true is required to delete replica data")
        with self._lock:
            job = self.job(job_id)
            if job.state in BUSY or job.operation or job.test_vm:
                raise InvalidState("Stop active operations and clean up the test before deletion")
            self.runtime.delete(job)
            del self._jobs[job_id]
            self._save()

    def tick(self):
        now = self.clock()
        with self._lock:
            due = []
            for job in self._jobs.values():
                if job.state not in {"new", "idle"} or job.operation:
                    continue
                last = datetime.fromisoformat(job.last_attempt) if job.last_attempt else None
                interval = job.schedule.get("interval_minutes")
                if interval is not None:
                    ready = last is None or now - last >= timedelta(minutes=interval)
                else:
                    hour, minute = map(int, job.schedule["at"].split(":"))
                    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    ready = now >= slot and (last is None or last < slot)
                if ready:
                    due.append(job.id)
        result = []
        for job_id in due:
            try:
                result.append(self.sync(job_id))
            except InvalidState:
                pass  # a manual operation won the reservation
            except Exception:
                with self._lock:
                    job = self.job(job_id)
                    job.state = "failed"
                    job.error = "Could not schedule replication; inspect source and node"
                    self._save()
        return result
