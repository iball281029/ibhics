import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from ibhics.hypervisor import PoolSpec, PoolType
from ibhics.hypervisor.fake import FakeDriver
from ibhics.hypervisor.pool_xml import (
    build_iscsi_session_xml,
    build_lvm_pool_xml,
    build_pool_xml,
    describe_pool_xml,
    vg_name,
)
from ibhics.manager import create_app

IQN = "iqn.2004-04.com.example:storage.lun1"


# Validation


@pytest.mark.parametrize(
    "spec",
    [
        PoolSpec(name="local", type=PoolType.DIR),
        PoolSpec(name="local2", type=PoolType.DIR, path="/data/vms"),
        PoolSpec(name="nas", type=PoolType.NFS, host="10.0.0.50", export="/vol/vms"),
        PoolSpec(
            name="san", type=PoolType.ISCSI, host="10.0.0.60", iqn=IQN, lun=1, confirm_wipe=True
        ),
        PoolSpec(name="fc", type=PoolType.LVM, device="/dev/mapper/mpatha", confirm_wipe=True),
    ],
)
def test_valid_pool_specs(spec):
    spec.validate()


@pytest.mark.parametrize(
    "spec",
    [
        PoolSpec(name="bad name", type=PoolType.DIR),
        PoolSpec(name="rel", type=PoolType.DIR, path="data/vms"),
        PoolSpec(name="nas", type=PoolType.NFS, host="10.0.0.50"),
        PoolSpec(name="nas", type=PoolType.NFS, host="10.0.0.50", export="vol"),
        PoolSpec(name="san", type=PoolType.ISCSI, host="10.0.0.60", iqn="bogus", confirm_wipe=True),
        PoolSpec(name="san", type=PoolType.ISCSI, host="10.0.0.60", iqn=IQN),  # no confirm_wipe
        PoolSpec(name="fc", type=PoolType.LVM, device="/dev/mapper/mpatha"),  # no confirm_wipe
        PoolSpec(name="fc", type=PoolType.LVM, device="/etc/passwd", confirm_wipe=True),
        PoolSpec(name="fc", type=PoolType.LVM, device="/dev/sdb; rm -rf /", confirm_wipe=True),
    ],
)
def test_invalid_pool_specs(spec):
    with pytest.raises(ValueError):
        spec.validate()


# libvirt XML


def test_dir_pool_xml():
    root = ET.fromstring(build_pool_xml(PoolSpec(name="local", type=PoolType.DIR)))
    assert root.get("type") == "dir"
    assert root.findtext("target/path") == "/var/lib/ibhics/pools/local"


def test_nfs_pool_xml():
    spec = PoolSpec(name="nas", type=PoolType.NFS, host="10.0.0.50", export="/vol/vms")
    root = ET.fromstring(build_pool_xml(spec))
    assert root.get("type") == "netfs"
    assert root.find("source/host").get("name") == "10.0.0.50"
    assert root.find("source/dir").get("path") == "/vol/vms"
    assert root.find("source/format").get("type") == "nfs"
    assert root.findtext("target/path") == "/mnt/ibhics/nas"


def test_iscsi_session_and_lvm_xml():
    spec = PoolSpec(name="san-a", type=PoolType.ISCSI, host="10.0.0.60", iqn=IQN, port=3261)
    session = ET.fromstring(build_iscsi_session_xml(spec))
    assert session.get("type") == "iscsi"
    assert session.findtext("name") == "san-a-iscsi"
    assert session.find("source/host").attrib == {"name": "10.0.0.60", "port": "3261"}
    assert session.find("source/device").get("path") == IQN
    assert session.findtext("target/path") == "/dev/disk/by-path"

    lun = "/dev/disk/by-path/ip-10.0.0.60:3261-iscsi-" + IQN + "-lun-1"
    lvm = ET.fromstring(build_lvm_pool_xml("san-a", lun))
    assert lvm.get("type") == "logical"
    assert lvm.find("source/device").get("path") == lun
    assert lvm.findtext("source/name") == "ibhics_san_a"
    assert lvm.findtext("target/path") == "/dev/ibhics_san_a"


def test_vg_names_are_lvm_safe():
    assert vg_name("fc.prod-1") == "ibhics_fc_prod_1"


