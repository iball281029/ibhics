"""Disk error protection must not return transient storage failures to a guest."""

import types
import xml.etree.ElementTree as ET
from unittest.mock import Mock

import pytest

from ibhics.hypervisor import libvirt_driver as lv
from ibhics.hypervisor.domain_xml import disk_error_protected, normalize_disk_error_policy


def test_protection_covers_all_qemu_data_disks_but_not_cdrom():
    root = ET.fromstring("""<domain><devices>
      <disk device="disk">
        <driver name="qemu" type="qcow2" cache="writeback" rerror_policy="report"/>
        <target bus="sata"/>
      </disk>
      <disk device="disk"><driver name="qemu" type="raw"/><target bus="virtio"/></disk>
      <disk device="cdrom"><driver name="qemu" type="raw"/></disk>
    </devices></domain>""")
    assert normalize_disk_error_policy(root) and disk_error_protected(root)
    assert not normalize_disk_error_policy(root)
    assert root.find("devices/disk/driver").get("cache") == "writeback"
    assert root.findall("devices/disk")[2].find("driver").get("error_policy") is None


def test_paused_start_resumes_existing_domain_without_recreating_it(monkeypatch):
    monkeypatch.setattr(lv, "libvirt", types.SimpleNamespace(VIR_DOMAIN_PAUSED=3))
    driver = object.__new__(lv.LibvirtDriver)
    dom = Mock()
    dom.isActive.return_value = True
    dom.state.return_value = (3, 5)
    driver._lookup = Mock(return_value=dom)
    driver._info = Mock(return_value="resumed")
    driver._conn = Mock()
    assert driver.start_vm("vm") == "resumed"
    dom.resume.assert_called_once_with()
    dom.create.assert_not_called()
    dom.destroy.assert_not_called()
    driver._conn.defineXML.assert_not_called()


def test_safety_definition_failure_prevents_unprotected_boot(monkeypatch):
    monkeypatch.setattr(
        lv, "libvirt", types.SimpleNamespace(VIR_DOMAIN_XML_INACTIVE=2, VIR_DOMAIN_XML_SECURE=1)
    )
    driver = object.__new__(lv.LibvirtDriver)
    dom = Mock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = (
        '<domain><devices><disk device="disk">'
        '<driver name="qemu" type="qcow2"/></disk></devices></domain>'
    )
    driver._lookup = Mock(return_value=dom)
    driver._nvram_qcow2 = Mock()
    driver._add_tablet = Mock()
    driver._conn = Mock()
    driver._conn.defineXML.side_effect = RuntimeError("definition failed")
    with pytest.raises(RuntimeError, match="definition failed"):
        driver.start_vm("vm")
    dom.create.assert_not_called()


def test_remote_vm_state_remains_an_enum_for_os_context_and_migration_checks():
    from dataclasses import asdict

    from ibhics.hypervisor.base import VMInfo, VMSpec, VMState
    from ibhics.hypervisor.serialize import info_from_json
    vm = VMInfo(uuid="test", spec=VMSpec(name="test"), state=VMState.RUNNING)
    payload = {**asdict(vm.spec), "uuid": vm.uuid, "state": "running"}
    restored = info_from_json(payload)
    assert restored.state is VMState.RUNNING
    assert restored.state.value == "running"
