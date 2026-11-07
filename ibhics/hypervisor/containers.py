"""Containers (v1.0): system containers with LXC, beside the VMs.

A container shares the node's kernel, so it starts in a second and uses only the memory
its programs use. ibHiCS keeps them simple, like VMs: pick a template, CPU, memory, disk
and network; the root file system is an image file in a storage pool (so it has a real
size limit and moves with the pool), the network is a port on the same virtual switch as
VMs, and they are unprivileged by default (root in the container is nobody on the node).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import shlex
import subprocess
import threading
import time
import uuid as uuidlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .base import InvalidState

log = logging.getLogger("ibhics.containers")

CT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")  # also its host name: DNS-safe
SNAP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


class ContainerNotFound(LookupError):
    pass


# Templates: distribution images from images.linuxcontainers.org (lxc "download"), and
# app templates that are a distribution plus a first-boot script.
DISTROS = {
    "debian-13": ("debian", "trixie", "Debian 13"),
    "debian-12": ("debian", "bookworm", "Debian 12"),
    "ubuntu-24.04": ("ubuntu", "noble", "Ubuntu 24.04 LTS"),
    "ubuntu-22.04": ("ubuntu", "jammy", "Ubuntu 22.04 LTS"),
    "rockylinux-9": ("rockylinux", "9", "Rocky Linux 9"),
    "almalinux-9": ("almalinux", "9", "AlmaLinux 9"),
    "alpine-3.20": ("alpine", "3.20", "Alpine Linux 3.20"),
}
APT = "apt-get update && apt-get install -y "
# Docker in an unprivileged container: overlay2 when the kernel lets the container mount
# overlay (tried on the same disk as /var/lib/docker), else fuse-overlayfs, never vfs
# (vfs copies every layer and fills the disk on the first pull).
DOCKER = (
    APT + "docker.io docker-compose fuse-overlayfs && "
    "mkdir -p /var/lib/ovl-test/l /var/lib/ovl-test/u /var/lib/ovl-test/w /var/lib/ovl-test/m && "
    "if mount -t overlay overlay -o lowerdir=/var/lib/ovl-test/l,upperdir=/var/lib/ovl-test/u,"
    "workdir=/var/lib/ovl-test/w /var/lib/ovl-test/m 2>/dev/null; "
    "then umount /var/lib/ovl-test/m; "
    "else mkdir -p /etc/docker && "
    """echo '{"storage-driver": "fuse-overlayfs"}' > /etc/docker/daemon.json; fi; """
    "rm -rf /var/lib/ovl-test; systemctl restart docker && "
    "echo Docker storage driver: $(docker info --format '{{.Driver}}')"
)
NPM_COMPOSE = (
    "services:\n  app:\n    image: jc21/nginx-proxy-manager:latest\n"
    '    restart: unless-stopped\n    ports: ["80:80", "81:81", "443:443"]\n'
    "    volumes: [./data:/data, ./letsencrypt:/etc/letsencrypt]\n"
)
DOCKER_APPS = {"docker", "npm"}  # these turn nesting on
APPS = {
    "nginx": ("debian-13", "Nginx web server", APT + "nginx"),
    "mariadb": ("debian-13", "MariaDB database", APT + "mariadb-server"),
    "postgresql": ("debian-13", "PostgreSQL database", APT + "postgresql"),
    "redis": ("debian-13", "Redis cache", APT + "redis-server"),
    "docker": ("debian-13", "Docker host (nesting on)", DOCKER),
    "npm": (
        "debian-13",
        "Nginx Proxy Manager (Docker; admin on port 81)",
        DOCKER + " && mkdir -p /opt/npm && cd /opt/npm && "
        f"printf '{NPM_COMPOSE}' > docker-compose.yml && docker-compose up -d",
    ),
    "zabbix-agent": ("debian-13", "Zabbix agent", APT + "zabbix-agent2"),
    "zabbix": (
        "debian-13",
        "Zabbix server (web UI on /zabbix)",
        APT + "postgresql zabbix-server-pgsql zabbix-frontend-php php-pgsql "
        "apache2 libapache2-mod-php && "
        'su postgres -c "createuser zabbix; createdb -O zabbix zabbix" && '
        "zcat /usr/share/zabbix-server-pgsql/schema.sql.gz /usr/share/zabbix-server-pgsql/"
        "images.sql.gz /usr/share/zabbix-server-pgsql/data.sql.gz 2>/dev/null "
        "| su zabbix -s /bin/sh -c 'psql -q zabbix'; "
        "ln -sf /usr/share/zabbix /var/www/html/zabbix; "
        "systemctl enable --now zabbix-server apache2",
    ),
    "grafana": (
        "debian-13",
        "Grafana dashboards (port 3000)",
        APT + "apt-transport-https wget gpg && mkdir -p /etc/apt/keyrings && "
        "wget -qO- https://apt.grafana.com/gpg.key | gpg --dearmor "
        "> /etc/apt/keyrings/grafana.gpg && "
        "echo 'deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main' "
        "> /etc/apt/sources.list.d/grafana.list && "
        + APT
        + "grafana && systemctl enable --now grafana-server",
    ),
    "wordpress": (
        "debian-13",
        "WordPress (Apache, PHP, MariaDB)",
        APT + "apache2 php php-mysql mariadb-server wordpress",
    ),
    "nextcloud": (
        "debian-13",
        "Nextcloud files (Apache, PHP, MariaDB; open /nextcloud)",
        APT + "apache2 libapache2-mod-php mariadb-server wget bzip2 php-gd php-mysql "
        "php-curl php-mbstring php-intl php-gmp php-bcmath php-xml php-imagick php-zip && "
        "wget -qO /tmp/nc.tar.bz2 https://download.nextcloud.com/server/releases/latest.tar.bz2 "
        "&& tar -xjf /tmp/nc.tar.bz2 -C /var/www/html && chown -R www-data: "
        "/var/www/html/nextcloud && rm /tmp/nc.tar.bz2 && systemctl restart apache2",
    ),
    "nodejs": ("debian-13", "Node.js runtime", APT + "nodejs npm"),
    "python": ("debian-13", "Python 3 with pip and venv", APT + "python3-pip python3-venv"),
    "samba": ("debian-13", "Samba file share", APT + "samba"),
    "pihole-dns": ("debian-13", "DNS/DHCP server (dnsmasq)", APT + "dnsmasq"),
    "haproxy": ("debian-13", "HAProxy load balancer", APT + "haproxy"),
    "gitea": ("debian-13", "Git server (git + SSH)", APT + "git openssh-server"),
    "lamp": ("debian-13", "LAMP stack (Apache, MariaDB, PHP)", APT + "apache2 mariadb-server php"),
}

IMAGE_INDEX = "https://images.linuxcontainers.org/meta/1.0/index-system"
LXC_ID = re.compile(r"^lxc:([a-z0-9][a-z0-9.-]*):([A-Za-z0-9][A-Za-z0-9._-]*)$")


def parse_image_index(text: str, arch: str = "amd64") -> list[dict]:
    """Every distribution and release images.linuxcontainers.org has, from its index
    (lines of distro;release;arch;variant;serial;path)."""
    seen, out = set(), []
    for line in text.splitlines():
        parts = line.strip().split(";")
        if len(parts) < 4 or parts[2] != arch or parts[3] != "default":
            continue
        distro, release = parts[0], parts[1]
        tid = f"lxc:{distro}:{release}"
        if (distro, release) in seen or not LXC_ID.match(tid):
            continue
        seen.add((distro, release))
        out.append({"id": tid, "kind": "image", "title": f"{distro.capitalize()} {release}",
                    "distro": distro, "release": release})  # fmt: skip
    return sorted(out, key=lambda t: (t["distro"], t["release"]))


def catalog(images: list[dict] | None = None) -> list[dict]:
    """The curated templates first, then the rest of the image server's list."""
    out = [
        {"id": k, "kind": "os", "title": title, "distro": d, "release": r}
        for k, (d, r, title) in DISTROS.items()
    ]
    for k, (base, title, _script) in APPS.items():
        d, r, os_title = DISTROS[base]
        out.append({"id": f"app:{k}", "kind": "app", "title": title, "distro": d,
                    "release": r, "base": os_title})  # fmt: skip
    curated = {(t["distro"], t["release"]) for t in out}
    out += [t for t in images or [] if (t["distro"], t["release"]) not in curated]
    return out


