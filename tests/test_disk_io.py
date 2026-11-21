"""Disk tuning without changing running guests or explicit storage choices."""

import types
import xml.etree.ElementTree as ET
from unittest.mock import Mock

import pytest

from ibhics.hypervisor import DiskSpec, InvalidState, VMEdit, VMSpec
from ibhics.hypervisor import libvirt_driver as lv
from ibhics.hypervisor.base import DiskEdit
from ibhics.hypervisor.domain_xml import (
    build_domain_xml,
    disk_iothread_ids,
    normalize_disk_io,
)


def definition():
    root = ET.fromstring(
        build_domain_xml(VMSpec(name="vm", disks=[DiskSpec(size_gb=10)], iso="/iso/os.iso"))
    )
    root.remove(root.find("iothreads"))
    driver = root.find("devices/disk/driver")
    for attr in ("io", "iothread"):
        driver.attrib.pop(attr)
    return root


def test_new_disks_and_cd_keep_bus_and_format():
    root = ET.fromstring(
        build_domain_xml(
            VMSpec(
                name="vm",
                disks=[DiskSpec(size_gb=1), DiskSpec(size_gb=1, bus="sata")],
                iso="/iso/os.iso",
            )
        )
    )
    assert root.findtext("iothreads") == "1"
    virtio, sata, cd = root.findall("devices/disk")
    assert virtio.find("driver").attrib == dict(
        name="qemu", type="qcow2", cache="none", io="native", discard="unmap", iothread="1"
    )
    assert virtio.find("target").get("bus") == "virtio"
    assert sata.find("target").get("bus") == "sata"
    assert sata.find("driver").attrib == dict(
        name="qemu", type="qcow2", cache="none", discard="unmap"
    )
    assert cd.find("target").get("bus") == "sata"
    assert cd.find("driver").attrib == dict(name="qemu", type="raw")


def test_normalize_idempotent_and_preserves_other_xml():
    root = definition()
    disk, cd = root.findall("devices/disk")
    untouched = [ET.tostring(el) for el in (disk.find("source"), disk.find("target"), cd)]
    assert normalize_disk_io(root)
    assert root.findtext("iothreads") == "1"
    assert disk.find("driver").get("io") == "native"
    assert disk.find("driver").get("iothread") == "1"
    assert disk.find("driver").get("type") == "qcow2"
    normalized = ET.tostring(root)
    assert not normalize_disk_io(root)
    assert ET.tostring(root) == normalized
    assert untouched == [ET.tostring(el) for el in (disk.find("source"), disk.find("target"), cd)]


@pytest.mark.parametrize("cache", ["writeback", "writethrough", "directsync", None])
def test_custom_cache_is_untouched(cache):
    root = definition()
    driver = root.find("devices/disk/driver")
    if cache is None:
        driver.attrib.pop("cache")
    else:
        driver.set("cache", cache)
    before = ET.tostring(root)
    assert not normalize_disk_io(root)
    assert ET.tostring(root) == before


@pytest.mark.parametrize("io", ["threads", "io_uring", "native"])
def test_existing_io_and_thread_choices_are_preserved(io):
    root = definition()
    ET.SubElement(root, "iothreads").text = "2"
    ET.SubElement(ET.SubElement(root, "iothreadids"), "iothread", id="7")
    driver = root.find("devices/disk/driver")
    driver.set("io", io)
    driver.set("iothread", "7")
    driver.set("discard", "ignore")
    before = ET.tostring(root)
    assert not normalize_disk_io(root)
    assert ET.tostring(root) == before


def test_custom_thread_ids_and_disabled_threads():
    root = definition()
    count = ET.SubElement(root, "iothreads")
    count.text = "1"
    ET.SubElement(ET.SubElement(root, "iothreadids"), "iothread", id="7")
    assert normalize_disk_io(root)
    assert root.find("devices/disk/driver").get("iothread") == "7"
    assert root.findtext("iothreads") == "1"
    count.text = "0"
    assert disk_iothread_ids(root) == set()


def test_sata_definition_is_untouched():
    root = definition()
    root.find("devices/disk/target").set("bus", "sata")
    before = ET.tostring(root)
    assert not normalize_disk_io(root)
    assert ET.tostring(root) == before


@pytest.fixture
def start_driver(monkeypatch):
    monkeypatch.setattr(
        lv, "libvirt", types.SimpleNamespace(VIR_DOMAIN_XML_INACTIVE=2, VIR_DOMAIN_XML_SECURE=1)
    )
    root = definition()
    xml = ET.tostring(root, encoding="unicode")
    dom = Mock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = xml
    driver = object.__new__(lv.LibvirtDriver)
    driver._lookup = Mock(return_value=dom)
    driver._info = Mock(return_value="started")
    driver._nvram_qcow2 = Mock()
    driver._add_tablet = Mock()
    driver._conn = Mock()

    def define(xml):
        dom.XMLDesc.return_value = xml

    driver._conn.defineXML.side_effect = define
    return driver, dom


