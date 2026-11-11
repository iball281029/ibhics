"""Growing a running VM's CPUs and memory without a restart (libvirt driver)."""

import types
import xml.etree.ElementTree as ET

import pytest

from ibhics.hypervisor import VMEdit, VMInfo, VMSpec, VMState
from ibhics.hypervisor import libvirt_driver as lv
from ibhics.hypervisor.domain_xml import build_domain_xml, hotplug_ready, make_hotplug_ready

LIVE, CONFIG, GUEST = 1, 2, 4


class Domain:
    def __init__(self, xml, active=True):
        self.xml, self.active, self.calls = xml, active, []

    def XMLDesc(self, _flags=0):
        return self.xml

    def isActive(self):
        return self.active

    def setVcpusFlags(self, n, flags):
        self.calls.append(("vcpus", n, flags))

    def attachDeviceFlags(self, xml, flags):
        self.calls.append(("attach", xml, flags))


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr(
        lv,
        "libvirt",
        types.SimpleNamespace(
            libvirtError=Exception,
            VIR_DOMAIN_XML_INACTIVE=2,
            VIR_DOMAIN_AFFECT_LIVE=LIVE,
            VIR_DOMAIN_AFFECT_CONFIG=CONFIG,
            VIR_DOMAIN_VCPU_GUEST=GUEST,
        ),
    )

    def make(xml, vcpus, memory_mb, active=True):
        dom = Domain(xml, active)
        defined = []
        d = object.__new__(lv.LibvirtDriver)
        d._conn = types.SimpleNamespace(defineXML=lambda x: defined.append(x))
        d._lookup = lambda _u: dom
        spec = VMSpec(name="vm", vcpus=vcpus, memory_mb=memory_mb)
        d._info = lambda _d: VMInfo("u", spec, VMState.RUNNING if active else VMState.STOPPED)
        return d, dom, defined

    return make


def test_running_vm_gets_cpus_and_a_dimm_live(make):
    xml = build_domain_xml(VMSpec(name="vm", vcpus=2, memory_mb=4096), uuid="u")
    d, dom, defined = make(xml, 2, 4096)
    d.update_vm("u", VMEdit(vcpus=6, memory_mb=4096 + 2048))
    assert ("vcpus", 6, LIVE) in dom.calls
    [attach] = [c for c in dom.calls if c[0] == "attach"]
    assert "<size unit='MiB'>2048</size>" in attach[1] and attach[2] == LIVE | CONFIG
    saved = ET.fromstring(defined[-1])
    assert saved.find("vcpu").get("current") == "6"
    assert saved.findtext("memory") in ("4096", str(4096 * 1024))  # the DIMM adds the rest


def test_shrink_or_odd_size_waits_for_restart(make):
    xml = build_domain_xml(VMSpec(name="vm", vcpus=4, memory_mb=4096), uuid="u")
    d, dom, defined = make(xml, 4, 4096)
    d.update_vm("u", VMEdit(vcpus=2, memory_mb=4096 + 100))
    assert dom.calls == []  # nothing live: saved for the next start
    saved = ET.fromstring(defined[-1])
    assert saved.find("vcpu").get("current") == "2"
    assert saved.findtext("memory") == str((4096 + 100) * 1024)


def test_older_definition_gains_room_for_next_time(make):
    old = (
        "<domain><name>vm</name><memory unit='KiB'>2097152</memory><vcpu>2</vcpu>"
        "<cpu mode='host-model'/><devices/></domain>"
    )
    assert not hotplug_ready(ET.fromstring(old))
    d, dom, defined = make(old, 2, 2048)
    d.update_vm("u", VMEdit(vcpus=4))
    assert dom.calls == []  # not hot-pluggable yet: applies after a restart
    saved = ET.fromstring(defined[-1])
    assert hotplug_ready(saved) and saved.find("vcpu").get("current") == "4"


def test_make_hotplug_ready_keeps_values():
    root = ET.fromstring("<domain><memory unit='MiB'>1024</memory><vcpu>3</vcpu></domain>")
    make_hotplug_ready(root, 3, 1024)
    assert (
        root.find("vcpu").get("current") == "3" and root.find("cpu/numa/cell").get("cpus") == "0-31"
    )