@dataclass
class ContainerSpec:
    name: str
    template: str = "debian-13"
    vcpus: int = 1
    memory_mb: int = 512
    swap_mb: int = 512
    disk_gb: int = 8
    pool: str = "default"
    network: str = "default"
    ip: str = "dhcp"  # "dhcp" or an address with prefix, e.g. 10.0.0.50/24
    gateway: str = ""
    dns: str = ""
    password: str = ""  # root password; empty: none (SSH key or console only)
    ssh_key: str = ""
    unprivileged: bool = True
    nesting: bool = False  # lets Docker and the like run inside
    autostart: bool = False
    description: str = ""
    mac: str = ""  # eth0's; made at create
    # More network cards (eth1, eth2, ...): {"network", "ip", "gateway", "mac"} each.
    nics: list[dict] = field(default_factory=list)

    def interfaces(self) -> list[dict]:
        """Every network card, eth0 first."""
        first = {"network": self.network, "ip": self.ip, "gateway": self.gateway, "mac": self.mac}
        return [{**n, "name": f"eth{i}"} for i, n in enumerate([first, *self.nics])]

    def with_interfaces(self, cards: list[dict], dns: str | None = None) -> ContainerSpec:
        if not 1 <= len(cards) <= MAX_NICS:
            raise ValueError(f"a container has 1 to {MAX_NICS} network cards")
        cards = [
            {
                "network": str(c.get("network") or "default"),
                "ip": str(c.get("ip") or "dhcp").strip(),
                "gateway": str(c.get("gateway") or "").strip(),
                "mac": str(c.get("mac") or "").lower() or random_mac(),
            }
            for c in cards
        ]
        first, *more = cards
        return replace(
            self,
            network=first["network"],
            ip=first["ip"],
            gateway=first["gateway"],
            mac=first["mac"],
            nics=more,
            dns=self.dns if dns is None else dns.strip(),
        )

    def validate(self) -> None:
        if not CT_NAME_RE.match(self.name):
            raise ValueError(
                "container names use small letters, digits and '-' (it is the host name too)"
            )
        known = self.template in DISTROS or self.template.removeprefix("app:") in APPS
        if not known and not LXC_ID.match(self.template):
            raise ValueError(f"unknown template {self.template!r}")
        if not 1 <= self.vcpus <= 256 or self.memory_mb < 64 or not 1 <= self.disk_gb <= 65536:
            raise ValueError("1-256 vCPU, at least 64 MB of memory and 1 GB of disk")
        if self.swap_mb < 0:
            raise ValueError("swap cannot be negative")
        if len(self.nics) + 1 > MAX_NICS:
            raise ValueError(f"a container has 1 to {MAX_NICS} network cards")
        macs = set()
        for card in self.interfaces():
            _check_card(card)
            if card["mac"]:
                if card["mac"] in macs:
                    raise ValueError(f"{card['name']}: MAC {card['mac']} is used twice")
                macs.add(card["mac"])
        for server in self.dns.split():
            ipaddress.ip_address(server)
        if self.ssh_key and not re.match(r"^(ssh-|ecdsa-)\S+ \S+", self.ssh_key.strip()):
            raise ValueError("the SSH key is one line starting with ssh-ed25519, ssh-rsa, ...")
        if "\n" in self.password:
            raise ValueError("the password is one line")
        if self.template.removeprefix("app:") in DOCKER_APPS:
            self.nesting = True

    @property
    def distro(self) -> tuple[str, str]:
        if m := LXC_ID.match(self.template):
            return m.group(1), m.group(2)
        key = APPS[self.template[4:]][0] if self.template.startswith("app:") else self.template
        return DISTROS[key][0], DISTROS[key][1]

    def public(self) -> dict:
        data = asdict(self)
        data.pop("password")
        return data


@dataclass
class ContainerInfo:
    spec: ContainerSpec
    state: str = "stopped"  # running, stopped
    addresses: list[str] = field(default_factory=list)
    cpu_seconds: float = 0.0
    memory_used_mb: int = 0
    disk_used_gb: float = 0.0
    snapshots: list[str] = field(default_factory=list)
    path: str = ""  # its folder on the node: <pool>/containers/<name>

    def to_dict(self) -> dict:
        return {
            **self.spec.public(),
            "state": self.state,
            "addresses": self.addresses,
            "cpu_seconds": self.cpu_seconds,
            "memory_used_mb": self.memory_used_mb,
            "disk_used_gb": self.disk_used_gb,
            "snapshots": self.snapshots,
            "path": self.path,
            "kind": "container",
        }