def test_start_normalizes_persistent_definition_once(start_driver):
    driver, dom = start_driver
    assert driver.start_vm("u") == "started"
    driver._conn.defineXML.assert_called_once()
    xml = driver._conn.defineXML.call_args.args[0]
    assert ET.fromstring(xml).find("devices/disk/driver").get("iothread") == "1"
    # Secure XML keeps console passwords in the redefined VM.
    assert any(call.args == (3,) for call in dom.XMLDesc.call_args_list)
    dom.create.assert_called_once()
    driver.start_vm("u")
    driver._conn.defineXML.assert_called_once()


def test_active_vm_never_redefined_or_started(start_driver):
    driver, dom = start_driver
    dom.isActive.return_value = True
    with pytest.raises(InvalidState, match="already running"):
        driver.start_vm("u")
    driver._normalize_disk_io(dom)
    driver._conn.defineXML.assert_not_called()
    dom.XMLDesc.assert_not_called()
    dom.create.assert_not_called()


def test_redefine_failure_logs_warning_but_starts_original(start_driver, caplog):
    driver, dom = start_driver
    original = dom.XMLDesc.return_value
    driver._conn.defineXML.side_effect = RuntimeError("unsupported I/O tuning")
    assert driver.start_vm("u") == "started"
    assert dom.XMLDesc.return_value == original
    dom.create.assert_called_once()
    assert "starting with existing definition" in caplog.text


@pytest.mark.parametrize(
    "saved,live,expected", [(True, True, "1"), (True, False, None), (False, False, None)]
)
def test_attach_uses_only_threads_available_in_both_definitions(monkeypatch, saved, live, expected):
    monkeypatch.setattr(
        lv,
        "libvirt",
        types.SimpleNamespace(
            libvirtError=LookupError,
            VIR_DOMAIN_XML_INACTIVE=2,
            VIR_DOMAIN_XML_SECURE=1,
            VIR_DOMAIN_AFFECT_LIVE=1,
        ),
    )
    root = definition()
    if saved:
        ET.SubElement(root, "iothreads").text = "1"
    active_root = ET.fromstring(ET.tostring(root))
    if not live and active_root.find("iothreads") is not None:
        active_root.remove(active_root.find("iothreads"))
    spec = VMSpec(name="vm", disks=[DiskSpec(size_gb=10)])
    dom = Mock()
    dom.isActive.return_value = True
    dom.XMLDesc.side_effect = lambda flags: ET.tostring(
        root if flags == 2 else active_root, encoding="unicode"
    )
    driver = object.__new__(lv.LibvirtDriver)
    driver._lookup = Mock(return_value=dom)
    driver._info = Mock(return_value=types.SimpleNamespace(spec=spec))
    driver._conn = Mock()
    pool = Mock()
    pool.XMLDesc.return_value = "<pool type='dir'/>"
    pool.storageVolLookupByName.side_effect = LookupError
    driver._pool = Mock(return_value=pool)
    driver.update_vm("u", VMEdit(disks=[DiskEdit(size_gb=10, index=0), DiskEdit(size_gb=2)]))
    dom.attachDeviceFlags.assert_called_once()
    attached = ET.fromstring(dom.attachDeviceFlags.call_args.args[0])
    assert attached.find("driver").get("iothread") == expected
    assert attached.find("driver").get("io") == "native"
    assert attached.find("driver").get("type") == "qcow2"
    assert attached.find("target").get("bus") == "virtio"
    persistent = ET.fromstring(driver._conn.defineXML.call_args.args[0])
    assert (
        persistent.findall("devices/disk[@device='disk']")[-1].find("driver").attrib
        == attached.find("driver").attrib
    )


def test_explicit_and_automatic_iothread_ids():
    root = ET.fromstring(
        "<domain><iothreads>3</iothreads><iothreadids>"
        "<iothread id='7'/><iothread id='1'/></iothreadids></domain>"
    )
    assert disk_iothread_ids(root) == {"1", "2", "7"}


def test_existing_thread_mapping_is_preserved():
    root = definition()
    ET.SubElement(root, "iothreads").text = "2"
    driver = root.find("devices/disk/driver")
    ET.SubElement(ET.SubElement(driver, "iothreads"), "iothread", id="2")
    assert normalize_disk_io(root)
    assert driver.get("iothread") is None
    assert driver.find("iothreads/iothread").get("id") == "2"
    assert root.findtext("iothreads") == "2"
    assert not normalize_disk_io(root)


def test_failed_tuned_start_restores_previous_definition(start_driver):
    driver, dom = start_driver
    original = dom.XMLDesc.return_value
    lv.libvirt.libvirtError = RuntimeError
    dom.create.side_effect = [RuntimeError("qemu refused"), None]
    driver._conn.defineXML.side_effect = None
    driver._conn.defineXML.return_value = dom
    assert driver.start_vm("u") == "started"
    assert driver._conn.defineXML.call_args_list[-1].args == (original,)
    assert dom.create.call_count == 2
