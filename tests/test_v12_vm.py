"""v1.2: USB tablet, display adapter choice, switch a Windows VM's SATA disks to VirtIO."""

import types
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

import ibhics.hypervisor.libvirt_driver as lv
from ibhics.hypervisor import DiskSpec, VMSpec
from ibhics.hypervisor.domain_xml import META_NS, build_domain_xml
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app

API = "/api/v1"


def test_tablet_and_video_in_new_vms():
    root = ET.fromstring(build_domain_xml(VMSpec(name="a", video="vga")))
    assert root.find("devices/input[@type='tablet']").get("bus") == "usb"
    model = root.find("devices/video/model")
    assert model.get("type") == "vga" and model.get("vram") == "16384"
    assert lv._video_of(root) == "vga"
    assert (
        ET.fromstring(build_domain_xml(VMSpec(name="b"))).find("devices/video/model").get("type")
        == "virtio"
    )


def test_video_via_api_and_restart_needed():
    client = TestClient(create_app(FakeDriver()))
    vm = client.post(
        f"{API}/vms", json={"name": "w12", "video": "vga", "disks": [{"size_gb": 1}]}
    ).json()
    assert vm["video"] == "vga"
    assert client.post(f"{API}/vms", json={"name": "x", "video": "matrox"}).status_code == 422
    client.post(f"{API}/vms/{vm['uuid']}/start")
    r = client.patch(f"{API}/vms/{vm['uuid']}", json={"video": "virtio"}).json()
    assert r["video"] == "virtio" and r["restart_needed"]


class Conn:
    def __init__(self, dom):
        self.dom, self.defined = dom, []

    def defineXML(self, xml):
        self.defined.append(xml)
        self.dom.xml = xml


class Dom:
    def __init__(self, xml, active=False):
        self.xml, self.active = xml, active

    def XMLDesc(self, _f=0):
        return self.xml

    def isActive(self):
        return self.active


def libvirt_stub(monkeypatch):
    monkeypatch.setattr(
        lv,
        "libvirt",
        types.SimpleNamespace(libvirtError=Exception, VIR_DOMAIN_XML_INACTIVE=2),
    )


def test_old_vms_get_the_tablet_when_they_start(monkeypatch):
    libvirt_stub(monkeypatch)
    xml = build_domain_xml(VMSpec(name="old", disks=[DiskSpec(size_gb=1)]))
    root = ET.fromstring(xml)
    root.find("devices").remove(root.find("devices/input"))
    dom = Dom(ET.tostring(root, encoding="unicode"))
    d = object.__new__(lv.LibvirtDriver)
    d._conn = Conn(dom)
    d._add_tablet(dom)
    assert ET.fromstring(dom.xml).find("devices/input[@type='tablet']") is not None
    d._add_tablet(dom)
    assert len(d._conn.defined) == 1  # only once


def test_switch_sata_disks_to_virtio(monkeypatch):
    libvirt_stub(monkeypatch)
    monkeypatch.setattr(lv.time, "sleep", lambda _s: None)
    disks = [DiskSpec(size_gb=40, bus="sata"), DiskSpec(size_gb=10, bus="sata")]
    spec = VMSpec(name="w2012", os_type="Windows Server 2012 R2", disks=disks)
    dom = Dom(build_domain_xml(spec))
    probes = []

    class Vol:
        def __init__(self, xml):
            self.n = ET.fromstring(xml).findtext("name")
            self.deleted = False

        def name(self):
            return self.n

        def path(self):
            return f"/pool/{self.n}"

        def delete(self, _f):
            self.deleted = True

    def create(xml, _f):
        probes.append(Vol(xml))
        return probes[-1]

    pool = types.SimpleNamespace(createXML=create, XMLDesc=lambda _f=0: "<pool type='dir'/>")
    d = object.__new__(lv.LibvirtDriver)
    d._conn = Conn(dom)
    d._lookup = lambda _u: dom
    d._pool = lambda _n: pool
    seen = {}

    def start(_u):
        seen["probe"] = ET.fromstring(dom.xml).find("devices/disk/target[@dev='vdz']") is not None
        dom.active = True

    def off(_dom, timeout):
        dom.active = False

    d.start_vm = start
    d._power_off = off
    d._wait_guest_agent = lambda _dom, timeout: None
    d._info = lambda _dom: types.SimpleNamespace(spec=lv._spec_from_xml(dom.xml))
    info = d.switch_to_virtio("u")
    assert seen["probe"]  # started once with the probe disk
    root = ET.fromstring(dom.xml)
    targets = [t.get("dev") for t in root.findall("devices/disk[@device='disk']/target")]
    buses = {t.get("bus") for t in root.findall("devices/disk[@device='disk']/target")}
    assert targets == ["vda", "vdb"] and buses == {"virtio"}
    metas = [m.get("dev") for m in root.findall(f"metadata/{{{META_NS}}}vm/{{{META_NS}}}disk")]
    assert metas == ["vda", "vdb"]
    assert probes[0].deleted and probes[0].name().endswith("-virtio-probe.qcow2")
    assert [disk.bus for disk in info.spec.disks] == ["virtio", "virtio"]


def test_switch_to_virtio_api():
    client = TestClient(create_app(FakeDriver()))
    body = {"name": "w8", "os_type": "Windows 8.1", "disks": [{"size_gb": 1, "bus": "sata"}]}
    vm = client.post(f"{API}/vms", json=body).json()
    assert vm["disks"][0]["bus"] == "sata"
    r = client.post(f"{API}/vms/{vm['uuid']}/to-virtio")
    assert r.status_code == 200 and r.json()["disks"][0]["bus"] == "virtio"
    assert client.post(f"{API}/vms/{vm['uuid']}/to-virtio").status_code == 409
