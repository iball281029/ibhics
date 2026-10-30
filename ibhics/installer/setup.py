"""ibhics-setup: the first-boot wizard on a freshly installed node's console.

It asks what this node is (first node with the manager, an additional compute node,
or a witness), its name and management address, the passwords, and the cluster
token, then configures the system and starts the right service. The rendering and
applying steps are separate from the questions so they can be tested.
"""

from __future__ import annotations

import getpass
import ipaddress
import re
import secrets
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__

PRODUCT = "ibHiCS Hyper-Converged Infrastructure"
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
ROLES = {
    "manager": "First node: web console + compute (start a new cluster)",
    "compute": "Additional compute node (join an existing cluster)",
    "witness": "Witness (quorum vote for a 2-node cluster, runs no VMs)",
}
MANAGER_PORT = 8006
AGENT_PORT = 8007
BRIDGE = "br0"


@dataclass
class Config:
    role: str
    hostname: str
    interface: str
    address: str  # CIDR, e.g. 10.0.0.11/24
    gateway: str
    dns: list[str] = field(default_factory=list)
    timezone: str = "Asia/Bangkok"
    token: str = ""

    @property
    def ip(self) -> str:
        return str(ipaddress.ip_interface(self.address).ip)

    @property
    def runs_vms(self) -> bool:
        return self.role in ("manager", "compute")

    def validate(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        check_hostname(self.hostname)
        check_cidr(self.address)
        check_ip(self.gateway)
        if ipaddress.ip_address(self.gateway) not in ipaddress.ip_interface(self.address).network:
            raise ValueError("the gateway must be inside the node's subnet")
        for server in self.dns:
            check_ip(server)
        check_token(self.token)


# Validation


def check_hostname(value: str) -> str:
    if not HOSTNAME_RE.match(value):
        raise ValueError("use lowercase letters, digits and '-', up to 63 characters")
    return value


def check_cidr(value: str) -> str:
    try:
        iface = ipaddress.ip_interface(value)
    except ValueError:
        raise ValueError("write the address with its prefix, e.g. 10.0.0.11/24") from None
    if "/" not in value or iface.version != 4 or iface.network.prefixlen >= 31:
        raise ValueError("write an IPv4 address with its prefix, e.g. 10.0.0.11/24")
    return value


def check_ip(value: str) -> str:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        raise ValueError(f"{value!r} is not an IPv4 address") from None
    return value


def check_token(value: str) -> str:
    if len(value) < 16:
        raise ValueError("the cluster token is at least 16 characters; copy it exactly")
    return value


def check_password(value: str) -> str:
    if len(value) < 8:
        raise ValueError("use at least 8 characters")
    return value


# What gets written


def render_interfaces(cfg: Config) -> str:
    """ifupdown config. Nodes that run VMs put the management address on a bridge so
    VMs can sit on the same LAN; a witness keeps it on the NIC."""
    dns = f"    dns-nameservers {' '.join(cfg.dns)}\n" if cfg.dns else ""
    head = "# Written by ibhics-setup.\nsource /etc/network/interfaces.d/*\n\n"
    head += "auto lo\niface lo inet loopback\n\n"
    if not cfg.runs_vms:
        return head + (
            f"auto {cfg.interface}\niface {cfg.interface} inet static\n"
            f"    address {cfg.address}\n    gateway {cfg.gateway}\n{dns}"
        )
    return head + (
        f"iface {cfg.interface} inet manual\n\n"
        f"auto {BRIDGE}\niface {BRIDGE} inet static\n"
        f"    bridge_ports {cfg.interface}\n    bridge_stp off\n    bridge_fd 0\n"
        f"    address {cfg.address}\n    gateway {cfg.gateway}\n{dns}"
    )


def render_env(cfg: Config) -> str:
    lines = [
        "# Written by ibhics-setup. Read by ibhics-manager.service / ibhics-agent.service.",
        f"IBHICS_NODE_NAME={cfg.hostname}",
        f"IBHICS_CLUSTER_TOKEN={cfg.token}",
    ]
    if cfg.role == "manager":
        lines += [
            "IBHICS_HOST=0.0.0.0",
            f"IBHICS_PORT={MANAGER_PORT}",
            "IBHICS_DRIVER=libvirt",
            "IBHICS_TLS_CERT=/etc/ibhics/tls/manager.crt",
            "IBHICS_TLS_KEY=/etc/ibhics/tls/manager.key",
            f"IBHICS_MIGRATION_HOST={cfg.ip}",
        ]
    else:
        lines += [
            f"IBHICS_ROLE={cfg.role}",
            f"IBHICS_AGENT_HOST={cfg.ip}",
            f"IBHICS_AGENT_PORT={AGENT_PORT}",
            "IBHICS_DRIVER=libvirt",
        ]
    return "\n".join(lines) + "\n"


def render_hosts(cfg: Config) -> str:
    return (
        "127.0.0.1\tlocalhost\n"
        f"{cfg.ip}\t{cfg.hostname}\n\n"
        "::1\tlocalhost ip6-localhost ip6-loopback\n"
        "ff02::1\tip6-allnodes\nff02::2\tip6-allrouters\n"
    )


def render_issue(cfg: Config) -> str:
    """The text above the console login prompt: where to go next."""
    bar = "=" * 64
    if cfg.role == "manager":
        where = f"  Web console:  https://{cfg.ip}:{MANAGER_PORT}   (user: admin)"
    else:
        where = (
            f"  Add this {cfg.role} in the web console under Nodes > Join node:\n"
            f"    name {cfg.hostname}, address http://{cfg.ip}:{AGENT_PORT}"
        )
    return (
        f"{bar}\n  {PRODUCT} beta version {_short_version()}\n"
        f"  Node {cfg.hostname} ({cfg.role}) at {cfg.ip}\n\n{where}\n{bar}\n\n"
    )


def render_bridge_network() -> str:
    """libvirt's 'default' network, pointed at the LAN bridge instead of NAT."""
    return (
        "<network><name>default</name><forward mode='bridge'/>"
        f"<bridge name='{BRIDGE}'/></network>\n"
    )


def _short_version() -> str:
    return ".".join(__version__.split(".")[:2])


# Applying

Runner = Callable[..., object]


def apply(
    cfg: Config,
    root_password: str,
    admin_password: str | None,
    root: Path = Path("/"),
    run: Runner = subprocess.run,
) -> None:
    """Configure this machine. `root` and `run` let tests do it in a scratch directory."""
    cfg.validate()

    def write(path: str, text: str, mode: int = 0o644) -> None:
        target = root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        target.chmod(mode)

    def sh(*cmd: str, check: bool = True, stdin: str | None = None) -> None:
        run(list(cmd), check=check, input=stdin, text=True)

    write("/etc/hostname", cfg.hostname + "\n")
    write("/etc/hosts", render_hosts(cfg))
    write("/etc/network/interfaces", render_interfaces(cfg))
    write("/etc/ibhics/ibhics.env", render_env(cfg), 0o600)  # holds the cluster token
    write("/etc/issue", render_issue(cfg))
    sh("hostname", cfg.hostname)
    sh("timedatectl", "set-timezone", cfg.timezone)
    sh("chpasswd", stdin=f"root:{root_password}\n")
    sh("systemctl", "restart", "networking")

    if cfg.runs_vms:
        write("/etc/ibhics/default-network.xml", render_bridge_network())
        sh("virsh", "net-destroy", "default", check=False)
        sh("virsh", "net-undefine", "default", check=False)
        sh("virsh", "net-define", "/etc/ibhics/default-network.xml")
        sh("virsh", "net-autostart", "default")
        sh("virsh", "net-start", "default")
        sh(
            "virsh",
            "pool-define-as",
            "default",
            "dir",
            "--target",
            "/var/lib/ibhics/pools/default",
            check=False,  # already there if setup is run again
        )
        sh("virsh", "pool-autostart", "default")
        sh("virsh", "pool-start", "default", check=False)

    if cfg.role == "manager":
        tls = root / "etc/ibhics/tls"
        tls.mkdir(parents=True, exist_ok=True)
        sh(
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-days",
            "3650",
            "-subj",
            f"/CN={cfg.hostname}",
            "-addext",
            f"subjectAltName=DNS:{cfg.hostname},IP:{cfg.ip}",
            "-keyout",
            "/etc/ibhics/tls/manager.key",
            "-out",
            "/etc/ibhics/tls/manager.crt",
        )
        from ..manager.auth import Auth

        auth = Auth(state_path=root / "var/lib/ibhics/users.json")
        auth.load()
        if admin_password:
            auth.bootstrap(admin_password, must_change=False)
        service = "ibhics-manager.service"
    else:
        service = "ibhics-agent.service"

    write("/etc/ibhics/configured", f"{cfg.role}\n")
    sh("systemctl", "enable", "--now", service)
    sh("systemctl", "disable", "ibhics-firstboot.service", check=False)


# Questions


def detect_interfaces(sys_net: Path = Path("/sys/class/net")) -> list[str]:
    """Physical NICs: the ones backed by a device, so not lo, bridges or tap devices."""
    if not sys_net.exists():
        return []
    return sorted(p.name for p in sys_net.iterdir() if (p / "device").exists())


def current_address(interface: str) -> tuple[str, str]:
    """The DHCP address and default gateway the installer got, as suggestions."""
    try:
        addr = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", interface],
            capture_output=True,
            text=True,
        ).stdout.split()
        route = subprocess.run(
            ["ip", "-4", "route", "show", "default"], capture_output=True, text=True
        ).stdout.split()
    except OSError:
        return "", ""
    cidr = addr[addr.index("inet") + 1] if "inet" in addr else ""
    gateway = route[route.index("via") + 1] if "via" in route else ""
    return cidr, gateway


