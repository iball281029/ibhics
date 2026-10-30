"""ibHiCS Migrate in the manager: sources, what is on them, and import jobs."""

from __future__ import annotations

import itertools
import json
import os
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..cluster import Cluster
from ..hypervisor import VMExists
from ..hypervisor.base import BLOCK_POOL_TYPES
from ..importer import Source, SourceNotFound, SourceType, SourceVM, list_vms
from ..importer.sources import IMAGE_SUFFIXES


@dataclass
class ImportJob:
    id: str
    source: str
    vm: str  # the source VM's ref
    name: str  # the VM's name in ibHiCS
    node: str
    created: str
    state: str = "queued"  # queued, running, done, failed
    finished: str | None = None
    result: str | None = None  # the new VM's uuid
    error: str | None = None
    step: str = ""  # what the conversion is doing now
    percent: int | None = None


class NotEnoughSpace(ValueError):
    pass


def check_space(member, pool: str, vm: SourceVM) -> None:
    """Refuse an import that cannot fit: the VM's disks may grow to their full size, and
    the converted images wait on the same pool first. On an LVM pool that waiting room is a
    logical volume of the full size; on a directory pool the images are sparse.
    Sizes the source does not report (0) are not counted."""
    disks = sum(vm.disks_gb)
    if not disks:
        return
    info = next((p for p in member.driver.list_pools() if p.name == pool), None)
    if info is None:
        return
    needed = disks * 2 if info.type in BLOCK_POOL_TYPES else disks
    if info.available_gb >= needed:
        return
    room = f"{disks} GB of disks"
    if needed > disks:
        room = f"{needed} GB ({room} plus room to convert them)"
    raise NotEnoughSpace(
        f"{vm.name} needs {room}, but storage pool {pool!r} on {member.name} has "
        f"only {info.available_gb:.0f} GB free. Pick a larger pool (for example an NFS or "
        f"iSCSI pool under Storage) or free up space."
    )


UPLOAD_DIR = Path("/var/lib/ibhics/uploads")
UPLOADS = "uploads"
UPLOAD_SUFFIXES = (".ova", *IMAGE_SUFFIXES)


def upload_path(folder: Path, filename: str) -> Path:
    """Where an uploaded file goes; refuses names that could leave the folder."""
    name = Path(filename).name
    if name != filename or name.startswith(".") or not name:
        raise ValueError("file name must not contain a path")
    if Path(name).suffix.lower() not in UPLOAD_SUFFIXES:
        raise ValueError(f"upload an OVA or a disk image ({', '.join(UPLOAD_SUFFIXES)})")
    return folder / name


class ImportManager:
    def __init__(
        self,
        cluster: Cluster,
        state_path: Path | None = None,
        discover: Callable[[Source], list[SourceVM]] = list_vms,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        executor: Executor | None = None,
        upload_dir: Path = UPLOAD_DIR,
    ) -> None:
        self._cluster = cluster
        self.upload_dir = Path(upload_dir)
        self._state_path = state_path
        self._discover = discover
        self._clock = clock
        # Conversions are disk- and CPU-heavy; two at a time across the cluster.
        self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="import")
        self._sources: dict[str, Source] = {}
        self._jobs: deque[ImportJob] = deque(maxlen=200)
        self._ids = itertools.count(1)
        self._lock = threading.RLock()

    # Sources

    @property
    def uploads_source(self) -> Source:
        return Source(UPLOADS, SourceType.UPLOAD, path=str(self.upload_dir))

    def sources(self) -> list[Source]:
        with self._lock:
            return [self.uploads_source, *self._sources.values()]

    def source(self, name: str) -> Source:
        if name == UPLOADS:
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            return self.uploads_source
        with self._lock:
            try:
                return self._sources[name]
            except KeyError:
                raise SourceNotFound(name) from None

    def add_source(self, source: Source) -> Source:
        source.validate()
        if source.type == SourceType.UPLOAD:
            raise ValueError("uploaded files have their own built-in source")
        with self._lock:
            if source.name in self._sources or source.name == UPLOADS:
                raise VMExists(f"a migrate source named {source.name!r} already exists")
            self._sources[source.name] = source
            self._save()
        return source

    def remove_source(self, name: str) -> None:
        if name == UPLOADS:
            raise ValueError("the uploads source is built in; delete the files instead")
        with self._lock:
            self.source(name)
            del self._sources[name]
            self._save()

    def source_vms(self, name: str) -> list[SourceVM]:
        """What is on a source. Folders are read on the manager's node, which mounts the
        same share as the node that will run the conversion."""
        return self._discover(self.source(name))

    # Jobs

    def jobs(self) -> list[ImportJob]:
        with self._lock:
            return list(reversed(self._jobs))

    def start(
        self,
        source_name: str,
        vm_ref: str,
        name: str | None = None,
        node: str | None = None,
        pool: str = "default",
        network: str | list[str] = "default",
        vcpus: int | None = None,
        memory_mb: int | None = None,
        start: bool = False,
    ) -> ImportJob:
        self._cluster.require_quorum()
        source = self.source(source_name)
        vm = next((v for v in self._discover(source) if v.ref == vm_ref), None)
        if vm is None:
            raise SourceNotFound(f"{source_name}: no VM {vm_ref!r}")
        name = name or vm.name
        with self._lock:
            taken = {v.spec.name for _, v in self._cluster.all_vms()}
            taken |= {j.name for j in self._jobs if j.state in ("queued", "running")}
            if name in taken:
                raise VMExists(f"a VM named {name!r} already exists; choose another name")
            if source.type == SourceType.UPLOAD:
                # Uploaded files are on the manager's own disk, so it converts them.
                member = next((m for m in self._cluster.compute_members() if m.local), None)
                if member is None or (node and node != member.name):
                    raise ValueError(
                        "uploaded files are imported on the node that runs the web console"
                    )
            else:
                member = (
                    self._cluster.member(node)
                    if node
                    else self._cluster.pick_node(memory_mb or vm.memory_mb)
                )
            check_space(member, pool, vm)
            job = ImportJob(
                id=str(next(self._ids)),
                source=source_name,
                vm=vm_ref,
                name=name,
                node=member.name,
                created=self._now(),
            )
            self._jobs.append(job)

        def run() -> None:
            job.state = "running"

            def progress(step: str, percent: int | None) -> None:
                job.step, job.percent = step, percent

            try:
                info = member.imports.run(
                    source, vm, name, pool, network, vcpus, memory_mb, start, progress=progress
                )
                job.result, job.state, job.step, job.percent = info.uuid, "done", "", 100
            except Exception as exc:  # recorded on the job; nobody else is waiting
                job.error, job.state = f"{type(exc).__name__}: {exc}", "failed"
            job.finished = self._now()

        self._executor.submit(run)
        return job

    def job_log(self, job_id: str) -> str:
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job is None:
            raise SourceNotFound(f"no import job {job_id}")
        return self._cluster.member(job.node).imports.log(job.name)

    # Persistence: VMware passwords are kept, so the file is readable by root only.

    def load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        data = json.loads(self._state_path.read_text())
        with self._lock:
            for entry in data["sources"]:
                source = Source.from_dict(entry)
                self._sources[source.name] = source

    def _save(self) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"sources": [s.to_dict() for s in self._sources.values()]}, f, indent=2)
        tmp.replace(self._state_path)

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")


__all__ = ["ImportJob", "ImportManager", "SourceType"]
