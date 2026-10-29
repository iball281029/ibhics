"""Map domain errors to HTTP responses for the manager and agent APIs."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..hypervisor import InvalidState, PoolNotFound, VMExists, VMNotFound


def _handler(status: int, prefix: str = ""):
    async def handle(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": f"{prefix}{exc}"})

    return handle


def install_driver_errors(app: FastAPI) -> None:
    app.add_exception_handler(VMNotFound, _handler(404, "VM not found: "))
    app.add_exception_handler(PoolNotFound, _handler(404, "storage pool not found: "))
    app.add_exception_handler(VMExists, _handler(409))
    app.add_exception_handler(InvalidState, _handler(409))
    # Validation the driver does beyond the request schema.
    app.add_exception_handler(ValueError, _handler(422))


def install_cluster_errors(app: FastAPI) -> None:
    from ..cluster import MembershipError, NodeUnavailable, NoQuorum

    app.add_exception_handler(NoQuorum, _handler(503))
    app.add_exception_handler(NodeUnavailable, _handler(503))
    app.add_exception_handler(MembershipError, _handler(400))
