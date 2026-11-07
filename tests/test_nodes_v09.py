"""v0.9 nodes: maintenance mode, static routes and interface roles, system disk, metadata."""

import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from test_migration import Env

from ibhics.hypervisor.host import (
    HostNetwork,
    IfaceConfig,
    parse_interfaces,
    render_interfaces,
    system_disk_health,
)
from ibhics.hypervisor.libvirt_driver import disk_source
from ibhics.manager import create_app


def test_maintenance_keeps_vms_off_the_node(tmp_path):
    env = Env(tmp_path)
    client = TestClient(create_app(env.cluster))
    r = client.post("/api/v1/nodes/node2/maintenance", json={"on": True, "evacuate": False})
    assert r.json()["maintenance"] is True
    members = {m["name"]: m for m in client.get("/api/v1/cluster").json()["members"]}
    assert members["node2"]["maintenance"] and not members["node1"]["maintenance"]
    for _ in range(3):  # placement never picks node2
        vm = client.post("/api/v1/vms", json={"name": f"a{_}", "disks": [{"size_gb": 1}]}).json()
        assert vm["node"] != "node2"
    assert client.post("/api/v1/nodes/node2/maintenance", json={"on": False}).json() == {
        "node": "node2",
        "maintenance": False,
        "moves": [],
    }


def test_routes_and_roles_round_trip():
    net = HostNetwork(
        [
            IfaceConfig(
                "br0",
                "bridge",
                "static",
                "10.0.0.11/24",
                "10.0.0.1",
                ports=["eno1"],
                roles=["management", "vm"],
            ),
            IfaceConfig("eno1"),
            IfaceConfig(
                "eno2",
                method="static",
                address="10.9.0.11/24",
                roles=["storage"],
                routes=["10.20.0.0/16 via 10.9.0.1"],
            ),
        ]
    )
    net.validate({"eno1", "eno2"})
    text = render_interfaces(net)
    assert "up ip route add 10.20.0.0/16 via 10.9.0.1 dev eno2" in text
    back = parse_interfaces(text)
    assert back.get("eno2").routes == ["10.20.0.0/16 via 10.9.0.1"]
    assert back.get("eno2").roles == ["storage"] and back.get("br0").roles == ["management", "vm"]
    assert back.get("eno2").options == []
    for bad in (["10.20.0.0/16 via 10.8.0.1"], ["10.20.0.0/16 gw 10.9.0.1"]):
        net.interfaces[2].routes = bad
        with pytest.raises(ValueError):
            net.validate({"eno1", "eno2"})


def test_system_disk_health_from_smartctl():
    import json

    answers = {
        "findmnt": (0, "/dev/mapper/ibhics-root\n"),
        "lsblk": (0, "ibhics-root lvm\nsda3 part\nsda disk\n"),
        "smartctl": (
            0,
            json.dumps(
                {
                    "model_name": "SATADOM",
                    "smart_status": {"passed": True},
                    "temperature": {"current": 41},
                    "power_on_time": {"hours": 480},
                    "ata_smart_attributes": {
                        "table": [{"name": "Wear_Leveling_Count", "value": 93}]
                    },
                }
            ),
        ),
    }
    d = system_disk_health(lambda cmd: answers[cmd[0]])
    assert d["device"] == "/dev/sda" and d["health"] == "ok" and d["wear_percent"] == 7
    assert d["temperature_c"] == 41 and "root_free_gb" in d


def test_disk_metadata_survives_a_hand_edited_device_name():
    xml = """<domain><metadata><ibhics:vm xmlns:ibhics="https://ibhics.org/xmlns/vm">
      <ibhics:disk dev="vda" pool="SSD01" volume="EX-disk0.qcow2"/></ibhics:vm></metadata>
      <devices><disk type="file" device="disk"><driver type="qcow2"/>
      <source file="/var/lib/ibhics/pools/SSD01/EX-disk0.qcow2"/><target dev="sdc" bus="sata"/>
      </disk></devices></domain>"""
    from ibhics.hypervisor.domain_xml import META_NS

    xml = xml.replace("https://ibhics.org/xmlns/vm", META_NS)
    root = ET.fromstring(xml)
    assert disk_source(root, root.find("devices/disk")) == ("SSD01", "EX-disk0.qcow2")


def test_flow_control_round_trip():
    net = HostNetwork(
        [
            IfaceConfig("br0", "bridge", "static", "10.0.0.11/24", ports=["eno1"]),
            IfaceConfig("eno1", flow_control="off"),
        ]
    )
    net.validate({"eno1"})
    text = render_interfaces(net)
    assert "post-up ethtool -A eno1 autoneg off rx off tx off || true" in text
    assert parse_interfaces(text).get("eno1").flow_control == "off"
    net.interfaces[0].flow_control = "on"
    with pytest.raises(ValueError, match="network ports"):
        net.validate({"eno1"})


def test_migration_data_goes_over_the_migration_interface(tmp_path):
    from types import SimpleNamespace

    from ibhics.manager.migrations import MigrationManager

    env = Env(tmp_path)
    mm = MigrationManager(env.cluster)
    net = HostNetwork(
        [
            IfaceConfig("br0", "bridge", "static", "10.0.0.12/24", ports=["eno1"]),
            IfaceConfig("eno2", method="static", address="10.9.0.12/24", roles=["migration"]),
        ]
    )
    member = SimpleNamespace(
        driver=SimpleNamespace(host_system=lambda: SimpleNamespace(network=lambda: net))
    )
    assert mm.migration_address(member) == "10.9.0.12"
    net.interfaces[1].roles = []
    assert mm.migration_address(member) == ""


def test_host_info_counts_sockets_cores_threads():
    """2 × E5-2620 v4: 16 cores, 32 threads; vCPUs are compared with threads."""
    from types import SimpleNamespace

    import ibhics.hypervisor.libvirt_driver as lv

    d = object.__new__(lv.LibvirtDriver)
    d._conn = SimpleNamespace(
        getInfo=lambda: ["x86_64", 193280, 32, 2100, 1, 2, 8, 2],
        getHostname=lambda: "ibhics-test",
        getLibVersion=lambda: 9000000,
    )
    h = d.host_info()
    assert (h.sockets, h.cores, h.threads, h.cpus) == (2, 16, 32, 32)
    js = open("ibhics/manager/static/app.js").read()
    assert "threads ·" in js or "threads`" in js
    assert 'kind: JOB_KINDS[j.kind] || "Backup", icon: "i-backup"' in js  # not "Restore"
