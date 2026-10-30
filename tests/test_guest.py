"""VM inventory: the admin's OS label and what the guest agent reports."""

import types

from fastapi.testclient import TestClient

from ibhics.hypervisor import DiskSpec, NicSpec, VMSpec
from ibhics.hypervisor import libvirt_driver as lv
from ibhics.hypervisor.domain_xml import build_domain_xml
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app


def test_os_type_round_trips_through_domain_xml():
    spec = VMSpec(name="dc01", vcpus=2, memory_mb=4096, disks=[DiskSpec(size_gb=60)])
    spec.os_type = "Windows Server 2022"
    xml = build_domain_xml(spec)
    assert "https://ibhics.org/xmlns/vm/1" in xml
    assert lv._spec_from_xml(xml).os_type == "Windows Server 2022"
    spec.os_type = ""
    assert "metadata" not in build_domain_xml(spec)


def test_fake_guest_agent_only_answers_when_running():
    driver = FakeDriver()
    vm = driver.create_vm(VMSpec(name="web-01", vcpus=1, memory_mb=1024, nics=[NicSpec()]))
    assert driver.guest_info(vm.uuid).agent is False
    driver.start_vm(vm.uuid)
    guest = driver.guest_info(vm.uuid)
    assert guest.agent and guest.hostname == "web-01" and guest.os_name.startswith("Ubuntu")
    assert guest.interfaces[0].mac == driver.get_vm(vm.uuid).spec.nics[0].mac
    assert len(guest.ips) == 1 and "/" not in guest.ips[0]
    assert set(driver.list_guest_info()) == {vm.uuid}


class LibvirtError(Exception):
    pass


class Domain:
    def __init__(self, agent):
        self.agent = agent

    def isActive(self):
        return True

    def guestInfo(self, types_, flags):
        if not self.agent:
            raise LibvirtError("QEMU guest agent is not connected")
        return {
            "os.pretty-name": "Rocky Linux 9.4",
            "os.version": "9.4",
            "os.kernel-release": "5.14.0",
            "hostname": "db01",
        }

    def interfaceAddresses(self, source, flags):
        if source == 1 and not self.agent:
            raise LibvirtError("no agent")
        return {
            "lo": {"hwaddr": "00:00:00:00:00:00", "addrs": [{"addr": "127.0.0.1", "prefix": 8}]},
            "eth0": {
                "hwaddr": "52:54:00:aa:bb:cc",
                "addrs": [{"addr": "10.0.0.5", "prefix": 24, "type": 0}],
            },
        }


def libvirt_driver(monkeypatch, agent):
    fake = types.SimpleNamespace(
        libvirtError=LibvirtError,
        VIR_DOMAIN_GUEST_INFO_OS=2,
        VIR_DOMAIN_GUEST_INFO_HOSTNAME=16,
        VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE=0,
        VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT=1,
    )
    monkeypatch.setattr(lv, "libvirt", fake)
    d = object.__new__(lv.LibvirtDriver)
    d._conn = types.SimpleNamespace(lookupByUUIDString=lambda _u: Domain(agent))
    return d


def test_libvirt_guest_info_with_agent(monkeypatch):
    guest = libvirt_driver(monkeypatch, agent=True).guest_info("u")
    assert (guest.agent, guest.os_name, guest.hostname) == (True, "Rocky Linux 9.4", "db01")
    assert [(i.name, i.mac, i.ips) for i in guest.interfaces] == [
        ("eth0", "52:54:00:aa:bb:cc", ["10.0.0.5/24"])
    ]


def test_libvirt_guest_info_without_agent_uses_leases(monkeypatch):
    guest = libvirt_driver(monkeypatch, agent=False).guest_info("u")
    assert guest.agent is False and guest.os_name == ""
    assert guest.ips == ["10.0.0.5"]


def test_inventory_api():
    client = TestClient(create_app(FakeDriver()))
    body = {
        "name": "win-app",
        "vcpus": 2,
        "memory_mb": 4096,
        "os_type": "Windows Server 2019",
        "disks": [{"size_gb": 40}],
        "nics": [{"network": "default"}],
    }
    vm = client.post("/api/v1/vms", json=body).json()
    assert vm["os_type"] == "Windows Server 2019"
    assert client.get("/api/v1/vms/guests").json() == []
    client.post(f"/api/v1/vms/{vm['uuid']}/start")
    (guest,) = client.get("/api/v1/vms/guests").json()
    assert guest["uuid"] == vm["uuid"] and guest["os_name"] == "Windows Server 2019"
    assert guest["hostname"] == "WIN-APP"
    single = client.get(f"/api/v1/vms/{vm['uuid']}/guest").json()
    assert single["ips"] == guest["ips"]
    assert client.get("/api/v1/vms/nope/guest").status_code == 404
