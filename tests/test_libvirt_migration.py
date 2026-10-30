"""LibvirtDriver.migrate_vm against a stand-in libvirt module."""

import types

import pytest

from ibhics.hypervisor import InvalidState, MigrationFailed
from ibhics.hypervisor import libvirt_driver as lv

DOMAIN_XML = """<domain><name>db</name><devices>
<disk type='volume' device='disk'><source pool='default' volume='db-disk0.qcow2'/></disk>
<disk type='volume' device='cdrom'><source pool='iso' volume='setup.iso'/></disk>
</devices></domain>"""

FLAGS = dict(
    VIR_MIGRATE_LIVE=1,
    VIR_MIGRATE_PEER2PEER=2,
    VIR_MIGRATE_PERSIST_DEST=8,
    VIR_MIGRATE_UNDEFINE_SOURCE=16,
    VIR_MIGRATE_NON_SHARED_DISK=64,
    VIR_MIGRATE_OFFLINE=1024,
    VIR_MIGRATE_AUTO_CONVERGE=8192,
)


class LibvirtError(Exception):
    pass


class Volume:
    def __init__(self, name, deleted):
        self.name, self.deleted = name, deleted

    def delete(self, _flags):
        self.deleted.append(self.name)


class Domain:
    def __init__(self, active, error=None):
        self.active, self.error, self.calls = active, error, []

    def XMLDesc(self, _flags):
        return DOMAIN_XML

    def isActive(self):
        return self.active

    def migrateToURI3(self, uri, params, flags):
        if self.error:
            raise LibvirtError(self.error)
        self.calls.append((uri, params, flags))


@pytest.fixture
def make(monkeypatch):
    fake = types.SimpleNamespace(
        libvirtError=LibvirtError, VIR_MIGRATE_PARAM_BANDWIDTH="bandwidth", **FLAGS
    )
    monkeypatch.setattr(lv, "libvirt", fake)

    def make(active, error=None):
        dom = Domain(active, error)
        deleted = []
        pool = types.SimpleNamespace(storageVolLookupByName=lambda n: Volume(n, deleted))
        d = object.__new__(lv.LibvirtDriver)
        d._conn = types.SimpleNamespace(
            lookupByUUIDString=lambda _u: dom, storagePoolLookupByName=lambda _n: pool
        )
        return d, dom, deleted

    return make


def flags(*names):
    return sum(FLAGS[f"VIR_MIGRATE_{n}"] for n in names)


BASE = ("PEER2PEER", "PERSIST_DEST", "UNDEFINE_SOURCE")


def test_live_on_shared_storage(make):
    driver, dom, deleted = make(active=True)
    driver.migrate_vm("u", "qemu+tls://node2/system")
    assert dom.calls == [("qemu+tls://node2/system", {}, flags(*BASE, "LIVE", "AUTO_CONVERGE"))]
    assert deleted == []


def test_live_with_storage_copy_removes_source_volumes(make):
    driver, dom, deleted = make(active=True)
    driver.migrate_vm("u", "qemu+tls://node2/system", copy_storage=True, bandwidth_mib=200)
    expected = flags(*BASE, "LIVE", "AUTO_CONVERGE", "NON_SHARED_DISK")
    assert dom.calls == [("qemu+tls://node2/system", {"bandwidth": 200}, expected)]
    assert deleted == ["db-disk0.qcow2"]  # disks only, not the ISO


def test_stopped_vm_is_moved_offline(make):
    driver, dom, _ = make(active=False)
    driver.migrate_vm("u", "qemu+ssh://root@node2/system")
    assert dom.calls[0][2] == flags(*BASE, "OFFLINE")
    with pytest.raises(InvalidState):
        driver.migrate_vm("u", "qemu+ssh://root@node2/system", copy_storage=True)


def test_libvirt_error_keeps_source_disks(make):
    driver, _, deleted = make(active=True, error="unable to connect to server")
    with pytest.raises(MigrationFailed, match="unable to connect"):
        driver.migrate_vm("u", "qemu+tls://node2/system", copy_storage=True)
    assert deleted == []