def test_describe_pools():
    sessions = {"san-iscsi": "10.0.0.60:3260 " + IQN}
    dir_xml = build_pool_xml(PoolSpec(name="local", type=PoolType.DIR))
    assert describe_pool_xml(dir_xml, sessions) == (PoolType.DIR, "/var/lib/ibhics/pools/local")

    nfs = PoolSpec(name="nas", type=PoolType.NFS, host="nas1", export="/vms")
    assert describe_pool_xml(build_pool_xml(nfs), sessions) == (PoolType.NFS, "nas1:/vms")

    fc_xml = build_lvm_pool_xml("fc", "/dev/mapper/mpatha")
    assert describe_pool_xml(fc_xml, sessions) == (PoolType.LVM, "/dev/mapper/mpatha")

    kind, source = describe_pool_xml(build_lvm_pool_xml("san", "/dev/disk/by-path/x"), sessions)
    assert kind == PoolType.ISCSI and IQN in source

    session_xml = build_iscsi_session_xml(
        PoolSpec(name="san", type=PoolType.ISCSI, host="10.0.0.60", iqn=IQN)
    )
    assert describe_pool_xml(session_xml, sessions) is None  # internal, hidden


# API


@pytest.fixture
def client():
    return TestClient(create_app(FakeDriver()))


def node_name(client):
    return client.get("/api/v1/cluster").json()["members"][0]["name"]


def test_default_pool_listed(client):
    pools = client.get("/api/v1/pools").json()
    assert [(p["name"], p["type"]) for p in pools] == [("default", "dir")]
    assert pools[0]["node"] == node_name(client)


def test_create_nfs_pool_and_place_vm(client):
    node = node_name(client)
    resp = client.post(
        f"/api/v1/nodes/{node}/pools",
        json={"name": "nas", "type": "nfs", "host": "10.0.0.50", "export": "/vol/vms"},
    )
    assert resp.status_code == 201
    assert resp.json()["source"] == "10.0.0.50:/vol/vms"

    vm = client.post(
        "/api/v1/vms", json={"name": "web", "disks": [{"size_gb": 50, "pool": "nas"}]}
    ).json()
    assert vm["disks"] == [{"size_gb": 50, "backend": "file", "pool": "nas"}]
    nas = next(p for p in client.get("/api/v1/pools").json() if p["name"] == "nas")
    assert nas["allocation_gb"] == 50


def test_lvm_pool_needs_confirm_wipe(client):
    node = node_name(client)
    body = {"name": "fc", "type": "lvm", "device": "/dev/mapper/mpatha"}
    assert client.post(f"/api/v1/nodes/{node}/pools", json=body).status_code == 422
    body["confirm_wipe"] = True
    assert client.post(f"/api/v1/nodes/{node}/pools", json=body).status_code == 201

    vm = client.post(
        "/api/v1/vms", json={"name": "db", "disks": [{"size_gb": 100, "pool": "fc"}]}
    ).json()
    assert vm["disks"][0]["backend"] == "block"


def test_iscsi_pool(client):
    node = node_name(client)
    body = {"name": "san", "type": "iscsi", "host": "10.0.0.60", "iqn": IQN, "lun": 1}
    body["confirm_wipe"] = True
    resp = client.post(f"/api/v1/nodes/{node}/pools", json=body)
    assert resp.status_code == 201
    assert resp.json()["type"] == "iscsi"


def test_pool_in_use_cannot_be_deleted(client):
    node = node_name(client)
    client.post(f"/api/v1/nodes/{node}/pools", json={"name": "extra", "type": "dir"})
    uuid = client.post(
        "/api/v1/vms", json={"name": "v", "disks": [{"size_gb": 10, "pool": "extra"}]}
    ).json()["uuid"]
    assert client.delete(f"/api/v1/nodes/{node}/pools/extra").status_code == 409
    client.delete(f"/api/v1/vms/{uuid}")
    assert client.delete(f"/api/v1/nodes/{node}/pools/extra").status_code == 204


def test_missing_pool(client):
    node = node_name(client)
    assert client.delete(f"/api/v1/nodes/{node}/pools/nope").status_code == 404
    resp = client.post("/api/v1/vms", json={"name": "v", "disks": [{"size_gb": 10, "pool": "x"}]})
    assert resp.status_code == 404


def test_duplicate_pool(client):
    node = node_name(client)
    assert client.post(f"/api/v1/nodes/{node}/pools", json={"name": "default", "type": "dir"})
    resp = client.post(f"/api/v1/nodes/{node}/pools", json={"name": "default", "type": "dir"})
    assert resp.status_code == 409
