"""The node's edge: a virtual router for VLAN networks (gateway, NAT to the LAN, DHCP), a
distributed firewall between networks, and connectivity tests.

The router lives on one node (the manager's): it takes the gateway address on the VLAN's
bridge, so the VMs of every routed VLAN reach each other through it, and masquerades them
out of the LAN bridge. DHCP is one dnsmasq per network. Firewall rules go to every compute
node: an `inet` table filters traffic routed between networks, a `bridge` table traffic
inside one. Nothing here touches the node's own input, so a rule cannot lock anyone out.
Free of libvirt so it can be unit tested anywhere.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .base import NAME_RE, InvalidState

EDGE_STATE = "/etc/ibhics/edge.json"
DNSMASQ_DIR = "/etc/ibhics/dnsmasq"
SYSCTL_FILE = "/etc/sysctl.d/90-ibhics-router.conf"
ACTIONS = ("allow", "deny")
PROTOCOLS = ("any", "tcp", "udp", "icmp")
TARGET_RE = re.compile(r"^[A-Za-z0-9.:-]{1,253}$")


@dataclass
class RouterSpec:
    """The gateway of a VLAN network: `gateway` is the router's address with the prefix,
    for example 192.168.50.1/24."""

    network: str
    gateway: str
    nat: bool = True
    dhcp: bool = True
    dhcp_start: str = ""  # blank: the subnet's .100 to .200 (or its upper half)
    dhcp_end: str = ""
    dns: str = ""  # handed out by DHCP; blank: the gateway's own upstream (none)

    def iface(self) -> ipaddress.IPv4Interface:
        return ipaddress.IPv4Interface(self.gateway)

    def validate(self) -> None:
        if not NAME_RE.match(self.network) or self.network == "default":
            raise ValueError("a router serves a VLAN network, not the LAN itself")
        try:
            iface = self.iface()
        except ValueError:
            raise ValueError(
                "gateway must be an IPv4 address with a prefix, like 10.0.50.1/24"
            ) from None
        net = iface.network
        if net.prefixlen > 29 or iface.ip in (net.network_address, net.broadcast_address):
            raise ValueError("gateway must be a host address in a /29 or larger subnet")
        if self.dhcp:
            start, end = self.dhcp_range()
            if not (start in net and end in net and start <= end):
                raise ValueError("the DHCP range must lie in the gateway's subnet")
            if start <= iface.ip <= end:
                raise ValueError("the DHCP range must not include the gateway address")
        if self.dns:
            ipaddress.IPv4Address(self.dns)

    def dhcp_range(self) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]:
        if self.dhcp_start and self.dhcp_end:
            return ipaddress.IPv4Address(self.dhcp_start), ipaddress.IPv4Address(self.dhcp_end)
        net = self.iface().network
        hosts = net.num_addresses - 2
        if hosts >= 254:
            base = net.network_address
            return base + 100, base + 200
        return net.network_address + hosts // 2 + 1, net.broadcast_address - 1


@dataclass
class FirewallRule:
    """Allow or deny traffic from `src` to `dst`: a network name, a CIDR or "any"."""

    name: str
    action: str = "deny"
    src: str = "any"
    dst: str = "any"
    proto: str = "any"
    port: int | None = None  # tcp/udp destination port
    enabled: bool = True

    def validate(self, networks: set[str]) -> None:
        if not NAME_RE.match(self.name):
            raise ValueError("rule name must use only letters, digits, '_', '.', '-'")
        if self.action not in ACTIONS or self.proto not in PROTOCOLS:
            raise ValueError("action is allow or deny; protocol any, tcp, udp or icmp")
        for end in (self.src, self.dst):
            if end != "any" and end not in networks:
                try:
                    ipaddress.IPv4Network(end, strict=False)
                except ValueError:
                    raise ValueError(f"{end!r} is not a network name or an IPv4 CIDR") from None
        bad_port = self.proto not in ("tcp", "udp") or not 0 < (self.port or 0) < 65536
        if self.port is not None and bad_port:
            raise ValueError("a port needs protocol tcp or udp and must be 1 to 65535")


@dataclass
class EdgeConfig:
    routers: list[RouterSpec] = field(default_factory=list)
    rules: list[FirewallRule] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "routers": [asdict(r) for r in self.routers],
            "rules": [asdict(r) for r in self.rules],
        }

    @classmethod
    def from_dict(cls, data: dict) -> EdgeConfig:
        return cls(
            routers=[RouterSpec(**r) for r in data.get("routers", [])],
            rules=[FirewallRule(**r) for r in data.get("rules", [])],
        )

    def validate(self, networks: set[str]) -> None:
        seen = set()
        subnets = []
        for r in self.routers:
            r.validate()
            if r.network not in networks:
                raise ValueError(f"no network named {r.network!r}")
            if r.network in seen:
                raise ValueError(f"network {r.network!r} has two routers")
            seen.add(r.network)
            net = r.iface().network
            if any(net.overlaps(other) for other in subnets):
                raise ValueError(f"subnet {net} overlaps another routed network")
            subnets.append(net)
        names = set()
        for rule in self.rules:
            rule.validate(networks)
            if rule.name in names:
                raise ValueError(f"two firewall rules are named {rule.name!r}")
            names.add(rule.name)


# Rendering


def _match(end: str, bridges: dict[str, str], family: str, side: str) -> str | None:
    """The nft match for one end of a rule, or None when it cannot match in `family`."""
    if end == "any":
        return ""
    if end in bridges:
        bridge = bridges[end]
        if family == "bridge":
            return f'meta ibrname "{bridge}"' if side == "src" else f'meta obrname "{bridge}"'
        return f'iifname "{bridge}"' if side == "src" else f'oifname "{bridge}"'
    cidr = ipaddress.IPv4Network(end, strict=False)
    return f"ip {'saddr' if side == 'src' else 'daddr'} {cidr}"


def _rule_line(rule: FirewallRule, bridges: dict[str, str], family: str) -> str | None:
    if not rule.enabled:
        return None
    parts = [_match(rule.src, bridges, family, "src"), _match(rule.dst, bridges, family, "dst")]
    if None in parts:
        return None
    if family == "bridge" and rule.src in bridges and rule.dst in bridges and rule.src != rule.dst:
        return None  # two VLANs never share a bridge: that traffic is routed
    if rule.proto != "any":
        parts.append(f"meta l4proto {rule.proto}")
        if rule.port:
            parts.append(f"th dport {rule.port}")
    verdict = "accept" if rule.action == "allow" else "drop"
    return " ".join(p for p in parts if p) + f' counter {verdict} comment "{rule.name}"'


def render_nft(config: EdgeConfig, bridges: dict[str, str], lan: str, router: bool) -> str:
    """The node's whole ibHiCS ruleset; loading it replaces the previous one."""
    out = ["#!/usr/sbin/nft -f", "# Written by ibHiCS: virtual router and firewall."]
    for family, table in (("inet", "ibhics_fw"), ("bridge", "ibhics_fw"), ("ip", "ibhics_nat")):
        out += [f"table {family} {table}", f"delete table {family} {table}"]
    for family in ("inet", "bridge"):
        lines = [_rule_line(r, bridges, family) for r in config.rules]
        out += [
            f"table {family} ibhics_fw {{",
            "  chain forward {",
            "    type filter hook forward priority 0; policy accept;",
        ]
        if family == "inet":
            out.append("    ct state established,related accept")
        out += [f"    {line}" for line in lines if line]
        out += ["  }", "}"]
    nat = [r for r in config.routers if r.nat] if router else []
    out += [
        "table ip ibhics_nat {",
        "  chain postrouting {",
        "    type nat hook postrouting priority 100; policy accept;",
    ]
    for r in nat:
        out.append(f'    ip saddr {r.iface().network} oifname "{lan}" masquerade')
    out += ["  }", "}"]
    return "\n".join(out) + "\n"


