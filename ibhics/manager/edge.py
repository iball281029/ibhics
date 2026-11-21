"""The cluster's virtual router and distributed firewall: one config, kept by the manager
and pushed to every compute node. The router runs on one node; firewall rules on all."""

from __future__ import annotations

import copy
import ipaddress
import json
import threading
import time
from pathlib import Path

from ..cluster import Cluster, NodeUnavailable
from ..hypervisor import InvalidState
from ..hypervisor.edge import OBJECT_PREFIXES, EdgeConfig, FirewallRule, RouterSpec

SAFE_SECONDS = 60  # a firewall change not confirmed in time is rolled back


class EdgeManager:
    def __init__(
        self, cluster: Cluster, state_path: Path | None = None, groups=None, clock=time.monotonic
    ) -> None:
        self._cluster = cluster
        self._path = state_path
        self._groups = groups
        self._clock = clock
        self._lock = threading.Lock()
        self.config = EdgeConfig()
        self.router_node = ""
        self._pushed: dict | None = None  # the compiled config the nodes have
        self._pending: tuple[EdgeConfig, float] | None = None  # (previous config, deadline)
        self._timers = clock is time.monotonic  # tests drive the clock and call _expire
        if state_path and state_path.is_file():
            data = json.loads(state_path.read_text())
            self.config = EdgeConfig.from_dict(data)
            self.router_node = data.get("router_node", "")

    def view(self) -> dict:
        pending = None
        if self._pending:
            pending = max(0, round(self._pending[1] - self._clock()))
        return {
            **self.config.to_dict(),
            "router_node": self._router_node(),
            "pending_confirm_seconds": pending,
        }

    # VM and group rule ends

    def _inventory(self) -> list[dict]:
        """Every VM in the cluster: name, uuid, NIC networks and MACs, guest IPv4s."""
        vms = []
        for member in self._cluster.compute_members():
            try:
                infos = member.driver.list_vms()
                guests = member.driver.list_guest_info()
            except NodeUnavailable:
                continue
            for vm in infos:
                guest = guests.get(vm.uuid)
                ips = []
                if guest is not None:
                    for ip in guest.ips:
                        try:
                            ips.append(str(ipaddress.IPv4Address(ip)))
                        except ValueError:
                            continue  # IPv6: the firewall filters IPv4
                vms.append(
                    {
                        "name": vm.spec.name,
                        "uuid": vm.uuid,
                        "node": member.name,
                        "networks": [n.network for n in vm.spec.nics],
                        "macs": [n.mac.lower() for n in vm.spec.nics if n.mac],
                        "ips": ips,
                    }
                )
        return vms

    def _members_of(self, end: str, inventory: list[dict]) -> list[dict]:
        kind, _, value = end.partition(":")
        if kind == "vm":
            return [v for v in inventory if v["name"] == value]
        if kind == "group" and self._groups is not None:
            uuids = {u for u, g in self._groups.members().items() if g == value}
            return [v for v in inventory if v["uuid"] in uuids]
        return []

    def _compiled(self, config: EdgeConfig, inventory: list[dict] | None = None) -> EdgeConfig:
        """`config` with every VM/group end resolved to MACs and guest addresses."""
        if not any(e.startswith(OBJECT_PREFIXES) for r in config.rules for e in (r.src, r.dst)):
            return config
        inventory = self._inventory() if inventory is None else inventory
        out = copy.deepcopy(config)
        for rule in out.rules:
            for end, attr in ((rule.src, "src_match"), (rule.dst, "dst_match")):
                if not end.startswith(OBJECT_PREFIXES):
                    continue
                found = set()
                for vm in self._members_of(end, inventory):
                    found |= {f"mac:{m}" for m in vm["macs"]} | {f"ip:{i}" for i in vm["ips"]}
                setattr(rule, attr, sorted(found))
        return out

    def refresh(self) -> bool:
        """Re-resolve VM and group rules (a VM moved, got a new address); push if changed."""
        self._expire()
        with self._lock:
            compiled = self._compiled(self.config).to_dict()
            if compiled == self._pushed:
                return False
            self.push(strict=False)
            return True

    # Test a flow against the rules without sending anything

    def _endpoint(self, value: str, inventory: list[dict]) -> dict:
        value = value.strip()
        if value.startswith("vm:") or not _is_ip(value):
            name = value.removeprefix("vm:")
            vm = next((v for v in inventory if v["name"] == name), None)
            if vm is None:
                raise InvalidState(f"no VM named {name!r}")
            return {"label": name, "vm": vm, "ips": vm["ips"], "networks": vm["networks"]}
        return {"label": value, "vm": None, "ips": [value], "networks": []}

    def _end_matches(self, end: str, point: dict, inventory: list[dict]) -> bool:
        if end == "any":
            return True
        if end.startswith(OBJECT_PREFIXES):
            return point["vm"] is not None and any(
                v["uuid"] == point["vm"]["uuid"] for v in self._members_of(end, inventory)
            )
        if end in point["networks"]:
            return True
        try:
            net = ipaddress.IPv4Network(end, strict=False)
        except ValueError:
            return False  # a network name the endpoint is not on
        return any(ipaddress.IPv4Address(ip) in net for ip in point["ips"])

    def simulate(self, src: str, dst: str, proto: str = "tcp", port: int | None = None) -> dict:
        """Would traffic from `src` to `dst` pass? Returns the deciding rule, top to bottom."""
        inventory = self._inventory()
        a, b = self._endpoint(src, inventory), self._endpoint(dst, inventory)
        for index, rule in enumerate(self.config.rules):
            if not rule.enabled:
                continue
            if rule.proto != "any" and rule.proto != proto:
                continue
            ranges = rule.dports()
            if ranges and not (port and any(lo <= port <= hi for lo, hi in ranges)):
                continue
            if self._end_matches(rule.src, a, inventory) and self._end_matches(
                rule.dst, b, inventory
            ):
                return {
                    "allowed": rule.action == "allow",
                    "rule": rule.name,
                    "index": index + 1,
                    "src": a["label"],
                    "dst": b["label"],
                }
        return {"allowed": True, "rule": "", "index": None, "src": a["label"], "dst": b["label"]}

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

    def _change(
        self, config: EdgeConfig, router_node: str | None = None, safe: bool = False
    ) -> dict:
        self._cluster.require_quorum()
        self._expire()
        self._compiled(config).validate(self._networks())
        with self._lock:
            old, old_node = self.config, self.router_node
            if safe and self._pending is None:
                self._pending = (copy.deepcopy(old), self._clock() + SAFE_SECONDS)
            elif not safe:
                self._pending = None
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
        compiled = self._compiled(self.config).to_dict()
        self._pushed = compiled
        for member in self._cluster.compute_members():
            try:
                member.driver.apply_edge(compiled, member.name == router)
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

    def set_rules(self, rules: list[FirewallRule], safe: bool = False) -> dict:
        """With `safe`, the change rolls back by itself unless confirmed in SAFE_SECONDS:
        a rule that cuts the browser off from the manager undoes itself."""
        view = self._change(EdgeConfig(routers=list(self.config.routers), rules=rules), safe=safe)
        if safe and self._timers:
            timer = threading.Timer(SAFE_SECONDS + 1, self._expire)
            timer.daemon = True
            timer.start()
        return view

    def confirm(self) -> dict:
        with self._lock:
            self._pending = None
        return self.view()

    def _expire(self) -> None:
        """Roll back an unconfirmed safe change whose time ran out."""
        pending = self._pending
        if pending is None or self._clock() < pending[1]:
            return
        with self._lock:
            if self._pending is not pending:
                return
            self._pending = None
            self.config = pending[0]
            self.push(strict=False)
            self._save()

    def set_router_node(self, node: str) -> dict:
        if node not in {m.name for m in self._cluster.compute_members()}:
            raise InvalidState(f"{node!r} is not a compute node")
        return self._change(EdgeConfig(list(self.config.routers), list(self.config.rules)), node)

    def test(self, kind: str, target: str, network: str = "", node: str = "") -> dict:
        node = node or self._router_node()
        member = self._cluster.member(node)
        ok, output = member.driver.net_test(kind, target, network)
        return {"ok": ok, "output": output, "node": node}


def _is_ip(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True