MAX_NICS = 8
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def random_mac() -> str:
    import secrets

    return "00:16:3e:" + ":".join(f"{b:02x}" for b in secrets.token_bytes(3))


def _check_card(card: dict) -> None:
    name, ip, gw = card["name"], card["ip"], card["gateway"]
    if card["mac"] and not MAC_RE.match(card["mac"]):
        raise ValueError(f"{name}: MAC is six pairs of hex digits, e.g. 00:16:3e:12:34:56")
    if ip == "dhcp":
        if gw:
            raise ValueError(f"{name}: DHCP gives the gateway; leave it empty")
        return
    try:
        iface = ipaddress.IPv4Interface(ip)
    except ValueError:
        raise ValueError(
            f"{name}: IP is 'dhcp' or an address with prefix, e.g. 10.0.0.50/24"
        ) from None
    if "/" not in ip:
        raise ValueError(f"{name}: write the IP with its prefix, e.g. 10.0.0.50/24")
    if gw:
        try:
            inside = ipaddress.IPv4Address(gw) in iface.network
        except ValueError:
            raise ValueError(f"{name}: the gateway is an IPv4 address") from None
        if not inside:
            raise ValueError(f"{name}: the gateway must be inside {iface.network}")


def lxc_config(spec: ContainerSpec, bridge, rootfs: str = "") -> str:
    """The container's LXC config, after the distribution's own includes. `bridge` is
    one bridge for every card, or a function from network name to bridge."""
    bridge_of = bridge if callable(bridge) else (lambda _n: bridge)
    lines = [
        "# Written by ibHiCS: change the container in the web console.",
        "lxc.include = /usr/share/lxc/config/common.conf",
    ]
    if spec.unprivileged:
        lines += [
            "lxc.include = /usr/share/lxc/config/userns.conf",
            "lxc.idmap = u 0 100000 65536",
            "lxc.idmap = g 0 100000 65536",
        ]
    lines += [
        f"lxc.uts.name = {spec.name}",
        *([f"lxc.rootfs.path = loop:{rootfs}"] if rootfs else []),
        f"lxc.cgroup2.memory.max = {spec.memory_mb}M",
        f"lxc.cgroup2.memory.swap.max = {spec.swap_mb}M",
        f"lxc.cgroup2.cpu.max = {spec.vcpus * 100000} 100000",
        f"lxc.start.auto = {1 if spec.autostart else 0}",
        "lxc.apparmor.profile = generated",
        f"lxc.apparmor.allow_nesting = {1 if spec.nesting else 0}",
    ]
    # Addresses are set inside the container (guest_network_script), so DHCP and fixed
    # IPs both work with the distribution's own network setup.
    for i, card in enumerate(spec.interfaces()):
        lines += [
            f"lxc.net.{i}.type = veth",
            f"lxc.net.{i}.link = {bridge_of(card['network'])}",
            f"lxc.net.{i}.flags = up",
            f"lxc.net.{i}.name = {card['name']}",
        ]
        if card["mac"]:
            lines.append(f"lxc.net.{i}.hwaddr = {card['mac']}")
    if spec.nesting:
        lines += [
            "lxc.include = /usr/share/lxc/config/nesting.conf",
            # Docker: overlay mounts and /dev/fuse (fuse-overlayfs). The device list
            # repeats LXC's defaults, so adding /dev/fuse keeps /dev/null, ttys, ...
            "lxc.apparmor.raw = mount fstype=overlay,",
            "lxc.apparmor.raw = mount fstype=fuse.fuse-overlayfs,",
            *(f"lxc.cgroup2.devices.allow = {d}" for d in NESTING_DEVICES),
            "lxc.mount.entry = /dev/fuse dev/fuse none bind,create=file,optional 0 0",
        ]
    return "\n".join(lines) + "\n"


NESTING_DEVICES = (
    "c *:* m", "b *:* m", "c 1:3 rwm", "c 1:5 rwm", "c 1:7 rwm", "c 1:8 rwm", "c 1:9 rwm",
    "c 5:0 rwm", "c 5:1 rwm", "c 5:2 rwm", "c 136:* rwm", "c 10:200 rwm", "c 10:229 rwm",
)  # fmt: skip


def guest_network_script(spec: ContainerSpec) -> str:
    """Write the cards' settings the way the distribution reads them (netplan, systemd-
    networkd or ifupdown) and apply them. Run inside the container after each start."""
    q = shlex.quote
    cards = spec.interfaces()
    netplan = ["network:", "  version: 2", "  ethernets:"]
    interfaces = ["auto lo", "iface lo inet loopback", ""]
    steps = ["mkdir -p /etc/systemd/network"]
    dns = spec.dns.split()
    for card in cards:
        n, ip, gw = card["name"], card["ip"], card["gateway"]
        netplan.append(f"    {n}:")
        if ip == "dhcp":
            netplan.append("      dhcp4: true")
            networkd = f"[Match]\nName={n}\n\n[Network]\nDHCP=ipv4\n"
            interfaces += [f"auto {n}", f"iface {n} inet dhcp", ""]
        else:
            netplan += ["      dhcp4: false", f"      addresses: [{ip}]"]
            if gw:
                netplan += ["      routes:", f"        - to: default\n          via: {gw}"]
            if dns:
                netplan.append(f"      nameservers: {{addresses: [{', '.join(dns)}]}}")
            networkd = f"[Match]\nName={n}\n\n[Network]\nAddress={ip}\n"
            networkd += f"Gateway={gw}\n" if gw else ""
            networkd += "".join(f"DNS={s}\n" for s in dns)
            interfaces += [f"auto {n}", f"iface {n} inet static", f"    address {ip}"]
            interfaces += [f"    gateway {gw}"] if gw else []
            interfaces.append("")
        steps.append(f"printf '%b' {q(networkd)} > /etc/systemd/network/{n}.network")
    names = " ".join(c["name"] for c in cards)
    steps += [
        # Cards that were removed lose their files.
        'for f in /etc/systemd/network/eth*.network; do n=$(basename "$f" .network);'
        f' case " {names} " in *" $n "*) ;; *) rm -f "$f" ;; esac; done',
        "if [ -d /etc/netplan ]; then",
        "  rm -f /etc/netplan/*.yaml",
        f"  printf '%s\\n' {' '.join(q(x) for x in netplan)} > /etc/netplan/10-ibhics.yaml",
        "  chmod 600 /etc/netplan/10-ibhics.yaml",
        "  netplan apply 2>&1 || true",
        "elif command -v networkctl >/dev/null && systemctl is-enabled -q systemd-networkd; then",
        "  networkctl reload && for n in " + names + "; do networkctl reconfigure $n; done",
        "elif [ -f /etc/network/interfaces ]; then",
        "  ifdown -a --exclude=lo 2>/dev/null || true",
        f"  printf '%s\\n' {' '.join(q(x) for x in interfaces)} > /etc/network/interfaces",
        "  ifup -a 2>&1 || true",
        "fi",
    ]
    if dns:
        resolv = "".join(f"nameserver {s}\n" for s in dns)
        steps.append(
            f"[ -L /etc/resolv.conf ] && command -v resolvectl >/dev/null || "
            f"printf %s {q(resolv)} > /etc/resolv.conf"
        )
    return "\n".join(steps) + "\n"


