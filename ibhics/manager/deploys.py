"""Deploying VMs from templates (and cloning VMs) as background jobs: a full clone copies
every disk on the node, which takes minutes for large ones."""

from __future__ import annotations

import itertools
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from ..cluster import Cluster
from ..hypervisor import CloneSpec, InvalidState, VMExists, VMInfo, VMState


@dataclass
class Deploy:
    id: str
    vm: str  # the new VM's name
    source: str
    source_uuid: str
    node: str
    linked: bool
    customized: bool
    power_on: bool
    state: str  # queued, running, done, failed
    created: str
    finished: str | None = None
    error: str | None = None
    result: str | None = None  # the new VM's uuid


class DeployManager:
    def __init__(
        self,
        cluster: Cluster,
        on_done: Callable[[Deploy, VMInfo], None] = lambda _job, _vm: None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        executor: Executor | None = None,
    ) -> None:
        self._cluster = cluster
        self.on_done = on_done
        self._clock = clock
        self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="deploy")
        self._jobs: deque[Deploy] = deque(maxlen=200)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def jobs(self) -> list[Deploy]:
        with self._lock:
            return list(reversed(self._jobs))

    def busy(self, uuid: str) -> bool:
        """A deploy is copying from this VM or template right now."""
        with self._lock:
            return any(
                j.source_uuid == uuid and j.state in ("queued", "running") for j in self._jobs
            )

    def start(self, uuid: str, clone: CloneSpec, power_on: bool = False) -> Deploy:
        self._cluster.require_quorum()
        clone.validate()
        member, source = self._cluster.find_vm(uuid)
        if source.state != VMState.STOPPED:
            raise InvalidState(f"shut {source.spec.name!r} down before cloning it")
        if clone.linked and not source.spec.template:
            raise InvalidState("linked clones are made from templates: convert the VM first")
        with self._lock:
            pending = {j.vm for j in self._jobs if j.state in ("queued", "running")}
            if clone.name in pending or any(
                vm.spec.name == clone.name for _, vm in self._cluster.all_vms()
            ):
                raise VMExists(f"VM named {clone.name!r} already exists in the cluster")
            job = Deploy(
                id=str(next(self._ids)),
                vm=clone.name,
                source=source.spec.name,
                source_uuid=uuid,
                node=member.name,
                linked=clone.linked,
                customized=clone.customization is not None,
                power_on=power_on,
                state="queued",
                created=self._now(),
            )
            self._jobs.append(job)
        self._executor.submit(self._run, job, clone)
        return job

    def _run(self, job: Deploy, clone: CloneSpec) -> None:
        job.state = "running"
        try:
            member = self._cluster.member(job.node)
            vm = member.driver.clone_vm(job.source_uuid, clone)
            job.result = vm.uuid
            self.on_done(job, vm)
            if job.power_on:
                try:
                    member.driver.start_vm(vm.uuid)
                except Exception as exc:
                    raise RuntimeError(f"deployed, but it did not power on: {exc}") from exc
        except Exception as exc:  # the job records every failure; nothing else is waiting
            job.state, job.error = "failed", str(exc) or type(exc).__name__
        else:
            job.state = "done"
        finally:
            job.finished = self._now()

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")
