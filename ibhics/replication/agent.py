"""Cluster-token protected node replication protocol; no credentials are persisted."""

from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

from ..cluster import Cluster
from ..hypervisor import InvalidState
from ..importer import Source, SourceVM
from .runtime import LocalRuntime
from .store import operation_lock


def install(api, name, driver, root: Path, guard=Path("/run/lock/ibhics-replication.lock")):
    runtime = LocalRuntime(Cluster.standalone(name, driver), root)
    progress: dict[str, tuple[int, int]] = {}  # running syncs, read by the manager
    stops: set[str] = set()

    def report(job_id):
        def update(done, total):
            if job_id in stops:
                raise RuntimeError("Replication stopped by the operator")
            progress[job_id] = (done, total)

        return update

    @api.post("/replications/{job_id}/{action}")
    def execute(job_id: str, action: str, body: dict):
        import re

        from ..manager.replications import ReplicationJob

        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(422, "Invalid replication id")
        try:
            job = replace(ReplicationJob(**body["job"]), node=name, via="")
        except (KeyError, TypeError):
            raise HTTPException(422, "Invalid replication job") from None
        if job.id != job_id:
            raise HTTPException(422, "Replication id mismatch")
        if action == "progress":
            done, total = progress.get(job_id, (0, 0))
            return {"done": done, "total": total, "running": job_id in progress}
        if action == "stop":
            stops.add(job_id)
            return {"ok": True}
        if action == "validate":
            runtime.store(job)
            return {"supported": True}
        if action == "manifest":
            return runtime.store(job).current()
        if action == "ownership":
            return runtime.ownership(job)
        with operation_lock(guard, shared=True):
            try:
                if action == "sync":
                    source = Source.from_dict(body["source"])
                    vm = SourceVM.from_dict(job.source_vm)
                    stops.discard(job_id)
                    progress[job_id] = (0, 0)
                    try:
                        return runtime.sync(job, source, vm, report(job_id))
                    finally:
                        progress.pop(job_id, None)
                        stops.discard(job_id)
                if action == "materialize":
                    if body.get("test") is False and body.get("source_powered_off") is not True:
                        raise ValueError("source_powered_off: true is required for cutover")
                    if body.get("manifest"):
                        runtime.adopt_manifest(job, body["manifest"])
                    vm = SourceVM.from_dict(job.source_vm)
                    # Conversion is entirely local and never uses original credentials.
                    source = Source(job.source, "kvm")
                    result = runtime.materialize(
                        job, source, vm, test=body.get("test", True), networks=body.get("networks")
                    )
                    return {"uuid": result}
                if action == "test-cleanup":
                    runtime.cleanup_test(job)
                    return {"ok": True}
                if action == "delete":
                    runtime.delete(job)
                    return {"ok": True}
            except Exception:
                # Agent clients and access logs must never echo SSH output or request secrets.
                raise InvalidState("Replication operation failed on this node") from None
        raise HTTPException(404, "Unknown replication operation")