def first_boot_script(spec: ContainerSpec) -> str:
    """Run once inside the new container: host name, DNS, root access, the app."""
    q = shlex.quote
    steps = ["set -e", f"echo {q(spec.name)} > /etc/hostname"]
    if spec.dns:
        resolv = "".join(f"nameserver {s}\n" for s in spec.dns.split())
        steps.append(f"printf %s {q(resolv)} > /etc/resolv.conf")
    if spec.password:
        steps.append(f"echo root:{q(spec.password)} | chpasswd")
    if spec.ssh_key:
        steps += [
            "mkdir -p /root/.ssh && chmod 700 /root/.ssh",
            f"echo {q(spec.ssh_key.strip())} >> /root/.ssh/authorized_keys",
            "chmod 600 /root/.ssh/authorized_keys",
        ]
    if spec.template.startswith("app:"):
        steps.append("export DEBIAN_FRONTEND=noninteractive")
        steps.append(APPS[spec.template[4:]][2])
    return "\n".join(steps) + "\n"


EXEC_TIMEOUT = 120
JOB_TIMEOUT = 3600  # s a console command may run (apt upgrade, docker pull, ...)
JOB_OUTPUT = 1 << 20  # bytes of output kept per command (the rest scrolls away)


class ExecJobs:
    """Console commands that run long: started in the background, their output read as
    it comes (offset in, new text out), and stoppable."""

    def __init__(self, popen=subprocess.Popen) -> None:
        self._popen = popen
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, argv: list[str]) -> str:
        self._expire()
        proc = self._popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL)  # fmt: skip
        job = {"proc": proc, "buf": bytearray(), "dropped": 0, "done": False, "code": None,
               "ended": 0.0}  # fmt: skip
        jid = uuidlib.uuid4().hex[:12]
        with self._lock:
            self._jobs[jid] = job

        def pump() -> None:
            read = getattr(proc.stdout, "read1", proc.stdout.read)
            while chunk := read(65536):
                with self._lock:
                    job["buf"] += chunk
                    over = len(job["buf"]) - JOB_OUTPUT
                    if over > 0:
                        del job["buf"][:over]
                        job["dropped"] += over
            code = proc.wait()
            with self._lock:
                job.update(done=True, code=code, ended=time.monotonic())

        threading.Thread(target=pump, daemon=True, name=f"exec-{jid}").start()
        return jid

    def read(self, jid: str, offset: int = 0) -> dict:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                raise InvalidState("that command has ended and its output is gone")
            start = max(0, offset - job["dropped"])
            text = bytes(job["buf"][start:]).decode(errors="replace")
            return {"output": text, "offset": job["dropped"] + len(job["buf"]),
                    "done": job["done"], "code": job["code"]}  # fmt: skip

    def stop(self, jid: str) -> None:
        with self._lock:
            job = self._jobs.get(jid)
        if job and not job["done"]:
            job["proc"].kill()

    def _expire(self) -> None:
        with self._lock:
            old = [j for j, v in self._jobs.items()
                   if v["done"] and time.monotonic() - v["ended"] > 600]  # fmt: skip
            for j in old:
                del self._jobs[j]


