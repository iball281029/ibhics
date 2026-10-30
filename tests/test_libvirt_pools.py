"""LibvirtDriver storage logic against an in-memory stand-in for a libvirt connection."""

import types
import xml.etree.ElementTree as ET

import pytest

from ibhics.hypervisor import InvalidState, PoolNotFound, PoolSpec, PoolType
from ibhics.hypervisor import libvirt_driver as lv

IQN = "iqn.2004-04.com.example:storage"
GIB = 1024**3


class LibvirtError(Exception):
    pass


class Volume:
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path


class Pool:
    def __init__(self, conn, xml):
        self.conn, self.xml = conn, xml
        self.active = False
        self.autostart = False
        self.built = False
        self.volumes = []

    def name(self):
        return ET.fromstring(self.xml).findtext("name")

    def XMLDesc(self, _flags):
        return self.xml

    def isActive(self):
        return self.active

    def create(self, _flags):
        if self.conn.fail_start:
            raise LibvirtError("cannot find any matching source devices")
        self.active = True

    def destroy(self):
        self.active = False

    def setAutostart(self, value):
        self.autostart = bool(value)

    def build(self, flags):
        if self.conn.fail_build:
            raise LibvirtError("vgcreate failed")
        self.built = flags or True

    def undefine(self):
        del self.conn.pools[self.name()]

    def info(self):
        return (2, 100 * GIB, 10 * GIB, 90 * GIB)

    def refresh(self, _flags):
        pass

    def listAllVolumes(self):
        return self.volumes


class Domain:
    def __init__(self, name, pool):
        self._name, self._pool = name, pool

    def name(self):
        return self._name

    def XMLDesc(self, _flags):
        return (
            f"<domain><name>{self._name}</name><devices><disk type='volume' device='disk'>"
            f"<source pool='{self._pool}' volume='v'/></disk></devices></domain>"
        )


class Conn:
    def __init__(self):
        self.pools = {}
        self.domains = []
        self.fail_build = False
        self.fail_start = False
        self.luns = []

    def storagePoolDefineXML(self, xml):
        pool = Pool(self, xml)
        if ET.fromstring(xml).get("type") == "iscsi":
            pool.volumes = [Volume(p) for p in self.luns]
        self.pools[pool.name()] = pool
        return pool

    def storagePoolLookupByName(self, name):
        try:
            return self.pools[name]
        except KeyError:
            raise LibvirtError(name) from None

    def listAllStoragePools(self):
        return list(self.pools.values())

    def listAllDomains(self):
        return self.domains


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.setattr(
        lv,
        "libvirt",
        types.SimpleNamespace(libvirtError=LibvirtError, VIR_STORAGE_POOL_BUILD_OVERWRITE=4),
    )
    d = object.__new__(lv.LibvirtDriver)
    d._conn = Conn()
    return d


def test_dir_pool_is_built_started_and_autostarted(driver):
    info = driver.create_pool(PoolSpec(name="local", type=PoolType.DIR))
    pool = driver._conn.pools["local"]
    assert pool.built and pool.active and pool.autostart
    assert (info.type, info.capacity_gb, info.available_gb) == (PoolType.DIR, 100.0, 90.0)


def test_iscsi_pool_uses_requested_lun(driver):
    base = "/dev/disk/by-path/ip-10.0.0.60:3260-iscsi-" + IQN
    driver._conn.luns = [base + "-lun-0", base + "-lun-1"]
    spec = PoolSpec(name="san", type=PoolType.ISCSI, host="10.0.0.60", iqn=IQN, lun=1)
    spec.confirm_wipe = True
    info = driver.create_pool(spec)

    session = driver._conn.pools["san-iscsi"]
    assert session.active and session.autostart
    lvm = ET.fromstring(driver._conn.pools["san"].xml)
    assert lvm.find("source/device").get("path") == base + "-lun-1"
    assert info.type == PoolType.ISCSI
    assert [p.name for p in driver.list_pools()] == ["san"]  # the session pool stays hidden


