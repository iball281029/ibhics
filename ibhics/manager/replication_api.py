"""Replication API; explicit source shutdown acknowledgement is mandatory."""

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..replication.providers import capability
from .schemas import ImportRequest


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_minutes: int | None = Field(default=None, ge=1, le=525600, strict=True)
    at: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ReplicationRequest(ImportRequest):
    model_config = ConfigDict(extra="forbid")
    schedule: Schedule = Field(default_factory=lambda: Schedule(interval_minutes=15))


class ReplicationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule: Schedule | None = None
    paused: StrictBool | None = None


class CutoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_powered_off: StrictBool


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    networks: list[str] = Field(default_factory=list)


def install(api: APIRouter, manager) -> None:
    @api.get("/migrate/sources/{name}/replication-capability")
    def source_capability(name: str):
        source = (
            manager.imports.uploads_source if name == "uploads" else manager.imports.source(name)
        )
        return capability(source)

    @api.get("/migrate/replications")
    def jobs():
        return manager.jobs()

    @api.post("/migrate/replications", status_code=status.HTTP_202_ACCEPTED)
    def create(body: ReplicationRequest):
        return manager.view(manager.create(body.model_dump(exclude_none=True)))

    @api.get("/migrate/replications/{job_id}")
    def get(job_id: str):
        return manager.view(manager.job(job_id))

    @api.patch("/migrate/replications/{job_id}")
    def update(job_id: str, body: ReplicationPatch):
        return manager.view(
            manager.update(
                job_id,
                schedule=body.schedule.model_dump(exclude_none=True)
                if body.schedule is not None
                else None,
                paused=body.paused,
            )
        )

    @api.delete("/migrate/replications/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete(job_id: str, confirm: bool = Query(default=False)):
        manager.delete(job_id, confirm)

    @api.post("/migrate/replications/{job_id}/sync", status_code=status.HTTP_202_ACCEPTED)
    def sync(job_id: str):
        return manager.view(manager.sync(job_id))

    @api.post("/migrate/replications/{job_id}/cutover", status_code=status.HTTP_202_ACCEPTED)
    def cutover(job_id: str, body: CutoverRequest):
        if type(body.source_powered_off) is not bool:
            raise HTTPException(422, "source_powered_off must be true")
        return manager.view(manager.cutover(job_id, body.source_powered_off))

    @api.post("/migrate/replications/{job_id}/test-failover", status_code=status.HTTP_202_ACCEPTED)
    def failover(job_id: str, body: TestRequest):
        return manager.view(manager.test_failover(job_id, body.networks))

    @api.post("/migrate/replications/{job_id}/test-cleanup", status_code=status.HTTP_202_ACCEPTED)
    def cleanup(job_id: str):
        return manager.view(manager.cleanup_test(job_id))