class LxcContainers:
    """Containers on this node, with the lxc-* tools. Each lives in <pool>/containers/<name>
    (config, image file), and ibHiCS keeps its settings in ibhics.json beside them."""

    def __init__(
        self,
        pool_path: Callable[[str], str | None],
        bridge_of: Callable[[str], str],
        run: Callable[[list[str]], tuple[int, str]],
        pools: Callable[[], list[str]],
        jobs: ExecJobs | None = None,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ) -> None:
        self._pool_path = pool_path
        self._bridge_of = bridge_of
        self._run = run
        self._pools = pools
        self._jobs = jobs or ExecJobs()
        self._cgroup = cgroup_root

    def _dir(self, pool: str) -> Path:
        path = self._pool_path(pool)
        if not path:
            raise InvalidState(f"containers need a folder or file-system pool; {pool!r} is not")
        return Path(path) / "containers"

    def _find(self, name: str) -> tuple[Path, ContainerSpec]:
        for pool in self._pools():
            try:
                meta = self._dir(pool) / name / "ibhics.json"
            except InvalidState:
                continue
            if meta.is_file():
                return meta.parent.parent, ContainerSpec(**json.loads(meta.read_text()))
        raise ContainerNotFound(name)

    def _lxc(self, cmd: str, lxcpath: Path, name: str, *args: str, check: bool = True) -> str:
        code, out = self._run([f"lxc-{cmd}", "-P", str(lxcpath), "-n", name, *args])
        if check and code:
            raise InvalidState(f"lxc-{cmd} failed: {out.strip()[-500:]}")
        return out

    def list(self) -> list[ContainerInfo]:
        out = []
        for pool in self._pools():
            try:
                base = self._dir(pool)
            except InvalidState:
                continue
            if not base.is_dir():
                continue
            for meta in sorted(base.glob("*/ibhics.json")):
                out.append(self._info(base, ContainerSpec(**json.loads(meta.read_text()))))
        return out

    def get(self, name: str) -> ContainerInfo:
        base, spec = self._find(name)
        return self._info(base, spec)

    def _info(self, base: Path, spec: ContainerSpec) -> ContainerInfo:
        info = ContainerInfo(spec)
        code, text = self._run(["lxc-info", "-P", str(base), "-n", spec.name, "-H"])
        if code == 0:
            for line in text.splitlines():
                key, _, value = line.partition(":")
                value = value.strip()
                if key == "State":
                    info.state = "running" if value == "RUNNING" else "stopped"
                elif key == "IP":
                    info.addresses.append(value)
                elif key == "CPU use" and value:
                    info.cpu_seconds = round(int(value) / 1e9, 1)
                elif key == "Memory use" and value:
                    info.memory_used_mb = int(value) // 2**20
        if info.state == "running" and not info.memory_used_mb:
            # lxc-info leaves Memory use out on cgroup v2 hosts: read the cgroup itself.
            info.memory_used_mb = self._cgroup_memory(spec.name) // 2**20
        info.path = str(base / spec.name)
        image = base / spec.name / "rootfs.img"
        if image.exists():
            info.disk_used_gb = round(image.stat().st_blocks * 512 / 2**30, 2)
        snaps = base / spec.name / "snaps"
        if snaps.is_dir():
            info.snapshots = sorted(p.name for p in snaps.iterdir() if p.is_dir())
        return info

    def _cgroup_memory(self, name: str) -> int:
        for rel in (f"lxc.payload.{name}", f"lxc.payload/{name}", f"lxc/{name}"):
            try:
                return int((self._cgroup / rel / "memory.current").read_text().strip())
            except (OSError, ValueError):
                continue
        return 0

    def create(self, spec: ContainerSpec) -> ContainerInfo:
        spec.validate()
        try:
            self._find(spec.name)
        except ContainerNotFound:
            pass
        else:
            raise InvalidState(f"a container named {spec.name!r} already exists")
        spec = spec.with_interfaces(spec.interfaces())  # gives each card a MAC
        bridge = self._bridge_of
        for card in spec.interfaces():
            bridge(card["network"])  # an unknown network fails here, before anything is made
        base = self._dir(spec.pool)
        home = base / spec.name
        base.mkdir(parents=True, exist_ok=True)
        image = home / "rootfs.img"
        distro, release = spec.distro
        # No rootfs.path here: with one, lxc-create -B loop expects the image to exist.
        # It makes <home>/rootdev itself; ibHiCS renames that to rootfs.img afterwards.
        conf = base / f".{spec.name}.create.conf"
        conf.write_text(lxc_config(spec, bridge))
        try:
            self._lxc(
                "create",
                base,
                spec.name,
                "-f",
                str(conf),
                "-B",
                "loop",
                "--fssize",
                f"{spec.disk_gb}G",
                "-t",
                "download",
                "--",
                "-d",
                distro,
                "-r",
                release,
                "-a",
                "amd64",
            )
            if (home / "rootdev").exists():
                (home / "rootdev").rename(image)
            (home / "config").write_text(
                _keep_template_lines(home / "config", lxc_config(spec, bridge, str(image)))
            )
            (home / "ibhics.json").write_text(json.dumps(asdict(replace(spec, password=""))))
            self._lxc("start", base, spec.name)
            self._wait_running(base, spec.name)
            self._apply_network(spec)
            self.exec(spec.name, first_boot_script(spec), timeout=1800)
        except Exception:
            self._lxc("stop", base, spec.name, "-k", check=False)
            self._lxc("destroy", base, spec.name, check=False)
            raise
        finally:
            conf.unlink(missing_ok=True)
        return self._info(base, spec)

    def _wait_running(self, base: Path, name: str, timeout: float = 30) -> None:
        self._lxc("wait", base, name, "-s", "RUNNING", "-t", str(int(timeout)), check=False)
        time.sleep(2)  # let the network come up before the first-boot script

    def _apply_network(self, spec: ContainerSpec) -> None:
        code, out = self.exec(spec.name, guest_network_script(spec), timeout=90)
        if code:
            log.warning("%s: network settings: %s", spec.name, out.strip()[-300:])

    def action(self, name: str, act: str) -> ContainerInfo:
        base, spec = self._find(name)
        if act in ("start", "restart"):  # settings of newer releases (Docker's, ...) apply
            self._write_config(base, spec)
        if act == "start":
            self._lxc("start", base, name)
        elif act == "stop":
            self._lxc("stop", base, name, "-t", "60")
        elif act == "kill":
            self._lxc("stop", base, name, "-k")
        elif act == "restart":
            self._lxc("stop", base, name, "-t", "60", check=False)
            self._lxc("start", base, name)
        else:
            raise ValueError("action is start, stop, kill or restart")
        if act in ("start", "restart"):
            self._wait_running(base, name)
            self._apply_network(spec)
        return self._info(base, spec)

    def _write_config(self, base: Path, spec: ContainerSpec) -> None:
        home = base / spec.name
        cfg = home / "config"
        if cfg.is_file():
            cfg.write_text(_keep_template_lines(
                cfg, lxc_config(spec, self._bridge_of, str(home / "rootfs.img"))))  # fmt: skip

    def move(self, name: str, pool: str) -> ContainerInfo:
        """A stopped container's folder (disk image, config, snapshots) to another pool."""
        base, spec = self._find(name)
        if pool == spec.pool:
            return self._info(base, spec)
        if pool not in self._pools():
            raise ValueError(f"no storage pool named {pool!r}")
        if self._info(base, spec).state == "running":
            raise InvalidState("stop the container to move it")
        new_base = self._dir(pool)
        old_home, home = base / name, new_base / name
        if home.exists():
            raise InvalidState(f"{home} already exists")
        new_base.mkdir(parents=True, exist_ok=True)
        code, out = self._run(["cp", "-a", "--sparse=always", str(old_home), str(home)])
        if code:
            self._run(["rm", "-rf", str(home)])
            raise InvalidState(f"could not copy the container: {out.strip()[-300:]}")
        for cfg in [home / "config", *home.glob("snaps/*/config")]:
            cfg.write_text(cfg.read_text().replace(f"{old_home}/", f"{home}/"))
        moved = replace(spec, pool=pool)
        (home / "ibhics.json").write_text(json.dumps(asdict(moved)))
        self._run(["rm", "-rf", str(old_home)])
        return self._info(new_base, moved)

    def set_network(self, name: str, cards: list[dict], dns: str | None) -> ContainerInfo:
        """New network cards (network/VLAN, DHCP or fixed IP, gateway), applied by
        restarting the container when it runs."""
        base, spec = self._find(name)
        new = spec.with_interfaces(cards, dns)
        new.validate()
        for card in new.interfaces():
            self._bridge_of(card["network"])
        home = base / name
        cfg = home / "config"
        cfg.write_text(
            _keep_template_lines(cfg, lxc_config(new, self._bridge_of, str(home / "rootfs.img")))
        )
        (home / "ibhics.json").write_text(json.dumps(asdict(new)))
        if self._info(base, spec).state == "running":
            self._lxc("stop", base, name, "-t", "60", check=False)
            self._lxc("start", base, name)
            self._wait_running(base, name)
            self._apply_network(new)
        return self._info(base, new)

    def update(
        self,
        name: str,
        vcpus: int | None,
        memory_mb: int | None,
        disk_gb: int | None,
        autostart: bool | None,
        description: str | None,
    ) -> ContainerInfo:
        base, spec = self._find(name)
        new = replace(
            spec,
            vcpus=vcpus or spec.vcpus,
            memory_mb=memory_mb or spec.memory_mb,
            disk_gb=disk_gb or spec.disk_gb,
            autostart=spec.autostart if autostart is None else autostart,
            description=spec.description if description is None else description,
        )
        new.validate()
        if new.disk_gb < spec.disk_gb:
            raise InvalidState("a container's disk can grow, not shrink")
        info = self._info(base, spec)
        home = base / name
        if info.state == "running":  # CPU and memory change live
            self._lxc("cgroup", base, name, "memory.max", f"{new.memory_mb}M")
            self._lxc("cgroup", base, name, "cpu.max", f"{new.vcpus * 100000} 100000")
        if new.disk_gb > spec.disk_gb:
            image = str(home / "rootfs.img")
            code, out = self._run(["truncate", "-s", f"{new.disk_gb}G", image])
            if code:
                raise InvalidState(f"could not grow the disk: {out.strip()}")
            if info.state == "running":
                self._run(["sh", "-c", f"losetup -c $(losetup -j {shlex.quote(image)} -nO NAME)"])
                self._lxc("attach", base, name, "--", "resize2fs", "/dev/root", check=False)
                code, out = self._run(
                    ["sh", "-c", f"resize2fs $(losetup -j {shlex.quote(image)} -nO NAME)"]
                )
            else:
                self._run(["e2fsck", "-fy", image])
                code, out = self._run(["resize2fs", image])
            if code:
                raise InvalidState(f"the disk grew, but its file system did not: {out.strip()}")
        cfg = home / "config"
        cfg.write_text(
            _keep_template_lines(cfg, lxc_config(new, self._bridge_of, str(home / "rootfs.img")))
        )
        (home / "ibhics.json").write_text(json.dumps(asdict(new)))
        return self._info(base, new)

    def rename(self, name: str, new: str) -> ContainerInfo:
        """A stopped container's new name (and host name); its folder moves with it."""
        base, spec = self._find(name)
        renamed = replace(spec, name=new)
        renamed.validate()
        if new == name:
            return self._info(base, spec)
        try:
            self._find(new)
        except ContainerNotFound:
            pass
        else:
            raise InvalidState(f"a container named {new!r} already exists")
        if self._info(base, spec).state == "running":
            raise InvalidState("stop the container to rename it")
        old_home, home = base / name, base / new
        old_home.rename(home)
        # Paths in its config and its snapshots' configs name the old folder.
        for cfg in [home / "config", *home.glob("snaps/*/config")]:
            text = cfg.read_text().replace(f"{old_home}/", f"{home}/")
            cfg.write_text(text.replace(f"lxc.uts.name = {name}", f"lxc.uts.name = {new}"))
        (home / "ibhics.json").write_text(json.dumps(asdict(renamed)))
        return self._info(base, renamed)

    def delete(self, name: str) -> None:
        base, _spec = self._find(name)
        self._lxc("stop", base, name, "-k", check=False)
        self._lxc("destroy", base, name, "-s")

    def exec(self, name: str, script: str, timeout: int = EXEC_TIMEOUT) -> tuple[int, str]:
        base, _spec = self._find(name)
        code, out = self._run(
            [
                "timeout",
                str(timeout),
                "lxc-attach",
                "-P",
                str(base),
                "-n",
                name,
                "--clear-env",
                "--",
                "/bin/sh",
                "-c",
                script,
            ]
        )
        return code, out

    def exec_start(self, name: str, script: str) -> str:
        base, _spec = self._find(name)
        if self._info(base, _spec).state != "running":
            raise InvalidState(f"{name} is not running")
        env = "export TERM=dumb HOME=/root PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        argv = ["timeout", str(JOB_TIMEOUT), "lxc-attach", "-P", str(base), "-n", name]
        return self._jobs.start(
            [*argv, "--clear-env", "--", "/bin/sh", "-c", f"{env}; cd; {script}"]
        )

    def exec_read(self, name: str, job: str, offset: int = 0) -> dict:
        return self._jobs.read(job, offset)

    def exec_stop(self, name: str, job: str) -> None:
        self._jobs.stop(job)

    def snapshot(self, name: str, snap: str) -> ContainerInfo:
        if not SNAP_RE.match(snap):
            raise ValueError("snapshot names use letters, digits and '_.-'")
        base, spec = self._find(name)
        self._lxc("snapshot", base, name, "-N", snap)
        return self._info(base, spec)

    def restore(self, name: str, snap: str) -> ContainerInfo:
        base, spec = self._find(name)
        running = self._info(base, spec).state == "running"
        if running:
            self._lxc("stop", base, name, "-t", "60")
        self._lxc("snapshot", base, name, "-r", snap, "-N", name)
        if running:
            self._lxc("start", base, name)
        return self._info(base, spec)

    def delete_snapshot(self, name: str, snap: str) -> ContainerInfo:
        base, spec = self._find(name)
        self._lxc("snapshot", base, name, "-d", snap)
        return self._info(base, spec)


