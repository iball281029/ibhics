"""Restart protected VMs on another node when their node stops answering.

Safety does not rest on timing alone. VM disks live on shared NFSv4 storage where
QEMU holds a write lock on every image it runs; another node cannot open the image
while the original process still runs. A restart that meets that lock is undone and
tried again later, so a slow or cut-off node never ends up with a second copy of the
VM writing the same disk.

Limits: the manager runs on one node only. VMs on that node are not restarted
elsewhere if the node itself fails.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..cluster import Cluster, Member, NodeRole
from ..hypervisor import InvalidState, VMState
from .migrations import MigrationManager, _shared

GRACE_S = 90  # NFSv4 lease time: a dead node's image locks are gone after this
REFRESH_S = 60  # how often the saved definition and UEFI files are refreshed
RETRY_S = 30


class HighAvailability:
    def __init__(
        self,
        cluster: Cluster,
        state_path: Path | None = None,
        grace_s: float = GRACE_S,
        clock: Callable[[], float] | None = None,
        wall: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._cluster = cluster
        self._path = state_path
        self._grace = grace_s
        self._clock = clock or getattr(cluster, "_clock", time.monotonic)
        self._wall = wall
        self._lock = threading.Lock()
        self.enabled: set[str] = set()
        self.records: dict[str, dict] = {}  # uuid -> node, name, running, definition, files
        self.moved: dict[str, dict] = {}  # uuid -> {"from", "to"} until the old node is clean
        self.events: list[dict] = []
        self._refreshed: dict[str, float] = {}
        self._retry: dict[str, float] = {}
        self._load()

    # Settings

    def set_enabled(self, uuid: str, enabled: bool) -> None:
        member, vm = self._cluster.find_vm(uuid)
        if enabled:
            pools = {p.name: p for p in member.driver.list_pools()}
            others = [m for m in self._computes() if m.name != member.name]
            for disk in vm.spec.disks:
                if not others or not all(
                    _shared(pools.get(disk.pool), self._pool(o, disk.pool)) for o in others
                ):
                    raise InvalidState(
                        f"{vm.spec.name}: disk in {disk.pool!r} is not on storage shared with "
                        "the other compute nodes, so it cannot restart elsewhere"
                    )
        with self._lock:
            (self.enabled.add if enabled else self.enabled.discard)(uuid)
            if not enabled:
                self.records.pop(uuid, None)
            self._save()
        self._event("enable" if enabled else "disable", vm.spec.name, member.name)

    @staticmethod
    def _pool(member: Member, name: str):
        return next((p for p in member.driver.list_pools() if p.name == name), None)

    # The loop

    def _computes(self) -> list[Member]:
        return [m for m in self._cluster.members() if m.role == NodeRole.COMPUTE]

    def _down_for(self, member: Member) -> float:
        if member.local or self._cluster.is_healthy(member):
            return 0.0
        if member.last_seen is None:
            return float("inf")
        return self._clock() - member.last_seen

    def tick(self) -> None:
        if not self.enabled:
            return
        if not self._cluster.has_quorum():
            return  # this side may be the cut-off one: never act without a majority
        seen = self._observe()
        self._reconcile(seen)
        self._restart(seen)
        with self._lock:
            self._save()

    def _observe(self) -> dict[str, dict[str, VMState]]:
        """uuid -> {node: state} for protected VMs on every reachable compute."""
        seen: dict[str, dict[str, VMState]] = {}
        now = self._clock()
        for member in self._cluster.compute_members():
            try:
                vms = member.driver.list_vms()
            except Exception:
                continue
            for vm in vms:
                if vm.uuid not in self.enabled:
                    continue
                seen.setdefault(vm.uuid, {})[member.name] = vm.state
                moved = self.moved.get(vm.uuid)
                if moved and moved["to"] != member.name:
                    continue  # the stale copy on the old node; _reconcile handles it
                record = self.records.setdefault(vm.uuid, {})
                record.update(node=member.name, name=vm.spec.name)
                record["running"] = vm.state in (VMState.RUNNING, VMState.PAUSED)
                if now - self._refreshed.get(vm.uuid, -1e9) >= REFRESH_S:
                    try:
                        record["definition"] = member.driver.vm_definition(vm.uuid)
                        files = member.driver.migration_files(vm.uuid)
                        record["files"] = {
                            p: base64.b64encode(d).decode() for p, d in files.items()
                        }
                        self._refreshed[vm.uuid] = now
                    except Exception as exc:
                        self._event("refresh", vm.spec.name, member.name, str(exc))
        return seen

    def _reconcile(self, seen: dict[str, dict[str, VMState]]) -> None:
        """An old node is back: drop its copy of a VM that now runs elsewhere."""
        for uuid, move in list(self.moved.items()):
            where = seen.get(uuid, {})
            old, new = move["from"], move["to"]
            if old in where and new in where:
                try:
                    member = next(m for m in self._computes() if m.name == old)
                    member.driver.forget_vm(uuid)
                except Exception as exc:
                    self._event("cleanup", move.get("name", uuid), old, str(exc))
                    continue
                self._event("cleanup", move.get("name", uuid), old)
                del self.moved[uuid]
            elif old not in where and self._node_up(old):
                del self.moved[uuid]  # nothing was left behind there

    def _node_up(self, name: str) -> bool:
        return any(m.name == name and self._cluster.is_healthy(m) for m in self._computes())

    def _restart(self, seen: dict[str, dict[str, VMState]]) -> None:
        now = self._clock()
        for uuid in sorted(self.enabled):
            record = self.records.get(uuid)
            if not record or not record.get("running") or not record.get("definition"):
                continue
            node = next((m for m in self._computes() if m.name == record["node"]), None)
            if node is None or self._down_for(node) <= self._grace:
                continue
            if uuid in seen or now < self._retry.get(uuid, 0):
                continue
            self._restart_one(uuid, record, node)

    def _restart_one(self, uuid: str, record: dict, failed: Member) -> None:
        name = record.get("name", uuid)
        definition = record["definition"]
        files = {p: base64.b64decode(d) for p, d in record.get("files", {}).items()}
        memory = _memory_mb(definition)
        targets = sorted(
            (m for m in self._cluster.compute_members() if m.name != failed.name),
            key=lambda m: -_free(m),
        )
        if not targets or _free(targets[0]) < memory:
            self._retry[uuid] = self._clock() + RETRY_S
            self._event("restart", name, failed.name, "no other node has enough free memory")
            return
        target = targets[0]
        staged, defined = [], False
        try:
            staged = target.driver.stage_migration_files(files) if files else []
            target.driver.define_vm(definition)
            defined = True
            target.driver.start_vm(uuid)
        except Exception as exc:
            if defined:
                try:
                    target.driver.forget_vm(uuid)
                except Exception:
                    pass
            elif staged:
                try:
                    target.driver.remove_migration_files(staged)
                except Exception:
                    pass
            self._retry[uuid] = self._clock() + RETRY_S
            reason = str(exc) or type(exc).__name__
            if "lock" in reason.lower():
                reason = f"disk still in use by {failed.name}; waiting ({reason})"
            self._event("restart", name, target.name, reason)
            return
        self.moved[uuid] = {"from": failed.name, "to": target.name, "name": name}
        record["node"] = target.name
        self._event("restart", name, target.name, None, f"restarted after {failed.name} failed")

    # Status

    def status(self) -> dict:
        with self._lock:
            return {
                "grace_seconds": self._grace,
                "vms": [
                    {
                        "uuid": u,
                        "name": self.records.get(u, {}).get("name"),
                        "node": self.records.get(u, {}).get("node"),
                        "running": self.records.get(u, {}).get("running"),
                        "ready": bool(self.records.get(u, {}).get("definition")),
                    }
                    for u in sorted(self.enabled)
                ],
                "moved": dict(self.moved),
                "events": list(self.events),
            }

    def _event(self, action, vm, node, error=None, detail=None) -> None:
        with self._lock:
            self.events.append(
                {
                    "at": self._wall().isoformat(timespec="seconds"),
                    "action": action,
                    "vm": vm,
                    "node": node,
                    "ok": error is None,
                    "error": error,
                    "detail": detail,
                }
            )
            del self.events[:-100]

    # Persistence: definitions hold no secrets beyond what libvirt already stores.

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        data = json.loads(self._path.read_text())
        self.enabled = set(data.get("enabled", []))
        self.records = data.get("records", {})
        self.moved = data.get("moved", {})

    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"enabled": sorted(self.enabled), "records": self.records, "moved": self.moved}
            )
        )
        tmp.chmod(0o600)
        tmp.replace(self._path)


def _free(member: Member) -> int:
    try:
        return MigrationManager._free_memory(member)
    except Exception:
        return -1


def _memory_mb(definition: str) -> int:
    """Memory a definition needs: libvirt XML (KiB) or the fake driver's JSON."""
    if definition.lstrip().startswith("{"):
        return int(json.loads(definition).get("memory_mb", 0))
    import xml.etree.ElementTree as ET

    el = ET.fromstring(definition).find("memory")
    if el is None:
        return 0
    unit = (el.get("unit") or "KiB").lower()
    scale = {"kib": 1 / 1024, "k": 1 / 1024, "mib": 1, "m": 1, "gib": 1024, "g": 1024}
    return int(float(el.text or 0) * scale.get(unit, 1 / 1024))