def ask(
    prompt: str,
    default: str = "",
    check: Callable[[str], object] | None = None,
    reader: Callable[[str], str] = input,
) -> str:
    while True:
        hint = f" [{default}]" if default else ""
        value = reader(f"{prompt}{hint}: ").strip() or default
        if not value:
            print("  A value is needed.")
            continue
        try:
            if check:
                check(value)
        except ValueError as exc:
            print(f"  {exc}")
            continue
        return value


def ask_password(prompt: str, reader: Callable[[str], str] = getpass.getpass) -> str:
    while True:
        first = reader(f"{prompt}: ")
        try:
            check_password(first)
        except ValueError as exc:
            print(f"  {exc}")
            continue
        if reader("  Type it again: ") == first:
            return first
        print("  The two entries do not match.")


def choose(prompt: str, options: Sequence[tuple[str, str]], reader=input) -> str:
    print(prompt)
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    keys = [key for key, _ in options]

    def check(value: str) -> None:
        if not value.isdigit() or not 1 <= int(value) <= len(keys):
            raise ValueError(f"type a number from 1 to {len(keys)}")

    return keys[int(ask("Choose", "1", check, reader)) - 1]


def main() -> int:
    print(f"\n  Welcome to {PRODUCT} beta version {_short_version()}\n")
    print("  This sets up the node. You can run it again later with: ibhics-setup\n")
    role = choose("What is this node?", list(ROLES.items()))
    hostname = ask("Node name", "node1" if role == "manager" else "", check_hostname)

    nics = detect_interfaces()
    if not nics:
        print("No network interface found.")
        return 1
    interface = (
        nics[0] if len(nics) == 1 else choose("Management network port", [(n, n) for n in nics])
    )
    suggested, gateway = current_address(interface)
    address = ask("Management address with prefix", suggested, check_cidr)
    gateway = ask("Default gateway", gateway, check_ip)
    dns = ask("DNS servers (space separated)", gateway, lambda v: [check_ip(x) for x in v.split()])
    timezone = ask("Time zone", "Asia/Bangkok")

    if role == "manager":
        token = secrets.token_urlsafe(24)
    else:
        token = ask("Cluster token (shown on the first node's console)", "", check_token)
    root_password = ask_password("Password for the console and SSH user 'root'")
    admin_password = (
        ask_password("Password for the web console user 'admin'") if role == "manager" else None
    )

    cfg = Config(role, hostname, interface, address, gateway, dns.split(), timezone, token)
    try:
        cfg.validate()
    except ValueError as exc:
        print(f"\n  {exc}\n")
        return 1
    print(f"\n  {ROLES[role]}\n  {hostname} at {address} via {gateway} on {interface}\n")
    if ask("Apply these settings? (yes/no)", "yes").lower() not in ("y", "yes"):
        return 1
    apply(cfg, root_password, admin_password)
    print("\n" + render_issue(cfg))
    if role == "manager":
        print(
            f"  Cluster token (needed to add nodes; also in /etc/ibhics/ibhics.env):\n  {token}\n"
        )
    input("Press Enter to continue to the login prompt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