def _keep_template_lines(path: Path, ours: str) -> str:
    """The distribution's include line from lxc-create, then ours (without duplicates)."""
    theirs = path.read_text().splitlines() if path.exists() else []
    ours_too = ("common.conf", "userns.conf", "nesting.conf")
    keep = [
        ln
        for ln in theirs
        if ln.startswith("lxc.include")
        and "/usr/share/lxc/config/" in ln
        and ln.split("/")[-1] not in ours_too
    ]
    return "\n".join(keep + ours.splitlines()) + "\n"


class FakeContainers:
    """In memory, for tests and the demo console."""

    def __init__(self, networks: Callable[[], set[str]], pools: Callable[[], set[str]]) -> None:
        self._cts: dict[str, ContainerInfo] = {}
        self._jobs: dict[str, dict] = {}
        self._networks = networks
        self._pools = pools

    def list(self) -> list[ContainerInfo]:
        return list(self._cts.values())

    def get(self, name: str) -> ContainerInfo:
        if name not in self._cts:
            raise ContainerNotFound(name)
        return self._cts[name]

    def create(self, spec: ContainerSpec) -> ContainerInfo:
        spec.validate()
        if spec.name in self._cts:
            raise InvalidState(f"a container named {spec.name!r} already exists")
        spec = spec.with_interfaces(spec.interfaces())
        self._check_networks(spec)
        if spec.pool not in self._pools():
            raise InvalidState(f"no storage pool named {spec.pool!r}")
        info = ContainerInfo(replace(spec, password=""), "running", _fake_ips(spec), 0.5, 40, 0.6)
        self._cts[spec.name] = info
        return info

    def _check_networks(self, spec: ContainerSpec) -> None:
        for card in spec.interfaces():
            if card["network"] not in self._networks():
                raise InvalidState(f"no network named {card['network']!r}")

    def set_network(self, name: str, cards: list[dict], dns: str | None) -> ContainerInfo:
        info = self.get(name)
        new = info.spec.with_interfaces(cards, dns)
        new.validate()
        self._check_networks(new)
        info.spec = new
        info.addresses = _fake_ips(new) if info.state == "running" else []
        return info

    def action(self, name: str, act: str) -> ContainerInfo:
        info = self.get(name)
        if act not in ("start", "stop", "kill", "restart"):
            raise ValueError("action is start, stop, kill or restart")
        info.state = "stopped" if act in ("stop", "kill") else "running"
        return info

    def update(self, name, vcpus, memory_mb, disk_gb, autostart, description) -> ContainerInfo:
        info = self.get(name)
        if disk_gb and disk_gb < info.spec.disk_gb:
            raise InvalidState("a container's disk can grow, not shrink")
        info.spec = replace(
            info.spec,
            vcpus=vcpus or info.spec.vcpus,
            memory_mb=memory_mb or info.spec.memory_mb,
            disk_gb=disk_gb or info.spec.disk_gb,
            autostart=info.spec.autostart if autostart is None else autostart,
            description=info.spec.description if description is None else description,
        )
        info.spec.validate()
        return info

    def rename(self, name: str, new: str) -> ContainerInfo:
        info = self.get(name)
        renamed = replace(info.spec, name=new)
        renamed.validate()
        if new != name:
            if new in self._cts:
                raise InvalidState(f"a container named {new!r} already exists")
            if info.state == "running":
                raise InvalidState("stop the container to rename it")
            info.spec = renamed
            self._cts[new] = self._cts.pop(name)
        return info

    def delete(self, name: str) -> None:
        self.get(name)
        del self._cts[name]

    def exec(self, name: str, script: str, timeout: int = EXEC_TIMEOUT) -> tuple[int, str]:
        info = self.get(name)
        if info.state != "running":
            raise InvalidState(f"{name} is not running")
        return 0, f"(fake container {name}) {script}"

    def exec_start(self, name: str, script: str) -> str:
        code, out = self.exec(name, script)
        jid = f"job{len(self._jobs) + 1}"
        self._jobs[jid] = {"output": out + "\n", "code": code}
        return jid

    def exec_read(self, name: str, job: str, offset: int = 0) -> dict:
        if job not in self._jobs:
            raise InvalidState("that command has ended and its output is gone")
        out = self._jobs[job]["output"]
        return {"output": out[offset:], "offset": len(out), "done": True,
                "code": self._jobs[job]["code"]}  # fmt: skip

    def exec_stop(self, name: str, job: str) -> None:
        self._jobs.pop(job, None)

    def move(self, name: str, pool: str) -> ContainerInfo:
        info = self.get(name)
        if pool not in self._pools():
            raise ValueError(f"no storage pool named {pool!r}")
        if pool != info.spec.pool and info.state == "running":
            raise InvalidState("stop the container to move it")
        info.spec = replace(info.spec, pool=pool)
        return info

    def snapshot(self, name: str, snap: str) -> ContainerInfo:
        if not SNAP_RE.match(snap):
            raise ValueError("snapshot names use letters, digits and '_.-'")
        info = self.get(name)
        if snap in info.snapshots:
            raise InvalidState(f"{name} already has a snapshot named {snap!r}")
        info.snapshots.append(snap)
        return info

    def restore(self, name: str, snap: str) -> ContainerInfo:
        info = self.get(name)
        if snap not in info.snapshots:
            raise InvalidState(f"{name} has no snapshot {snap!r}")
        return info

    def delete_snapshot(self, name: str, snap: str) -> ContainerInfo:
        info = self.get(name)
        if snap not in info.snapshots:
            raise InvalidState(f"{name} has no snapshot {snap!r}")
        info.snapshots.remove(snap)
        return info


