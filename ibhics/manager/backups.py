"""Backup targets, schedules and jobs, kept by the manager and run on the nodes."""

from __future__ import annotations

import itertools
import json
import os
import re
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..backup import BackupInfo, BackupTarget
from ..backup.repository import parse_id
from ..cluster import Cluster, Member, NodeUnavailable
from ..hypervisor import VMExists

AT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class TargetNotFound(LookupError):
    pass


class PolicyNotFound(LookupError):
    pass


@dataclass
class Policy:
    name: str
    target: str
    vms: list[str] = field(default_factory=list)  # empty: every VM in the cluster
    at: str | None = None  # "HH:MM", daily, in the manager's local time
    every_hours: int | None = None
    keep_last: int = 7
    enabled: bool = True
    last_run: str | None = None

    def validate(self) -> None:
        if (self.at is None) == (self.every_hours is None):
            raise ValueError("a policy runs either daily 'at' a time or 'every_hours'")
        if self.at is not None and not AT_RE.match(self.at):
            raise ValueError("'at' must be HH:MM")
        if self.every_hours is not None and self.every_hours < 1:
            raise ValueError("every_hours must be at least 1")
        if self.keep_last < 1:
            raise ValueError("keep_last must be at least 1")

    def due(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        last = datetime.fromisoformat(self.last_run) if self.last_run else None
        if self.every_hours is not None:
            return last is None or now - last >= timedelta(hours=self.every_hours)
        hour, minute = (int(x) for x in self.at.split(":"))
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now >= slot and (last is None or last < slot)


@dataclass
class Job:
    id: str
    kind: str  # "backup" or "restore"
    target: str
    vm: str
    node: str | None
    policy: str | None
    created: str
    state: str = "queued"  # queued, running, done, failed
    finished: str | None = None
    result: str | None = None  # backup id, or the restored VM's uuid
    error: str | None = None


class BackupManager:
    def __init__(
        self,
        cluster: Cluster,
        state_path: Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        executor: Executor | None = None,
    ) -> None:
        self._cluster = cluster
        self._state_path = state_path
        self._clock = clock
        self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="backup")
        self._targets: dict[str, BackupTarget] = {}
        self._policies: dict[str, Policy] = {}
        self._jobs: deque[Job] = deque(maxlen=200)
        self._ids = itertools.count(1)
        self._lock = threading.RLock()

    # Targets

    def targets(self) -> list[BackupTarget]:
        with self._lock:
            return list(self._targets.values())

    def target(self, name: str) -> BackupTarget:
        with self._lock:
            try:
                return self._targets[name]
            except KeyError:
                raise TargetNotFound(name) from None

    def add_target(self, target: BackupTarget) -> BackupTarget:
        target.validate()
        with self._lock:
            if target.name in self._targets:
                raise VMExists(f"a backup target named {target.name!r} already exists")
            self._targets[target.name] = target
            self._save()
        return target

    def remove_target(self, name: str) -> None:
        """Forget the target. Backups already on it stay where they are."""
        with self._lock:
            self.target(name)
            users = [p.name for p in self._policies.values() if p.target == name]
            if users:
                raise ValueError(f"backup policies {users} still use target {name!r}")
            del self._targets[name]
            self._save()

    # Policies

    def policies(self) -> list[Policy]:
        with self._lock:
            return list(self._policies.values())

    def add_policy(self, policy: Policy) -> Policy:
        policy.validate()
        with self._lock:
            self.target(policy.target)
            if policy.name in self._policies:
                raise VMExists(f"a backup policy named {policy.name!r} already exists")
            self._policies[policy.name] = policy
            self._save()
        return policy

    def remove_policy(self, name: str) -> None:
        with self._lock:
            if name not in self._policies:
                raise PolicyNotFound(name)
            del self._policies[name]
            self._save()

    def tick(self) -> list[Job]:
        """Start the jobs of every policy that is due. The manager calls this every minute."""
        now = self._clock()
        jobs = []
        for policy in self.policies():
            if policy.due(now):
                jobs.extend(self.run_policy(policy.name, now))
        return jobs

    def run_policy(self, name: str, now: datetime | None = None) -> list[Job]:
        with self._lock:
            policy = self._policies.get(name)
            if policy is None:
                raise PolicyNotFound(name)
            policy.last_run = (now or self._clock()).isoformat()
            self._save()
        jobs = []
        for member, vm in self._cluster.all_vms():
            if not policy.vms or vm.spec.name in policy.vms:
                jobs.append(
                    self._backup_job(
                        member, vm.uuid, vm.spec.name, policy.target, policy.keep_last, policy.name
                    )
                )
        return jobs

    # Jobs

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs))

    def start_backup(self, uuid: str, target_name: str, keep_last: int | None = None) -> Job:
        self.target(target_name)
        member, vm = self._cluster.find_vm(uuid)
        return self._backup_job(member, uuid, vm.spec.name, target_name, keep_last, None)

    def _backup_job(
        self,
        member: Member,
        uuid: str,
        vm_name: str,
        target_name: str,
        keep_last: int | None,
        policy: str | None,
    ) -> Job:
        job = self._new_job("backup", target_name, vm_name, member.name, policy)

        def work() -> str:
            target = self.target(target_name)
            info = member.backups.backup(uuid, target)
            if keep_last:
                member.backups.prune(target, vm_name, keep_last)
            return info.id

        self._submit(job, work)
        return job

    def start_restore(
        self,
        target_name: str,
        backup_id: str,
        name: str | None = None,
        node: str | None = None,
        pool: str | None = None,
        source_node: str | None = None,
    ) -> Job:
        target = self.target(target_name)
        vm_name, _ = parse_id(backup_id)
        new_name = name or vm_name
        if any(vm.spec.name == new_name for _, vm in self._cluster.all_vms()):
            raise VMExists(
                f"VM named {new_name!r} already exists; restore under another name or delete it"
            )
        if target.shared:
            member = self._cluster.member(node) if node else self._cluster.pick_node(0)
        else:
            # A local target lives on one node's disk, so the VM comes back on that node.
            if not source_node:
                raise ValueError("say which node holds this backup (source_node)")
            member = self._cluster.member(source_node)
        job = self._new_job("restore", target_name, new_name, member.name, None)

        def work() -> str:
            return member.backups.restore(target, backup_id, name, pool).uuid

        self._submit(job, work)
        return job

    def _new_job(self, kind, target, vm, node, policy) -> Job:
        job = Job(
            id=str(next(self._ids)),
            kind=kind,
            target=target,
            vm=vm,
            node=node,
            policy=policy,
            created=self._clock().isoformat(),
        )
        with self._lock:
            self._jobs.append(job)
        return job

    def _submit(self, job: Job, work: Callable[[], str]) -> None:
        def run() -> None:
            job.state = "running"
            try:
                job.result = work()
                job.state = "done"
            except Exception as exc:
                job.error = f"{type(exc).__name__}: {exc}"
                job.state = "failed"
            job.finished = self._clock().isoformat()

        self._executor.submit(run)

    # Backups on a target

    def list_backups(self, target_name: str) -> list[tuple[str | None, BackupInfo]]:
        target = self.target(target_name)
        if target.shared:
            last_error = None
            for member in self._cluster.compute_members():
                try:
                    return [(None, b) for b in member.backups.list(target)]
                except NodeUnavailable as exc:
                    last_error = exc
            raise last_error or NodeUnavailable("no compute node is available")
        result = []
        for member in self._cluster.compute_members():
            try:
                result.extend((member.name, b) for b in member.backups.list(target))
            except NodeUnavailable as exc:
                member.last_error = str(exc)
        return result

    def delete_backup(self, target_name: str, backup_id: str, node: str | None = None) -> None:
        target = self.target(target_name)
        if target.shared:
            members = self._cluster.compute_members()
            if not members:
                raise NodeUnavailable("no compute node is available")
            member = members[0]
        else:
            if not node:
                raise ValueError("say which node holds this backup (node)")
            member = self._cluster.member(node)
        member.backups.delete(target, backup_id)

    # Persistence: targets hold credentials, so the file is readable by root only.

    def load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        data = json.loads(self._state_path.read_text())
        with self._lock:
            for entry in data.get("targets", []):
                target = BackupTarget.from_dict(entry)
                self._targets[target.name] = target
            for entry in data.get("policies", []):
                policy = Policy(**entry)
                self._policies[policy.name] = policy

    def _save(self) -> None:
        if not self._state_path:
            return
        data = {
            "targets": [t.to_dict() for t in self._targets.values()],
            "policies": [asdict(p) for p in self._policies.values()],
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self._state_path)
