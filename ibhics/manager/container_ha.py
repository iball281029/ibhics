"""Quorum-gated container restart. The agent's NFS run lock fences takeover."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from ..hypervisor import InvalidState
from .events import publish
from .ha import GRACE_S, RETRY_S, _free
from .migrations import _shared


class ContainerHA:
    def __init__(self, cluster, state_path=None, grace_s=GRACE_S, clock=None, tasks=None):
        self._cluster = cluster
        self._path = state_path
        self._grace = grace_s
        self._clock = clock or getattr(cluster, "_clock", time.monotonic)
        self._lock = threading.RLock()
        self._tasks = tasks
        self.enabled = set()
        self.records = {}
        self.events = []
        self._retry = {}
        if state_path and state_path.exists():
            data = json.loads(state_path.read_text())
            self.enabled = set(data.get("enabled", []))
            self.records = data.get("records", {})

    def set_enabled(self, name, enabled):
        self._cluster.require_quorum()
        with self._lock:
            if enabled:
                found = [
                    (m, ct)
                    for m in self._cluster.compute_members()
                    for ct in m.driver.containers().list()
                    if ct.spec.name == name
                ]
                if len(found) != 1:
                    raise InvalidState("Container owner is unavailable or ambiguous")
                member, ct = found[0]
                if ct.migration_pending:
                    raise InvalidState("Finish container migration before enabling HA")
                pool = next((p for p in member.driver.list_pools() if p.name == ct.spec.pool), None)
                others = [m for m in self._cluster.compute_members(False) if m.name != member.name]
                if not others or not all(
                    any(p.name == ct.spec.pool and _shared(pool, p) for p in m.driver.list_pools())
                    for m in others
                ):
                    raise InvalidState(
                        "Container HA requires storage shared with other compute nodes"
                    )
                self.enabled.add(name)
                self.records[name] = dict(
                    node=member.name,
                    running=ct.state == "running",
                    memory_mb=ct.spec.memory_mb,
                    pool=ct.spec.pool,
                )
            else:
                self.enabled.discard(name)
                self.records.pop(name, None)
            self._save()

    def renamed(self, old, new):
        with self._lock:
            if old in self.enabled:
                self.enabled.remove(old)
                self.enabled.add(new)
                record = self.records.pop(old, None)
                if record:
                    self.records[new] = record
                self._save()

    def tick(self):
        with self._lock:
            if not self.enabled or not self._cluster.has_quorum():
                return
            seen = set()
            for member in self._cluster.compute_members():
                try:
                    containers = member.driver.containers().list()
                except Exception:
                    continue
                for ct in containers:
                    name = ct.spec.name
                    if name not in self.enabled:
                        continue
                    record = self.records.get(name, {})
                    if ct.state != "running" and record.get("target"):
                        continue  # failed/uncertain start must remain retryable
                    if ct.owner_node and ct.owner_node != member.name:
                        continue
                    seen.add(name)
                    self.records[name] = dict(
                        node=member.name,
                        running=ct.state == "running",
                        memory_mb=ct.spec.memory_mb,
                        pool=ct.spec.pool,
                    )
            for name in sorted(self.enabled - seen):
                record = self.records.get(name, {})
                if not record.get("running") or self._clock() < self._retry.get(name, 0):
                    continue
                failed = next(
                    (m for m in self._cluster.compute_members(False) if m.name == record["node"]),
                    None,
                )
                if failed is None or failed.local or self._cluster.is_healthy(failed):
                    continue
                if failed.last_seen is None or self._clock() - failed.last_seen <= self._grace:
                    continue  # unknown failure age must not bypass grace
                self._takeover(name, record, failed)
            self._save()

    def _takeover(self, name, record, failed):
        self._retry[name] = self._clock() + RETRY_S
        targets = sorted(
            (
                m
                for m in self._cluster.compute_members()
                if m.name != failed.name
                and not m.maintenance
                and (not record.get("target") or record["target"] == m.name)
                and _free(m) >= record["memory_mb"]
            ),
            key=lambda m: -_free(m),
        )
        try:
            if not targets:
                raise InvalidState("No healthy compute node has enough free memory")
            target = targets[0]
            # Persist uncertain RPC destination so retries never pick a second owner.
            record["target"] = target.name
            self._save()

            def work():
                return target.driver.containers().ha_takeover(name, failed.name)

            if self._tasks:
                result = self._tasks.run_task(
                    "ct-ha-restart",
                    f"after {failed.name} failed",
                    name,
                    target.name,
                    f"ct:{name}",
                    work,
                )
            else:
                result = work()
            if result.state != "running":
                raise InvalidState("HA takeover did not reach running state")
            record.update(node=target.name)
            record.pop("target", None)
            self._event(name, target.name)
        except Exception as exc:
            self._event(name, record.get("target", failed.name), str(exc))

    def _event(self, name, node, error=None):
        self.events.append(
            dict(
                at=datetime.now().astimezone().isoformat(timespec="seconds"),
                action="restart",
                container=name,
                node=node,
                ok=error is None,
                error=error,
            )
        )
        del self.events[:-100]
        publish(
            self,
            "container.ha_failed" if error else "container.ha_restarted",
            f"Container HA {name} on {node}: {error or 'restarted'}",
            f"ct-ha:{name}:{node}",
        )

    def status(self):
        with self._lock:
            return dict(
                grace_seconds=self._grace,
                containers=[dict(name=n, **self.records.get(n, {})) for n in sorted(self.enabled)],
                events=list(self.events),
            )

    def _save(self):
        if self._path:
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(dict(enabled=sorted(self.enabled), records=self.records)))
            temp.chmod(0o600)
            temp.replace(self._path)
