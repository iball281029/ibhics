"""ibHiCS Manager: REST API and web console for a standalone node or a cluster."""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, WebSocket, status
from fastapi.staticfiles import StaticFiles

from .. import RELEASE_NAME, __version__, console
from ..cluster import Cluster, NodeUnavailable, Topology
from ..hypervisor import HypervisorDriver, InvalidState, VMExists, VMInfo, VMNotFound, VMState
from ..hypervisor.serialize import guest_to_json
from ..importer import SourceNotFound
from . import datastore
from .auth import Auth, AuthError, Forbidden, Role, User
from .backups import BackupManager, Policy
from .deploys import Deploy, DeployManager
from .errors import install_cluster_errors, install_driver_errors
from .imports import ImportManager, upload_path
from .migrations import MigrationManager
from .recycle import RecycleBin
from .schemas import (
    AuditOut,
    BackupOut,
    BackupRequest,
    BackupTargetIn,
    BackupTargetOut,
    CdromRequest,
    CloneIn,
    ClusterOut,
    DeployOut,
    FolderIn,
    GroupMembersIn,
    GuestOut,
    HostNetworkIn,
    HostOut,
    HostSettingsIn,
    ImportJobOut,
    ImportRequest,
    IsoOut,
    JobOut,
    LocalDiskOut,
    LoginOut,
    LoginRequest,
    MemberOut,
    MigrateRequest,
    MigrationOut,
    NetworkIn,
    NetworkOut,
    NodeJoin,
    PasswordChange,
    PolicyIn,
    PolicyOut,
    PoolCreate,
    PoolFileOut,
    PoolOut,
    PowerIn,
    RecycleBinOut,
    RecycledOut,
    RestoreRequest,
    SnapshotIn,
    SnapshotOut,
    SourceIn,
    SourceOut,
    SourceVMOut,
    StopRequest,
    TemplateRequest,
    UploadOut,
    UserIn,
    UserOut,
    UserUpdate,
    VMCreate,
    VMEditIn,
    VMGroupIn,
    VMGroupOut,
    VMGroupsOut,
    VMGroupUpdate,
    VMOut,
    VMStatsOut,
)
from .vmgroups import VMGroups

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "ibhics_session"
API = "/api/v1"
# Not under /usr: AppArmor's virt-aa-helper will not let a running VM open files there.
GUEST_TOOLS_ISO = "/var/lib/ibhics/tools/guest-tools.iso"
# Install media for new VMs, uploaded from the browser (Storage > ISO library).
ISO_DIR = Path("/var/lib/ibhics/iso")


def iso_path(folder: Path, filename: str) -> Path:
    """Where an uploaded ISO goes; refuses names that could leave the folder."""
    name = Path(filename).name
    if name != filename or name.startswith(".") or not name:
        raise ValueError("file name must not contain a path")
    if not name.lower().endswith(".iso") or not re.fullmatch(r"[\w.+\- ()]+", name):
        raise ValueError("upload an .iso file named with letters, digits, spaces, . _ - + ( )")
    return folder / name


