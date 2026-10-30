"""Live migration: checks before a VM moves, the jobs that move it, and node evacuation."""

from __future__ import annotations

import itertools
import socket
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from ..cluster import Cluster, Member, NodeUnavailable
from ..hypervisor import InvalidState, PoolInfo, PoolType, VMInfo, VMState

# How the source node's libvirtd reaches the destination's. TLS needs the cluster's
# libvirt certificates on every node; SSH needs root keys; plain TCP is for labs only.
TRANSPORTS = {
    "tls": "qemu+tls://{host}/system",
    "ssh": "qemu+ssh://root@{host}/system",
    "tcp": "qemu+tcp://{host}/system",
}


@dataclass
class Migration:
    id: str
    vm: str
    uuid: str
    source: str
    target: str | None
    copy_storage: bool
    live: bool
    created: str
    state: str = "queued"  # queued, running, done, failed
    finished: str | None = None
    error: str | None = None


@dataclass
class Plan:
    source: Member
    vm: VMInfo
    target: Member
    copy_storage: bool


class MigrationManager:
    def __init__(
        self,
        cluster: Cluster,
        transport: str = "tls",
        local_host: str | None = None,
        busy: Callable[[str], bool] = lambda _vm_name: False,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        executor: Executor | None = None,
    ) -> None:
        if transport not in TRANSPORTS:
            raise ValueError(f"migration transport must be one of {', '.join(TRANSPORTS)}")
        self._cluster = cluster
        self._transport = transport
        self._local_host = local_host or socket.getfqdn()
        self._busy = busy  # true while another job (a backup) works on the VM
        self._clock = clock
        self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="migrate")
        self._jobs: deque[Migration] = deque(maxlen=200)
        self._moving: set[str] = set()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def jobs(self) -> list[Migration]:
        with self._lock:
            return list(reversed(self._jobs))

    def uri(self, member: Member) -> str:
        host = self._local_host if member.local else urlparse(member.address).hostname
        return TRANSPORTS[self._transport].format(host=host)

    # Planning

    def plan(self, uuid: str, node: str | None = None, copy_storage: bool | None = None) -> Plan:
        """Check that the VM can move and choose where; raises with the reason if not."""
        self._cluster.require_quorum()
        source, vm = self._cluster.find_vm(uuid)
        if vm.state not in (VMState.RUNNING, VMState.PAUSED, VMState.STOPPED):
            raise InvalidState(f"VM {vm.spec.name!r} is {vm.state}; wait for it to settle")
        if self._busy(vm.spec.name):
            raise InvalidState(f"a backup of {vm.spec.name!r} is running; try again after it")
        with self._lock:
            if uuid in self._moving:
                raise InvalidState(f"VM {vm.spec.name!r} is already being migrated")
        if node is not None:
            target = self._cluster.member(node)
            if target.name == source.name:
                raise InvalidState(f"VM {vm.spec.name!r} is already on {node}")
            return Plan(source, vm, target, self._check(source, vm, target, copy_storage))

        reasons = []
        best = None
        for target in self._cluster.compute_members():
            if target.name == source.name:
                continue
            try:
                copy = self._check(source, vm, target, copy_storage)
                free = self._free_memory(target)
            except (InvalidState, NodeUnavailable) as exc:
                reasons.append(str(exc))
                continue
            if best is None or free > best[0]:
                best = (free, Plan(source, vm, target, copy))
        if best is None:
            detail = "; ".join(reasons) or "there is no other healthy compute node"
            raise InvalidState(f"no node can take {vm.spec.name!r}: {detail}")
        return best[1]

    def _check(self, source: Member, vm: VMInfo, target: Member, copy_storage: bool | None) -> bool:
        """Raise if `target` cannot take the VM; return whether its disks must be copied."""
        name = target.name
        src_pools = {p.name: p for p in source.driver.list_pools()}
        dst_pools = {p.name: p for p in target.driver.list_pools()}
        needs_copy: dict[str, int] = {}
        for disk in vm.spec.disks:
            dst = dst_pools.get(disk.pool)
            if dst is None or not dst.active:
                raise InvalidState(
                    f"{name} has no storage pool {disk.pool!r}; add it there under the same name"
                )
            if not _shared(src_pools.get(disk.pool), dst):
                needs_copy[disk.pool] = needs_copy.get(disk.pool, 0) + disk.size_gb
        if needs_copy:
            pools = ", ".join(sorted(needs_copy))
            if copy_storage is False:
                raise InvalidState(f"disks in {pools} are local to {source.name}; allow copying")
            if vm.state == VMState.STOPPED:
                raise InvalidState(
                    f"disks in {pools} are local to {source.name}; a stopped VM can only move "
                    "on shared storage, so start it first"
                )
            for pool, size_gb in needs_copy.items():
                if dst_pools[pool].available_gb < size_gb:
                    raise InvalidState(f"pool {pool!r} on {name} has no room for {size_gb} GB")
        if vm.state != VMState.STOPPED and self._free_memory(target) < vm.spec.memory_mb:
            raise InvalidState(f"{name} has not enough free memory for {vm.spec.memory_mb} MB")
        return bool(needs_copy)

    @staticmethod
    def _free_memory(member: Member) -> int:
        host = member.driver.host_info()
        used = sum(v.spec.memory_mb for v in member.driver.list_vms() if v.state != VMState.STOPPED)
        return host.memory_mb - used

    # Jobs

    def start(
        self,
        uuid: str,
        node: str | None = None,
        copy_storage: bool | None = None,
        bandwidth_mib: int | None = None,
    ) -> Migration:
        plan = self.plan(uuid, node, copy_storage)
        with self._lock:
            if uuid in self._moving:
                raise InvalidState(f"VM {plan.vm.spec.name!r} is already being migrated")
            self._moving.add(uuid)
            job = self._new(plan.vm, plan.source.name, plan.target.name, plan.copy_storage)
        self._executor.submit(self._run, job, plan, bandwidth_mib)
        return job

    def evacuate(self, node: str) -> list[Migration]:
        """Move every VM off a node before maintenance. VMs that cannot move get a failed
        job saying why, so nothing is skipped silently."""
        source = self._cluster.member(node)
        jobs = []
        for vm in source.driver.list_vms():
            try:
                jobs.append(self.start(vm.uuid))
            except (InvalidState, NodeUnavailable) as exc:
                with self._lock:
                    job = self._new(vm, node, None, False)
                job.state, job.error, job.finished = "failed", str(exc), self._now()
                jobs.append(job)
        return jobs

    def _new(self, vm: VMInfo, source: str, target: str | None, copy: bool) -> Migration:
        job = Migration(
            id=str(next(self._ids)),
            vm=vm.spec.name,
            uuid=vm.uuid,
            source=source,
            target=target,
            copy_storage=copy,
            live=vm.state != VMState.STOPPED,
            created=self._now(),
        )
        self._jobs.append(job)
        return job

    def _run(self, job: Migration, plan: Plan, bandwidth_mib: int | None) -> None:
        job.state = "running"
        try:
            plan.source.driver.migrate_vm(
                job.uuid, self.uri(plan.target), plan.copy_storage, bandwidth_mib
            )
        except Exception as exc:  # the job records every failure; nothing else is waiting
            job.state, job.error = "failed", str(exc) or type(exc).__name__
        else:
            job.state = "done"
        finally:
            job.finished = self._now()
            with self._lock:
                self._moving.discard(job.uuid)

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")


def _shared(src: PoolInfo | None, dst: PoolInfo) -> bool:
    """Both nodes see the same storage: a network or SAN pool with the same source."""
    return (
        src is not None
        and src.type != PoolType.DIR
        and src.type == dst.type
        and src.source == dst.source
    )
