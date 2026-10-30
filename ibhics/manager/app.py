"""ibHiCS Manager: REST API and web console for a standalone node or a cluster."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request, Response, WebSocket, status
from fastapi.staticfiles import StaticFiles

from .. import RELEASE_NAME, __version__, console
from ..cluster import Cluster, NodeUnavailable, Topology
from ..hypervisor import HypervisorDriver, InvalidState, VMExists, VMNotFound, VMState
from ..hypervisor.serialize import guest_to_json
from .auth import Auth, AuthError, Forbidden, Role, User
from .backups import BackupManager, Policy
from .errors import install_cluster_errors, install_driver_errors
from .imports import ImportManager
from .migrations import MigrationManager
from .schemas import (
    AuditOut,
    BackupOut,
    BackupRequest,
    BackupTargetIn,
    BackupTargetOut,
    ClusterOut,
    GuestOut,
    HostOut,
    ImportJobOut,
    ImportRequest,
    JobOut,
    LoginOut,
    LoginRequest,
    MemberOut,
    MigrateRequest,
    MigrationOut,
    NodeJoin,
    PasswordChange,
    PolicyIn,
    PolicyOut,
    PoolCreate,
    PoolOut,
    RestoreRequest,
    SourceIn,
    SourceOut,
    SourceVMOut,
    StopRequest,
    UserIn,
    UserOut,
    UserUpdate,
    VMCreate,
    VMOut,
)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "ibhics_session"
API = "/api/v1"

# Viewers may read everything and operators may change VMs; these need an admin.
ADMIN_ROUTES = {
    ("POST", f"{API}/cluster/nodes"),
    ("DELETE", f"{API}/cluster/nodes/{{name}}"),
    ("POST", f"{API}/nodes/{{name}}/pools"),
    ("DELETE", f"{API}/nodes/{{name}}/pools/{{pool}}"),
    ("POST", f"{API}/nodes/{{name}}/evacuate"),
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
) -> FastAPI:
    """Without `auth` every request is allowed: for tests and embedding only. The
    ibhics-manager command always passes one."""
    if isinstance(cluster, HypervisorDriver):
        cluster = Cluster.standalone(socket.gethostname(), cluster)
    backups = backups or BackupManager(cluster)

    def backing_up(vm_name: str) -> bool:
        return any(
            j.kind == "backup" and j.vm == vm_name and j.state in ("queued", "running")
            for j in backups.jobs()
        )

    migrations = migrations or MigrationManager(cluster, busy=backing_up)

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
    app.state.migrations = migrations
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

    @api.get("/vms/{uuid}/guest", response_model=GuestOut)
    def vm_guest(uuid: str) -> GuestOut:
        member, _ = cluster.find_vm(uuid)
        return GuestOut(uuid=uuid, **guest_to_json(member.driver.guest_info(uuid)))

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
        )
        return ImportJobOut(**job.__dict__)

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
