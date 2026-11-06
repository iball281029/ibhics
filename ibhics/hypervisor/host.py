"""The node itself, apart from its VMs: hardware and load (Summary), its network ports and
the ifupdown config that sets them up (Network), and system settings (Settings).

The Network page edits /etc/network/interfaces, which ibhics-setup wrote. A change is
applied at once and put back by itself after CONFIRM_SECONDS unless it is confirmed, so a
mistake that cuts the node off the network mends itself.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from .. import __version__
from .base import InvalidState
from .net_xml import IFACES_DIR, LAN_BRIDGE, VLAN_BRIDGE

CONFIRM_SECONDS = 120
INTERFACES = "/etc/network/interfaces"
CHRONY_CONF = "/etc/chrony/chrony.conf"
CHRONY_SOURCES = "/etc/chrony/sources.d/ibhics.sources"
RESOLV_CONF = "/etc/resolv.conf"
ZONEINFO = "/usr/share/zoneinfo"
KINDS = ("ethernet", "bond", "bridge")
METHODS = ("manual", "static", "dhcp")
BOND_MODES = ("active-backup", "802.3ad", "balance-alb", "balance-tlb", "balance-xor")
BOND_NAME = re.compile(r"^bond\d{1,2}$")
BRIDGE_NAME = re.compile(r"^br[a-z0-9]{0,8}$")
TIMEZONE = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}$")
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9-]+)*$")
COMMENTED = "#ibhics# "  # marks lines of chrony.conf that the NTP setting turned off

Runner = Callable[[list[str]], str]


def run_command(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


# What the pages show


@dataclass
class NodeSummary:
    hostname: str
    vendor: str
    model: str
    serial: str
    bios: str
    cpu_model: str
    sockets: int
    cores: int
    threads: int
    virtualization: str  # "Intel VT-x", "AMD-V" or "" when the CPU lacks it
    memory_mb: int
    memory_used_mb: int
    cpu_percent: float
    load: list[float]
    uptime_s: int
    kernel: str
    os: str
    version: str
    timezone: str


@dataclass
class HostNic:
    """A network port as it is now: physical ports, bonds and bridges."""

    name: str
    kind: str  # ethernet, bond, bridge
    mac: str = ""
    up: bool = False  # switched on
    link: bool = False  # cable in and the other end answering
    speed_mbps: int | None = None
    mtu: int = 1500
    driver: str = ""
    model: str = ""
    master: str | None = None  # the bridge or bond it is part of
    members: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)  # CIDR


@dataclass
class IfaceConfig:
    """One interface in /etc/network/interfaces."""

    name: str
    kind: str = "ethernet"
    method: str = "manual"  # manual: no address of its own
    address: str = ""  # CIDR, for static
    gateway: str = ""
    mtu: int | None = None
    auto: bool = True  # brought up at boot
    ports: list[str] = field(default_factory=list)  # bridge ports or bond members
    bond_mode: str = ""
    options: list[str] = field(default_factory=list)  # other lines, kept as they are


@dataclass
class HostNetwork:
    interfaces: list[IfaceConfig]

    def get(self, name: str) -> IfaceConfig | None:
        return next((i for i in self.interfaces if i.name == name), None)

    def owner(self, name: str) -> IfaceConfig | None:
        """The bridge or bond that has `name` as a port."""
        return next((i for i in self.interfaces if name in i.ports), None)

    def validate(self, physical: set[str], keep_address: str = "") -> None:
        names = [i.name for i in self.interfaces]
        if len(names) != len(set(names)):
            raise ValueError("each interface can be listed once")
        used: dict[str, str] = {}
        gateways = []
        for i in self.interfaces:
            if i.kind not in KINDS:
                raise ValueError(f"{i.name}: kind is one of {', '.join(KINDS)}")
            if i.method not in METHODS:
                raise ValueError(f"{i.name}: IP setting is one of {', '.join(METHODS)}")
            if i.kind == "ethernet" and i.name not in physical:
                raise ValueError(f"there is no network port named {i.name!r} on this node")
            if i.kind == "bond":
                if not BOND_NAME.match(i.name):
                    raise ValueError(f"{i.name}: name a bond bond0, bond1, ...")
                if i.bond_mode not in BOND_MODES:
                    raise ValueError(f"{i.name}: bond mode is one of {', '.join(BOND_MODES)}")
                if not i.ports:
                    raise ValueError(f"{i.name}: a bond needs at least one port")
            if i.kind == "bridge" and (not BRIDGE_NAME.match(i.name) or VLAN_BRIDGE.match(i.name)):
                raise ValueError(f"{i.name}: name a bridge br0, br1, brstorage, ...")
            if i.kind == "ethernet" and i.ports:
                raise ValueError(f"{i.name}: only bridges and bonds have ports")
            for port in i.ports:
                member = self.get(port)
                if port == i.name or member is None and port not in physical:
                    raise ValueError(f"{i.name}: there is no port named {port!r}")
                if member is not None and member.kind == "bridge":
                    raise ValueError(f"{i.name}: a bridge cannot be a port of {i.name}")
                if i.kind == "bond" and member is not None and member.kind != "ethernet":
                    raise ValueError(f"{i.name}: a bond is made of network ports only")
                if port in used:
                    raise ValueError(f"{port} is already a port of {used[port]}")
                used[port] = i.name
            if i.mtu is not None and not 576 <= i.mtu <= 9216:
                raise ValueError(f"{i.name}: MTU is between 576 and 9216")
            if i.method == "static":
                try:
                    net = ipaddress.IPv4Interface(i.address)
                except ValueError:
                    raise ValueError(
                        f"{i.name}: write the address with its prefix, e.g. 10.0.0.11/24"
                    ) from None
                if "/" not in i.address:
                    raise ValueError(f"{i.name}: write the address with its prefix, e.g. /24")
                if i.gateway:
                    try:
                        gw = ipaddress.IPv4Address(i.gateway)
                    except ValueError:
                        raise ValueError(f"{i.name}: {i.gateway!r} is not an IPv4") from None
                    if gw not in net.network:
                        raise ValueError(f"{i.name}: the gateway must be inside {net.network}")
                    gateways.append(i.name)
            elif i.address or i.gateway:
                raise ValueError(f"{i.name}: an address needs the Static IP setting")
        for name, owner in used.items():
            member = self.get(name)
            if member is not None and member.method != "manual":
                raise ValueError(f"{name} is a port of {owner}, so the address goes on {owner}")
        if len(gateways) > 1:
            raise ValueError(f"only one default gateway: {' and '.join(gateways)} both have one")
        if not any(i.method != "manual" for i in self.interfaces):
            raise ValueError("at least one interface needs an address, or the node is unreachable")
        lan = self.get(LAN_BRIDGE)
        if lan is None or lan.kind != "bridge":
            raise ValueError(f"{LAN_BRIDGE} carries the VM networks and stays")
        if keep_address and not any(
            i.method == "static" and i.address.split("/")[0] == keep_address
            for i in self.interfaces
        ):
            raise ValueError(
                f"the cluster reaches this node at {keep_address}: keep that address "
                "(change it with ibhics-setup on the node's console)"
            )


@dataclass
class NetworkState:
    pending: bool = False  # applied, waiting to be confirmed
    seconds_left: int = 0
    message: str = ""  # what happened last, e.g. a change put back after the timeout


@dataclass
class HostSettings:
    hostname: str
    timezone: str
    dns: list[str]
    ntp: list[str]
    ntp_synced: bool | None = None
    time: str = ""


@dataclass
class SettingsEdit:
    timezone: str | None = None
    dns: list[str] | None = None
    ntp: list[str] | None = None

    def validate(self, timezones: list[str]) -> None:
        if self.timezone is not None and self.timezone not in timezones:
            raise ValueError(f"unknown time zone {self.timezone!r}")
        for server in self.dns or []:
            try:
                ipaddress.ip_address(server)
            except ValueError:
                raise ValueError(f"DNS server {server!r} is not an IP address") from None
        for server in self.ntp or []:
            try:
                ipaddress.ip_address(server)
            except ValueError:
                if not HOSTNAME.match(server.lower()):
                    raise ValueError(f"NTP server {server!r} is not a host name or IP") from None


# /etc/network/interfaces


def parse_interfaces(text: str) -> HostNetwork:
    autos: set[str] = set()
    found: dict[str, IfaceConfig] = {}
    netmasks: dict[str, str] = {}
    current: IfaceConfig | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        key = words[0]
        if key in ("auto", "allow-hotplug") or key.startswith("allow-"):
            autos.update(words[1:])
            current = None
        elif key == "iface" and len(words) >= 4:
            name, method = words[1], words[3]
            current = None if name == "lo" else IfaceConfig(name=name)
            if current is not None:
                current.method = {"static": "static", "dhcp": "dhcp"}.get(method, "manual")
                found[name] = current
        elif key in ("source", "source-directory", "mapping"):
            current = None
        elif current is not None:
            value = words[1:]
            norm = key.replace("_", "-")
            if norm == "address" and value:
                current.address = value[0]
            elif norm == "netmask" and value:
                netmasks[current.name] = value[0]
            elif norm == "gateway" and value:
                current.gateway = value[0]
            elif norm == "mtu" and value and value[0].isdigit():
                current.mtu = int(value[0])
            elif norm == "bridge-ports":
                current.kind = "bridge"
                current.ports = [p for p in value if p != "none"]
            elif norm in ("bond-slaves", "slaves"):
                current.kind = "bond"
                current.ports = [p for p in value if p != "none"]
            elif norm == "bond-mode" and value:
                current.kind = "bond"
                current.bond_mode = value[0]
            elif norm in ("bridge-stp", "bridge-fd", "bond-miimon", "bond-master"):
                pass  # written again from the settings above
            else:
                current.options.append(line)
    for name, iface in found.items():
        iface.auto = name in autos
        if iface.address and "/" not in iface.address and name in netmasks:
            net = ipaddress.IPv4Network(f"0.0.0.0/{netmasks[name]}")
            iface.address = f"{iface.address}/{net.prefixlen}"
        if iface.kind == "ethernet" and BOND_NAME.match(name) and not iface.ports:
            iface.kind = "bond"
        if iface.kind == "bond":
            iface.bond_mode = iface.bond_mode or "active-backup"
    return HostNetwork(list(found.values()))


def render_interfaces(net: HostNetwork, node: str = "") -> str:
    where = f"Nodes > {node} > Network" if node else "the node's Network page"
    out = [
        f"# Written by ibHiCS. Change it in the web console ({where}): changes made",
        "# there are put back by themselves unless confirmed.",
        f"source {IFACES_DIR}/*",
        "",
        "auto lo",
        "iface lo inet loopback",
    ]
    order = {"ethernet": 0, "bond": 1, "bridge": 2}
    bonds = {p: i.name for i in net.interfaces if i.kind == "bond" for p in i.ports}
    for i in sorted(net.interfaces, key=lambda i: (order[i.kind], i.name)):
        out.append("")
        if i.auto:
            out.append(f"auto {i.name}")
        out.append(f"iface {i.name} inet {i.method}")
        if i.method == "static":
            out.append(f"    address {i.address}")
            if i.gateway:
                out.append(f"    gateway {i.gateway}")
        if i.mtu:
            out.append(f"    mtu {i.mtu}")
        if i.name in bonds:
            out.append(f"    bond-master {bonds[i.name]}")
        if i.kind == "bond":
            out += [
                f"    bond-slaves {' '.join(i.ports)}",
                f"    bond-mode {i.bond_mode}",
                "    bond-miimon 100",
            ]
        if i.kind == "bridge":
            out += [
                f"    bridge_ports {' '.join(i.ports) or 'none'}",
                "    bridge_stp off",
                "    bridge_fd 0",
            ]
        out += [f"    {o}" for o in i.options]
    return "\n".join(out) + "\n"


def stack_order(net: HostNetwork) -> list[str]:
    """Interfaces from the bottom up: ports before the bonds and bridges on them."""
    order = {"ethernet": 0, "bond": 1, "bridge": 2}
    return [i.name for i in sorted(net.interfaces, key=lambda i: (order[i.kind], i.name))]


def affected(old: HostNetwork, new: HostNetwork) -> set[str]:
    """Interfaces to take down and bring up again: the ones that changed, and the bonds and
    bridges they are (or were) part of, since those go down with a port."""
    names = {i.name for i in old.interfaces} | {i.name for i in new.interfaces}
    changed = {n for n in names if old.get(n) != new.get(n)}
    grew = True
    while grew:
        grew = False
        for net in (old, new):
            for n in list(changed):
                owner = net.owner(n)
                if owner is not None and owner.name not in changed:
                    changed.add(owner.name)
                    grew = True
    return changed


# Settings files


def parse_resolv(text: str) -> list[str]:
    return [line.split()[1] for line in text.splitlines() if line.split()[:1] == ["nameserver"]]


def render_resolv(old: str, dns: list[str]) -> str:
    keep = [ln for ln in old.splitlines() if ln.split()[:1] in (["search"], ["domain"])]
    keep += [ln for ln in old.splitlines() if ln.split()[:1] == ["options"]]
    head = "# Written by ibHiCS (Nodes > Settings)."
    return "\n".join([head, *keep, *(f"nameserver {s}" for s in dns)]) + "\n"


def chrony_servers(conf: str, sources: str | None) -> list[str]:
    text = sources if sources is not None else conf
    return [
        line.split()[1]
        for line in text.splitlines()
        if line.split()[:1] in (["server"], ["pool"]) and len(line.split()) > 1
    ]


def render_chrony_conf(conf: str, own_servers: bool) -> str:
    """Turns chrony.conf's own pool/server lines off while ours are in use, on again
    when the list is emptied (back to Debian's pool)."""
    out = []
    for line in conf.splitlines():
        word = line.split()[:1]
        if own_servers and word in (["pool"], ["server"]):
            out.append(COMMENTED + line)
        elif not own_servers and line.startswith(COMMENTED):
            out.append(line[len(COMMENTED) :])
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def render_chrony_sources(servers: list[str]) -> str:
    lines = ["# Written by ibHiCS (Nodes > Settings)."]
    lines += [f"server {s} iburst" for s in servers]
    return "\n".join(lines) + "\n"


def list_timezones(zoneinfo: Path) -> list[str]:
    zones = {"UTC"}
    for name in ("tzdata.zi",):
        path = zoneinfo / name
        if path.exists():
            for line in path.read_text().splitlines():
                parts = line.split()
                if parts[:1] == ["Z"] and len(parts) > 1:
                    zones.add(parts[1])
                elif parts[:1] == ["L"] and len(parts) > 2:
                    zones.add(parts[2])
    for name in ("zone1970.tab", "zone.tab"):
        path = zoneinfo / name
        if path.exists():
            for line in path.read_text().splitlines():
                parts = line.split("\t")
                if not line.startswith("#") and len(parts) >= 3:
                    zones.add(parts[2])
    return sorted(z for z in zones if TIMEZONE.match(z) and not z.startswith(("posix/", "right/")))


# The node


class HostSystem:
    """What every node offers. Holds the pending-change logic: apply, then confirm in
    time or have it put back."""

    confirm_seconds = CONFIRM_SECONDS

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._deadline = 0.0
        self._previous = ""  # interfaces text before the pending change
        self._message = ""

    # Implemented per node

    def summary(self) -> NodeSummary:
        raise NotImplementedError

    def nics(self) -> list[HostNic]:
        raise NotImplementedError

    def timezones(self) -> list[str]:
        raise NotImplementedError

    def settings(self) -> HostSettings:
        raise NotImplementedError

    def _write_settings(self, edit: SettingsEdit) -> None:
        raise NotImplementedError

    def power(self, action: str) -> None:
        raise NotImplementedError

    def _read_interfaces(self) -> str:
        raise NotImplementedError

    def _switch(self, old_text: str, new_text: str) -> None:
        """Write new_text as the interfaces file and bring the changed interfaces over."""
        raise NotImplementedError

    def _keep_address(self) -> str:
        """An address the cluster reaches this node at, which a change must keep."""
        return ""

    # Shared

    def network(self) -> HostNetwork:
        return parse_interfaces(self._read_interfaces())

    def physical(self) -> set[str]:
        return {n.name for n in self.nics() if n.kind == "ethernet"}

    def update_settings(self, edit: SettingsEdit) -> HostSettings:
        edit.validate(self.timezones())
        self._write_settings(edit)
        return self.settings()

    def network_state(self) -> NetworkState:
        with self._lock:
            pending = self._timer is not None
            left = max(0, round(self._deadline - time.monotonic())) if pending else 0
            return NetworkState(pending=pending, seconds_left=left, message=self._message)

    def apply_network(self, net: HostNetwork, node: str = "") -> NetworkState:
        net.validate(self.physical(), self._keep_address())
        with self._lock:
            if self._timer is not None:
                raise InvalidState("confirm or undo the last network change first")
            old = self._read_interfaces()
            new = render_interfaces(net, node)
            if parse_interfaces(old) == parse_interfaces(new):
                return NetworkState(message="nothing changed")
            try:
                self._switch(old, new)
            except Exception as exc:
                try:
                    self._switch(new, old)
                except Exception:
                    pass
                raise InvalidState(f"the change did not apply and was put back: {exc}") from exc
            self._previous = old
            self._message = ""
            self._deadline = time.monotonic() + self.confirm_seconds
            self._timer = threading.Timer(self.confirm_seconds, self._timeout)
            self._timer.daemon = True
            self._timer.start()
        return self.network_state()

    def confirm_network(self) -> NetworkState:
        with self._lock:
            if self._timer is None:
                raise InvalidState("there is no network change waiting to be confirmed")
            self._timer.cancel()
            self._timer = None
            self._message = "confirmed"
            self._confirmed()
        return self.network_state()

    def undo_network(self) -> NetworkState:
        with self._lock:
            if self._timer is None:
                raise InvalidState("there is no network change to undo")
            self._put_back("put back")
        return self.network_state()

    def _confirmed(self) -> None:
        pass

    def _timeout(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._put_back(f"not confirmed within {self.confirm_seconds} s, so it was put back")

    def _put_back(self, why: str) -> None:
        self._timer.cancel()
        self._timer = None
        try:
            self._switch(self._read_interfaces(), self._previous)
            self._message = f"The network change was {why}."
        except Exception as exc:  # leave the reason where the page shows it
            self._message = f"Putting the network change back failed: {exc}"


class LinuxHost(HostSystem):
    def __init__(
        self,
        root: Path = Path("/"),
        run: Runner = run_command,
        reattach: Callable[[], None] = lambda: None,
        sample_seconds: float = 0.2,
    ) -> None:
        super().__init__()
        self.root = root
        self.run = run
        self.reattach = reattach  # puts VM ports back on bridges that were re-created
        self.sample_seconds = sample_seconds

    def _path(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def _read(self, path: str, default: str = "") -> str:
        try:
            return self._path(path).read_text().strip()
        except OSError:
            return default

    # Summary

    def summary(self) -> NodeSummary:
        cpuinfo = self._read("/proc/cpuinfo")
        blocks = [b for b in cpuinfo.split("\n\n") if b.strip()]
        fields = [dict(_kv(line) for line in b.splitlines() if ":" in line) for b in blocks]
        model = next((f["model name"] for f in fields if "model name" in f), "")
        sockets = len({f.get("physical id", "0") for f in fields}) or 1
        cores = len(
            {(f.get("physical id", "0"), f.get("core id", i)) for i, f in enumerate(fields)}
        ) or len(fields)
        flags = fields[0].get("flags", "").split() if fields else []
        virt = "Intel VT-x" if "vmx" in flags else "AMD-V" if "svm" in flags else ""
        mem = dict(_kv(line) for line in self._read("/proc/meminfo").splitlines() if ":" in line)
        total = int(mem.get("MemTotal", "0 kB").split()[0]) // 1024
        avail = int(mem.get("MemAvailable", "0 kB").split()[0]) // 1024
        load = [float(x) for x in (self._read("/proc/loadavg", "0 0 0").split()[:3])]
        uptime = int(float((self._read("/proc/uptime", "0").split() or ["0"])[0]))
        os_release = dict(
            _kv(line, "=") for line in self._read("/etc/os-release").splitlines() if "=" in line
        )
        dmi = "/sys/class/dmi/id/"
        return NodeSummary(
            hostname=self._read("/etc/hostname") or socket.gethostname(),
            vendor=self._read(dmi + "sys_vendor"),
            model=self._read(dmi + "product_name"),
            serial=self._read(dmi + "product_serial"),
            bios=self._read(dmi + "bios_version"),
            cpu_model=" ".join(model.split()),
            sockets=sockets,
            cores=cores,
            threads=len(fields),
            virtualization=virt,
            memory_mb=total,
            memory_used_mb=max(0, total - avail),
            cpu_percent=self._cpu_percent(),
            load=load,
            uptime_s=uptime,
            kernel=self._read("/proc/sys/kernel/osrelease"),
            os=os_release.get("PRETTY_NAME", "").strip('"'),
            version=__version__,
            timezone=self._timezone(),
        )

    def _cpu_percent(self) -> float:
        def sample() -> tuple[int, int]:
            first = self._read("/proc/stat").splitlines()[:1]
            nums = [int(x) for x in first[0].split()[1:]] if first else [0]
            idle = sum(nums[3:5])  # idle + iowait
            return sum(nums), idle

        total1, idle1 = sample()
        time.sleep(self.sample_seconds)
        total2, idle2 = sample()
        busy = (total2 - total1) - (idle2 - idle1)
        return round(100.0 * busy / (total2 - total1), 1) if total2 > total1 else 0.0

    def _timezone(self) -> str:
        tz = self._read("/etc/timezone")
        if tz:
            return tz
        try:
            target = os.readlink(self._path("/etc/localtime"))
        except OSError:
            return "UTC"
        return target.split("zoneinfo/", 1)[-1]

    # Network ports

    def nics(self) -> list[HostNic]:
        base = self._path("/sys/class/net")
        if not base.exists():
            return []
        addresses = self._addresses()
        found = []
        for path in sorted(base.iterdir()):
            name = path.name
            if (path / "bridge").exists():
                kind = "bridge"
            elif (path / "bonding").exists():
                kind = "bond"
            elif (path / "device").exists():
                kind = "ethernet"
            else:
                continue  # lo, VM ports, VLAN devices
            if VLAN_BRIDGE.match(name):
                continue  # VLAN networks have their own page
            nic = HostNic(name=name, kind=kind)
            nic.mac = self._read(f"/sys/class/net/{name}/address")
            nic.up = self._read(f"/sys/class/net/{name}/operstate") in ("up", "unknown") or bool(
                int(self._read(f"/sys/class/net/{name}/flags", "0x0"), 16) & 1
            )
            nic.link = self._read(f"/sys/class/net/{name}/carrier") == "1"
            speed = self._read(f"/sys/class/net/{name}/speed")
            nic.speed_mbps = int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None
            mtu = self._read(f"/sys/class/net/{name}/mtu")
            nic.mtu = int(mtu) if mtu.isdigit() else 1500
            nic.driver = _link_name(path / "device" / "driver")
            nic.master = _link_name(path / "master") or None
            if kind == "bridge":
                nic.members = sorted(
                    p.name
                    for p in (path / "brif").iterdir()
                    if not p.name.startswith(("vnet", "tap"))
                )
            elif kind == "bond":
                nic.members = self._read(f"/sys/class/net/{name}/bonding/slaves").split()
            if kind == "ethernet":
                nic.model = self._model(_link_name(path / "device"))
            nic.addresses = addresses.get(name, [])
            found.append(nic)
        return found

    def _addresses(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self.run(["ip", "-j", "-4", "addr", "show"]) or "[]")
        except (RuntimeError, OSError, ValueError):
            return {}
        return {
            d["ifname"]: [f"{a['local']}/{a['prefixlen']}" for a in d.get("addr_info", [])]
            for d in data
            if "ifname" in d
        }

    def _model(self, slot: str) -> str:
        if not slot:
            return ""
        try:
            out = self.run(["lspci", "-s", slot])
        except (RuntimeError, OSError):
            return ""
        line = out.strip().splitlines()[:1]
        return line[0].split(": ", 1)[-1] if line else ""

    def _read_interfaces(self) -> str:
        return self._read(INTERFACES) + "\n"

    def _keep_address(self) -> str:
        # A compute node's agent listens on one address, the one the manager knows.
        return os.environ.get("IBHICS_AGENT_HOST", "")

    def _switch(self, old_text: str, new_text: str) -> None:
        old, new = parse_interfaces(old_text), parse_interfaces(new_text)
        names = affected(old, new)
        prev = self._path(INTERFACES + ".ibhics-prev")
        prev.write_text(old_text)
        for name in reversed(stack_order(old)):
            if name in names:
                try:
                    self.run(["ifdown", "--force", f"--interfaces={prev}", name])
                except RuntimeError:
                    pass  # already down
        self._path(INTERFACES).write_text(new_text)
        errors = []
        for name in stack_order(new):
            if name in names and new.get(name).auto:  # bridges and bonds bring up their ports
                try:
                    self.run(["ifup", name])
                except RuntimeError as exc:
                    errors.append(str(exc))
        if LAN_BRIDGE in names:
            self._vlans_again()
        self.reattach()
        if errors:
            raise RuntimeError("; ".join(errors))

    def _vlans_again(self) -> None:
        """VLAN networks hang off br0 and went with it: bring them back."""
        folder = self._path(IFACES_DIR)
        for path in sorted(folder.glob("ibhics-vlan*")) if folder.exists() else []:
            bridge = f"ibr{path.name.removeprefix('ibhics-vlan')}"
            for cmd in (["ifdown", "--force", bridge], ["ifup", bridge]):
                try:
                    self.run(cmd)
                except RuntimeError:
                    pass

    # Settings

    def timezones(self) -> list[str]:
        return list_timezones(self._path(ZONEINFO))

    def settings(self) -> HostSettings:
        sources = self._path(CHRONY_SOURCES)
        try:
            synced = self.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
            synced_flag: bool | None = synced.strip() == "yes"
        except (RuntimeError, OSError):
            synced_flag = None
        return HostSettings(
            hostname=self._read("/etc/hostname") or socket.gethostname(),
            timezone=self._timezone(),
            dns=parse_resolv(self._read(RESOLV_CONF)),
            ntp=chrony_servers(
                self._read(CHRONY_CONF), sources.read_text() if sources.exists() else None
            ),
            ntp_synced=synced_flag,
            time=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    def _write_settings(self, edit: SettingsEdit) -> None:
        if edit.timezone is not None and edit.timezone != self._timezone():
            self.run(["timedatectl", "set-timezone", edit.timezone])
        if edit.dns is not None:
            path = self._path(RESOLV_CONF)
            if path.is_symlink():
                path.unlink()  # a stub from another resolver: ours replaces it
            path.write_text(render_resolv(self._read(RESOLV_CONF), edit.dns))
        if edit.ntp is not None:
            sources = self._path(CHRONY_SOURCES)
            conf = self._path(CHRONY_CONF)
            if edit.ntp:
                sources.parent.mkdir(parents=True, exist_ok=True)
                sources.write_text(render_chrony_sources(edit.ntp))
            elif sources.exists():
                sources.unlink()
            if conf.exists():
                conf.write_text(render_chrony_conf(conf.read_text(), bool(edit.ntp)))
            try:
                self.run(["systemctl", "restart", "chrony"])
            except RuntimeError:
                pass  # picked up when chrony next starts

    def power(self, action: str) -> None:
        verb = {"reboot": "reboot", "shutdown": "poweroff"}.get(action)
        if verb is None:
            raise ValueError("action is reboot or shutdown")
        # A moment later, so this request still gets its answer.
        self.run(["systemd-run", "--on-active=5", "/bin/systemctl", verb])


def _kv(line: str, sep: str = ":") -> tuple[str, str]:
    key, _, value = line.partition(sep)
    return key.strip(), value.strip()


def _link_name(path: Path) -> str:
    try:
        return os.readlink(path).rstrip("/").rsplit("/", 1)[-1]
    except OSError:
        return ""


class FakeHost(HostSystem):
    """A node with four ports, for development and tests."""

    confirm_seconds = CONFIRM_SECONDS

    def __init__(self, hostname: str = "") -> None:
        super().__init__()
        self.hostname = hostname or socket.gethostname()
        self.text = render_interfaces(
            HostNetwork(
                [
                    IfaceConfig("ens2f0"),
                    IfaceConfig("ens2f1", auto=False),
                    IfaceConfig("ens3f0", auto=False),
                    IfaceConfig("ens3f1", auto=False),
                    IfaceConfig(
                        LAN_BRIDGE,
                        kind="bridge",
                        method="static",
                        address="10.0.0.11/24",
                        gateway="10.0.0.1",
                        ports=["ens2f0"],
                    ),
                ]
            )
        )
        self._settings = HostSettings(
            hostname=self.hostname,
            timezone="Asia/Bangkok",
            dns=["10.0.0.1"],
            ntp=["0.debian.pool.ntp.org"],
            ntp_synced=True,
        )
        self.powered: list[str] = []
        self.started = time.time()

    def summary(self) -> NodeSummary:
        return NodeSummary(
            hostname=self.hostname,
            vendor="Lenovo",
            model="ThinkSystem SR645",
            serial="J30ABCDE",
            bios="D8E132H",
            cpu_model="AMD EPYC 7313 16-Core Processor",
            sockets=2,
            cores=32,
            threads=64,
            virtualization="AMD-V",
            memory_mb=262144,
            memory_used_mb=50331,
            cpu_percent=7.5,
            load=[1.2, 0.9, 0.8],
            uptime_s=int(time.time() - self.started) + 86400 * 3,
            kernel="6.12.48-amd64",
            os="Debian GNU/Linux 13 (trixie)",
            version=__version__,
            timezone=self._settings.timezone,
        )

    def nics(self) -> list[HostNic]:
        net = self.network()
        out = []
        for i, name in enumerate(["ens2f0", "ens2f1", "ens3f0", "ens3f1"]):
            cfg = net.get(name) or IfaceConfig(name)
            owner = net.owner(name)
            out.append(
                HostNic(
                    name=name,
                    kind="ethernet",
                    mac=f"08:c0:eb:5a:3e:{i:02x}",
                    up=cfg.auto or owner is not None,
                    link=i in (0, 2),
                    speed_mbps=10000 if i in (0, 2) else None,
                    mtu=cfg.mtu or 1500,
                    driver="ice" if i < 2 else "bnxt_en",
                    model="Intel Corporation Ethernet Controller E810-C"
                    if i < 2
                    else "Broadcom Inc. BCM57414 NetXtreme-E 10Gb/25Gb",
                    master=owner.name if owner else None,
                    addresses=[cfg.address] if cfg.method == "static" else [],
                )
            )
        for cfg in net.interfaces:
            if cfg.kind in ("bond", "bridge"):
                out.append(
                    HostNic(
                        name=cfg.name,
                        kind=cfg.kind,
                        mac="08:c0:eb:5a:3e:00",
                        up=cfg.auto,
                        link=True,
                        mtu=cfg.mtu or 1500,
                        members=list(cfg.ports),
                        master=net.owner(cfg.name).name if net.owner(cfg.name) else None,
                        addresses=[cfg.address] if cfg.method == "static" else [],
                    )
                )
        return out

    def timezones(self) -> list[str]:
        return [
            "America/New_York",
            "Asia/Bangkok",
            "Asia/Singapore",
            "Asia/Tokyo",
            "Europe/London",
            "UTC",
        ]

    def settings(self) -> HostSettings:
        return replace(self._settings, time=datetime.now().astimezone().isoformat("T", "seconds"))

    def _write_settings(self, edit: SettingsEdit) -> None:
        if edit.timezone is not None:
            self._settings.timezone = edit.timezone
        if edit.dns is not None:
            self._settings.dns = list(edit.dns)
        if edit.ntp is not None:
            self._settings.ntp = list(edit.ntp) or ["0.debian.pool.ntp.org"]

    def power(self, action: str) -> None:
        if action not in ("reboot", "shutdown"):
            raise ValueError("action is reboot or shutdown")
        self.powered.append(action)

    def _read_interfaces(self) -> str:
        return self.text

    def _switch(self, old_text: str, new_text: str) -> None:
        self.text = new_text
