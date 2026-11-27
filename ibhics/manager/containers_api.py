"""Manager API for containers (v1.0): the cluster's LXC containers beside its VMs."""

from __future__ import annotations

import threading
import time
import urllib.request
from typing import Literal

from fastapi import status
from pydantic import BaseModel, Field

from ..cluster import NodeUnavailable
from ..hypervisor import InvalidState
from ..hypervisor.containers import (
    IMAGE_INDEX,
    ContainerNotFound,
    ContainerSpec,
    catalog,
    parse_image_index,
)
from .schemas import JobOut


class ContainerIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    template: str = "debian-13"
    node: str = ""
    vcpus: int = Field(default=1, ge=1, le=256)
    memory_mb: int = Field(default=512, ge=64)
    swap_mb: int = Field(default=512, ge=0)
    disk_gb: int = Field(default=8, ge=1)
    pool: str = "default"
    network: str = "default"
    ip: str = "dhcp"
    gateway: str = ""
    dns: str = ""
    password: str = ""
    ssh_key: str = ""
    unprivileged: bool = True
    nesting: bool = False
    autostart: bool = False
    description: str = ""


class ContainerUpdate(BaseModel):
    vcpus: int | None = Field(default=None, ge=1, le=256)
    memory_mb: int | None = Field(default=None, ge=64)
    disk_gb: int | None = Field(default=None, ge=1)
    autostart: bool | None = None
    description: str | None = None


class ContainerAction(BaseModel):
    action: str = Field(pattern="^(start|stop|kill|restart)$")


class ContainerExec(BaseModel):
    command: str = Field(min_length=1, max_length=4000)


class ContainerMove(BaseModel):
    pool: str = Field(min_length=1, max_length=64)