def _fake_ips(spec: ContainerSpec) -> list[str]:
    return [
        c["ip"].split("/")[0] if c["ip"] != "dhcp" else f"10.0.{i}.123"
        for i, c in enumerate(spec.interfaces())
    ]


def info_from_dict(data: dict) -> ContainerInfo:
    names = {f for f in ContainerSpec.__dataclass_fields__ if f != "password"}
    spec = ContainerSpec(**{k: v for k, v in data.items() if k in names})
    extra = {k: data[k] for k in ContainerInfo.__dataclass_fields__ if k != "spec" and k in data}
    return ContainerInfo(spec, **extra)


class RemoteContainers:
    """A node's containers, through its agent."""

    def __init__(self, client) -> None:
        self._client = client

    def _call(self, method: str, path: str = "", timeout: float = 120, **kwargs):
        resp = self._client.request(method, f"/containers{path}", timeout=timeout, **kwargs)
        detail = resp.json().get("detail", "") if resp.content and resp.status_code >= 400 else ""
        if resp.status_code == 404:
            raise ContainerNotFound(str(detail).removeprefix("container not found: "))
        if resp.status_code == 409:
            raise InvalidState(detail or "invalid state")
        if resp.status_code == 422:
            raise ValueError(str(detail))
        if resp.status_code >= 400:
            from ..cluster.remote import NodeUnavailable

            raise NodeUnavailable(f"{self._client.address}: HTTP {resp.status_code} {detail}")
        return resp.json() if resp.content else None

    def list(self) -> list[ContainerInfo]:
        return [info_from_dict(d) for d in self._call("GET")]

    def get(self, name: str) -> ContainerInfo:
        return info_from_dict(self._call("GET", f"/{name}"))

    def create(self, spec: ContainerSpec) -> ContainerInfo:
        return info_from_dict(self._call("POST", json=asdict(spec), timeout=3600))

    def action(self, name: str, act: str) -> ContainerInfo:
        return info_from_dict(self._call("POST", f"/{name}/action", json={"action": act}))

    def update(self, name, vcpus, memory_mb, disk_gb, autostart, description) -> ContainerInfo:
        body = {
            "vcpus": vcpus,
            "memory_mb": memory_mb,
            "disk_gb": disk_gb,
            "autostart": autostart,
            "description": description,
        }
        return info_from_dict(self._call("PATCH", f"/{name}", json=body, timeout=600))

    def rename(self, name: str, new: str) -> ContainerInfo:
        return info_from_dict(self._call("POST", f"/{name}/rename", json={"name": new}))

    def set_network(self, name: str, cards: list[dict], dns: str | None) -> ContainerInfo:
        body = {"nics": cards, "dns": dns}
        return info_from_dict(self._call("PUT", f"/{name}/network", json=body, timeout=300))

    def delete(self, name: str) -> None:
        self._call("DELETE", f"/{name}")

    def exec(self, name: str, script: str, timeout: int = EXEC_TIMEOUT) -> tuple[int, str]:
        data = self._call("POST", f"/{name}/exec", json={"command": script}, timeout=timeout + 30)
        return data["code"], data["output"]

    def exec_start(self, name: str, script: str) -> str:
        return self._call("POST", f"/{name}/jobs", json={"command": script})["job"]

    def exec_read(self, name: str, job: str, offset: int = 0) -> dict:
        return self._call("GET", f"/{name}/jobs/{job}", params={"offset": offset})

    def exec_stop(self, name: str, job: str) -> None:
        self._call("DELETE", f"/{name}/jobs/{job}")

    def move(self, name: str, pool: str) -> ContainerInfo:
        return info_from_dict(self._call("POST", f"/{name}/move", json={"pool": pool},
                                         timeout=None))  # fmt: skip

    def snapshot(self, name: str, snap: str) -> ContainerInfo:
        return info_from_dict(
            self._call("POST", f"/{name}/snapshots", json={"name": snap}, timeout=1800)
        )

    def restore(self, name: str, snap: str) -> ContainerInfo:
        return info_from_dict(self._call("POST", f"/{name}/snapshots/{snap}/restore", timeout=1800))

    def delete_snapshot(self, name: str, snap: str) -> ContainerInfo:
        return info_from_dict(self._call("DELETE", f"/{name}/snapshots/{snap}", timeout=600))