async def receive_file(request: Request, target: Path) -> int:
    """Stream a request body to `target` through a hidden partial file; returns its size."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial")
    size = 0
    try:
        with open(partial, "wb") as f:
            async for chunk in request.stream():
                await asyncio.to_thread(f.write, chunk)
                size += len(chunk)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return size


# Viewers may read everything and operators may change VMs; these need an admin.
ADMIN_ROUTES = {
    ("POST", f"{API}/cluster/nodes"),
    ("DELETE", f"{API}/cluster/nodes/{{name}}"),
    ("POST", f"{API}/nodes/{{name}}/pools"),
    ("DELETE", f"{API}/nodes/{{name}}/pools/{{pool}}"),
    ("DELETE", f"{API}/nodes/{{name}}/pools/{{pool}}/files"),
    ("POST", f"{API}/nodes/{{name}}/evacuate"),
    ("PUT", f"{API}/nodes/{{name}}/network"),
    ("POST", f"{API}/nodes/{{name}}/network/confirm"),
    ("POST", f"{API}/nodes/{{name}}/network/undo"),
    ("PUT", f"{API}/nodes/{{name}}/settings"),
    ("POST", f"{API}/nodes/{{name}}/power"),
    ("POST", f"{API}/backup/targets"),
    ("DELETE", f"{API}/backup/targets/{{name}}"),
    ("DELETE", f"{API}/backup/targets/{{name}}/backups/{{backup_id}}"),
    ("POST", f"{API}/backup/policies"),
    ("DELETE", f"{API}/backup/policies/{{name}}"),
    ("GET", f"{API}/users"),
    ("POST", f"{API}/users"),
    ("PATCH", f"{API}/users/{{name}}"),
    ("DELETE", f"{API}/users/{{name}}"),
    ("GET", f"{API}/audit"),
    ("POST", f"{API}/migrate/sources"),
    ("POST", f"{API}/networks"),
    ("DELETE", f"{API}/networks/{{name}}"),
    ("DELETE", f"{API}/migrate/sources/{{name}}"),
}
# Open to any signed-in user, even one who still has to replace their initial password.
SELF_ROUTES = {f"{API}/auth/me", f"{API}/auth/logout", f"{API}/auth/password"}


def required_role(method: str, route: str) -> Role:
    if (method, route) in ADMIN_ROUTES:
        return Role.ADMIN
    if method in ("GET", "HEAD") or route in SELF_ROUTES:
        return Role.VIEWER
    return Role.OPERATOR


def session_token(headers, cookies) -> str | None:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    return cookies.get(SESSION_COOKIE)


def create_app(
    cluster: Cluster | HypervisorDriver,
    heartbeat_interval: float = 0,
    backups: BackupManager | None = None,
    scheduler_interval: float = 0,
    migrations: MigrationManager | None = None,
    auth: Auth | None = None,
    imports: ImportManager | None = None,
    iso_dir: Path = ISO_DIR,
    groups: VMGroups | None = None,
    deploys: DeployManager | None = None,
    recycle_days: int = 0,
) -> FastAPI:
    """Without `auth` every request is allowed: for tests and embedding only. The
    ibhics-manager command always passes one."""
    if isinstance(cluster, HypervisorDriver):
        cluster = Cluster.standalone(socket.gethostname(), cluster)
    backups = backups or BackupManager(cluster)
    groups = groups or VMGroups()

    def backing_up(vm_name: str) -> bool:
        return any(
            j.kind == "backup" and j.vm == vm_name and j.state in ("queued", "running")
            for j in backups.jobs()
        )

    migrations = migrations or MigrationManager(cluster, busy=backing_up)

    def deployed(job: Deploy, vm: VMInfo) -> None:
        group = groups.members().get(job.source_uuid)  # beside its template in the tree
        if group:
            groups.assign([vm.uuid], group)

    deploys = deploys or DeployManager(cluster)
    deploys.on_done = deployed
    recycle = RecycleBin(cluster, recycle_days, forget=groups.forget)

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
                    await asyncio.to_thread(recycle.purge_expired)
                    await asyncio.sleep(scheduler_interval)

            scheduler = asyncio.create_task(schedule())
        yield
        for t in (task, scheduler):
            if t:
                t.cancel()

    app = FastAPI(title="ibHiCS Manager", version=__version__, lifespan=lifespan)
    app.state.cluster = cluster
    app.state.backups = backups
    app.state.migrations = migrations
    app.state.deploys = deploys
    app.state.auth = auth
    imports = imports or ImportManager(cluster)
    app.state.imports = imports
    install_driver_errors(app)
    install_cluster_errors(app)

    def guard(request: Request) -> User | None:
        if auth is None:
            return None
        user = auth.authenticate(session_token(request.headers, request.cookies))
        request.state.user = user
        route = request.scope["route"].path
        if user.must_change_password and route not in SELF_ROUTES:
            raise Forbidden("change your initial password first")
        needed = required_role(request.method, route)
        if user.role.rank < needed.rank:
            raise Forbidden(f"this needs the {needed} role; you are {user.role}")
        return user

    @app.middleware("http")
    async def audit(request: Request, call_next):
        response = await call_next(request)
        if (
            auth is not None
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and request.url.path.startswith(API)
            and request.url.path != f"{API}/auth/login"  # recorded by the login route
        ):
            user = getattr(request.state, "user", None)
            auth.record(
                user.name if user else "-",
                request.method,
                request.url.path,
                response.status_code,
                request.client.host if request.client else "",
            )
        return response

    public = APIRouter(prefix=API)
    api = APIRouter(prefix=API, dependencies=[Depends(guard)])

    @public.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "release": RELEASE_NAME,
            "auth": auth is not None,
        }

    # Sign-in. Browsers get an HttpOnly, SameSite=Strict cookie, which other sites can
    # neither read nor make the browser send; scripts use the returned bearer token.

    @public.post("/auth/login", response_model=LoginOut)
    def login(body: LoginRequest, request: Request, response: Response) -> LoginOut:
        if auth is None:
            raise AuthError("sign-in is not enabled on this manager")
        address = request.client.host if request.client else ""
        try:
            session = auth.login(body.username, body.password)
        except AuthError:
            auth.record(body.username, "LOGIN", "failed", 401, address)
            raise
        auth.record(body.username, "LOGIN", "ok", 200, address)
        response.set_cookie(
            SESSION_COOKIE,
            session.token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        user = auth.user(session.user)
        return LoginOut(
            token=session.token,
            idle_timeout=int(auth.idle_timeout),
            user=UserOut.from_user(user),
        )

    @api.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(request: Request, response: Response) -> None:
        if auth is not None:
            auth.logout(session_token(request.headers, request.cookies) or "")
        response.delete_cookie(SESSION_COOKIE, path="/")

    @api.get("/auth/me", response_model=UserOut | None)
    def me(request: Request) -> UserOut | None:
        user = getattr(request.state, "user", None)
        return UserOut.from_user(user) if user else None

    @api.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
    def change_password(body: PasswordChange, request: Request) -> None:
        if auth is None:
            raise AuthError("sign-in is not enabled on this manager")
        auth.change_password(request.state.user.name, body.current_password, body.new_password)

    # Users and the audit log (admins only, see ADMIN_ROUTES)

    def users() -> Auth:
        if auth is None:
            raise AuthError("sign-in is not enabled on this manager")
        return auth

    def acting(request: Request) -> str | None:
        user = getattr(request.state, "user", None)
        return user.name if user else None

    @api.get("/users", response_model=list[UserOut])
    def list_users() -> list[UserOut]:
        return [UserOut.from_user(u) for u in users().users()]

    @api.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
    def add_user(body: UserIn) -> UserOut:
        return UserOut.from_user(users().add_user(body.name, Role(body.role), body.password))

    @api.patch("/users/{name}", response_model=UserOut)
    def update_user(name: str, body: UserUpdate, request: Request) -> UserOut:
        user = users().update_user(
            name,
            role=Role(body.role) if body.role else None,
            enabled=body.enabled,
            password=body.password,
            acting=acting(request),
        )
        return UserOut.from_user(user)

    @api.delete("/users/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_user(name: str, request: Request) -> None:
        users().delete_user(name, acting=acting(request))

    @api.get("/audit", response_model=list[AuditOut])
    def audit_log(limit: int = 200) -> list[AuditOut]:
        return [AuditOut(**e.__dict__) for e in users().audit(min(max(limit, 1), 1000))]

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

    @api.get("/nodes/{name}/disks", response_model=list[LocalDiskOut])
    def node_disks(name: str) -> list[LocalDiskOut]:
        """Whole disks on the node that a new LVM pool could use."""
        return [LocalDiskOut(**asdict(d)) for d in cluster.member(name).driver.list_local_disks()]

    # The node itself: Nodes > node > Summary, Network and Settings.

    def host_of(name: str):
        return cluster.member(name).driver.host_system()

    @api.get("/nodes/{name}/summary")
    def node_summary(name: str) -> dict:
        return asdict(host_of(name).summary())

    @api.get("/nodes/{name}/nics")
    def node_nics(name: str) -> list[dict]:
        return [asdict(n) for n in host_of(name).nics()]

    @api.get("/nodes/{name}/network")
    def node_network(name: str) -> dict:
        return asdict(host_of(name).network())

    @api.put("/nodes/{name}/network")
    def apply_node_network(name: str, body: HostNetworkIn) -> dict:
        """Applies at once; put back by itself unless confirmed within two minutes."""
        return asdict(host_of(name).apply_network(body.to_network(), name))

    @api.get("/nodes/{name}/network/state")
    def node_network_state(name: str) -> dict:
        return asdict(host_of(name).network_state())

    @api.post("/nodes/{name}/network/confirm")
    def confirm_node_network(name: str) -> dict:
        return asdict(host_of(name).confirm_network())

    @api.post("/nodes/{name}/network/undo")
    def undo_node_network(name: str) -> dict:
        return asdict(host_of(name).undo_network())

    @api.get("/nodes/{name}/settings")
    def node_settings(name: str) -> dict:
        return asdict(host_of(name).settings())

    @api.put("/nodes/{name}/settings")
    def update_node_settings(name: str, body: HostSettingsIn) -> dict:
        return asdict(host_of(name).update_settings(body.to_edit()))

    @api.get("/nodes/{name}/timezones")
    def node_timezones(name: str) -> list[str]:
        return host_of(name).timezones()

    @api.post("/nodes/{name}/power", status_code=status.HTTP_202_ACCEPTED)
    def node_power(name: str, body: PowerIn) -> None:
        member = cluster.member(name)
        running = [vm.spec.name for vm in member.driver.list_vms() if vm.state != VMState.STOPPED]
        if running and not body.force:
            raise InvalidState(
                f"{len(running)} VM(s) still run on {name} ({', '.join(running[:3])}): "
                "shut them down or move them first"
            )
        member.driver.host_system().power(body.action)

    # VM networks: the LAN (default) and VLANs on it, the same on every compute node so
    # VMs can move between them.

    @api.get("/networks", response_model=list[NetworkOut])
    def list_networks() -> list[NetworkOut]:
        found: dict[str, NetworkOut] = {}
        for member in cluster.compute_members():
            for net in member.driver.list_networks():
                out = found.setdefault(net.name, NetworkOut(**asdict(net)))
                out.nodes.append(member.name)
        return sorted(found.values(), key=lambda n: (n.vlan or 0, n.name))

    @api.post("/networks", response_model=NetworkOut, status_code=status.HTTP_201_CREATED)
    def create_network(body: NetworkIn) -> NetworkOut:
        cluster.require_quorum()
        done = []
        try:
            for member in cluster.compute_members():
                member.driver.create_network(body.to_spec())
                done.append(member)
        except Exception:
            for member in done:  # all nodes or none, so VMs never land where it is missing
                try:
                    member.driver.delete_network(body.name)
                except Exception:
                    pass
            raise
        return NetworkOut(
            name=body.name, bridge=f"ibr{body.vlan}", vlan=body.vlan, nodes=[m.name for m in done]
        )

    @api.delete("/networks/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_network(name: str) -> None:
        cluster.require_quorum()
        for member in cluster.compute_members():
            if any(n.name == name for n in member.driver.list_networks()):
                member.driver.delete_network(name)

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

    # Datastore browser: the files of a folder, thin-disk or NFS pool, with folders
    # (such as "iso") that uploads go into.

    def pool_root(name: str, pool: str) -> str:
        driver = cluster.member(name).driver
        root = driver.pool_path(pool)
        if root is None:
            kind = next((p.type for p in driver.list_pools() if p.name == pool), None)
            if kind is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"no pool {pool!r} on {name}")
            if kind in ("lvm", "iscsi"):
                raise ValueError("an LVM pool holds VM disks only; it has no files or folders")
            raise ValueError("files of this pool can be browsed only on the node holding it")
        return root

    def pool_call(fn, *args):
        try:
            return fn(*args)
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @api.get("/nodes/{name}/pools/{pool}/files", response_model=list[PoolFileOut])
    def pool_files(name: str, pool: str, path: str = "") -> list[PoolFileOut]:
        entries = pool_call(datastore.listing, pool_root(name, pool), path)
        return [PoolFileOut(**asdict(e)) for e in entries]

    @api.post("/nodes/{name}/pools/{pool}/folders", status_code=status.HTTP_201_CREATED)
    def pool_folder(name: str, pool: str, body: FolderIn) -> dict:
        datastore.make_folder(pool_root(name, pool), body.path)
        return {"path": str(datastore.clean(body.path))}

    @api.put("/nodes/{name}/pools/{pool}/files", response_model=PoolFileOut)
    async def pool_upload(name: str, pool: str, path: str, request: Request) -> PoolFileOut:
        root = pool_root(name, pool)
        target = pool_call(datastore.upload_target, root, path)
        size = await receive_file(request, target)
        rel = str(target.relative_to(Path(root).resolve()))
        return PoolFileOut(name=target.name, path=rel, dir=False, size_bytes=size, modified=0)

    @api.delete("/nodes/{name}/pools/{pool}/files", status_code=status.HTTP_204_NO_CONTENT)
    def pool_delete(name: str, pool: str, path: str) -> None:
        in_use = {vm.spec.iso for _, vm in cluster.all_vms() if vm.spec.iso}
        pool_call(datastore.remove, pool_root(name, pool), path, in_use)

    # VMs: reads work without quorum so operators can see what is going on;
    # anything that changes state needs a majority of nodes.

    @api.get("/vms", response_model=list[VMOut])
    def list_vms() -> list[VMOut]:
        return [VMOut.from_info(vm, member.name) for member, vm in cluster.all_vms()]

    @api.post("/vms", response_model=VMOut, status_code=status.HTTP_201_CREATED)
    def create_vm(body: VMCreate) -> VMOut:
        cluster.require_quorum()
        for _, vm in cluster.all_vms():
            if vm.spec.name == body.name:
                where = " (in the recycle bin)" if vm.spec.recycled else ""
                raise VMExists(f"VM named {body.name!r} already exists in the cluster{where}")
        member = cluster.member(body.node) if body.node else cluster.pick_node(body.memory_mb)
        if body.driver_iso is None and "windows" in body.os_type.lower():
            if Path(GUEST_TOOLS_ISO).is_file():
                body.driver_iso = GUEST_TOOLS_ISO
        return VMOut.from_info(member.driver.create_vm(body.to_spec()), member.name)

    # Inventory: what the guests report about themselves (qemu-guest-agent).

    @api.get("/vms/guests", response_model=list[GuestOut])
    def vm_guests() -> list[GuestOut]:
        result = []
        for member in cluster.compute_members():
            try:
                guests = member.driver.list_guest_info()
            except NodeUnavailable as exc:
                member.last_error = str(exc)
                continue
            result.extend(GuestOut(uuid=uuid, **guest_to_json(g)) for uuid, g in guests.items())
        return result

    # Live load of running VMs, for the CPU and memory columns of the VM list.

    @api.get("/vms/stats", response_model=list[VMStatsOut])
    def vm_stats() -> list[VMStatsOut]:
        result = []
        for member in cluster.compute_members():
            try:
                stats = member.driver.list_vm_stats()
            except NodeUnavailable as exc:
                member.last_error = str(exc)
                continue
            result.extend(VMStatsOut(uuid=uuid, **asdict(s)) for uuid, s in stats.items())
        return result

    # VM groups: the folder tree beside the VM list.

    def groups_out() -> VMGroupsOut:
        return VMGroupsOut(
            groups=[VMGroupOut(**asdict(g)) for g in groups.groups()], members=groups.members()
        )

    @api.get("/vm-groups", response_model=VMGroupsOut)
    def list_groups() -> VMGroupsOut:
        return groups_out()

    @api.post("/vm-groups", response_model=VMGroupOut, status_code=status.HTTP_201_CREATED)
    def create_group(body: VMGroupIn) -> VMGroupOut:
        return VMGroupOut(**asdict(groups.create(body.name, body.parent, body.description)))

    @api.put("/vm-groups/members", response_model=VMGroupsOut)
    def move_to_group(body: GroupMembersIn) -> VMGroupsOut:
        known = {vm.uuid for _, vm in cluster.all_vms()}
        missing = [u for u in body.vms if u not in known]
        if missing:
            raise VMNotFound(missing[0])
        groups.assign(body.vms, body.group)
        return groups_out()

    @api.patch("/vm-groups/{group_id}", response_model=VMGroupOut)
    def update_group(group_id: str, body: VMGroupUpdate) -> VMGroupOut:
        return VMGroupOut(
            **asdict(groups.update(group_id, body.name, body.parent, body.description))
        )

    @api.delete("/vm-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_group(group_id: str) -> None:
        groups.delete(group_id)

    @api.get("/vms/{uuid}/guest", response_model=GuestOut)
    def vm_guest(uuid: str) -> GuestOut:
        member, _ = cluster.find_vm(uuid)
        return GuestOut(uuid=uuid, **guest_to_json(member.driver.guest_info(uuid)))

    @api.get("/vms/{uuid}", response_model=VMOut)
    def get_vm(uuid: str) -> VMOut:
        member, vm = cluster.find_vm(uuid)
        return VMOut.from_info(vm, member.name)

    @api.post("/vms/{uuid}/cdrom", response_model=VMOut)
    def insert_cdrom(uuid: str, body: CdromRequest) -> VMOut:
        cluster.require_quorum()
        member, _ = cluster.find_vm(uuid)
        return VMOut.from_info(member.driver.insert_cdrom(uuid, body.iso), member.name)

    @api.patch("/vms/{uuid}", response_model=VMOut)
    def update_vm(uuid: str, body: VMEditIn) -> VMOut:
        """Change CPU, memory, disks, NICs, boot order and options."""
        cluster.require_quorum()
        member, _ = cluster.find_vm(uuid)
        return VMOut.from_info(member.driver.update_vm(uuid, body.to_edit()), member.name)

    @api.post("/vms/{uuid}/guest-tools", response_model=VMOut)
    def insert_guest_tools(uuid: str) -> VMOut:
        """The guest tools CD (virtio drivers and guest agent) in the VM's drive."""
        cluster.require_quorum()
        member, _ = cluster.find_vm(uuid)
        try:
            return VMOut.from_info(member.driver.insert_cdrom(uuid, GUEST_TOOLS_ISO), member.name)
        except ValueError as exc:
            if "is not on node" not in str(exc):
                raise
            raise InvalidState(
                f"the guest tools CD is missing on {member.name}: run the upgrade from the "
                "ibHiCS ISO on that node again, which puts it in place"
            ) from None

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

    @api.post("/vms/{uuid}/restart", response_model=VMOut)
    def restart_vm(uuid: str, body: StopRequest | None = None) -> VMOut:
        cluster.require_quorum()
        member, _ = cluster.find_vm(uuid)
        vm = member.driver.restart_vm(uuid, force=bool(body and body.force))
        return VMOut.from_info(vm, member.name)

    @api.delete("/vms/{uuid}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_vm(uuid: str, permanent: bool = False) -> None:
        """Into the recycle bin when it is on, unless `permanent`; for good otherwise."""
        cluster.require_quorum()
        _, vm = cluster.find_vm(uuid)
        if deploys.busy(uuid):
            raise InvalidState(f"a VM is being deployed from {vm.spec.name!r}; wait for it")
        recycle.delete(uuid, permanent)

    # Recycle bin: deleted VMs, kept for `recycle_days`.

    def recycled_out(node: str, vm: VMInfo) -> RecycledOut:
        expires = recycle.expires(vm)
        return RecycledOut(
            vm=VMOut.from_info(vm, node),
            expires=expires.isoformat(timespec="seconds") if expires else None,
        )

    @api.get("/recycle-bin", response_model=RecycleBinOut)
    def recycle_bin() -> RecycleBinOut:
        return RecycleBinOut(
            days=recycle.days, vms=[recycled_out(n, vm) for n, vm in recycle.contents()]
        )

    @api.post("/recycle-bin/{uuid}/restore", response_model=VMOut)
    def restore_vm(uuid: str) -> VMOut:
        vm = recycle.restore(uuid)
        member, _ = cluster.find_vm(uuid)
        return VMOut.from_info(vm, member.name)

    @api.delete("/recycle-bin/{uuid}", status_code=status.HTTP_204_NO_CONTENT)
    def purge_vm(uuid: str) -> None:
        _, vm = cluster.find_vm(uuid)
        if not vm.spec.recycled:
            raise InvalidState(f"{vm.spec.name} is not in the recycle bin")
        recycle.delete(uuid, permanent=True)

    # Snapshots: go back to how a VM was before a change.

    @api.get("/vms/{uuid}/snapshots", response_model=list[SnapshotOut])
    def list_snapshots(uuid: str) -> list[SnapshotOut]:
        member, _ = cluster.find_vm(uuid)
        return [SnapshotOut(**asdict(s)) for s in member.driver.list_snapshots(uuid)]

    @api.post(
        "/vms/{uuid}/snapshots", response_model=SnapshotOut, status_code=status.HTTP_201_CREATED
    )
    def create_snapshot(uuid: str, body: SnapshotIn) -> SnapshotOut:
        cluster.require_quorum()
        member, _ = cluster.find_vm(uuid)
        return SnapshotOut(
            **asdict(member.driver.create_snapshot(uuid, body.name, body.description))
        )

    @api.post("/vms/{uuid}/snapshots/{name}/revert", response_model=VMOut)
    def revert_snapshot(uuid: str, name: str) -> VMOut:
        cluster.require_quorum()
        member, _ = cluster.find_vm(uuid)
        return VMOut.from_info(member.driver.revert_snapshot(uuid, name), member.name)

    @api.delete("/vms/{uuid}/snapshots/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_snapshot(uuid: str, name: str) -> None:
        cluster.require_quorum()
        member, _ = cluster.find_vm(uuid)
        member.driver.delete_snapshot(uuid, name)

    # Templates: convert a stopped VM, and deploy new VMs from it (VMware-style).

    @api.post("/vms/{uuid}/template", response_model=VMOut)
    def set_template(uuid: str, body: TemplateRequest) -> VMOut:
        cluster.require_quorum()
        member, vm = cluster.find_vm(uuid)
        if not body.template and deploys.busy(uuid):
            raise InvalidState(f"a VM is being deployed from {vm.spec.name!r}; wait for it")
        return VMOut.from_info(member.driver.set_template(uuid, body.template), member.name)

    @api.post("/vms/{uuid}/clone", response_model=DeployOut, status_code=status.HTTP_202_ACCEPTED)
    def clone_vm(uuid: str, body: CloneIn) -> DeployOut:
        """Deploy a VM from a template, or clone a stopped VM. Runs as a task."""
        return DeployOut(**asdict(deploys.start(uuid, body.to_clone(), body.power_on)))

    @api.get("/deploys", response_model=list[DeployOut])
    def list_deploys() -> list[DeployOut]:
        return [DeployOut(**asdict(j)) for j in deploys.jobs()]

    # Live migration. Moving a VM changes where it runs, so it needs quorum like any change.

    @api.post(
        "/vms/{uuid}/migrate", response_model=MigrationOut, status_code=status.HTTP_202_ACCEPTED
    )
    def migrate_vm(uuid: str, body: MigrateRequest | None = None) -> MigrationOut:
        body = body or MigrateRequest()
        job = migrations.start(uuid, body.node, body.copy_storage, body.bandwidth_mib)
        return MigrationOut(**job.__dict__)

    @api.get("/migrations", response_model=list[MigrationOut])
    def list_migrations() -> list[MigrationOut]:
        return [MigrationOut(**job.__dict__) for job in migrations.jobs()]

    @api.post(
        "/nodes/{name}/evacuate",
        response_model=list[MigrationOut],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def evacuate_node(name: str) -> list[MigrationOut]:
        return [MigrationOut(**job.__dict__) for job in migrations.evacuate(name)]

    # ibHiCS Migrate: bring VMs over from VMware, OVA exports and disk images.

    @api.get("/migrate/sources", response_model=list[SourceOut])
    def list_sources() -> list[SourceOut]:
        return [SourceOut(name=s.name, type=s.type, settings=s.masked()) for s in imports.sources()]

    @api.post("/migrate/sources", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
    def add_source(body: SourceIn) -> SourceOut:
        cluster.require_quorum()
        source = imports.add_source(body.to_source())
        return SourceOut(name=source.name, type=source.type, settings=source.masked())

    @api.delete("/migrate/sources/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def remove_source(name: str) -> None:
        cluster.require_quorum()
        imports.remove_source(name)

    # ISO library: install media for new VMs and the CD/DVD drive, kept on the manager.

    @api.get("/isos", response_model=list[IsoOut])
    def list_isos() -> list[IsoOut]:
        files = sorted(iso_dir.iterdir()) if iso_dir.is_dir() else []
        isos = [
            IsoOut(name=p.name, path=str(p), size_bytes=p.stat().st_size)
            for p in files
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() == ".iso"
        ]
        # .iso files kept in storage pools (for example SSD01/iso) on this node.
        for member in cluster.compute_members():
            try:
                pools = member.driver.list_pools()
            except NodeUnavailable:
                continue
            for pool in pools:
                try:
                    root = member.driver.pool_path(pool.name)
                except Exception:  # a pool that went away: just skip it
                    continue
                if not root:
                    continue
                for p in datastore.find_isos(root):
                    rel = p.relative_to(root)
                    isos.append(
                        IsoOut(
                            name=p.name,
                            path=str(p),
                            size_bytes=p.stat().st_size,
                            pool=pool.name,
                            folder=str(rel.parent) if rel.parent.parts else "",
                        )
                    )
        tools = Path(GUEST_TOOLS_ISO)
        if tools.is_file():
            isos.append(
                IsoOut(name="ibHiCS guest tools", path=str(tools), size_bytes=tools.stat().st_size)
            )
        return isos

    @api.put("/isos/{filename}", response_model=IsoOut)
    async def upload_iso(filename: str, request: Request) -> IsoOut:
        target = iso_path(iso_dir, filename)
        size = await receive_file(request, target)
        return IsoOut(name=target.name, path=str(target), size_bytes=size)

    @api.delete("/isos/{filename}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_iso(filename: str) -> None:
        target = iso_path(iso_dir, filename)
        if not target.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no ISO named {filename!r}")
        in_use = [vm.spec.name for _, vm in cluster.all_vms() if vm.spec.iso == str(target)]
        if in_use:
            names = ", ".join(in_use)
            raise ValueError(f"{filename} is in the CD/DVD drive of {names}; eject it first")
        target.unlink()

    # Files uploaded from the browser (OVA, VMDK, VHD/VHDX, QCOW2, raw): for VMs from any
    # platform that can export them. They appear under the built-in "uploads" source.

    @api.get("/migrate/uploads", response_model=list[UploadOut])
    def list_uploads() -> list[UploadOut]:
        folder = imports.upload_dir
        files = sorted(folder.iterdir()) if folder.is_dir() else []
        return [
            UploadOut(name=p.name, size_bytes=p.stat().st_size)
            for p in files
            if p.is_file() and not p.name.startswith(".")
        ]

    @api.put("/migrate/uploads/{filename}", response_model=UploadOut)
    async def upload(filename: str, request: Request) -> UploadOut:
        target = upload_path(imports.upload_dir, filename)
        size = await receive_file(request, target)
        return UploadOut(name=target.name, size_bytes=size)

    @api.delete("/migrate/uploads/{filename}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_upload(filename: str) -> None:
        target = upload_path(imports.upload_dir, filename)
        if not target.is_file():
            raise SourceNotFound(f"uploads: no file {filename!r}")
        target.unlink()

    @api.get("/migrate/sources/{name}/vms", response_model=list[SourceVMOut])
    def source_vms(name: str) -> list[SourceVMOut]:
        return [SourceVMOut(**vm.to_dict()) for vm in imports.source_vms(name)]

    @api.post("/migrate/jobs", response_model=ImportJobOut, status_code=status.HTTP_202_ACCEPTED)
    def start_import(body: ImportRequest) -> ImportJobOut:
        job = imports.start(
            body.source,
            body.vm,
            body.name,
            body.node,
            body.pool,
            body.network,
            body.vcpus,
            body.memory_mb,
            body.start,
            clear_hibernation=body.clear_hibernation,
        )
        return ImportJobOut(**job.__dict__)

    @api.get("/migrate/jobs/{job_id}/log")
    def import_log(job_id: str) -> dict:
        return {"log": imports.job_log(job_id)}

    @api.get("/migrate/jobs", response_model=list[ImportJobOut])
    def list_imports() -> list[ImportJobOut]:
        return [ImportJobOut(**job.__dict__) for job in imports.jobs()]

    # VM console: the browser's noVNC WebSocket is relayed to the VM's VNC display, on this
    # node directly or through the agent of the VM's node.

    @app.websocket("/api/v1/vms/{uuid}/console")
    async def vm_console(ws: WebSocket, uuid: str) -> None:
        await console.accept(ws)
        if auth is not None:
            # Cookies ride along on WebSocket handshakes from any page, so also insist the
            # page that opened it is our own.
            origin = ws.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != ws.headers.get("host"):
                await console.refuse(ws, "console opened from another site")
                return
            try:
                user = auth.authenticate(session_token(ws.headers, ws.cookies))
            except AuthError as exc:
                await console.refuse(ws, str(exc))
                return
            if user.must_change_password or user.role.rank < Role.OPERATOR.rank:
                await console.refuse(ws, "the console needs the operator role")
                return
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

    app.include_router(public)
    app.include_router(api)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="console")
    return app
