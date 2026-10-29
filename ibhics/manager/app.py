"""ibHiCS Manager: REST API and web console for a single node (phase 1)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..hypervisor import HypervisorDriver, InvalidState, VMExists, VMNotFound
from .schemas import HostOut, StopRequest, VMCreate, VMOut

STATIC_DIR = Path(__file__).parent / "static"


def create_app(driver: HypervisorDriver) -> FastAPI:
    app = FastAPI(title="ibHiCS Manager", version=__version__)
    app.state.driver = driver

    @app.exception_handler(VMNotFound)
    async def _not_found(_: Request, exc: VMNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"VM {exc} not found"})

    @app.exception_handler(VMExists)
    async def _exists(_: Request, exc: VMExists) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(InvalidState)
    async def _invalid_state(_: Request, exc: InvalidState) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    api = APIRouter(prefix="/api/v1")

    @api.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @api.get("/host", response_model=HostOut)
    def host() -> HostOut:
        return HostOut.from_info(driver.host_info())

    @api.get("/vms", response_model=list[VMOut])
    def list_vms() -> list[VMOut]:
        return [VMOut.from_info(vm) for vm in driver.list_vms()]

    @api.post("/vms", response_model=VMOut, status_code=status.HTTP_201_CREATED)
    def create_vm(body: VMCreate) -> VMOut:
        try:
            return VMOut.from_info(driver.create_vm(body.to_spec()))
        except (VMExists, InvalidState):
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @api.get("/vms/{uuid}", response_model=VMOut)
    def get_vm(uuid: str) -> VMOut:
        return VMOut.from_info(driver.get_vm(uuid))

    @api.post("/vms/{uuid}/start", response_model=VMOut)
    def start_vm(uuid: str) -> VMOut:
        return VMOut.from_info(driver.start_vm(uuid))

    @api.post("/vms/{uuid}/stop", response_model=VMOut)
    def stop_vm(uuid: str, body: StopRequest | None = None) -> VMOut:
        return VMOut.from_info(driver.stop_vm(uuid, force=bool(body and body.force)))

    @api.delete("/vms/{uuid}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_vm(uuid: str) -> None:
        driver.delete_vm(uuid)

    app.include_router(api)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="console")
    return app
