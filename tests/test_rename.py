"""v1.1: rename a VM (Thai names too); snapshots and backup policies follow."""

import types
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from test_snapshots import Dom, LibvirtError, Snap

import ibhics.hypervisor.libvirt_driver as lv
from ibhics.hypervisor import DiskSpec, InvalidState, VMExists, VMSpec
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app

API = "/api/v1"


def make_vm(client, name):
    return client.post(f"{API}/vms", json={"name": name, "disks": [{"size_gb": 1}]}).json()


def test_rename_via_api_thai_and_rules():
    client = TestClient(create_app(FakeDriver()))
    a, b = make_vm(client, "web01"), make_vm(client, "db01")
    r = client.post(f"{API}/vms/{a['uuid']}/rename", json={"name": "เว็บ-01"})
    assert r.status_code == 200 and r.json()["name"] == "เว็บ-01"
    names = {v["name"] for v in client.get(f"{API}/vms").json()}
    assert names == {"เว็บ-01", "db01"}
    assert client.post(f"{API}/vms/{a['uuid']}/rename", json={"name": "db01"}).status_code == 409
    assert client.post(f"{API}/vms/{a['uuid']}/rename", json={"name": "a b"}).status_code == 422
    client.post(f"{API}/vms/{b['uuid']}/start")
    r = client.post(f"{API}/vms/{b['uuid']}/rename", json={"name": "db02"})
    assert r.status_code == 409 and "shut" in r.json()["detail"]
    kinds = [j["kind"] for j in client.get(f"{API}/backup/jobs").json()]
    assert "rename" in kinds


def test_policies_follow_the_new_name():
    from ibhics.manager.backups import BackupManager, Policy

    m = BackupManager(FakeDriver())
    m._policies["p"] = Policy(name="p", target="t", vms=["old", "other"])
    m.rename_vm("old", "ใหม่")
    assert m._policies["p"].vms == ["ใหม่", "other"]


def test_rename_through_node_agents(tmp_path):
    from test_migration import Env

    client = TestClient(create_app(Env(tmp_path).cluster))
    vm = make_vm(client, "app1")
    r = client.post(f"{API}/vms/{vm['uuid']}/rename", json={"name": "แอป1"})
    assert r.status_code == 200 and r.json()["name"] == "แอป1"


class RenDom(Dom):
    def __init__(self, spec):
        super().__init__(spec, active=False)
        self.renamed = None

    def name(self):
        return ET.fromstring(self.xml).findtext("name")

    def rename(self, new, _f):
        assert not [s for s in self.snaps if not s.deleted], "libvirt refuses with snapshots"
        root = ET.fromstring(self.xml)
        root.find("name").text = new
        self.xml = ET.tostring(root, encoding="unicode")
        self.snaps = []

    def hasCurrentSnapshot(self, _f):
        return 1 if self.snaps else 0

    def snapshotCurrent(self, _f):
        return self.snaps[-1]

    def snapshotCreateXML(self, xml, flags):
        self.snaps.append(Snap(xml))
        self.snaps[-1].flags = flags
        return self.snaps[-1]


def test_libvirt_rename_keeps_snapshots(monkeypatch):
    lib = types.SimpleNamespace(
        libvirtError=LibvirtError,
        VIR_DOMAIN_XML_INACTIVE=2,
        VIR_DOMAIN_SNAPSHOT_DELETE_METADATA_ONLY=2,
        VIR_DOMAIN_SNAPSHOT_CREATE_REDEFINE=1,
        VIR_DOMAIN_SNAPSHOT_CREATE_CURRENT=2,
    )
    monkeypatch.setattr(lv, "libvirt", lib)
    d = object.__new__(lv.LibvirtDriver)
    dom = RenDom(VMSpec(name="a", disks=[DiskSpec(size_gb=1)]))
    dom.snaps = [Snap(f"<domainsnapshot><name>s1</name>{dom.xml}</domainsnapshot>")]
    d._lookup = lambda _u: dom
    d._info = lambda _dom: types.SimpleNamespace(spec=lv._spec_from_xml(dom.xml))

    def lookup(name):
        if name == "taken":
            return object()
        raise LibvirtError(name)

    d._conn = types.SimpleNamespace(lookupByName=lookup)
    assert d.rename_vm("u", "บี").spec.name == "บี"
    (snap,) = dom.snaps
    assert snap.getName() == "s1" and snap.flags == 3  # redefined, and still current
    assert ET.fromstring(snap.xml).findtext("domain/name") == "บี"
    with pytest.raises(VMExists):
        d.rename_vm("u", "taken")
    dom.active = True
    with pytest.raises(InvalidState):
        d.rename_vm("u", "c")
