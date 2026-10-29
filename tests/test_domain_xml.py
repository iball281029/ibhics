import xml.etree.ElementTree as ET

import pytest

from ibhics.hypervisor import DiskSpec, Firmware, NicSpec, VMSpec
from ibhics.hypervisor.domain_xml import build_domain_xml, build_volume_xml
from ibhics.hypervisor.libvirt_driver import _spec_from_xml


def spec(**kw):
    defaults = dict(
        name="db-01",
        vcpus=4,
        memory_mb=8192,
        disks=[DiskSpec(size_gb=100), DiskSpec(size_gb=500, backend="rbd", pool="ceph-vm")],
        nics=[NicSpec(network="prod", mac="52:54:00:aa:bb:cc")],
    )
    defaults.update(kw)
    return VMSpec(**defaults)


def test_domain_basics():
    root = ET.fromstring(build_domain_xml(spec(), uuid="1234"))
    assert root.get("type") == "kvm"
    assert root.findtext("name") == "db-01"
    assert root.findtext("uuid") == "1234"
    assert root.findtext("vcpu") == "4"
    assert root.find("memory").get("unit") == "MiB"
    assert root.findtext("memory") == "8192"
    assert root.find("cpu").get("mode") == "host-model"


def test_disks_map_to_volumes():
    disks = ET.fromstring(build_domain_xml(spec())).findall("./devices/disk[@device='disk']")
    assert [d.find("target").get("dev") for d in disks] == ["vda", "vdb"]
    assert disks[0].find("driver").get("type") == "qcow2"
    assert disks[0].find("source").get("volume") == "db-01-disk0.qcow2"
    assert disks[1].find("driver").get("type") == "raw"
    assert disks[1].find("source").attrib == {"pool": "ceph-vm", "volume": "db-01-disk1"}


def test_uefi_and_iso():
    root = ET.fromstring(build_domain_xml(spec(firmware=Firmware.UEFI, iso="/isos/win.iso")))
    assert root.find("os").get("firmware") == "efi"
    assert [b.get("dev") for b in root.findall("./os/boot")] == ["cdrom", "hd"]
    assert root.find("./devices/disk[@device='cdrom']/source").get("file") == "/isos/win.iso"


def test_vnc_listens_locally():
    graphics = ET.fromstring(build_domain_xml(spec())).find("./devices/graphics")
    assert graphics.get("listen") == "127.0.0.1"


def test_volume_xml():
    file_vol = ET.fromstring(build_volume_xml("vm", 0, DiskSpec(size_gb=20)))
    assert file_vol.findtext("capacity") == "20"
    assert file_vol.find("./target/format").get("type") == "qcow2"
    rbd_vol = ET.fromstring(build_volume_xml("vm", 1, DiskSpec(size_gb=20, backend="rbd")))
    assert rbd_vol.find("target") is None


def test_round_trip_through_libvirt_xml():
    original = spec(firmware=Firmware.UEFI, iso="/isos/x.iso", description="primary db")
    parsed = _spec_from_xml(build_domain_xml(original))
    assert parsed.name == original.name
    assert parsed.vcpus == 4
    assert parsed.memory_mb == 8192
    assert parsed.firmware == Firmware.UEFI
    assert [d.backend for d in parsed.disks] == ["file", "rbd"]
    assert parsed.nics[0].mac == "52:54:00:aa:bb:cc"
    assert parsed.iso == "/isos/x.iso"
    assert parsed.description == "primary db"


def test_invalid_name_rejected():
    with pytest.raises(ValueError):
        build_domain_xml(spec(name="no spaces"))
