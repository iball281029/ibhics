"""ibHiCS Manager: REST API and web console for a standalone node or a cluster."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from pathlib import Path

from fastapi import APIRouter, FastAPI, status
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..cluster import Cluster, NodeUnavailable, Topology
from ..hypervisor import HypervisorDriver, VMExists
from .errors import install_cluster_errors, install_driver_errors
from .schemas import (
    ClusterOut,
    HostOut,
    MemberOut,
    NodeJoin,
    PoolCreate,
    PoolOut,
    StopRequest,
    VMCreate,
    VMOut,
)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    cluster: Cluster | HypervisorDriver,
    heartbeat_interval: float = 0,
) -> FastAPI:
    if isinstance(cluster, HypervisorDriver):
        cluster = Cluster.standalone(socket.gethostname(), cluster)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if heartbeat_interval > 0:

            async def beat() -> None:
                while True:
                    await asyncio.to_thread(cluster.refresh)
                    await asyncio.sleep(heartbeat_interval)

            task = asyncio.create_task(beat())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="ibHiCS Manager", version=__version__, lifespan=lifespan)
    app.state.cluster = cluster
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

    app.include_router(api)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="console")
    return app
