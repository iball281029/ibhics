"""ibHiCS Agent: runs on every node and exposes that node's hypervisor to the manager."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, status

from .. import __version__
from ..backup import BackupTarget, NodeBackup
from ..cluster.topology import NodeRole
from ..hypervisor import HypervisorDriver
from ..manager.errors import install_driver_errors
from ..manager.schemas import (
    AgentBackupDelete,
    AgentBackupList,
    AgentBackupRun,
    AgentPrune,
    AgentRestore,
    BackupOut,
    HostOut,
    PoolCreate,
    PoolOut,
    StopRequest,
    VMCreate,
    VMOut,
)


def create_agent_app(
    name: str,
    role: NodeRole,
    token: str,
    driver: HypervisorDriver | None = None,
    backups: NodeBackup | None = None,
) -> FastAPI:
    if not token:
        raise ValueError("an agent needs a cluster token")
    if role == NodeRole.COMPUTE and driver is None:
        raise ValueError("a compute agent needs a hypervisor driver")

    def check_token(x_ibhics_token: str = Header(default="")) -> None:
        if not hmac.compare_digest(x_ibhics_token.encode(), token.encode()):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")

    app = FastAPI(title=f"ibHiCS Agent ({name})", version=__version__)
    install_driver_errors(app)
    api = APIRouter(prefix="/agent/v1", dependencies=[Depends(check_token)])

    @api.get("/health")
    def health() -> dict:
        return {"name": name, "role": str(role), "version": __version__}

    if role == NodeRole.COMPUTE:

        @api.get("/host", response_model=HostOut)
        def host() -> HostOut:
            return HostOut.from_info(driver.host_info())

        @api.get("/vms", response_model=list[VMOut])
        def list_vms() -> list[VMOut]:
            return [VMOut.from_info(vm) for vm in driver.list_vms()]

        @api.post("/vms", response_model=VMOut, status_code=status.HTTP_201_CREATED)
        def create_vm(body: VMCreate) -> VMOut:
            return VMOut.from_info(driver.create_vm(body.to_spec()))

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

        @api.get("/pools", response_model=list[PoolOut])
        def list_pools() -> list[PoolOut]:
            return [PoolOut.from_info(p) for p in driver.list_pools()]

        @api.post("/pools", response_model=PoolOut, status_code=status.HTTP_201_CREATED)
        def create_pool(body: PoolCreate) -> PoolOut:
            return PoolOut.from_info(driver.create_pool(body.to_spec()))

        @api.delete("/pools/{name}", status_code=status.HTTP_204_NO_CONTENT)
        def delete_pool(name: str) -> None:
            driver.delete_pool(name)

        backups = backups or NodeBackup(driver)

        def target(data: dict) -> BackupTarget:
            try:
                return BackupTarget.from_dict(data)
            except (KeyError, TypeError) as exc:
                raise ValueError(f"bad backup target: {exc}") from None

        # Backup jobs are long; they run in FastAPI's worker threads, one request each.

        @api.post("/backups/run", response_model=BackupOut)
        def run_backup(body: AgentBackupRun) -> BackupOut:
            return BackupOut.from_info(backups.backup(body.uuid, target(body.target)))

        @api.post("/backups/list", response_model=list[BackupOut])
        def list_backups(body: AgentBackupList) -> list[BackupOut]:
            return [BackupOut.from_info(b) for b in backups.list(target(body.target), body.vm_name)]

        @api.post("/backups/restore", response_model=VMOut)
        def restore(body: AgentRestore) -> VMOut:
            vm = backups.restore(target(body.target), body.backup_id, body.name, body.pool)
            return VMOut.from_info(vm)

        @api.post("/backups/delete")
        def delete_backup(body: AgentBackupDelete) -> dict:
            return {"freed_chunks": backups.delete(target(body.target), body.backup_id)}

        @api.post("/backups/prune")
        def prune(body: AgentPrune) -> dict:
            return {"removed": backups.prune(target(body.target), body.vm_name, body.keep_last)}

    app.include_router(api)
    return app
