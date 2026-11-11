"""Cluster membership as seen by the manager: who is in, who is alive, who has quorum.

This is the manager's view built from agent heartbeats. Moving the membership
record into etcd (node 1, node 2 and the witness as its three voters) is the
next step; the quorum rule here is the same one etcd will enforce.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..backup import NodeBackup
from ..hypervisor import HypervisorDriver, VMInfo, VMNotFound
from ..importer import NodeImport
from .remote import AgentClient, NodeUnavailable, RemoteBackup, RemoteDriver, RemoteImport
from .topology import NodeRole, Topology, classify, has_quorum


class NoQuorum(RuntimeError):
    pass


class MembershipError(ValueError):
    pass


@dataclass
class Member:
    name: str
    role: NodeRole
    address: str | None  # None: runs inside the manager process
    driver: HypervisorDriver | None = None
    client: AgentClient | None = None
    backups: NodeBackup | RemoteBackup | None = None  # runs backup jobs on this node
    imports: NodeImport | RemoteImport | None = None  # converts VMs brought in by Migrate
    last_seen: float | None = None
    last_error: str | None = None
    # Maintenance mode: no new VM is placed, started or moved here (v0.9).
    maintenance: bool = False
    display_name: str = ""
    agent_token: str = field(default="", repr=False)

    @property
    def local(self) -> bool:
        return self.address is None


ClientFactory = Callable[[str, str], AgentClient]


class Cluster:
    def __init__(
        self,
        token: str = "",
        heartbeat_timeout: float = 15.0,
        state_path: Path | None = None,
        client_factory: ClientFactory = AgentClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self._timeout = heartbeat_timeout
        self._state_path = state_path
        self._client_factory = client_factory
        self._clock = clock
        self._members: dict[str, Member] = {}
        self._lock = threading.RLock()
        self.cluster_id = ""
        self.cluster_name = ""
        self.local_agent = ""  # this node's agent URL, saved for a standby manager

    @classmethod
    def standalone(cls, name: str, driver: HypervisorDriver, **kwargs) -> Cluster:
        cluster = cls(**kwargs)
        cluster.add_local(name, driver)
        return cluster

    # Membership

    def pairing(self) -> dict:
        if self.cluster_id or len(self._members) != 1:
            raise MembershipError("only a standalone server can supply a pairing key")
        if not self._token:
            raise MembershipError("this server has no agent key configured")
        local = next(iter(self._members.values()))
        return {
            "name": local.name,
            "display_name": local.display_name or local.name,
            "agent_token": self._token,
        }

    def create_cluster(self, name: str) -> None:
        name = name.strip()
        if (
            not name
            or len(name) > 80
            or any(unicodedata.category(char).startswith("C") for char in name)
        ):
            raise MembershipError("cluster name must be 1–80 characters without control characters")
        with self._lock:
            if self.cluster_id:
                raise MembershipError("this node already belongs to a created cluster")
            if len(self._members) != 1 or not next(iter(self._members.values())).local:
                raise MembershipError("create a cluster from a standalone node first")
            self.cluster_id, self.cluster_name = str(uuid.uuid4()), name
            try:
                self._save()
            except Exception:
                self.cluster_id = self.cluster_name = ""
                raise

    _maintenance: set[str] = set()

    def add_local(
        self,
        name: str,
        driver: HypervisorDriver,
        backups: NodeBackup | None = None,
        imports: NodeImport | None = None,
    ) -> Member:
        with self._lock:
            self._ensure_free(name)
            member = Member(
                name=name,
                role=NodeRole.COMPUTE,
                address=None,
                driver=driver,
                backups=backups or NodeBackup(driver),
                imports=imports or NodeImport(driver),
            )
            self._members[name] = member
            return member

    def join(self, name: str, role: NodeRole, address: str, agent_token: str = "") -> Member:
        token = agent_token or self._token
        if not token:
            raise MembershipError("set a cluster token before adding remote nodes")
        client = self._client_factory(address, token)
        health = client.health()  # raises NodeUnavailable if unreachable
        if health.get("name") != name:
            raise MembershipError(
                f"agent at {address} reports name {health.get('name')!r}, expected {name!r}"
            )
        if health.get("role") != role:
            raise MembershipError(
                f"agent at {address} runs as {health.get('role')!r}, expected {str(role)!r}"
            )
        with self._lock:
            self._ensure_free(name)
            if role == NodeRole.WITNESS and any(
                m.role == NodeRole.WITNESS for m in self._members.values()
            ):
                raise MembershipError("the cluster already has a witness")
            member = Member(
                agent_token=agent_token,
                name=name,
                role=role,
                address=address,
                client=client,
                driver=RemoteDriver(client) if role == NodeRole.COMPUTE else None,
                backups=RemoteBackup(client) if role == NodeRole.COMPUTE else None,
                imports=RemoteImport(client) if role == NodeRole.COMPUTE else None,
                last_seen=self._clock(),
            )
            self._members[name] = member
            self._save()
            return member

    def remove(self, name: str) -> None:
        with self._lock:
            member = self._members.get(name)
            if member is None:
                raise MembershipError(f"no node named {name!r}")
            if member.local:
                raise MembershipError("the node running the manager cannot be removed")
            del self._members[name]
            self._save()

    def load(self) -> None:
        """Re-add remote members saved by a previous run. Unreachable ones start unhealthy."""
        if not self._state_path or not self._state_path.exists():
            return
        data = json.loads(self._state_path.read_text())
        metadata = data.get("cluster", {})
        self.cluster_id = metadata.get("id", "")
        self.cluster_name = metadata.get("name", "")
        self._maintenance = set(data.get("maintenance", []))
        display_names = data.get("display_names", {})
        for member in self._members.values():
            member.maintenance = member.name in self._maintenance
            member.display_name = display_names.get(member.name, "")
        for entry in data["members"]:
            client = self._client_factory(entry["address"], entry.get("agent_token") or self._token)
            role = NodeRole(entry["role"])
            with self._lock:
                if entry["name"] in self._members:
                    continue
                self._members[entry["name"]] = Member(
                    agent_token=entry.get("agent_token", ""),
                    display_name=display_names.get(entry["name"], ""),
                    maintenance=entry["name"] in self._maintenance,
                    name=entry["name"],
                    role=role,
                    address=entry["address"],
                    client=client,
                    driver=RemoteDriver(client) if role == NodeRole.COMPUTE else None,
                    backups=RemoteBackup(client) if role == NodeRole.COMPUTE else None,
                    imports=RemoteImport(client) if role == NodeRole.COMPUTE else None,
                )

    def set_display_name(self, name: str, display_name: str) -> Member:
        """Change a label without changing membership keys or VM/job references."""
        display_name = display_name.strip()
        if len(display_name) > 80 or any(
            unicodedata.category(char).startswith("C") for char in display_name
        ):
            raise ValueError(
                "display name must be at most 80 characters without control characters"
            )
        with self._lock:
            member = self._members.get(name)
            if member is None:
                raise MembershipError(f"no node named {name!r}")
            previous = member.display_name
            member.display_name = display_name
            try:
                self._save()
            except Exception:
                member.display_name = previous
                raise
            return member

    # Health and quorum

    def refresh(self) -> None:
        with self._lock:
            remote = [m for m in self._members.values() if not m.local]
        for member in remote:
            try:
                member.client.health()
            except Exception as exc:  # any failure, including a garbled reply, means unhealthy
                member.last_error = str(exc)
            else:
                member.last_seen = self._clock()
                member.last_error = None

    def is_healthy(self, member: Member) -> bool:
        if member.local:
            return True
        return member.last_seen is not None and self._clock() - member.last_seen <= self._timeout

    def has_quorum(self) -> bool:
        with self._lock:
            members = list(self._members.values())
        return has_quorum(sum(self.is_healthy(m) for m in members), len(members))

    def require_quorum(self) -> None:
        if not self.has_quorum():
            raise NoQuorum(
                "cluster has lost quorum; changes are blocked until a majority of nodes is back"
            )

    def topology(self) -> Topology:
        with self._lock:
            return classify(m.role for m in self._members.values())

    def members(self) -> list[Member]:
        with self._lock:
            return list(self._members.values())

    def compute_members(self, healthy_only: bool = True) -> list[Member]:
        return [
            m
            for m in self.members()
            if m.role == NodeRole.COMPUTE and (not healthy_only or self.is_healthy(m))
        ]

    # VM placement

    def member(self, name: str) -> Member:
        with self._lock:
            member = self._members.get(name)
        if member is None or member.role != NodeRole.COMPUTE:
            raise MembershipError(f"no compute node named {name!r}")
        if not self.is_healthy(member):
            raise NodeUnavailable(f"node {name!r} is not responding")
        return member

    def all_vms(self) -> list[tuple[Member, VMInfo]]:
        result = []
        for member in self.compute_members():
            try:
                result.extend((member, vm) for vm in member.driver.list_vms())
            except NodeUnavailable as exc:
                member.last_error = str(exc)
        return result

    def find_vm(self, uuid: str) -> tuple[Member, VMInfo]:
        for member in self.compute_members():
            try:
                return member, member.driver.get_vm(uuid)
            except VMNotFound:
                continue
            except NodeUnavailable as exc:
                member.last_error = str(exc)
        raise VMNotFound(uuid)

    def pick_node(self, memory_mb: int) -> Member:
        """Place a new VM on the healthy compute node with the most unallocated memory."""
        best, best_free = None, None
        for member in self.compute_members():
            if member.maintenance:
                continue
            try:
                host = member.driver.host_info()
                used = sum(vm.spec.memory_mb for vm in member.driver.list_vms())
            except NodeUnavailable:
                continue
            free = host.memory_mb - used
            if best_free is None or free > best_free:
                best, best_free = member, free
        if best is None:
            raise NodeUnavailable("no compute node is available (all are down or in maintenance)")
        return best

    def set_maintenance(self, name: str, on: bool) -> Member:
        member = self.member(name)
        member.maintenance = on
        self._save()
        return member

    def require_in_service(self, member: Member) -> None:
        from ..hypervisor import InvalidState

        if member.maintenance:
            raise InvalidState(
                f"node {member.name} is in maintenance mode: exit maintenance first, or "
                "move the VM to another node"
            )

    # Internals

    def _ensure_free(self, name: str) -> None:
        if name in self._members:
            raise MembershipError(f"a node named {name!r} is already in the cluster")

    def save(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        if not self._state_path:
            return
        entries = [
            {
                "name": m.name,
                "role": str(m.role),
                "address": m.address,
                **({"agent_token": m.agent_token} if m.agent_token else {}),
            }
            for m in self._members.values()
            if not m.local
        ]
        local = next((m for m in self._members.values() if m.local), None)
        if local is not None and self.local_agent:
            entries.append(
                {
                    "name": local.name,
                    "role": str(local.role),
                    "address": self.local_agent,
                    **({"agent_token": self._token} if self._token else {}),
                }
            )
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        maintenance = sorted(m.name for m in self._members.values() if m.maintenance)
        display_names = {m.name: m.display_name for m in self._members.values() if m.display_name}
        data = {"members": entries, "maintenance": maintenance, "display_names": display_names}
        if self.cluster_id:
            data["cluster"] = {"id": self.cluster_id, "name": self.cluster_name}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, indent=2)
        tmp.replace(self._state_path)
