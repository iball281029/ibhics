"""The node itself: Summary, Network (ifupdown config with apply and put-back) and Settings."""

import os
import time
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from ibhics.agent import create_agent_app
from ibhics.cluster import AgentClient, NodeRole
from ibhics.cluster.remote import RemoteDriver
from ibhics.hypervisor import InvalidState
from ibhics.hypervisor.fake import FakeDriver
from ibhics.hypervisor.host import (
    HostNetwork,
    IfaceConfig,
    LinuxHost,
    SettingsEdit,
    affected,
    parse_interfaces,
    render_chrony_conf,
    render_interfaces,
    render_resolv,
)
from ibhics.installer import setup
from ibhics.manager import create_app

SETUP_TEXT = setup.render_interfaces(
    setup.Config(
        role="manager",
        hostname="n1",
        interface="ens2f0",
        address="172.16.10.88/24",
        gateway="172.16.10.1",
        dns=["8.8.8.8"],
        token="x" * 16,
    )
)
PORTS = {"ens2f0", "ens2f1", "ens3f0", "ens3f1"}


def lan(**kw):
    base = dict(kind="bridge", method="static", address="172.16.10.88/24", gateway="172.16.10.1")
    return IfaceConfig("br0", **{**base, "ports": ["ens2f0"], **kw})


def test_reads_what_setup_wrote_and_writes_it_back():
    net = parse_interfaces(SETUP_TEXT)
    assert [(i.name, i.kind, i.method, i.auto) for i in net.interfaces] == [
        ("ens2f0", "ethernet", "manual", False),
        ("br0", "bridge", "static", True),
    ]
    br0 = net.get("br0")
    assert (br0.address, br0.gateway, br0.ports) == ("172.16.10.88/24", "172.16.10.1", ["ens2f0"])
    assert br0.options == ["dns-nameservers 8.8.8.8"]  # kept as it was
    again = render_interfaces(net, "n1")
    assert parse_interfaces(again) == net
    assert "source /etc/network/interfaces.d/*" in again  # VLAN networks stay included
    assert "    bridge_ports ens2f0\n    bridge_stp off\n    bridge_fd 0" in again


def test_netmask_and_bond_are_understood():
    net = parse_interfaces(
        "auto bond0\niface bond0 inet static\n address 10.1.1.5\n netmask 255.255.255.0\n"
        " bond-slaves ens3f0 ens3f1\n bond-mode 802.3ad\n bond-miimon 100\n"
    )
    bond = net.get("bond0")
    assert (bond.kind, bond.address, bond.ports, bond.bond_mode) == (
        "bond",
        "10.1.1.5/24",
        ["ens3f0", "ens3f1"],
        "802.3ad",
    )
    text = render_interfaces(
        HostNetwork([bond, IfaceConfig("ens3f0"), IfaceConfig("ens3f1"), lan()])
    )
    assert "iface ens3f0 inet manual\n    bond-master bond0" in text
    assert text.index("iface ens3f0") < text.index("iface bond0") < text.index("iface br0")


@pytest.mark.parametrize(
    "interfaces, message",
    [
        ([lan(), IfaceConfig("eth9")], "no network port named 'eth9'"),
        ([lan(gateway="10.0.0.1")], "gateway must be inside"),
        ([lan(address="172.16.10.88")], "prefix"),
        ([lan(method="manual", address="", gateway="")], "at least one interface needs an address"),
        ([lan(), IfaceConfig("br1", kind="bridge", ports=["ens2f0"])], "already a port of br0"),
        (
            [lan(), IfaceConfig("ens2f0", method="static", address="10.0.0.2/24")],
            "address goes on br0",
        ),
        ([IfaceConfig("brx", kind="bridge", method="dhcp")], "br0 carries the VM networks"),
        ([lan(), IfaceConfig("bond0", kind="bond", ports=["ens3f0"])], "bond mode"),
        ([lan(mtu=100)], "MTU"),
        (
            [
                lan(),
                IfaceConfig("ens3f0", method="static", address="10.9.0.2/24", gateway="10.9.0.1"),
            ],
            "only one default gateway",
        ),
        ([lan(), IfaceConfig("ibr20", kind="bridge")], "name a bridge"),
    ],
)
def test_network_validation(interfaces, message):
    with pytest.raises(ValueError, match=message):
        HostNetwork(interfaces).validate(PORTS)