class ContainerRename(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")


class ContainerNicIn(BaseModel):
    network: str = Field(min_length=1, max_length=64)
    ip: str = Field(default="dhcp", max_length=43)
    gateway: str = Field(default="", max_length=15)
    mac: str = Field(default="", pattern=r"^$|^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


class ContainerNetworkIn(BaseModel):
    nics: list[ContainerNicIn] = Field(min_length=1, max_length=8)
    dns: str | None = Field(default=None, max_length=200)


class ContainerSnapIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


class ContainerBackupIn(BaseModel):
    target: str = Field(min_length=1)
    allow_stop: bool = False
    keep_last: int | None = Field(default=None, ge=1, le=1000)


class ContainerRestoreIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    node: str | None = None
    pool: str | None = None
    source_node: str | None = None


class ContainerMigrateIn(BaseModel):
    mode: Literal["live", "offline"] = "live"
    node: str = Field(min_length=1, max_length=253)
    allow_stop: bool = False


def install(api, cluster, backups, ha=None) -> None:
    def everywhere(strict=False):
        found = {}
        for member in cluster.compute_members():
            try:
                cts = member.driver.containers().list()
            except (NodeUnavailable, NotImplementedError) as exc:
                member.last_error = str(exc) or member.last_error
                if strict:
                    raise NodeUnavailable(
                        f"Cannot verify container ownership on {member.name}; retry when reachable"
                    ) from exc
                continue
            for ct in cts:
                # Both hosts see the same metadata on shared storage. A matching
                # path alone is insufficient: independent local pools can use it too.
                key = (
                    (ct.spec.name, ct.path, ct.spec.mac)
                    if ct.path and ct.spec.mac
                    else (member.name, ct.spec.name)
                )
                previous = found.get(key)
                if previous and previous[1].state == "running" and ct.state == "running":
                    raise InvalidState(f"Container {ct.spec.name!r} is running on multiple nodes")
                if (
                    previous is None
                    or ct.state == "running"
                    or (ct.owner_node and not previous[1].owner_node)
                ):
                    found[key] = (member, ct)
        yield from found.values()

    def find(name: str, *, claim=True):
        matches = [(m, c) for m, c in everywhere(strict=True) if c.spec.name == name]
        if len(matches) > 1:
            raise InvalidState(
                f"Ambiguous container {name!r}; different node copies require review"
            )
        if matches:
            member, ct = matches[0]
            backend = member.driver.containers()
            if claim and not ct.owner_node and hasattr(backend, "claim_owner"):
                ct = backend.claim_owner(name)
            return member, ct
        raise ContainerNotFound(name)

    def out(member, ct) -> dict:
        return {**ct.to_dict(), "node": member.name,
                "ha_enabled": bool(ha and ct.spec.name in ha.enabled)}

    def cts(member):
        return member.driver.containers()

    def task(kind, detail, name, member, work):
        return backups.run_task(kind, detail, name, member.name, f"ct:{name}", work)

    images: dict = {"at": 0.0, "list": []}

    @api.get("/containers/templates")
    def ct_templates() -> list[dict]:
        """Curated templates, then every image on images.linuxcontainers.org (read at most
        every 6 hours; without internet, the curated list alone)."""
        if time.monotonic() - images["at"] > 6 * 3600 or not images["at"]:
            try:
                with urllib.request.urlopen(IMAGE_INDEX, timeout=8) as resp:
                    images["list"] = parse_image_index(resp.read().decode())
                images["at"] = time.monotonic()
            except (OSError, ValueError):
                images["at"] = time.monotonic() - 5.5 * 3600  # try again in half an hour
        return catalog(images["list"])

    @api.get("/containers")
    def ct_list() -> list[dict]:
        return [out(m, c) for m, c in everywhere()]

    @api.get("/containers/ha/status")
    def ct_ha_status() -> dict:
        return ha.status() if ha else {"containers": [], "events": []}

    @api.put("/containers/{name}/ha")
    def ct_ha(name: str, body: dict) -> dict:
        if ha is None:
            raise InvalidState("Container HA is not available here")
        if not isinstance(body.get("enabled"), bool):
            raise ValueError("enabled must be a boolean")
        ha.set_enabled(name, body["enabled"])
        return {"name": name, "enabled": name in ha.enabled}

    @api.get("/containers/{name}")
    def ct_get(name: str) -> dict:
        return out(*find(name, claim=False))

    @api.post("/containers/{name}/migrate")
    def ct_migrate(name: str, body: ContainerMigrateIn) -> dict:
        cluster.require_quorum()
        source, ct = find(name)
        destination = cluster.member(body.node)
        if destination.name == source.name:
            raise InvalidState("Select a different destination node")
        if (
            destination.role != "compute"
            or destination.maintenance
            or not cluster.is_healthy(destination)
        ):
            raise InvalidState("Destination must be a healthy compute node outside maintenance")
        if ct.state == "running" and body.mode == "offline" and not body.allow_stop:
            raise InvalidState("Allow a temporary stop before migrating a running container")

        def migrate():
            receipt = None
            committing = False
            try:
                receipt = cts(source).migration(
                    "prepare", name,
                    {"node": destination.name, "allow_stop": body.allow_stop, "mode": body.mode},
                )
                cts(destination).migration("probe", name, receipt)
                committing = True
                cts(source).migration("commit", name, receipt)
                result = cts(destination).migration("finish", name, receipt)
                message = "Container moved (stopped)"
                if result.get("method") == "live":
                    message = "Live (memory kept)"
                elif result.get("method") == "restart":
                    message = "Restarted on destination"
                    if result.get("live_error"):
                        message += f": {result['live_error']}"
                return {**result, "node": destination.name, "result": message}
            except Exception:
                # After an uncertain commit, leave the durable fence in place.
                # Restarting a source after a lost RPC could duplicate a running guest.
                if receipt and not committing:
                    cts(source).migration("abort", name, receipt)
                raise

        return task("ct-migrate", f"{source.name} → {destination.name}", name, source, migrate)

    @api.post("/containers/{name}/migration-recover")
    def ct_migration_recover(name: str) -> dict:
        cluster.require_quorum()
        member, ct = find(name, claim=False)
        return task(
            "ct-migrate-recover",
            member.name,
            name,
            member,
            lambda: {**cts(member).migration("recover", name, {}), "node": member.name},
        )

    @api.post("/containers/{name}/backup", response_model=JobOut, status_code=202)
    def ct_backup(name: str, body: ContainerBackupIn) -> JobOut:
        cluster.require_quorum()
        member, ct = find(name)
        if ct.state == "running" and not body.allow_stop:
            raise InvalidState("Allow a temporary stop or stop the container before backup")
        return JobOut(
            **backups.start_container_backup(
                member, name, body.target, body.allow_stop, body.keep_last
            ).__dict__
        )

    @api.post(
        "/backup/targets/{target}/backups/{backup_id}/restore-container",
        response_model=JobOut,
        status_code=202,
    )
    def ct_restore_backup(target: str, backup_id: str, body: ContainerRestoreIn) -> JobOut:
        cluster.require_quorum()
        names = {c.spec.name for _, c in everywhere(strict=True)}
        names |= {vm.spec.name for _, vm in cluster.all_vms()}
        if body.name in names:
            raise InvalidState("A VM or container with this name already exists")
        return JobOut(
            **backups.start_container_restore(
                target, backup_id, body.name, body.node, body.pool, body.source_node
            ).__dict__
        )

    @api.post("/containers", status_code=status.HTTP_202_ACCEPTED)
    def ct_create(body: ContainerIn) -> dict:
        cluster.require_quorum()
        spec = ContainerSpec(**body.model_dump(exclude={"node"}))
        spec.validate()
        names = {c.spec.name for _, c in everywhere()}
        names |= {vm.spec.name for _, vm in cluster.all_vms()}
        if spec.name in names:
            raise InvalidState(f"a VM or container named {spec.name!r} already exists")
        member = cluster.member(body.node) if body.node else cluster.pick_node(spec.memory_mb)
        cluster.require_in_service(member)

        def work():
            try:
                task(
                    "container-create",
                    spec.template,
                    spec.name,
                    member,
                    lambda: cts(member).create(spec),
                )
            except Exception:  # noqa: S110 - the task in Tasks holds the error
                pass

        threading.Thread(target=work, name=f"ct-create-{spec.name}", daemon=True).start()
        return {"name": spec.name, "node": member.name, "state": "creating"}

    @api.post("/containers/{name}/action")
    def ct_action(name: str, body: ContainerAction) -> dict:
        cluster.require_quorum()
        member, _ = find(name)
        if body.action in ("start", "restart"):
            cluster.require_in_service(member)
        return out(
            member,
            task(
                f"container-{body.action}",
                body.action,
                name,
                member,
                lambda: cts(member).action(name, body.action),
            ),
        )

    @api.patch("/containers/{name}")
    def ct_update(name: str, body: ContainerUpdate) -> dict:
        cluster.require_quorum()
        member, _ = find(name)
        args = body.model_dump()
        return out(
            member,
            task(
                "container-resize",
                "settings",
                name,
                member,
                lambda: cts(member).update(name, **args),
            ),
        )

    @api.put("/containers/{name}/network")
    def ct_network(name: str, body: ContainerNetworkIn) -> dict:
        """Network cards: network (VLAN), DHCP or fixed IP and gateway, DNS. A running
        container restarts to take them."""
        cluster.require_quorum()
        member, _ = find(name)
        cards = [n.model_dump() for n in body.nics]
        return out(
            member,
            task(
                "container-network",
                f"{len(cards)} card(s)",
                name,
                member,
                lambda: cts(member).set_network(name, cards, body.dns),
            ),  # fmt: skip
        )

    @api.post("/containers/{name}/rename")
    def ct_rename(name: str, body: ContainerRename) -> dict:
        cluster.require_quorum()
        member, _ = find(name)
        if body.name != name:
            taken = {c.spec.name for _, c in everywhere()}
            taken |= {vm.spec.name for _, vm in cluster.all_vms()}
            if body.name in taken:
                raise InvalidState(f"a VM or container named {body.name!r} already exists")
        result = out(
            member,
            task(
                "rename",
                f"{name} → {body.name}",
                name,
                member,
                lambda: cts(member).rename(name, body.name),
            ),  # fmt: skip
        )
        if ha:
            ha.renamed(name, body.name)
            result["ha_enabled"] = body.name in ha.enabled
        return result

    @api.delete("/containers/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def ct_delete(name: str) -> None:
        cluster.require_quorum()
        member, ct = find(name)
        if ct.state != "stopped":
            raise InvalidState("Stop the container before deleting it")

        def remove():
            # Recheck after acquiring the task lock; another request may have
            # started it while this deletion was waiting.
            owner, current = find(name)
            if owner.name != member.name or current.state != "stopped":
                raise InvalidState("Container ownership or state changed; refresh before deleting")
            return cts(owner).delete(name)

        task("container-delete", name, name, member, remove)
        if ha and name in ha.enabled:
            ha.set_enabled(name, False)

    @api.post("/containers/{name}/exec")
    def ct_exec(name: str, body: ContainerExec) -> dict:
        member, _ = find(name)
        code, output = member.driver.containers().exec(name, body.command)
        return {"code": code, "output": output}

    @api.post("/containers/{name}/jobs")
    def ct_job_start(name: str, body: ContainerExec) -> dict:
        """A console command that may run long; read its output with GET .../jobs/{job}."""
        member, _ = find(name)
        return {"job": cts(member).exec_start(name, body.command)}

    @api.get("/containers/{name}/jobs/{job}")
    def ct_job_read(name: str, job: str, offset: int = 0) -> dict:
        member, _ = find(name)
        return cts(member).exec_read(name, job, offset)

    @api.delete("/containers/{name}/jobs/{job}", status_code=status.HTTP_204_NO_CONTENT)
    def ct_job_stop(name: str, job: str) -> None:
        member, _ = find(name)
        cts(member).exec_stop(name, job)

    @api.post("/containers/{name}/move")
    def ct_move(name: str, body: ContainerMove) -> dict:
        cluster.require_quorum()
        if ha and name in ha.enabled:
            raise InvalidState("Disable container HA before moving its storage pool")
        member, _ = find(name)
        return out(
            member,
            task(
                "ct-move", f"→ {body.pool}", name, member, lambda: cts(member).move(name, body.pool)
            ),  # fmt: skip
        )

    @api.post("/containers/{name}/snapshots", status_code=status.HTTP_201_CREATED)
    def ct_snapshot(name: str, body: ContainerSnapIn) -> dict:
        cluster.require_quorum()
        member, _ = find(name)
        return out(
            member,
            task(
                "snapshot", body.name, name, member, lambda: cts(member).snapshot(name, body.name)
            ),
        )

    @api.post("/containers/{name}/snapshots/{snap}/restore")
    def ct_restore(name: str, snap: str) -> dict:
        cluster.require_quorum()
        member, _ = find(name)
        return out(
            member,
            task("snapshot-restore", snap, name, member, lambda: cts(member).restore(name, snap)),
        )

    @api.delete("/containers/{name}/snapshots/{snap}")
    def ct_delete_snapshot(name: str, snap: str) -> dict:
        cluster.require_quorum()
        member, _ = find(name)
        return out(
            member,
            task(
                "snapshot-delete",
                snap,
                name,
                member,
                lambda: cts(member).delete_snapshot(name, snap),
            ),
        )


__all__ = ["install"]
