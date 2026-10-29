"""LibvirtDriver backup primitives against a stand-in libvirt connection."""

import types
import xml.etree.ElementTree as ET

import pytest

from ibhics.hypervisor import InvalidState
from ibhics.hypervisor import libvirt_driver as lv

GIB = 1024**3
DOMAIN_XML = """<domain><name>db</name><devices>
<disk type='volume' device='disk'><driver name='qemu' type='qcow2'/>
  <source pool='default' volume='db-disk0.qcow2'/><target dev='vda'/></disk>
<disk type='volume' device='disk'><driver name='qemu' type='raw'/>
  <source pool='fc' volume='db-disk1'/><target dev='vdb'/></disk>
</devices></domain>"""


class LibvirtError(Exception):
    pass


class Volume:
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path

    def info(self):
        return (0, 10 * GIB, 1 * GIB)


class Pool:
    def storageVolLookupByName(self, name):
        return Volume(f"/pools/{name}")


class Domain:
    def __init__(self, active):
        self.active = active
        self.backup_xml = None
        self.polls = 0

    def name(self):
        return "db"

    def XMLDesc(self, _flags):
        return DOMAIN_XML

    def isActive(self):
        return self.active

    def backupBegin(self, xml, _checkpoint):
        self.backup_xml = xml

    def jobStats(self, flags):
        if flags == lv.libvirt.VIR_DOMAIN_JOB_STATS_COMPLETED:
            return {"type": lv.libvirt.VIR_DOMAIN_JOB_COMPLETED}
        self.polls += 1
        return {"type": 2 if self.polls < 3 else 0}  # running, running, done


class Conn:
    def __init__(self, dom):
        self.dom = dom

    def lookupByUUIDString(self, _uuid):
        return self.dom

    def storagePoolLookupByName(self, _name):
        return Pool()


@pytest.fixture
def make_driver(monkeypatch, tmp_path):
    fake = types.SimpleNamespace(
        libvirtError=LibvirtError,
        VIR_DOMAIN_JOB_NONE=0,
        VIR_DOMAIN_JOB_COMPLETED=3,
        VIR_DOMAIN_JOB_STATS_COMPLETED=1,
    )
    monkeypatch.setattr(lv, "libvirt", fake)
    monkeypatch.setattr(lv.time, "sleep", lambda _s: None)

    def make(active):
        d = object.__new__(lv.LibvirtDriver)
        d._conn = Conn(Domain(active))
        d.scratch_dir = str(tmp_path / "scratch")
        d.commands = []
        d.run = d.commands.append
        return d

    return make


def test_running_vm_uses_push_backup(make_driver):
    driver = make_driver(active=True)
    with driver.export_disks("u") as images:
        assert [p.name for p in images] == ["disk0.raw", "disk1.raw"]
        scratch = images[0].parent
    assert not scratch.exists()  # cleaned up afterwards

    root = ET.fromstring(driver._conn.dom.backup_xml)
    assert root.get("mode") == "push"
    disks = root.findall("disks/disk")
    assert [d.get("name") for d in disks] == ["vda", "vdb"]
    assert [d.find("target").get("file") for d in disks] == [str(p) for p in images]
    assert driver._conn.dom.polls == 3
    assert driver.commands == []


def test_stopped_vm_converts_volumes(make_driver):
    driver = make_driver(active=False)
    with driver.export_disks("u") as images:
        pass
    assert driver.commands == [
        ["qemu-img", "convert", "-O", "raw", "/pools/db-disk0.qcow2", str(images[0])],
        ["qemu-img", "convert", "-O", "raw", "/pools/db-disk1", str(images[1])],
    ]


def test_import_disk_keeps_volume_format(make_driver, tmp_path):
    driver = make_driver(active=False)
    raw = tmp_path / "in.raw"
    raw.write_bytes(b"x" * 1024)
    driver.import_disk("u", 0, raw)
    driver.import_disk("u", 1, raw)
    assert driver.commands == [
        [
            "qemu-img",
            "convert",
            "-n",
            "-f",
            "raw",
            "-O",
            "qcow2",
            str(raw),
            "/pools/db-disk0.qcow2",
        ],
        ["qemu-img", "convert", "-n", "-f", "raw", "-O", "raw", str(raw), "/pools/db-disk1"],
    ]


def test_import_disk_refuses_running_vm(make_driver, tmp_path):
    driver = make_driver(active=True)
    with pytest.raises(InvalidState):
        driver.import_disk("u", 0, tmp_path / "in.raw")
