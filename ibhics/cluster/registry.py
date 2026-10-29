"""Cluster membership as seen by the manager: who is in, who is alive, who has quorum.

This is the manager's view built from agent heartbeats. Moving the membership
record into etcd (node 1, node 2 and the witness as its three voters) is the
next step; the quorum rule here is the same one etcd will enforce.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..hypervisor import HypervisorDriver, VMInfo, VMNotFound
from .remote import AgentClient, NodeUnavailable, RemoteDriver
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
    last_seen: float | None = None
    last_error: str | None = None

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

    @classmethod
    def standalone(cls, name: str, driver: HypervisorDriver, **kwargs) -> Cluster:
        cluster = cls(**kwargs)
        cluster.add_local(name, driver)
        return cluster

    # Membership

    def add_local(self, name: str, driver: HypervisorDriver) -> Member:
        with self._lock:
            self._ensure_free(name)
            member = Member(name=name, role=NodeRole.COMPUTE, address=None, driver=driver)
            self._members[name] = member
            return member

    def join(self, name: str, role: NodeRole, address: str) -> Member:
        if not self._token:
            raise MembershipError("set a cluster token before adding remote nodes")
        client = self._client_factory(address, self._token)
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
                name=name,
                role=role,
                address=address,
                client=client,
                driver=RemoteDriver(client) if role == NodeRole.COMPUTE else None,
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
        for entry in json.loads(self._state_path.read_text())["members"]:
            client = self._client_factory(entry["address"], self._token)
            role = NodeRole(entry["role"])
            with self._lock:
                if entry["name"] in self._members:
                    continue
                self._members[entry["name"]] = Member(
                    name=entry["name"],
                    role=role,
                    address=entry["address"],
                    client=client,
                    driver=RemoteDriver(client) if role == NodeRole.COMPUTE else None,
                )

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
            try:
                host = member.driver.host_info()
                used = sum(vm.spec.memory_mb for vm in member.driver.list_vms())
            except NodeUnavailable:
                continue
            free = host.memory_mb - used
            if best_free is None or free > best_free:
                best, best_free = member, free
        if best is None:
            raise NodeUnavailable("no compute node is available")
        return best

    # Internals

    def _ensure_free(self, name: str) -> None:
        if name in self._members:
            raise MembershipError(f"a node named {name!r} is already in the cluster")

    def _save(self) -> None:
        if not self._state_path:
            return
        entries = [
            {"name": m.name, "role": str(m.role), "address": m.address}
            for m in self._members.values()
            if not m.local
        ]
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"members": entries}, indent=2))
        tmp.replace(self._state_path)
