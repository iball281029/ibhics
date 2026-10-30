"""ibHiCS Agent: runs on every node and exposes that node's hypervisor to the manager."""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import asdict

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, WebSocket, status

from .. import __version__, console
from ..backup import BackupTarget, NodeBackup
from ..cluster.remote import TOKEN_HEADER
from ..cluster.topology import NodeRole
from ..hypervisor import HypervisorDriver, InvalidState, VMNotFound
from ..hypervisor.serialize import guest_to_json
from ..importer import NodeImport, Source, SourceVM
from ..manager.errors import install_driver_errors
from ..manager.schemas import (
    AgentBackupDelete,
    AgentBackupList,
    AgentBackupRun,
    AgentImport,
    AgentMigrate,
    AgentPrune,
    AgentRestore,
    BackupOut,
    CdromRequest,
    HostNetworkIn,
    HostOut,
    HostSettingsIn,
    NetworkIn,
    PoolCreate,
    PoolOut,
    StopRequest,
    VMCreate,
    VMEditIn,
    VMOut,
)


def create_agent_app(
    name: str,
    role: NodeRole,
    token: str,
    driver: HypervisorDriver | None = None,
    backups: NodeBackup | None = None,
    imports: NodeImport | None = None,
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

        # The node itself: Nodes > node > Summary, Network, Settings.

        @api.get("/host/summary")
        def host_summary() -> dict:
            return asdict(driver.host_system().summary())

        @api.get("/host/nics")
        def host_nics() -> list[dict]:
            return [asdict(n) for n in driver.host_system().nics()]

        @api.get("/host/network")
        def host_network() -> dict:
            return asdict(driver.host_system().network())

        @api.put("/host/network")
        def apply_host_network(body: HostNetworkIn) -> dict:
            return asdict(driver.host_system().apply_network(body.to_network(), name))

        @api.get("/host/network/state")
        def host_network_state() -> dict:
            return asdict(driver.host_system().network_state())

        @api.post("/host/network/confirm")
        def confirm_host_network() -> dict:
            return asdict(driver.host_system().confirm_network())

        @api.post("/host/network/undo")
        def undo_host_network() -> dict:
            return asdict(driver.host_system().undo_network())

        @api.get("/host/settings")
        def host_settings() -> dict:
            return asdict(driver.host_system().settings())

        @api.put("/host/settings")
        def update_host_settings(body: HostSettingsIn) -> dict:
            return asdict(driver.host_system().update_settings(body.to_edit()))

        @api.get("/host/timezones")
        def host_timezones() -> list[str]:
            return driver.host_system().timezones()

        @api.post("/host/power/{action}", status_code=status.HTTP_202_ACCEPTED)
        def host_power(action: str) -> None:
            driver.host_system().power(action)

        @api.get("/disks")
        def disks() -> list[dict]:
            return [asdict(d) for d in driver.list_local_disks()]

        @api.get("/networks")
        def networks() -> list[dict]:
            return [asdict(n) for n in driver.list_networks()]

        @api.post("/networks", status_code=status.HTTP_201_CREATED)
        def create_network(body: NetworkIn) -> dict:
            return asdict(driver.create_network(body.to_spec()))

        @api.delete("/networks/{name}", status_code=status.HTTP_204_NO_CONTENT)
        def delete_network(name: str) -> None:
            driver.delete_network(name)

        @api.get("/guests")
        def guests() -> dict:
            return {uuid: guest_to_json(g) for uuid, g in driver.list_guest_info().items()}

        @api.get("/vms/{uuid}/guest")
        def guest(uuid: str) -> dict:
            return guest_to_json(driver.guest_info(uuid))

        @api.get("/vms/{uuid}", response_model=VMOut)
        def get_vm(uuid: str) -> VMOut:
            return VMOut.from_info(driver.get_vm(uuid))

        @api.post("/vms/{uuid}/cdrom", response_model=VMOut)
        def insert_cdrom(uuid: str, body: CdromRequest) -> VMOut:
            return VMOut.from_info(driver.insert_cdrom(uuid, body.iso))

        @api.patch("/vms/{uuid}", response_model=VMOut)
        def update_vm(uuid: str, body: VMEditIn) -> VMOut:
            return VMOut.from_info(driver.update_vm(uuid, body.to_edit()))

        @api.post("/vms/{uuid}/start", response_model=VMOut)
        def start_vm(uuid: str) -> VMOut:
            return VMOut.from_info(driver.start_vm(uuid))

        @api.post("/vms/{uuid}/stop", response_model=VMOut)
        def stop_vm(uuid: str, body: StopRequest | None = None) -> VMOut:
            return VMOut.from_info(driver.stop_vm(uuid, force=bool(body and body.force)))

        @api.delete("/vms/{uuid}", status_code=status.HTTP_204_NO_CONTENT)
        def delete_vm(uuid: str) -> None:
            driver.delete_vm(uuid)

        # Runs until the VM has moved, which can take minutes for a busy guest.
        @api.post("/vms/{uuid}/migrate", status_code=status.HTTP_204_NO_CONTENT)
        def migrate_vm(uuid: str, body: AgentMigrate) -> None:
            driver.migrate_vm(uuid, body.dest_uri, body.copy_storage, body.bandwidth_mib)

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

        imports = imports or NodeImport(driver)

        @api.get("/imports/progress")
        def import_progress() -> dict:
            return {name: list(p) for name, p in imports.progress.items()}

        @api.get("/imports/{name}/log")
        def import_log(name: str) -> dict:
            return {"log": imports.log(name)}

        @api.post("/imports/run", response_model=VMOut)
        def run_import(body: AgentImport) -> VMOut:
            try:
                source, vm = Source.from_dict(body.source), SourceVM.from_dict(body.vm)
                source.validate()
            except (KeyError, TypeError) as exc:
                raise ValueError(f"bad import request: {exc}") from None
            info = imports.run(
                source,
                vm,
                body.name,
                body.pool,
                body.network,
                body.vcpus,
                body.memory_mb,
                body.start,
            )
            return VMOut.from_info(info)

        @app.websocket("/agent/v1/vms/{uuid}/console")
        async def vm_console(ws: WebSocket, uuid: str) -> None:
            sent = ws.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(sent.encode(), token.encode()):
                await ws.close(code=1008)
                return
            await console.accept(ws)
            try:
                host, port = await asyncio.to_thread(driver.console_address, uuid)
                await console.bridge_tcp(ws, host, port)
            except VMNotFound:
                await console.refuse(ws, "VM not found on this node")
            except (InvalidState, OSError) as exc:
                await console.refuse(ws, str(exc))

    app.include_router(api)
    return app