def test_iscsi_missing_lun_rolls_back_session(driver):
    driver._conn.luns = ["/dev/disk/by-path/x-lun-0"]
    spec = PoolSpec(
        name="san", type=PoolType.ISCSI, host="10.0.0.60", iqn=IQN, lun=5, confirm_wipe=True
    )
    with pytest.raises(ValueError, match="LUN 5"):
        driver.create_pool(spec)
    assert driver._conn.pools == {}


def test_failed_vgcreate_leaves_nothing_defined(driver):
    driver._conn.fail_build = True
    spec = PoolSpec(name="fc", type=PoolType.LVM, device="/dev/mapper/mpatha", confirm_wipe=True)
    with pytest.raises(LibvirtError):
        driver.create_pool(spec)
    assert driver._conn.pools == {}


def test_lvm_pool_uses_the_real_device_and_cleans_up_a_failed_start(driver, monkeypatch):
    link = "/dev/disk/by-id/wwn-0x600"
    monkeypatch.setattr(lv.os.path, "realpath", {link: "/dev/sda"}.get)
    commands = []
    driver.run = commands.append

    driver.create_pool(PoolSpec(name="raid", type=PoolType.LVM, device=link, confirm_wipe=True))
    # libvirt matches the device pvs reports, so a /dev/disk/by-id link would not start.
    pool = ET.fromstring(driver._conn.pools["raid"].xml)
    assert pool.find("source/device").get("path") == "/dev/sda"

    driver._conn.fail_start = True
    spec = PoolSpec(name="raid2", type=PoolType.LVM, device=link, confirm_wipe=True)
    with pytest.raises(LibvirtError):
        driver.create_pool(spec)
    assert "raid2" not in driver._conn.pools
    assert commands == [["vgremove", "-f", "ibhics_raid2"], ["pvremove", "-ff", "-y", "/dev/sda"]]


def test_thin_disk_pool_formats_xfs_and_keeps_the_stable_name(driver):
    link = "/dev/disk/by-id/wwn-0x600"
    driver.create_pool(PoolSpec(name="raid", type=PoolType.DISK, device=link, confirm_wipe=True))
    pool = driver._conn.pools["raid"]
    root = ET.fromstring(pool.xml)
    assert root.get("type") == "fs" and root.find("source/format").get("type") == "xfs"
    assert root.find("source/device").get("path") == link  # mounted by id, not /dev/sdX
    assert root.findtext("target/path") == "/var/lib/ibhics/pools/raid"
    assert pool.built == 4 and pool.active and pool.autostart  # mkfs over the old contents
    info = next(p for p in driver.list_pools() if p.name == "raid")
    assert (info.type, info.source) == (PoolType.DISK, link)


def test_stable_device_prefers_wwn(tmp_path):
    disk = tmp_path / "sdb"
    disk.touch()
    for name in ("scsi-3600", "wwn-0x600", "wwn-0x600-part1"):
        (tmp_path / name).symlink_to(disk)
    assert lv.stable_device(str(disk), str(tmp_path)) == str(tmp_path / "wwn-0x600")
    assert lv.stable_device("/dev/disk/by-id/x", str(tmp_path)) == "/dev/disk/by-id/x"
    assert lv.stable_device("/dev/sdz", str(tmp_path / "missing")) == "/dev/sdz"


def test_duplicate_pool_rejected(driver):
    driver.create_pool(PoolSpec(name="local", type=PoolType.DIR))
    with pytest.raises(InvalidState):
        driver.create_pool(PoolSpec(name="local", type=PoolType.DIR))


def test_delete_pool_refuses_while_in_use_and_removes_session(driver):
    base = "/dev/disk/by-path/ip-10.0.0.60:3260-iscsi-" + IQN
    driver._conn.luns = [base + "-lun-0"]
    driver.create_pool(
        PoolSpec(name="san", type=PoolType.ISCSI, host="10.0.0.60", iqn=IQN, confirm_wipe=True)
    )
    driver._conn.domains = [Domain("db", "san")]
    with pytest.raises(InvalidState, match="db"):
        driver.delete_pool("san")

    driver._conn.domains = []
    driver.delete_pool("san")
    assert driver._conn.pools == {}


def test_delete_unknown_pool(driver):
    with pytest.raises(PoolNotFound):
        driver.delete_pool("ghost")