def render_dnsmasq(router: RouterSpec, bridge: str) -> str:
    start, end = router.dhcp_range()
    net = router.iface().network
    lines = [
        f"# Written by ibHiCS: DHCP for network {router.network}.",
        f"interface={bridge}",
        "bind-interfaces",
        "except-interface=lo",
        "port=0",  # DHCP only, no DNS server
        f"dhcp-range={start},{end},{net.netmask},12h",
        f"dhcp-option=option:router,{router.iface().ip}",
        f"dhcp-leasefile=/var/lib/ibhics/dhcp-{router.network}.leases",
        f"pid-file=/run/ibhics-dhcp-{router.network}.pid",
    ]
    if router.dns:
        lines.append(f"dhcp-option=option:dns-server,{router.dns}")
    return "\n".join(lines) + "\n"


# Applying, on a node


Runner = Callable[[list[str]], str]


def apply(
    config: EdgeConfig,
    bridges: dict[str, str],
    run: Runner,
    router: bool,
    lan: str = "br0",
    root: str = "",
) -> None:
    """Bring the node in line with `config`: gateway addresses, forwarding, NAT, DHCP
    and firewall rules. Safe to run again with the same config."""
    base = Path(root or "/")
    routers = config.routers if router else []
    for r in routers:
        if r.network not in bridges:
            raise InvalidState(f"network {r.network!r} is not on this node")
    # Gateway addresses: exactly the routers' on each VLAN bridge.
    wanted = {bridges[r.network]: r.gateway for r in routers}
    for net, bridge in bridges.items():
        if net == "default":
            continue
        run(["ip", "-4", "addr", "flush", "dev", bridge, "scope", "global"])
        if bridge in wanted:
            run(["ip", "addr", "add", wanted[bridge], "dev", bridge])
    sysctl = base / SYSCTL_FILE.lstrip("/")
    if routers:
        sysctl.parent.mkdir(parents=True, exist_ok=True)
        sysctl.write_text(
            "# Written by ibHiCS: this node routes between VM networks.\nnet.ipv4.ip_forward = 1\n"
        )
        run(["sysctl", "-q", "-w", "net.ipv4.ip_forward=1"])
    else:
        sysctl.unlink(missing_ok=True)
    rules = base / "etc/ibhics/edge.nft"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text(render_nft(config, bridges, lan, router))
    run(["nft", "-f", str(rules)])
    # DHCP: one dnsmasq per routed network that asks for it.
    conf_dir = base / DNSMASQ_DIR.lstrip("/")
    conf_dir.mkdir(parents=True, exist_ok=True)
    keep = set()
    for r in routers:
        if not r.dhcp:
            continue
        conf = conf_dir / f"{r.network}.conf"
        keep.add(conf.name)
        conf.write_text(render_dnsmasq(r, bridges[r.network]))
        _stop_dhcp(r.network, run, base)
        run(["dnsmasq", f"--conf-file={conf}"])
    for conf in conf_dir.glob("*.conf"):
        if conf.name not in keep:
            _stop_dhcp(conf.stem, run, base)
            conf.unlink()


