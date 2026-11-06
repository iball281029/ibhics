"""VM snapshots: take, revert, delete; through the manager to a remote node."""

import types
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from test_migration import Env, Inline

from ibhics.hypervisor import DiskSpec, InvalidState, PoolSpec, PoolType, VMSpec
from ibhics.hypervisor import libvirt_driver as lv
from ibhics.hypervisor.base import SnapshotNotFound
from ibhics.hypervisor.domain_xml import build_domain_xml
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.deploys import DeployManager


@pytest.fixture
def fake():
    d = FakeDriver(demo_console=False)
    vm = d.create_vm(VMSpec(name="app", disks=[DiskSpec(size_gb=1)]))
    return d, vm.uuid


def test_revert_brings_disks_and_settings_back(fake):
    d, uuid = fake
    d.write_disk(uuid, 0, 0, b"before")
    d.create_snapshot(uuid, "before-upgrade", "clean install")
    d.write_disk(uuid, 0, 0, b"broken")
    d.update_vm(uuid, __import__("ibhics.hypervisor", fromlist=["VMEdit"]).VMEdit(vcpus=4))
    vm = d.revert_snapshot(uuid, "before-upgrade")
    assert vm.spec.vcpus == 1 and d.read_disk(uuid, 0).startswith(b"before")


def test_snapshot_tree_and_current(fake):
    d, uuid = fake
    d.create_snapshot(uuid, "s1")
    d.start_vm(uuid)
    s2 = d.create_snapshot(uuid, "s2")
    assert (s2.parent, s2.vm_state, s2.current) == ("s1", "running", True)
    d.stop_vm(uuid, force=True)
    assert d.revert_snapshot(uuid, "s2").state == "running"  # memory included
    assert [s.current for s in d.list_snapshots(uuid)] == [False, True]
    d.delete_snapshot(uuid, "s1")
    assert [(s.name, s.parent) for s in d.list_snapshots(uuid)] == [("s2", "")]
    with pytest.raises(SnapshotNotFound):
        d.delete_snapshot(uuid, "s1")
    with pytest.raises(InvalidState, match="already"):
        d.create_snapshot(uuid, "s2")


def test_snapshots_refused_on_block_disks_and_templates():
    d = FakeDriver(demo_console=False)
    d.create_pool(PoolSpec(name="lvm", type=PoolType.LVM, device="/dev/sdb", confirm_wipe=True))
    vm = d.create_vm(VMSpec(name="db", disks=[DiskSpec(size_gb=1, pool="lvm")]))
    with pytest.raises(InvalidState, match="qcow2"):
        d.create_snapshot(vm.uuid, "x")
    t = d.create_vm(VMSpec(name="t", disks=[DiskSpec(size_gb=1)]))
    d.set_template(t.uuid, True)
    with pytest.raises(InvalidState, match="template"):
        d.create_snapshot(t.uuid, "x")


def test_bad_snapshot_name(fake):
    d, uuid = fake
    with pytest.raises(ValueError):
        d.create_snapshot(uuid, "../etc")


# ---------- libvirt ----------


class LibvirtError(Exception):
    pass


class Snap:
    def __init__(self, xml):
        self.xml = xml
        self.deleted = False

    def getName(self):
        return ET.fromstring(self.xml).findtext("name")

    def getXMLDesc(self, _f):
        root = ET.fromstring(self.xml)
        ET.SubElement(root, "creationTime").text = "1700000000"
        ET.SubElement(root, "state").text = "running"
        return ET.tostring(root, encoding="unicode")

    def getParent(self, _f):
        raise LibvirtError("no parent")

    def isCurrent(self, _f):
        return 1

    def delete(self, _f):
        self.deleted = True


class Dom:
    def __init__(self, spec, active=True):
        self.xml = build_domain_xml(spec)
        self.active = active
        self.snaps = []
        self.reverted = None

    def XMLDesc(self, _f):
        return self.xml

    def isActive(self):
        return self.active

    def listAllSnapshots(self, _f):
        return self.snaps

    def snapshotCreateXML(self, xml, _f):
        self.snaps.append(Snap(xml))
        return self.snaps[-1]

    def snapshotLookupByName(self, name, _f):
        for s in self.snaps:
            if s.getName() == name:
                return s
        raise LibvirtError(name)

    def revertToSnapshot(self, snap, _f):
        self.reverted = snap.getName()


@pytest.fixture
def lvd(monkeypatch):
    lib = types.SimpleNamespace(libvirtError=LibvirtError, VIR_DOMAIN_XML_INACTIVE=2)
    monkeypatch.setattr(lv, "libvirt", lib)

    def make(spec, active=True):
        d = object.__new__(lv.LibvirtDriver)
        dom = Dom(spec, active)
        d._lookup = lambda _u: dom
        d._info = lambda _dom: types.SimpleNamespace(spec=lv._spec_from_xml(dom.xml))
        return d, dom

    return make


def test_libvirt_snapshot_xml_and_ops(lvd):
    d, dom = lvd(VMSpec(name="a", disks=[DiskSpec(size_gb=1)]))
    snap = d.create_snapshot("u", "pre-patch.1", "note & more")
    root = ET.fromstring(dom.snaps[0].xml)
    assert root.findtext("name") == "pre-patch.1" and root.findtext("description") == "note & more"
    assert (snap.vm_state, snap.current, snap.created[:4]) == ("running", True, "2023")
    d.revert_snapshot("u", "pre-patch.1")
    assert dom.reverted == "pre-patch.1"
    d.delete_snapshot("u", "pre-patch.1")
    assert dom.snaps[0].deleted
    with pytest.raises(SnapshotNotFound):
        d.delete_snapshot("u", "nope")


