"""Manager API for containers (v1.0): the cluster's LXC containers beside its VMs."""

from __future__ import annotations

import threading

from fastapi import status
from pydantic import BaseModel, Field

from ..cluster import NodeUnavailable
from ..hypervisor import InvalidState
from ..hypervisor.containers import ContainerNotFound, ContainerSpec, catalog


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


class ContainerSnapIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def install(api, cluster, backups) -> None:
    def everywhere():
        for member in cluster.compute_members():
            try:
                cts = member.driver.containers().list()
            except (NodeUnavailable, NotImplementedError) as exc:
                member.last_error = str(exc) or member.last_error
                continue
            for ct in cts:
                yield member, ct

    def find(name: str):
        for member, ct in everywhere():
            if ct.spec.name == name:
                return member, ct
        raise ContainerNotFound(name)

    def out(member, ct) -> dict:
        return {**ct.to_dict(), "node": member.name}

    def cts(member):
        return member.driver.containers()

    def task(kind, detail, name, member, work):
        return backups.run_task(kind, detail, name, member.name, f"ct:{name}", work)

    @api.get("/containers/templates")
    def ct_templates() -> list[dict]:
        return catalog()

    @api.get("/containers")
    def ct_list() -> list[dict]:
        return [out(m, c) for m, c in everywhere()]

    @api.get("/containers/{name}")
    def ct_get(name: str) -> dict:
        return out(*find(name))

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

    @api.delete("/containers/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def ct_delete(name: str) -> None:
        cluster.require_quorum()
        member, _ = find(name)
        task("container-delete", name, name, member, lambda: cts(member).delete(name))

    @api.post("/containers/{name}/exec")
    def ct_exec(name: str, body: ContainerExec) -> dict:
        member, _ = find(name)
        code, output = member.driver.containers().exec(name, body.command)
        return {"code": code, "output": output}

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