def install_agent_routes(api, get_containers) -> None:
    """The agent's /containers endpoints over this node's containers."""

    @api.get("/containers")
    def ct_list() -> list[dict]:
        return [c.to_dict() for c in get_containers().list()]

    @api.post("/containers")
    def ct_create(body: dict) -> dict:
        names = ContainerSpec.__dataclass_fields__
        return (
            get_containers()
            .create(ContainerSpec(**{k: v for k, v in body.items() if k in names}))
            .to_dict()
        )

    @api.get("/containers/{name}")
    def ct_get(name: str) -> dict:
        return get_containers().get(name).to_dict()

    @api.post("/containers/{name}/action")
    def ct_action(name: str, body: dict) -> dict:
        return get_containers().action(name, str(body.get("action", ""))).to_dict()

    @api.patch("/containers/{name}")
    def ct_update(name: str, body: dict) -> dict:
        keys = ("vcpus", "memory_mb", "disk_gb", "autostart", "description")
        return get_containers().update(name, *(body.get(k) for k in keys)).to_dict()

    @api.put("/containers/{name}/network")
    def ct_network(name: str, body: dict) -> dict:
        return get_containers().set_network(name, list(body.get("nics") or []),
                                            body.get("dns")).to_dict()  # fmt: skip

    @api.post("/containers/{name}/rename")
    def ct_rename(name: str, body: dict) -> dict:
        return get_containers().rename(name, str(body.get("name", ""))).to_dict()

    @api.delete("/containers/{name}", status_code=204)
    def ct_delete(name: str) -> None:
        get_containers().delete(name)

    @api.post("/containers/{name}/exec")
    def ct_exec(name: str, body: dict) -> dict:
        code, out = get_containers().exec(name, str(body.get("command", "")))
        return {"code": code, "output": out}

    @api.post("/containers/{name}/jobs")
    def ct_job_start(name: str, body: dict) -> dict:
        return {"job": get_containers().exec_start(name, str(body.get("command", "")))}

    @api.get("/containers/{name}/jobs/{job}")
    def ct_job_read(name: str, job: str, offset: int = 0) -> dict:
        return get_containers().exec_read(name, job, offset)

    @api.delete("/containers/{name}/jobs/{job}", status_code=204)
    def ct_job_stop(name: str, job: str) -> None:
        get_containers().exec_stop(name, job)

    @api.post("/containers/{name}/move")
    def ct_move(name: str, body: dict) -> dict:
        return get_containers().move(name, str(body.get("pool", ""))).to_dict()

    @api.post("/containers/{name}/snapshots")
    def ct_snapshot(name: str, body: dict) -> dict:
        return get_containers().snapshot(name, str(body.get("name", ""))).to_dict()

    @api.post("/containers/{name}/snapshots/{snap}/restore")
    def ct_restore(name: str, snap: str) -> dict:
        return get_containers().restore(name, snap).to_dict()

    @api.delete("/containers/{name}/snapshots/{snap}")
    def ct_delete_snapshot(name: str, snap: str) -> dict:
        return get_containers().delete_snapshot(name, snap).to_dict()