def test_running_uefi_vm_moves_its_variables_to_qcow2_then_snapshots(lvd, tmp_path):
    d, dom = lvd(VMSpec(name="w", firmware="uefi", disks=[DiskSpec(size_gb=1)]))
    raw = tmp_path / "w_VARS.fd"
    raw.write_bytes(b"\0" * 16)
    root = ET.fromstring(dom.xml)
    os_el = root.find("os")
    ET.SubElement(os_el, "loader", readonly="yes", type="pflash").text = "/usr/share/OVMF/C.fd"
    ET.SubElement(os_el, "nvram", template="/usr/share/OVMF/V.fd").text = str(raw)
    dom.xml = ET.tostring(root, encoding="unicode")
    with pytest.raises(InvalidState, match="power off once"):
        d.create_snapshot("u", "x")
    assert not dom.snaps

    events = []
    dom.shutdown = lambda: (events.append("shutdown"), setattr(dom, "active", False))
    dom.create = lambda: (events.append("start"), setattr(dom, "active", True))
    dom.name = lambda: "w"

    def define(xml):
        events.append("define")
        dom.xml = xml

    def run(cmd):
        events.append(cmd[0])
        open(cmd[-1], "wb").close()

    d._conn = types.SimpleNamespace(defineXML=define)
    d.run = run
    d.create_snapshot("u", "w-20261106-1530", restart=True)
    assert events == ["shutdown", "qemu-img", "define", "start"] and dom.active
    nvram = ET.fromstring(dom.xml).find("os/nvram")
    assert nvram.get("format") == "qcow2" and nvram.text == str(tmp_path / "w_VARS.qcow2")
    assert ET.fromstring(dom.xml).find("os").get("firmware") is None and not raw.exists()
    assert lv._spec_from_xml(dom.xml).firmware == "uefi"  # still a UEFI VM
    assert [s.getName() for s in dom.snaps] == ["w-20261106-1530"]
    events.clear()
    d.create_snapshot("u", "again")  # live from now on
    assert events == [] and len(dom.snaps) == 2


def test_backup_scratch_is_writable_by_qemu(tmp_path):
    lv._qemu_may_write(tmp_path)
    assert tmp_path.stat().st_mode & 0o755 == 0o755


def test_libvirt_refuses_raw_disks(lvd):
    d, _ = lvd(VMSpec(name="b", disks=[DiskSpec(size_gb=1, backend="block", pool="vg")]), False)
    with pytest.raises(InvalidState, match="qcow2"):
        d.create_snapshot("u", "x")


# ---------- manager and agent ----------


def test_snapshots_through_manager_on_remote_node(tmp_path):
    env = Env(tmp_path)
    env.client = TestClient(
        create_app(
            env.cluster,
            migrations=env.migrations,
            deploys=DeployManager(env.cluster, executor=Inline()),
        )
    )
    uuid = env.create_vm("remote-vm", node="node2", start=False)
    api = env.client
    resp = api.post(f"/api/v1/vms/{uuid}/snapshots", json={"name": "base-line", "description": "x"})
    assert resp.status_code == 201, resp.text
    assert [s["name"] for s in api.get(f"/api/v1/vms/{uuid}/snapshots").json()] == ["base-line"]
    assert api.post(f"/api/v1/vms/{uuid}/snapshots/base-line/revert").status_code == 200
    assert api.delete(f"/api/v1/vms/{uuid}/snapshots/nope").status_code == 404
    assert api.delete(f"/api/v1/vms/{uuid}/snapshots/base-line").status_code == 204
    assert api.get(f"/api/v1/vms/{uuid}/snapshots").json() == []


def test_snapshot_tasks_and_list_of_all_snapshots():
    driver = FakeDriver(demo_console=False)
    client = TestClient(create_app(driver))
    vm = client.post("/api/v1/vms", json={"name": "web01", "disks": [{"size_gb": 1}]}).json()
    assert client.post(f"/api/v1/vms/{vm['uuid']}/snapshots", json={"name": "s1"}).is_success
    assert client.post(f"/api/v1/vms/{vm['uuid']}/snapshots/s1/revert").is_success
    every = client.get("/api/v1/snapshots").json()
    assert [(s["vm_name"], s["name"], s["stored_in"]) for s in every] == [
        ("web01", "s1", "default")
    ]
    jobs = client.get("/api/v1/backup/jobs").json()
    assert [(j["kind"], j["vm"], j["target"], j["state"]) for j in jobs][:2] == [
        ("snapshot-restore", "web01", "s1", "done"),
        ("snapshot", "web01", "s1", "done"),
    ]
    bad = client.post(f"/api/v1/vms/{vm['uuid']}/snapshots", json={"name": "s1"})
    assert bad.status_code == 409
    assert client.get("/api/v1/backup/jobs").json()[0]["state"] == "failed"


def test_libvirt_waits_for_the_state_change_lock(monkeypatch):
    monkeypatch.setattr(lv, "libvirt", types.SimpleNamespace(libvirtError=LibvirtError))
    calls = []

    def busy():
        calls.append(1)
        if len(calls) < 3:
            raise LibvirtError("cannot acquire state change lock (held by monitor=qemu-event)")
        return "ok"

    assert lv._retry_locked(busy, wait=0) == "ok" and len(calls) == 3
    with pytest.raises(LibvirtError):
        lv._retry_locked(lambda: (_ for _ in ()).throw(LibvirtError("other")), wait=0)
