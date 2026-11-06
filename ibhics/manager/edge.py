"""The cluster's virtual router and distributed firewall: one config, kept by the manager
and pushed to every compute node. The router runs on one node; firewall rules on all."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..cluster import Cluster, NodeUnavailable
from ..hypervisor import InvalidState
from ..hypervisor.edge import EdgeConfig, FirewallRule, RouterSpec


class EdgeManager:
    def __init__(self, cluster: Cluster, state_path: Path | None = None) -> None:
        self._cluster = cluster
        self._path = state_path
        self._lock = threading.Lock()
        self.config = EdgeConfig()
        self.router_node = ""
        if state_path and state_path.is_file():
            data = json.loads(state_path.read_text())
            self.config = EdgeConfig.from_dict(data)
            self.router_node = data.get("router_node", "")

    def view(self) -> dict:
        return {**self.config.to_dict(), "router_node": self._router_node()}

    def _router_node(self) -> str:
        names = [m.name for m in self._cluster.compute_members()]
        if self.router_node in names:
            return self.router_node
        return names[0] if names else ""

    def _networks(self) -> set[str]:
        for member in self._cluster.compute_members():
            try:
                return {n.name for n in member.driver.list_networks()}
            except NodeUnavailable:
                continue
        return {"default"}

    def _change(self, config: EdgeConfig, router_node: str | None = None) -> dict:
        self._cluster.require_quorum()
        config.validate(self._networks())
        with self._lock:
            old, old_node = self.config, self.router_node
            self.config = config
            if router_node is not None:
                self.router_node = router_node
            try:
                self.push()
            except Exception:
                self.config, self.router_node = old, old_node
                self.push(strict=False)  # put the nodes back as they were
                raise
            self._save()
        return self.view()

    def push(self, strict: bool = True) -> list[str]:
        """Send the config to every compute node; returns the nodes that missed it."""
        router = self._router_node()
        missed = []
        for member in self._cluster.compute_members():
            try:
                member.driver.apply_edge(self.config.to_dict(), member.name == router)
            except NodeUnavailable:
                missed.append(member.name)  # it gets the config back when it restarts
            except Exception:
                if strict:
                    raise
                missed.append(member.name)
        return missed

    def _save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {**self.config.to_dict(), "router_node": self.router_node}
            self._path.write_text(json.dumps(data, indent=2))

    def set_router(self, router: RouterSpec) -> dict:
        routers = [r for r in self.config.routers if r.network != router.network] + [router]
        return self._change(EdgeConfig(routers=routers, rules=list(self.config.rules)))

    def remove_router(self, network: str) -> dict:
        routers = [r for r in self.config.routers if r.network != network]
        if len(routers) == len(self.config.routers):
            raise InvalidState(f"network {network!r} has no router")
        return self._change(EdgeConfig(routers=routers, rules=list(self.config.rules)))

    def set_rules(self, rules: list[FirewallRule]) -> dict:
        return self._change(EdgeConfig(routers=list(self.config.routers), rules=rules))

    def set_router_node(self, node: str) -> dict:
        if node not in {m.name for m in self._cluster.compute_members()}:
            raise InvalidState(f"{node!r} is not a compute node")
        return self._change(EdgeConfig(list(self.config.routers), list(self.config.rules)), node)

    def test(self, kind: str, target: str, network: str = "", node: str = "") -> dict:
        node = node or self._router_node()
        member = self._cluster.member(node)
        ok, output = member.driver.net_test(kind, target, network)
        return {"ok": ok, "output": output, "node": node}
