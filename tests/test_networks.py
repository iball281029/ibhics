"""VLAN networks for VMs, and mapping a source VM's networks on import."""

from fastapi.testclient import TestClient

from ibhics.hypervisor.fake import FakeDriver
from ibhics.hypervisor.net_xml import build_network_xml, render_vlan_iface, vlan_of
from ibhics.importer.kvm import parse_qm_config
from ibhics.importer.sources import parse_vmware_xml
from ibhics.manager import create_app


def test_vlan_network_lifecycle():
    client = TestClient(create_app(FakeDriver()))
    assert [n["name"] for n in client.get("/api/v1/networks").json()] == ["default"]
    resp = client.post("/api/v1/networks", json={"name": "service", "vlan": 101})
    assert resp.status_code == 201, resp.text
    nets = client.get("/api/v1/networks").json()
    assert [(n["name"], n["vlan"], n["bridge"]) for n in nets] == [
        ("default", None, "br0"),
        ("service", 101, "ibr101"),
    ]
    assert client.post("/api/v1/networks", json={"name": "x", "vlan": 101}).status_code == 409
    assert client.post("/api/v1/networks", json={"name": "y", "vlan": 5000}).status_code == 422

    vm = client.post(
        "/api/v1/vms",
        json={"name": "z", "disks": [{"size_gb": 1}], "nics": [{"network": "service"}]},
    ).json()
    assert client.delete("/api/v1/networks/service").status_code == 409  # still in use
    client.delete(f"/api/v1/vms/{vm['uuid']}")
    assert client.delete("/api/v1/networks/service").status_code == 204
    assert client.delete("/api/v1/networks/default").status_code == 409


def test_vlan_interface_and_network_xml():
    stanza = render_vlan_iface(101)
    assert "auto ibr101" in stanza
    assert "ip link add link br0 name br0.101 type vlan id 101" in stanza
    assert "ip link set br0.101 master ibr101" in stanza
    assert '<forward mode="bridge" />' in build_network_xml("service", "ibr101")
    assert (vlan_of("ibr101"), vlan_of("br0")) == (101, None)


def test_source_networks_are_read():
    vm = parse_vmware_xml(
        "<domain><name>zabbix</name><memory unit='KiB'>8388608</memory><vcpu>4</vcpu><devices>"
        "<interface type='bridge'><source bridge='Service Network'/></interface>"
        "<interface type='bridge'><source bridge='VM Network'/></interface></devices></domain>"
    )
    assert vm.networks == ["Service Network", "VM Network"]
    qm, _volumes = parse_qm_config(
        "100",
        "name: web\nnet1: virtio=BC:24:11:00:00:02,bridge=vmbr1\n"
        "net0: virtio=BC:24:11:00:00:01,bridge=vmbr0,tag=20\n",
    )
    assert qm.networks == ["vmbr0 VLAN 20", "vmbr1"]