def _stop_dhcp(network: str, run: Runner, base: Path) -> None:
    pid = base / f"run/ibhics-dhcp-{network}.pid"
    if pid.is_file():
        try:
            run(["kill", pid.read_text().strip()])
        except RuntimeError:
            pass
        pid.unlink(missing_ok=True)


def net_test(
    kind: str, target: str, run: Callable[[list[str]], tuple[int, str]], source: str = ""
) -> tuple[bool, str]:
    """Ping or trace `target` from the node, out of a network's bridge when `source`
    names one. Returns whether it got through and the tool's output."""
    if kind not in ("ping", "trace") or not TARGET_RE.match(target) or target.startswith("-"):
        raise ValueError("test ping or trace to a host name or an IP address")
    if kind == "ping":
        cmd = ["ping", "-c", "4", "-W", "2", "-n"] + (["-I", source] if source else []) + [target]
    else:
        cmd = ["tracepath", "-n", "-m", "15", target]
    code, output = run(cmd)
    return code == 0, output


def restore_edge(driver) -> None:
    """At start-up: put back the node's router and firewall; never stop the service."""
    restore = getattr(driver, "restore_edge", None)
    if restore is None:
        return
    try:
        restore()
    except Exception as exc:  # a broken rule must not keep the node's API down
        print(f"ibHiCS: could not restore the virtual router and firewall: {exc}", flush=True)
