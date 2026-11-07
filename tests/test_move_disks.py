"""Migrate → Change storage: a VM's disks to other pools on its node, running or not."""

import types
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from test_v12_vm import Conn, Dom

import ibhics.hypervisor.libvirt_driver as lv
from ibhics.hypervisor import DiskSpec, VMSpec
from ibhics.hypervisor.base import InvalidState
from ibhics.hypervisor.domain_xml import META_NS, build_domain_xml
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app

API = "/api/v1"


class Vol:
    def __init__(self, name, pool):
        self.n, self.pool, self.deleted = name, pool, False

    def name(self):
        return self.n

    def path(self):
        return f"/{self.pool}/{self.n}"

    def info(self):
        return (0, 10 * 2**30, 0)

    def delete(self, _f):
        self.deleted = True


def setup(monkeypatch, active):
    monkeypatch.setattr(lv, "libvirt", types.SimpleNamespace(
        libvirtError=RuntimeError, VIR_DOMAIN_XML_INACTIVE=2, VIR_DOMAIN_BLOCK_COPY_REUSE_EXT=1,
        VIR_DOMAIN_BLOCK_COPY_TRANSIENT_JOB=4, VIR_DOMAIN_BLOCK_JOB_ABORT_PIVOT=2))  # fmt: skip
    monkeypatch.setattr(lv, "BLOCK_COPY_POLL", 0)
    disks = [DiskSpec(size_gb=10), DiskSpec(size_gb=5)]
    dom = Dom(build_domain_xml(VMSpec(name="npm-vm", disks=disks)), active=active)
    dom.snapshotNum = lambda _f: 0
    made, old = [], Vol("npm-vm-disk0.qcow2", "default")

    def pool(name, kind):
        return types.SimpleNamespace(
            XMLDesc=lambda _f=0: f"<pool type='{kind}'/>",
            listVolumes=lambda: [],
            createXML=lambda xml, _f: (
                made.append(Vol(ET.fromstring(xml).findtext("name"), name)) or made[-1]
            ),
            isActive=lambda: True,
        )

    pools = {"default": pool("default", "dir"), "hybrid01": pool("hybrid01", "fs"),
             "raid": pool("raid", "logical")}  # fmt: skip
    d = object.__new__(lv.LibvirtDriver)
    d._conn = Conn(dom)
    d._moves = {}
    d._lookup = lambda _u: dom
    d._pool = lambda n: pools[n]
    d._volume = lambda _disk: old
    d._info = lambda _dom: types.SimpleNamespace(spec=lv._spec_from_xml(dom.xml))
    return d, dom, made, old


def test_offline_move_to_hybrid(monkeypatch):
    d, dom, made, old = setup(monkeypatch, active=False)
    copies, steps = [], []

    def copy(src, fmt, dest, backend, progress):
        copies.append((src, fmt, dest, backend))
        progress(50)
        steps.append(d.disk_move_progress("u"))

    monkeypatch.setattr(d, "_offline_copy", copy)
    info = d.move_disks("u", {0: "hybrid01", 1: "default"})  # disk 2 stays
    assert copies == [("/default/npm-vm-disk0.qcow2", "qcow2",
                       "/hybrid01/npm-vm-disk0.qcow2", "file")]  # fmt: skip
    assert steps[0] == {"step": "Disk 1 → hybrid01", "percent": 50.0}
    assert old.deleted and d.disk_move_progress("u") == {}
    assert [x.pool for x in info.spec.disks] == ["hybrid01", "default"]
    root = ET.fromstring(dom.xml)
    first = root.find("devices/disk[@device='disk']")
    assert first.find("source").get("pool") == "hybrid01"
    assert first.find("target").get("dev") == "vda"
    meta = root.find(f"metadata/{{{META_NS}}}vm/{{{META_NS}}}disk")
    assert meta.get("pool") == "hybrid01" and meta.get("volume") == "npm-vm-disk0.qcow2"


