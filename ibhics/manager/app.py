"""ibHiCS Manager: REST API and web console for a standalone node or a cluster."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from pathlib import Path

from fastapi import APIRouter, FastAPI, WebSocket, status
from fastapi.staticfiles import StaticFiles

from .. import __version__, console
from ..cluster import Cluster, NodeUnavailable, Topology
from ..hypervisor import HypervisorDriver, InvalidState, VMExists, VMNotFound, VMState
from .backups import BackupManager, Policy
from .errors import install_cluster_errors, install_driver_errors
from .schemas import (
    BackupOut,
    BackupRequest,
    BackupTargetIn,
    BackupTargetOut,
    ClusterOut,
    HostOut,
    JobOut,
    MemberOut,
    NodeJoin,
    PolicyIn,
    PolicyOut,
    PoolCreate,
    PoolOut,
    RestoreRequest,
    StopRequest,
    VMCreate,
    VMOut,
)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    cluster: Cluster | HypervisorDriver,
    heartbeat_interval: float = 0,
    backups: BackupManager | None = None,
    scheduler_interval: float = 0,
) -> FastAPI:
    if isinstance(cluster, HypervisorDriver):
        cluster = Cluster.standalone(socket.gethostname(), cluster)
    backups = backups or BackupManager(cluster)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if heartbeat_interval > 0:

            async def beat() -> None:
                while True:
                    await asyncio.to_thread(cluster.refresh)
                    await asyncio.sleep(heartbeat_interval)

            task = asyncio.create_task(beat())
        scheduler = None
        if scheduler_interval > 0:

            async def schedule() -> None:
                while True:
                    await asyncio.to_thread(backups.tick)
                    await asyncio.sleep(scheduler_interval)

            scheduler = asyncio.create_task(schedule())
        yield
        for t in (task, scheduler):
            if t:
                t.cancel()

    app = FastAPI(title="ibHiCS Manager", version=__version__, lifespan=lifespan)
    app.state.cluster = cluster
    app.state.backups = backups
    install_driver_errors(app)
    install_cluster_errors(app)

    api = APIRouter(prefix="/api/v1")

    @api.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    # Cluster

    @api.get("/cluster", response_model=ClusterOut)
    def cluster_status() -> ClusterOut:
        topology = cluster.topology()
        return ClusterOut(
            topology=topology,
            quorum=cluster.has_quorum(),
            ha_capable=topology in (Topology.TWO_NODE_WITNESS, Topology.CLUSTER),
            members=[
                MemberOut(
                    name=m.name,
                    role=m.role,
                    address=m.address,
                    local=m.local,
                    healthy=cluster.is_healthy(m),
                    last_error=m.last_error,
                )
                for m in cluster.members()
            ],
        )

    @api.post("/cluster/nodes", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
    def join_node(body: NodeJoin) -> MemberOut:
        member = cluster.join(body.name, body.role, body.address)
        return MemberOut(
            name=member.name,
            role=member.role,
            address=member.address,
            local=member.local,
            healthy=cluster.is_healthy(member),
            last_error=None,
        )

    @api.delete("/cluster/nodes/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def remove_node(name: str) -> None:
        cluster.remove(name)

    @api.get("/nodes/{name}/host", response_model=HostOut)
    def node_host(name: str) -> HostOut:
        return HostOut.from_info(cluster.member(name).driver.host_info())

    # Storage pools live on each node. Shared storage (NFS, a shared LUN) is added
    # to every node that should reach it, under the same pool name.

    @api.get("/pools", response_model=list[PoolOut])
    def list_pools() -> list[PoolOut]:
        result = []
        for member in cluster.compute_members():
            try:
                pools = member.driver.list_pools()
            except NodeUnavailable as exc:
                member.last_error = str(exc)
                continue
            result.extend(PoolOut.from_info(pool, member.name) for pool in pools)
        return result

    @api.post("/nodes/{name}/pools", response_model=PoolOut, status_code=status.HTTP_201_CREATED)
    def create_pool(name: str, body: PoolCreate) -> PoolOut:
        cluster.require_quorum()
        member = cluster.member(name)
        return PoolOut.from_info(member.driver.create_pool(body.to_spec()), member.name)

    @api.delete("/nodes/{name}/pools/{pool}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_pool(name: str, pool: str) -> None:
        cluster.require_quorum()
        cluster.member(name).driver.delete_pool(pool)

    # VMs: reads work without quorum so operators can see what is going on;
    # anything that changes state needs a majority of nodes.

    @api.get("/vms", response_model=list[VMOut])
    def list_vms() -> list[VMOut]:
        return [VMOut.from_info(vm, member.name) for member, vm in cluster.all_vms()]

    @api.post("/vms", response_model=VMOut, status_code=status.HTTP_201_CREATED)
    def create_vm(body: VMCreate) -> VMOut:
        cluster.require_quorum()
        if any(vm.spec.name == body.name for _, vm in cluster.all_vms()):
            raise VMExists(f"VM named {body.name!r} already exists in the cluster")
        member = cluster.member(body.node) if body.node else cluster.pick_node(body.memory_mb)
        return VMOut.from_info(member.driver.create_vm(body.to_spec()), member.name)

    @api.get("/vms/{uuid}", response_model=VMOut)
    def get_vm(uuid: str) -> VMOut:
        member, vm = cluster.find_vm(uuid)
        return VMOut.from_info(vm, member.name)

    @api.post("/vms/{uuid}/start", response_model=VMOut)
    def start_vm(uuid: str) -> VMOut:
        cluster.require_quorum()
        member, _ = cluster.find_vm(uuid)
        return VMOut.from_info(member.driver.start_vm(uuid), member.name)

    @api.post("/vms/{uuid}/stop", response_model=VMOut)
    def stop_vm(uuid: str, body: StopRequest | None = None) -> VMOut:
        cluster.require_quorum()
        member, _ = cluster.find_vm(uuid)
        vm = member.driver.stop_vm(uuid, force=bool(body and body.force))
        return VMOut.from_info(vm, member.name)

    @api.delete("/vms/{uuid}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_vm(uuid: str) -> None:
        cluster.require_quorum()
        member, _ = cluster.find_vm(uuid)
        member.driver.delete_vm(uuid)

    # VM console: the browser's noVNC WebSocket is relayed to the VM's VNC display, on this
    # node directly or through the agent of the VM's node.

    @app.websocket("/api/v1/vms/{uuid}/console")
    async def vm_console(ws: WebSocket, uuid: str) -> None:
        await console.accept(ws)
        try:
            member, vm = await asyncio.to_thread(cluster.find_vm, uuid)
        except VMNotFound:
            await console.refuse(ws, "VM not found")
            return
        if vm.state != VMState.RUNNING:
            await console.refuse(ws, "start the VM to open its console")
            return
        try:
            if member.local:
                host, port = await asyncio.to_thread(member.driver.console_address, uuid)
                await console.bridge_tcp(ws, host, port)
            else:
                async with member.client.open_console(uuid) as remote:
                    await console.bridge_ws(ws, remote)
        except (InvalidState, NotImplementedError) as exc:
            await console.refuse(ws, str(exc) or "this node has no console")
        except Exception as exc:  # unreachable node or display: tell the browser why
            await console.refuse(ws, f"console unavailable: {exc}")

    # Backup. Targets and policies are cluster configuration and need quorum to change;
    # taking a backup does not, so VMs stay protected while a node is down.

    @api.get("/backup/targets", response_model=list[BackupTargetOut])
    def list_targets() -> list[BackupTargetOut]:
        return [BackupTargetOut.from_target(t) for t in backups.targets()]

    @api.post(
        "/backup/targets", response_model=BackupTargetOut, status_code=status.HTTP_201_CREATED
    )
    def add_target(body: BackupTargetIn) -> BackupTargetOut:
        cluster.require_quorum()
        return BackupTargetOut.from_target(backups.add_target(body.to_target()))

    @api.delete("/backup/targets/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def remove_target(name: str) -> None:
        cluster.require_quorum()
        backups.remove_target(name)

    @api.get("/backup/targets/{name}/backups", response_model=list[BackupOut])
    def list_backups(name: str) -> list[BackupOut]:
        return [BackupOut.from_info(info, node) for node, info in backups.list_backups(name)]

    @api.delete(
        "/backup/targets/{name}/backups/{backup_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    def delete_backup(name: str, backup_id: str, node: str | None = None) -> None:
        cluster.require_quorum()
        backups.delete_backup(name, backup_id, node)

    @api.post(
        "/backup/targets/{name}/backups/{backup_id}/restore",
        response_model=JobOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def restore_backup(name: str, backup_id: str, body: RestoreRequest) -> JobOut:
        cluster.require_quorum()
        job = backups.start_restore(
            name, backup_id, body.name, body.node, body.pool, body.source_node
        )
        return JobOut(**job.__dict__)

    @api.post("/vms/{uuid}/backup", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
    def backup_vm(uuid: str, body: BackupRequest) -> JobOut:
        return JobOut(**backups.start_backup(uuid, body.target, body.keep_last).__dict__)

    @api.get("/backup/jobs", response_model=list[JobOut])
    def list_jobs() -> list[JobOut]:
        return [JobOut(**job.__dict__) for job in backups.jobs()]

    @api.get("/backup/policies", response_model=list[PolicyOut])
    def list_policies() -> list[PolicyOut]:
        return [PolicyOut(**p.__dict__) for p in backups.policies()]

    @api.post("/backup/policies", response_model=PolicyOut, status_code=status.HTTP_201_CREATED)
    def add_policy(body: PolicyIn) -> PolicyOut:
        cluster.require_quorum()
        return PolicyOut(**backups.add_policy(Policy(**body.model_dump())).__dict__)

    @api.delete("/backup/policies/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def remove_policy(name: str) -> None:
        cluster.require_quorum()
        backups.remove_policy(name)

    @api.post(
        "/backup/policies/{name}/run",
        response_model=list[JobOut],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def run_policy(name: str) -> list[JobOut]:
        return [JobOut(**job.__dict__) for job in backups.run_policy(name)]

    app.include_router(api)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="console")
    return app
