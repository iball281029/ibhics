"""ibHiCS Agent: runs on every node and exposes that node's hypervisor to the manager."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, WebSocket, status

from .. import __version__, console
from ..backup import BackupTarget, NodeBackup
from ..cluster.remote import TOKEN_HEADER
from ..cluster.topology import NodeRole
from ..hypervisor import HypervisorDriver, InvalidState, VMNotFound
from ..hypervisor.serialize import guest_to_json
from ..importer import NodeImport, Source, SourceVM
from ..importer import list_vms as discover_source_vms
from ..importer.diagnostics import test_source
from ..manager.errors import install_driver_errors
from ..manager.poollabels import PoolLabels
from ..manager.schemas import (
    AgentBackupDelete,
    AgentBackupList,
    AgentBackupOperation,
    AgentBackupRun,
    AgentImport,
    AgentMigrate,
    AgentPrune,
    AgentRestore,
    AgentVerify,
    BackupOut,
    CdromRequest,
    CloneIn,
    DiskMovesIn,
    HostNetworkIn,
    HostOut,
    HostSettingsIn,
    NetworkIn,
    NodeDisplayNameIn,
    PoolCacheIn,
    PoolCreate,
    PoolOut,
    RenameIn,
    SnapshotIn,
    SnapshotOut,
    StopRequest,
    TemplateRequest,
    VMCreate,
    VMEditIn,
    VMOut,
)
from ..storage_cache import CacheModeIn, cache_status, set_cache_mode
from ..storage_node import NodeProvisionIn, provision


def create_agent_app(
    name: str,
    role: NodeRole,
    token: str,
    driver: HypervisorDriver | None = None,
    backups: NodeBackup | None = None,
    imports: NodeImport | None = None,
    pool_labels_path: Path | None = None,
    storage_root: Path = Path("/var/lib/ibhics/virtual-storage"),
    migration_pki=None,
    replication_root: Path = Path("/var/lib/ibhics/replica-manifests"),
    replication_guard: Path = Path("/run/lock/ibhics-replication.lock"),
    application_updater=None,
) -> FastAPI:
    if not token:
        raise ValueError("an agent needs a cluster token")
    if role == NodeRole.COMPUTE and driver is None:
        raise ValueError("a compute agent needs a hypervisor driver")

    def check_token(x_ibhics_token: str = Header(default="")) -> None:
        if not hmac.compare_digest(x_ibhics_token.encode(), token.encode()):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")

    app = FastAPI(title=f"ibHiCS Agent ({name})", version=__version__)
    pool_labels = PoolLabels(pool_labels_path)
    from ..storage_jobs import StorageJobs

    storage_jobs = StorageJobs(storage_root / "node-jobs")
    install_driver_errors(app)
    api = APIRouter(prefix="/agent/v1", dependencies=[Depends(check_token)])

    @api.get("/health")
    def health() -> dict:
        return {"name": name, "role": str(role), "version": __version__}

    @api.get("/application-update")
    def application_update_status():
        if application_updater is None:
            raise HTTPException(503, "application updater is not configured")
        data = application_updater.read()
        return {k: v for k, v in data.items() if k not in ("envelope", "wheel", "backup")}

    @api.get("/application-update/cluster-jobs/{identifier}")
    def application_cluster_job(identifier: str):
        import json
        import re

        if application_updater is None or not re.fullmatch(r"[0-9a-f]{16}", identifier):
            raise HTTPException(404, "job not found")
        path = application_updater.state / "update-jobs" / (identifier + ".json")
        if not path.exists():
            raise HTTPException(404, "job not found")
        return json.loads(path.read_text())

    @api.post("/application-update/prepare")
    async def application_update_prepare(request: Request):
        if application_updater is None:
            raise HTTPException(503, "application updater is not configured")
        from ..manager.settings import MAX_PACKAGE
        from ..manager.settings_api import limited_json_body

        body = await limited_json_body(request, MAX_PACKAGE * 4 // 3 + 65536)
        try:
            package = base64.b64decode(body.get("package", ""), validate=True)
            return await asyncio.to_thread(
                application_updater.prepare, body.get("envelope", {}), package
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from None

    @api.post("/application-update/install", status_code=202)
    def application_update_install(body: dict | None = None):
        if application_updater is None:
            raise HTTPException(503, "application updater is not configured")
        try:
            return application_updater.start(operation_id=(body or {}).get("operation_id"))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.post("/application-update/rollback", status_code=202)
    def application_update_rollback(body: dict | None = None):
        if application_updater is None:
            raise HTTPException(503, "application updater is not configured")
        try:
            return application_updater.start(
                rollback=True, operation_id=(body or {}).get("operation_id")
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    if migration_pki is not None and role == NodeRole.COMPUTE:
        from ..cluster.migration_pki import PKIError, schedule_activate

        def pki_call(fn, *args):
            try:
                return fn(*args)
            except PKIError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None

        @api.get("/migration-tls")
        def migration_tls_status() -> dict:
            return pki_call(migration_pki.status)

        @api.post("/migration-tls/prepare")
        def migration_tls_prepare(body: dict) -> dict:
            return pki_call(migration_pki.prepare, str(body.get("name", "")))

        @api.post("/migration-tls/install")
        def migration_tls_install(body: dict) -> dict:
            certs, peers = body.get("certs"), body.get("peers")
            if not isinstance(certs, dict) or not isinstance(peers, list):
                raise HTTPException(status_code=422, detail="certs and peers are required")
            return pki_call(
                migration_pki.install,
                str(body.get("name", "")),
                str(body.get("address", "")),
                str(body.get("ca", "")),
                {str(k): str(v) for k, v in certs.items()},
                [str(p) for p in peers],
            )

        @api.post("/migration-tls/peers")
        def migration_tls_peers(body: dict) -> dict:
            peers = body.get("peers")
            if not isinstance(peers, list):
                raise HTTPException(status_code=422, detail="peers must be a list")
            return {"peers": pki_call(migration_pki.set_peers, [str(p) for p in peers])}

        @api.post("/migration-tls/activate", status_code=status.HTTP_202_ACCEPTED)
        def migration_tls_activate() -> dict:
            pki_call(schedule_activate)
            return {"scheduled": True}

    @api.get("/virtual-storage/readiness")
    def storage_readiness() -> dict:
        from ..storage_software import software_status

        return software_status(str(role))

    @api.get("/virtual-storage/job")
    def storage_job() -> dict | None:
        return storage_jobs.current()

    @api.post("/virtual-storage/software", status_code=status.HTTP_202_ACCEPTED)
    def storage_software() -> dict:
        from ..storage_software import prepare_software

        return storage_jobs.start(
            "software",
            {"role": str(role)},
            lambda report: prepare_software(str(role), False, storage_root, report),
        )

    if role == NodeRole.COMPUTE:

        @api.get("/virtual-storage/manager-status")
        def storage_manager_status() -> dict:
            import json

            from ..storage_cluster import StorageController

            path = storage_root.with_name("virtual-storage-runtime") / "created.json"
            created = json.loads(path.read_text()) if path.exists() else None
            resource, error = None, None
            if created:
                try:
                    resource = next(
                        (
                            r
                            for r in StorageController._gateway()
                            if r["name"] == created["spec"]["name"]
                        ),
                        None,
                    )
                except Exception:
                    error = "Storage controller status is unavailable"
            return {"created": created, "resource": resource, "error": error}

        @api.get("/hardware-capabilities")
        def hardware_capabilities() -> dict:
            return driver.hardware_capabilities()

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

        @api.get("/host/lldp")
        def host_lldp() -> list[dict]:
            return driver.host_system().lldp()

        from ..hypervisor.fake import FakeDriver as _FakeDriver

        if not isinstance(driver, _FakeDriver):
            from ..hypervisor.lldp import start as _lldp_start

            _lldp_start()  # listen from the start, so the first question has an answer

        @api.get("/host/nics")
        def host_nics() -> list[dict]:
            return [asdict(n) for n in driver.host_system().nics()]

        @api.get("/host/hardware")
        def host_hardware() -> dict:
            return driver.host_system().hardware_health()

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

        @api.get("/virtual-storage/inventory")
        def virtual_storage_inventory() -> dict:
            from ..hypervisor.storage_inventory import inventory

            return inventory(driver)

        @api.get("/virtual-storage/cache/{pool}")
        def storage_cache(pool: str) -> dict:
            return cache_status(pool)

        @api.post("/virtual-storage/cache", status_code=status.HTTP_202_ACCEPTED)
        def storage_cache_mode(body: CacheModeIn) -> dict:
            return storage_jobs.start(
                "cache-mode",
                body.model_dump(),
                lambda report: set_cache_mode(body, root=storage_root, report=report),
            )

        @api.post("/virtual-storage/provision", status_code=status.HTTP_202_ACCEPTED)
        def storage_provision(body: NodeProvisionIn) -> dict:
            if not body.confirm_wipe or body.selection.node != name:
                raise ValueError(
                    "Explicit disk erasure confirmation and matching node are required"
                )
            return storage_jobs.start(
                "provision",
                body.model_dump(),
                lambda report: provision(driver, body, node=name, root=storage_root, report=report),
            )

        @api.get("/networks")
        def networks() -> list[dict]:
            return [asdict(n) for n in driver.list_networks()]

        @api.post("/networks", status_code=status.HTTP_201_CREATED)
        def create_network(body: NetworkIn) -> dict:
            return asdict(driver.create_network(body.to_spec()))

        @api.delete("/networks/{name}", status_code=status.HTTP_204_NO_CONTENT)
        def delete_network(name: str) -> None:
            driver.delete_network(name)

        @api.put("/edge", status_code=status.HTTP_204_NO_CONTENT)
        def apply_edge(body: dict) -> None:
            driver.apply_edge(body["config"], bool(body["router"]))

        @api.post("/terminal")
        def terminal(body: dict) -> dict:
            code, output = driver.run_command(str(body.get("command", "")))
            return {"code": code, "output": output}

        from ..hypervisor.containers import install_agent_routes

        install_agent_routes(api, driver.containers)

        @api.post("/net-test")
        def net_test(body: dict) -> dict:
            ok, output = driver.net_test(body["kind"], body["target"], body.get("network", ""))
            return {"ok": ok, "output": output}

        @api.get("/guests")
        def guests() -> dict:
            return {uuid: guest_to_json(g) for uuid, g in driver.list_guest_info().items()}

        @api.get("/stats")
        def stats() -> dict:
            return {uuid: asdict(s) for uuid, s in driver.list_vm_stats().items()}

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

        @api.post("/vms/{uuid}/restart", response_model=VMOut)
        def restart_vm(uuid: str, body: StopRequest | None = None) -> VMOut:
            return VMOut.from_info(driver.restart_vm(uuid, force=bool(body and body.force)))

        @api.delete("/vms/{uuid}", status_code=status.HTTP_204_NO_CONTENT)
        def delete_vm(uuid: str) -> None:
            driver.delete_vm(uuid)

        @api.get("/vms/{uuid}/snapshots", response_model=list[SnapshotOut])
        def list_snapshots(uuid: str) -> list[SnapshotOut]:
            return [SnapshotOut(**asdict(s)) for s in driver.list_snapshots(uuid)]

        @api.post("/vms/{uuid}/snapshots", response_model=SnapshotOut)
        def create_snapshot(uuid: str, body: SnapshotIn) -> SnapshotOut:
            snap = driver.create_snapshot(uuid, body.name, body.description, body.restart)
            return SnapshotOut(**asdict(snap))

        @api.post("/vms/{uuid}/move-disks", response_model=VMOut)
        def move_disks(uuid: str, body: DiskMovesIn) -> VMOut:
            return VMOut.from_info(driver.move_disks(uuid, body.as_dict()))

        @api.get("/vms/{uuid}/move-disks")
        def move_disks_progress(uuid: str) -> dict:
            return driver.disk_move_progress(uuid)

        @api.post("/vms/{uuid}/to-virtio", response_model=VMOut)
        def switch_to_virtio(uuid: str) -> VMOut:
            return VMOut.from_info(driver.switch_to_virtio(uuid))

        @api.post("/vms/{uuid}/sysprep", status_code=status.HTTP_204_NO_CONTENT)
        def sysprep_vm(uuid: str) -> None:
            driver.sysprep_vm(uuid)

        @api.post("/vms/{uuid}/rename", response_model=VMOut)
        def rename_vm(uuid: str, body: RenameIn) -> VMOut:
            return VMOut.from_info(driver.rename_vm(uuid, body.name))

        @api.post("/vms/{uuid}/snapshots/{name}/revert", response_model=VMOut)
        def revert_snapshot(uuid: str, name: str) -> VMOut:
            return VMOut.from_info(driver.revert_snapshot(uuid, name))

        @api.delete("/vms/{uuid}/snapshots/{name}", status_code=status.HTTP_204_NO_CONTENT)
        def delete_snapshot(uuid: str, name: str) -> None:
            driver.delete_snapshot(uuid, name)

        @api.post("/vms/{uuid}/recycled", response_model=VMOut)
        def set_recycled(uuid: str, body: dict) -> VMOut:
            return VMOut.from_info(driver.set_recycled(uuid, str(body.get("at", ""))))

        @api.post("/vms/{uuid}/template", response_model=VMOut)
        def set_template(uuid: str, body: TemplateRequest) -> VMOut:
            return VMOut.from_info(driver.set_template(uuid, body.template))

        # Runs until every disk is copied; the manager calls it from a background job.
        @api.post("/vms/{uuid}/clone", response_model=VMOut)
        def clone_vm(uuid: str, body: CloneIn) -> VMOut:
            return VMOut.from_info(driver.clone_vm(uuid, body.to_clone()))

        @api.get("/vms/{uuid}/definition")
        def vm_definition(uuid: str) -> dict:
            return {"definition": driver.vm_definition(uuid)}

        @api.post("/vms/define", response_model=VMOut)
        def define_vm(body: dict) -> VMOut:
            definition = body.get("definition")
            if not isinstance(definition, str) or not definition:
                raise ValueError("definition is required")
            return VMOut.from_info(driver.define_vm(definition))

        @api.post("/vms/{uuid}/forget", status_code=status.HTTP_204_NO_CONTENT)
        def forget_vm(uuid: str) -> None:
            driver.forget_vm(uuid)

        @api.get("/vms/{uuid}/migration-files")
        def migration_files(uuid: str) -> dict[str, str]:
            files = driver.migration_files(uuid)
            return {path: base64.b64encode(data).decode() for path, data in files.items()}

        @api.post("/migration-files")
        def stage_migration_files(body: dict) -> dict:
            raw = body.get("files")
            if not isinstance(raw, dict):
                raise ValueError("files must map paths to base64 content")
            try:
                files = {str(p): base64.b64decode(d, validate=True) for p, d in raw.items()}
            except (TypeError, binascii.Error):
                raise ValueError("file content must be base64") from None
            return {"staged": driver.stage_migration_files(files)}

        @api.post("/pools/refresh")
        def refresh_pools(body: dict) -> dict:
            names = body.get("names")
            if not isinstance(names, list):
                raise ValueError("names must be a list")
            driver.refresh_pools([str(n) for n in names])
            return {"refreshed": names}

        @api.post("/migration-files/remove")
        def remove_migration_files(body: dict) -> dict:
            paths = body.get("paths")
            if not isinstance(paths, list):
                raise ValueError("paths must be a list")
            return {"removed": driver.remove_migration_files([str(p) for p in paths])}

        # Runs until the VM has moved, which can take minutes for a busy guest.
        @api.post("/vms/{uuid}/migrate", status_code=status.HTTP_204_NO_CONTENT)
        def migrate_vm(uuid: str, body: AgentMigrate) -> None:
            driver.migrate_vm(
                uuid, body.dest_uri, body.copy_storage, body.bandwidth_mib, body.data_address
            )

        @api.get("/pools", response_model=list[PoolOut])
        def list_pools() -> list[PoolOut]:
            return [
                PoolOut.from_info(p).model_copy(update={"display_name": pool_labels.get(p.name)})
                for p in driver.list_pools()
            ]

        @api.patch("/pools/{name}", response_model=PoolOut)
        def rename_pool(name: str, body: NodeDisplayNameIn) -> PoolOut:
            pool = next((p for p in driver.list_pools() if p.name == name), None)
            if pool is None:
                raise HTTPException(404, "storage pool does not exist")
            label = pool_labels.set(name, body.display_name)
            return PoolOut.from_info(pool).model_copy(update={"display_name": label})

        @api.post("/pools", response_model=PoolOut, status_code=status.HTTP_201_CREATED)
        def create_pool(body: PoolCreate) -> PoolOut:
            return PoolOut.from_info(driver.create_pool(body.to_spec()))

        @api.post("/pools/{name}/cache", response_model=PoolOut)
        def pool_cache(name: str, body: PoolCacheIn) -> PoolOut:
            return PoolOut.from_info(driver.pool_cache(name, body.action, body.devices,
                                                       body.mode, body.size_gb))  # fmt: skip

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

        @api.post("/backups/cancel/{uuid}")
        def cancel_backup(uuid: str, target: str) -> dict:
            return {"accepted": backups.cancel_backup(uuid, target)}

        @api.get("/backups/progress/{uuid}")
        def backup_progress(uuid: str, target: str = "") -> dict:
            return backups.backup_progress(uuid, target)

        @api.post("/backups/run", response_model=BackupOut)
        def run_backup(body: AgentBackupRun) -> BackupOut:
            return BackupOut.from_info(backups.backup(body.uuid, target(body.target), body.mode))

        @api.post("/backups/synthetic", response_model=BackupOut)
        def synthetic_backup(body: AgentBackupOperation) -> BackupOut:
            return BackupOut.from_info(backups.synthetic_full(target(body.target), body.backup_id))

        @api.post("/backups/copy", response_model=BackupOut)
        def copy_backup(body: AgentBackupOperation) -> BackupOut:
            if body.destination is None:
                raise ValueError("Destination repository is required")
            return BackupOut.from_info(
                backups.copy(
                    target(body.target), target(body.destination), body.backup_id, body.archive
                )
            )

        @api.post("/backups/list", response_model=list[BackupOut])
        def list_backups(body: AgentBackupList) -> list[BackupOut]:
            return [BackupOut.from_info(b) for b in backups.list(target(body.target), body.vm_name)]

        @api.post("/backups/restore", response_model=VMOut)
        def restore(body: AgentRestore) -> VMOut:
            vm = backups.restore(target(body.target), body.backup_id, body.name, body.pool)
            return VMOut.from_info(vm)

        @api.post("/backups/verify")
        def verify_backup(body: AgentVerify) -> dict:
            return backups.verify(target(body.target), body.backup_id, body.pool, body.timeout)

        @api.post("/backups/usage")
        def backup_usage(body: AgentBackupList) -> dict:
            return backups.usage(target(body.target))

        @api.post("/backups/evidence")
        def backup_evidence(body: AgentBackupDelete) -> dict:
            return backups.evidence(target(body.target), body.backup_id)

        @api.post("/backups/delete")
        def delete_backup(body: AgentBackupDelete) -> dict:
            return {"freed_chunks": backups.delete(target(body.target), body.backup_id)}

        @api.post("/backups/prune")
        def prune(body: AgentPrune) -> dict:
            return {
                "removed": backups.prune(
                    target(body.target),
                    body.vm_name,
                    body.keep_last,
                    body.weekly,
                    body.monthly,
                    body.yearly,
                )
            }

        from ..replication import agent as replication_agent

        replication_agent.install(api, name, driver, replication_root, replication_guard)
        imports = imports or NodeImport(driver)

        def read_source(body: dict) -> Source:
            try:
                source = Source.from_dict(body)
            except (KeyError, TypeError, ValueError):
                raise ValueError("bad source request") from None
            source.validate()
            return source

        @api.get("/imports/capabilities")
        def import_capabilities() -> dict:
            return {"disk_targets": True, "nic_targets": True, "cpu_topology": True}

        @api.post("/imports/source-vms")
        def source_vms(body: dict) -> list[dict]:
            return [vm.to_dict() for vm in discover_source_vms(read_source(body))]

        @api.post("/imports/test-source")
        def check_source(body: dict) -> dict:
            result = test_source(read_source(body))
            result["node"] = name
            for check in result["checks"]:
                check["detail"] = f"Tested from node {name}; {check['detail']}"
            return result

        @api.get("/imports/progress")
        def import_progress() -> dict:
            return {name: list(p) for name, p in imports.progress.items()}

        @api.post("/imports/{name}/cancel")
        def cancel_import(name: str) -> dict:
            return {"stopped": imports.cancel(name)}

        @api.post("/vms/{uuid}/migrate/abort")
        def abort_migration(uuid: str) -> dict:
            return {"aborted": driver.abort_migration(uuid)}

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
                clear_hibernation=body.clear_hibernation,
                disks=[d.model_dump() for d in body.disks] if body.disks is not None else None,
                nics=[n.model_dump() for n in body.nics] if body.nics is not None else None,
                cpu_sockets=body.cpu_sockets,
                cpu_cores=body.cpu_cores,
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
