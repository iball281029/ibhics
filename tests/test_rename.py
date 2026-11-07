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
    r = client.post(f"{API}/vms/{b['uuid']}/rename", json={"name": "ฐานข้อมูล"})
    assert r.status_code == 200 and r.json()["state"] == "running"
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

    def setMetadata(self, _type, xml, prefix, uri, flags):
        root = ET.fromstring(self.xml)
        meta = root.find("metadata")
        if meta is None:
            meta = ET.SubElement(root, "metadata")
        for el in list(meta):
            if el.tag.startswith(f"{{{uri}}}"):
                meta.remove(el)
        if xml:
            el = ET.fromstring(xml)
            el.tag = f"{{{uri}}}{el.tag}"
            meta.append(el)
        self.xml = ET.tostring(root, encoding="unicode")
        self.meta_flags = flags

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
        VIR_DOMAIN_AFFECT_CONFIG=2,
        VIR_DOMAIN_AFFECT_LIVE=1,
        VIR_DOMAIN_METADATA_ELEMENT=2,
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
    # Running: the domain keeps its name, the new one is shown (metadata, live + config).
    dom.active = True
    assert d.rename_vm("u", "ซี").spec.name == "ซี"
    assert dom.name() == "บี" and dom.meta_flags == 3
    # Off again: the domain itself is renamed and the shown name cleared.
    dom.active = False
    assert d.rename_vm("u", "dee").spec.name == "dee" and dom.name() == "dee"
    assert "ซี" not in dom.xml


def test_sysprep_windows_through_api():
    client = TestClient(create_app(FakeDriver()))
    win = client.post(
        f"{API}/vms",
        json={"name": "w22", "os_type": "Windows Server 2022", "disks": [{"size_gb": 1}]},
    ).json()
    lin = make_vm(client, "lin")
    assert client.post(f"{API}/vms/{win['uuid']}/sysprep").status_code == 409  # off
    client.post(f"{API}/vms/{win['uuid']}/start")
    assert client.post(f"{API}/vms/{win['uuid']}/sysprep").status_code == 202
    assert client.get(f"{API}/vms/{win['uuid']}").json()["state"] == "stopped"
    client.post(f"{API}/vms/{lin['uuid']}/start")
    assert client.post(f"{API}/vms/{lin['uuid']}/sysprep").status_code == 409


def test_restart_applies_pending_changes():
    """Restart after editing memory powers the VM off and on, so the change applies."""
    client = TestClient(create_app(FakeDriver()))
    vm = make_vm(client, "ex2022")
    client.post(f"{API}/vms/{vm['uuid']}/start")
    edited = client.patch(f"{API}/vms/{vm['uuid']}", json={"memory_mb": 32768}).json()
    assert edited["restart_needed"]
    r = client.post(f"{API}/vms/{vm['uuid']}/restart").json()
    assert not r["restart_needed"] and r["state"] == "running" and r["memory_mb"] == 32768
    assert "restart" in [j["kind"] for j in client.get(f"{API}/backup/jobs").json()]


def test_libvirt_restart_with_pending_changes_power_cycles(monkeypatch):
    calls = []
    d = object.__new__(lv.LibvirtDriver)
    dom = types.SimpleNamespace(isActive=lambda: True, destroy=lambda: calls.append("destroy"),
                                reboot=lambda _f: calls.append("reboot"))  # fmt: skip
    d._lookup = lambda _u: dom
    pending = {"on": True}
    d._info = lambda _d: types.SimpleNamespace(restart_needed=pending["on"])

    def off(_dom, timeout):
        calls.append("shutdown")
        raise InvalidState("guest ignored it")

    d._power_off = off
    d.start_vm = lambda _u: calls.append("start") or "started"
    assert d.restart_vm("u") == "started"
    assert calls == ["shutdown", "destroy", "start"]
    calls.clear()
    pending["on"] = False
    d.restart_vm("u")
    assert calls == ["reboot"]  # nothing pending: a plain guest reboot