def test_compute_node_keeps_the_address_the_cluster_uses():
    net = HostNetwork([lan(address="172.16.10.90/24")])
    with pytest.raises(ValueError, match="cluster reaches this node at 172.16.10.88"):
        net.validate(PORTS, keep_address="172.16.10.88")
    HostNetwork([lan()]).validate(PORTS, keep_address="172.16.10.88")


def test_affected_includes_the_bridge_over_a_changed_port():
    old = HostNetwork([IfaceConfig("ens2f0"), lan()])
    new = HostNetwork([IfaceConfig("ens2f0", mtu=9000), lan()])
    assert affected(old, new) == {"ens2f0", "br0"}
    assert affected(old, old) == set()


def fake_root(tmp_path):
    """Enough of /proc, /sys and /etc for LinuxHost."""

    def put(path, text):
        p = tmp_path / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    cpu = "processor\t: {n}\nmodel name\t: AMD EPYC  7313 16-Core Processor\nphysical id\t: {s}\n"
    cpu += "core id\t: {c}\nflags\t: fpu svm lm\n"
    put(
        "/proc/cpuinfo",
        "\n".join(cpu.format(n=n, s=n // 4, c=(n % 4) // 2) for n in range(8)),
    )
    put("/proc/meminfo", "MemTotal:       8388608 kB\nMemAvailable:   6291456 kB\n")
    put("/proc/loadavg", "0.50 0.40 0.30 1/200 1234\n")
    put("/proc/uptime", "3600.5 7000.0\n")
    put("/proc/stat", "cpu  100 0 100 800 0 0 0 0 0 0\n")
    put("/proc/sys/kernel/osrelease", "6.12.48-amd64\n")
    put("/etc/os-release", 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    put("/etc/hostname", "n1\n")
    put("/etc/timezone", "Asia/Bangkok\n")
    put("/sys/class/dmi/id/sys_vendor", "Lenovo\n")
    put("/sys/class/dmi/id/product_name", "ThinkSystem SR645\n")
    put("/etc/network/interfaces", SETUP_TEXT)
    net = tmp_path / "sys/class/net"
    for name, carrier in (("ens2f0", "1"), ("ens2f1", "0")):
        put(f"/sys/class/net/{name}/address", "08:c0:eb:00:00:01\n")
        put(f"/sys/class/net/{name}/carrier", carrier + "\n")
        put(f"/sys/class/net/{name}/operstate", "up\n" if carrier == "1" else "down\n")
        put(f"/sys/class/net/{name}/speed", "10000\n" if carrier == "1" else "-1\n")
        put(f"/sys/class/net/{name}/mtu", "1500\n")
        os.symlink(tmp_path / "devices/0000:41:00.0", net / name / "device")
    (tmp_path / "drivers/ice").mkdir(parents=True)
    (tmp_path / "devices/0000:41:00.0").mkdir(parents=True)
    os.symlink(tmp_path / "drivers/ice", tmp_path / "devices/0000:41:00.0/driver")
    put("/sys/class/net/br0/bridge/stp_state", "0\n")
    (net / "br0/brif/ens2f0").mkdir(parents=True)
    (net / "br0/brif/vnet0").mkdir(parents=True)
    os.symlink(net / "br0", net / "ens2f0/master")
    put("/sys/class/net/br0/mtu", "1500\n")
    put("/sys/class/net/vnet0/mtu", "1500\n")  # a VM port: not listed
    return tmp_path


class Recorder:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = fail

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[0] == "ip":
            return '[{"ifname": "br0", "addr_info": [{"local": "172.16.10.88", "prefixlen": 24}]}]'
        if cmd[0] == "lspci":
            return "41:00.0 Ethernet controller: Intel Corporation Ethernet Controller E810-C\n"
        if cmd[0] == "timedatectl":
            return "yes\n"
        if any(f in cmd for f in self.fail):
            raise RuntimeError(f"{cmd[0]} failed")
        return ""


def test_linux_summary_and_ports(tmp_path):
    host = LinuxHost(fake_root(tmp_path), Recorder(), sample_seconds=0)
    s = host.summary()
    assert (s.hostname, s.vendor, s.model) == ("n1", "Lenovo", "ThinkSystem SR645")
    assert (s.cpu_model, s.sockets, s.cores, s.threads) == (
        "AMD EPYC 7313 16-Core Processor",
        2,
        4,
        8,
    )
    assert (s.virtualization, s.memory_mb, s.memory_used_mb) == ("AMD-V", 8192, 2048)
    assert (s.load, s.uptime_s, s.timezone) == ([0.5, 0.4, 0.3], 3600, "Asia/Bangkok")
    nics = {n.name: n for n in host.nics()}
    assert set(nics) == {"ens2f0", "ens2f1", "br0"}
    assert (nics["ens2f0"].link, nics["ens2f0"].speed_mbps, nics["ens2f0"].master) == (
        True,
        10000,
        "br0",
    )
    assert (nics["ens2f1"].link, nics["ens2f1"].speed_mbps) == (False, None)
    assert "E810-C" in nics["ens2f0"].model and nics["ens2f0"].driver == "ice"
    assert nics["br0"].members == ["ens2f0"]  # VM ports left out
    assert nics["br0"].addresses == ["172.16.10.88/24"]


def test_apply_takes_down_old_and_brings_up_new_then_puts_back(tmp_path):
    root = fake_root(tmp_path)
    (root / "etc/network/interfaces.d").mkdir(parents=True)
    (root / "etc/network/interfaces.d/ibhics-vlan20").write_text("auto ibr20\n")
    run, reattached = Recorder(), []
    host = LinuxHost(root, run, reattach=lambda: reattached.append(1), sample_seconds=0)
    net = host.network()
    net.get("br0").ports = ["ens2f0", "ens2f1"]
    net.interfaces.append(IfaceConfig("ens2f1", auto=False))
    state = host.apply_network(net, "n1")
    assert state.pending and 0 < state.seconds_left <= 120
    ifcmds = [c for c in run.calls if c[0] in ("ifdown", "ifup")]
    assert ifcmds[0][:2] == ["ifdown", "--force"] and ifcmds[0][-1] == "br0"
    assert ["ifup", "br0"] in ifcmds
    assert ["ifup", "ibr20"] in ifcmds  # VLAN networks follow br0
    assert reattached == [1]
    assert "bridge_ports ens2f0 ens2f1" in (root / "etc/network/interfaces").read_text()
    with pytest.raises(InvalidState, match="confirm or undo"):
        host.apply_network(net, "n1")

    host.undo_network()
    assert parse_interfaces((root / "etc/network/interfaces").read_text()) == parse_interfaces(
        SETUP_TEXT
    )
    assert "put back" in host.network_state().message
    assert not host.network_state().pending


def test_unconfirmed_change_is_put_back_by_itself(tmp_path):
    root = fake_root(tmp_path)
    host = LinuxHost(root, Recorder(), sample_seconds=0)
    host.confirm_seconds = 0.2
    net = host.network()
    net.get("br0").mtu = 9000
    host.apply_network(net)
    time.sleep(0.6)
    state = host.network_state()
    assert not state.pending and "not confirmed" in state.message
    assert "mtu" not in (root / "etc/network/interfaces").read_text()

    host.apply_network(net)
    host.confirm_network()
    time.sleep(0.4)
    assert "mtu 9000" in (root / "etc/network/interfaces").read_text()


def test_failed_ifup_puts_the_old_config_back(tmp_path):
    root = fake_root(tmp_path)
    host = LinuxHost(root, Recorder(fail={"br0"}), sample_seconds=0)
    net = host.network()
    net.get("br0").mtu = 9000
    with pytest.raises(InvalidState, match="did not apply and was put back"):
        host.apply_network(net)
    assert "mtu" not in (root / "etc/network/interfaces").read_text()
    assert not host.network_state().pending


def test_settings_files(tmp_path):
    root = fake_root(tmp_path)
    (root / "etc/resolv.conf").write_text("search lab.local\nnameserver 1.1.1.1\n")
    (root / "etc/chrony").mkdir()
    (root / "etc/chrony/chrony.conf").write_text(
        "pool 2.debian.pool.ntp.org iburst\nsourcedir /etc/chrony/sources.d\n"
    )
    (root / "usr/share/zoneinfo").mkdir(parents=True)
    (root / "usr/share/zoneinfo/zone1970.tab").write_text(
        "#c\tcoord\tTZ\nTH\t+1345+10031\tAsia/Bangkok\nGB\t+513030-0000731\tEurope/London\n"
    )
    run = Recorder()
    host = LinuxHost(root, run, sample_seconds=0)
    s = host.settings()
    assert (s.dns, s.ntp, s.ntp_synced) == (["1.1.1.1"], ["2.debian.pool.ntp.org"], True)
    assert host.timezones() == ["Asia/Bangkok", "Europe/London", "UTC"]

    s = host.update_settings(
        SettingsEdit(timezone="Europe/London", dns=["10.0.0.1", "10.0.0.2"], ntp=["ntp.lab.local"])
    )
    assert ["timedatectl", "set-timezone", "Europe/London"] in run.calls
    assert (s.dns, s.ntp) == (["10.0.0.1", "10.0.0.2"], ["ntp.lab.local"])
    assert "search lab.local" in (root / "etc/resolv.conf").read_text()
    assert (root / "etc/chrony/chrony.conf").read_text().startswith("#ibhics# pool")
    host.update_settings(SettingsEdit(ntp=[]))
    assert host.settings().ntp == ["2.debian.pool.ntp.org"]  # back to Debian's pool
    with pytest.raises(ValueError, match="time zone"):
        host.update_settings(SettingsEdit(timezone="Mars/Olympus"))
    with pytest.raises(ValueError, match="DNS server"):
        host.update_settings(SettingsEdit(dns=["dns.example"]))


def test_settings_renderers():
    assert render_resolv("search a\nnameserver 9.9.9.9\noptions edns0\n", ["1.1.1.1"]).splitlines()[
        1:
    ] == ["search a", "options edns0", "nameserver 1.1.1.1"]
    conf = "pool x iburst\nserver y\nmakestep 1 3\n"
    off = render_chrony_conf(conf, True)
    assert off == "#ibhics# pool x iburst\n#ibhics# server y\nmakestep 1 3\n"
    assert render_chrony_conf(off, False) == conf


def test_node_pages_through_the_manager():
    client = TestClient(create_app(FakeDriver()))
    node = client.get("/api/v1/cluster").json()["members"][0]["name"]
    base = f"/api/v1/nodes/{node}"
    summary = client.get(f"{base}/summary").json()
    assert summary["model"] == "ThinkSystem SR645" and summary["threads"] == 64
    nics = client.get(f"{base}/nics").json()
    assert [n["name"] for n in nics if n["kind"] == "ethernet"] == [
        "ens2f0",
        "ens2f1",
        "ens3f0",
        "ens3f1",
    ]
    net = client.get(f"{base}/network").json()
    br0 = next(i for i in net["interfaces"] if i["name"] == "br0")
    br0["mtu"] = 9000
    resp = client.put(f"{base}/network", json=net)
    assert resp.status_code == 200 and resp.json()["pending"], resp.text
    assert client.put(f"{base}/network", json=net).status_code == 409
    assert client.post(f"{base}/network/confirm").json()["message"] == "confirmed"
    br0["method"] = "manual"
    br0["address"] = br0["gateway"] = ""
    assert client.put(f"{base}/network", json=net).status_code == 422

    tz = client.get(f"{base}/timezones").json()
    assert "Asia/Bangkok" in tz
    s = client.put(f"{base}/settings", json={"timezone": "UTC", "dns": ["1.1.1.1"]}).json()
    assert (s["timezone"], s["dns"]) == ("UTC", ["1.1.1.1"])

    vm = client.post("/api/v1/vms", json={"name": "a", "disks": [{"size_gb": 1}]}).json()
    client.post(f"/api/v1/vms/{vm['uuid']}/start")
    resp = client.post(f"{base}/power", json={"action": "reboot"})
    assert resp.status_code == 409 and "still run" in resp.json()["detail"]
    assert client.post(f"{base}/power", json={"action": "reboot", "force": True}).status_code == 202


def test_node_pages_through_an_agent():
    driver = FakeDriver()
    agent = TestClient(create_agent_app("node2", NodeRole.COMPUTE, "t" * 16, driver))
    remote = RemoteDriver(AgentClient("http://node2", "t" * 16, http=agent)).host_system()
    assert remote.summary().model == "ThinkSystem SR645"
    assert {n.name for n in remote.nics()} >= {"ens2f0", "br0"}
    net = remote.network()
    net.get("br0").mtu = 9000
    assert remote.apply_network(net).pending
    with pytest.raises(InvalidState):
        remote.apply_network(net)
    assert remote.undo_network().message.startswith("The network change was put back")
    net.get("br0").address = "bad"
    with pytest.raises(ValueError, match="prefix"):
        remote.apply_network(net)
    assert remote.update_settings(SettingsEdit(dns=["9.9.9.9"])).dns == ["9.9.9.9"]
    remote.power("shutdown")
    assert driver.host_system().powered == ["shutdown"]
    assert asdict(remote.network_state())["pending"] is False