def test_live_move_copies_then_pivots(monkeypatch):
    d, dom, made, old = setup(monkeypatch, active=True)
    calls, polls = [], iter([{"cur": 0, "end": 100}, {"cur": 60, "end": 100},
                             {"cur": 100, "end": 100}])  # fmt: skip
    dom.blockCopy = lambda dev, xml, params, flags: calls.append(("copy", dev, xml, flags))
    dom.blockJobInfo = lambda dev, _f: next(polls)
    dom.blockJobAbort = lambda dev, flags: calls.append(("abort", dev, flags))
    d.move_disks("u", {0: "raid"})
    assert calls[0][0] == "copy" and calls[0][1] == "vda" and "type='block'" in calls[0][2]
    assert calls[0][3] == 5 and calls[-1] == ("abort", "vda", 2)  # pivot
    assert made[0].n == "npm-vm-disk0"  # raw LV, no .qcow2
    first = ET.fromstring(dom.xml).find("devices/disk[@device='disk']")
    assert first.get("type") == "block" and first.find("source").get("dev").endswith(
        "/npm-vm-disk0"
    )
    assert old.deleted


def test_failed_copy_keeps_the_old_disk(monkeypatch):
    d, dom, made, old = setup(monkeypatch, active=False)

    def boom(*_a):
        raise InvalidState("copying the disk failed")

    monkeypatch.setattr(d, "_offline_copy", boom)
    before = dom.xml
    with pytest.raises(InvalidState):
        d.move_disks("u", {0: "hybrid01"})
    assert made[0].deleted and not old.deleted and dom.xml == before


def test_snapshots_block_a_move(monkeypatch):
    d, dom, _made, _old = setup(monkeypatch, active=False)
    dom.snapshotNum = lambda _f: 2
    with pytest.raises(InvalidState, match="snapshots"):
        d.move_disks("u", {0: "hybrid01"})


def test_move_disks_api_task():
    drv = FakeDriver()
    client = TestClient(create_app(drv))
    node = client.get(f"{API}/pools").json()[0]["node"]
    client.post(f"{API}/nodes/{node}/pools", json={"name": "hy", "type": "dir"})
    vm = client.post(f"{API}/vms", json={"name": "npm-vm", "disks": [{"size_gb": 1}]}).json()
    r = client.post(
        f"{API}/vms/{vm['uuid']}/move-disks", json={"moves": [{"index": 0, "pool": "hy"}]}
    )
    assert r.status_code == 200 and r.json()["disks"][0]["pool"] == "hy"
    assert client.post(f"{API}/vms/{vm['uuid']}/move-disks",
                       json={"moves": [{"index": 3, "pool": "hy"}]}).status_code == 422  # fmt: skip
    jobs = client.get(f"{API}/backup/jobs").json()
    job = next(j for j in jobs if j["kind"] == "move-disks" and j["state"] == "done")
    assert job["state"] == "done" and "percent" in job and job["target"] == "disk 1 → hy"


def test_offline_copy_reads_qemu_img_progress(monkeypatch):
    import subprocess
    import sys

    real = subprocess.Popen
    script = "import sys; sys.stdout.write('  (12.50/100%)\\r  (100.00/100%)\\r')"
    monkeypatch.setattr(lv.subprocess, "Popen",
                        lambda argv, **kw: real([sys.executable, "-c", script], **kw))  # fmt: skip
    seen = []
    lv.LibvirtDriver._offline_copy("/a", "qcow2", "/b", "file", seen.append)
    assert 12.5 in seen and seen[-1] == 100.0


def test_pool_of_a_disk_moved_by_hand(monkeypatch):
    # npm-vm was moved with virsh: <source file='/var/lib/ibhics/pools/hybrid01/...'/>
    # and no metadata for it; the VM page still shows hybrid01.
    monkeypatch.setattr(lv, "libvirt", types.SimpleNamespace(libvirtError=RuntimeError))

    def pool(name, path):
        return types.SimpleNamespace(
            name=lambda: name,
            XMLDesc=lambda _f=0: f"<pool><target><path>{path}</path></target></pool>",
        )

    d = object.__new__(lv.LibvirtDriver)
    d._conn = types.SimpleNamespace(listAllStoragePools=lambda: [
        pool("default", "/var/lib/ibhics/pools/default"),
        pool("hybrid01", "/var/lib/ibhics/pools/hybrid01")])  # fmt: skip
    assert d._pool_of_path("/var/lib/ibhics/pools/hybrid01/npm-vm.qcow2") == "hybrid01"
    assert d._pool_of_path("/var/lib/ibhics/pools/hybrid01x/a.qcow2") == ""
    assert d._pool_of_path(None) == ""
