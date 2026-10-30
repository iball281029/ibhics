"""Map domain errors to HTTP responses for the manager and agent APIs."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..backup import BackupNotFound, RepositoryBusy, TargetUnavailable
from ..hypervisor import InvalidState, MigrationFailed, PoolNotFound, VMExists, VMNotFound


def _handler(status: int, prefix: str = ""):
    async def handle(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": f"{prefix}{exc}"})

    return handle


def install_driver_errors(app: FastAPI) -> None:
    app.add_exception_handler(VMNotFound, _handler(404, "VM not found: "))
    app.add_exception_handler(PoolNotFound, _handler(404, "storage pool not found: "))
    app.add_exception_handler(BackupNotFound, _handler(404, "backup not found: "))
    app.add_exception_handler(VMExists, _handler(409))
    app.add_exception_handler(RepositoryBusy, _handler(409))
    app.add_exception_handler(TargetUnavailable, _handler(502, "backup target unavailable: "))
    app.add_exception_handler(InvalidState, _handler(409))
    app.add_exception_handler(MigrationFailed, _handler(502, "migration failed: "))

    from ..importer import ConversionFailed, SourceNotFound, SourceUnavailable

    app.add_exception_handler(ConversionFailed, _handler(502, "conversion failed: "))
    app.add_exception_handler(SourceUnavailable, _handler(502, "source unavailable: "))
    app.add_exception_handler(SourceNotFound, _handler(404, "migrate source not found: "))
    # Validation the driver does beyond the request schema.
    app.add_exception_handler(ValueError, _handler(422))


def install_cluster_errors(app: FastAPI) -> None:
    from ..cluster import MembershipError, NodeUnavailable, NoQuorum
    from .auth import AuthError, Forbidden, UserNotFound
    from .backups import PolicyNotFound, TargetNotFound

    app.add_exception_handler(AuthError, _handler(401))
    app.add_exception_handler(Forbidden, _handler(403))
    app.add_exception_handler(UserNotFound, _handler(404, "user not found: "))

    app.add_exception_handler(TargetNotFound, _handler(404, "backup target not found: "))
    app.add_exception_handler(PolicyNotFound, _handler(404, "backup policy not found: "))
    app.add_exception_handler(NoQuorum, _handler(503))
    app.add_exception_handler(NodeUnavailable, _handler(503))
    app.add_exception_handler(MembershipError, _handler(400))
