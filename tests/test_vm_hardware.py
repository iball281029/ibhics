"""Advanced hardware and CPU topology across API, XML and remote import boundaries."""

import types
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient
from test_import_relay import TOKEN, VM, add_shared
from test_import_relay import env as relay_env  # noqa: F401

from ibhics.cluster.remote import AgentClient, RemoteImport
from ibhics.hypervisor import Firmware, InvalidState, VMEdit, VMInfo, VMSpec, VMState
from ibhics.hypervisor import libvirt_driver as lv
from ibhics.hypervisor.base import ADVANCED_FIELDS
from ibhics.hypervisor.domain_xml import build_domain_xml
from ibhics.hypervisor.fake import FakeDriver
from ibhics.hypervisor.serialize import spec_from_json, spec_to_json
from ibhics.importer import Source, SourceType
from ibhics.manager import create_app

env = relay_env


@pytest.mark.parametrize("machine", ["q35", "i440fx"])
@pytest.mark.parametrize("enabled", [False, True])
def test_xml_and_json_round_trip(machine, enabled):
    spec = VMSpec(
        name="hardware",
        vcpus=6,
        cpu_sockets=2,
        cpu_cores=3,
        cpu_mode="host-passthrough",
        mouse="ps2",
        keymap="de",
        boot_menu_seconds=7,
        restart_on_crash=enabled,
        ballooning=enabled,
        serial_port=enabled,
        usb_controller=enabled,
        machine_type=machine,
        tpm=enabled,
        firmware=Firmware.UEFI,
        secure_boot=enabled and machine == "q35",
        video="qxl",
    )
    xml = build_domain_xml(spec)
    parsed = lv._spec_from_xml(xml)
    for key in (*ADVANCED_FIELDS, "machine_type", "secure_boot", "video", "vcpus"):
        assert getattr(parsed, key) == getattr(spec, key), key
        assert getattr(spec_from_json(spec_to_json(spec)), key) == getattr(spec, key), key
    root = ET.fromstring(xml)
    assert root.findtext("vcpu") == "6"
    assert root.find("cpu/topology").attrib == {"sockets": "2", "cores": "3", "threads": "1"}
    assert root.find("cpu/numa/cell").get("cpus") == "0-5"
    assert root.find("os/bootmenu").get("timeout") == "7000"
    assert (root.find("devices/console") is not None) == enabled


def test_create_edit_and_running_guard():
    driver = FakeDriver(demo_console=False)
    client = TestClient(create_app(driver))
    created = client.post(
        "/api/v1/vms",
        json={
            "name": "hw",
            "vcpus": 4,
            "cpu_sockets": 2,
            "cpu_cores": 2,
            "machine_type": "i440fx",
            "cpu_mode": "host-passthrough",
            "autostart": True,
            "mouse": "ps2",
            "keymap": "fr",
            "ballooning": False,
            "video": "cirrus",
        },
    )
    assert created.status_code == 201, created.text
    vm = created.json()
    assert vm["cpu_sockets"] == 2 and vm["autostart"] and not vm["ballooning"]
    url = f"/api/v1/vms/{vm['uuid']}"
    changed = client.patch(url, json={"vcpus": 6, "cpu_cores": 3, "serial_port": False})
    assert changed.status_code == 200, changed.text
    assert changed.json()["cpu_cores"] == 3 and not changed.json()["serial_port"]
    client.post(url + "/start")
    assert client.patch(url, json={"mouse": "tablet"}).status_code == 409
    assert client.patch(url, json={"description": "unchanged hardware"}).status_code == 200
    assert client.get(url).json()["machine_type"] == "i440fx"


@pytest.mark.parametrize(
    "body",
    [
        {"vcpus": 4, "cpu_sockets": 2},
        {"vcpus": 4, "cpu_sockets": 2, "cpu_cores": 3},
        {"cpu_mode": "custom"},
        {"mouse": "unknown"},
        {"boot_menu_seconds": -1},
        {"tpm": True},
        {"secure_boot": True, "firmware": "uefi"},
        {"keymap": "th"},
    ],
)
def test_invalid_or_unavailable_hardware_is_rejected(body):
    client = TestClient(create_app(FakeDriver(demo_console=False)))
    response = client.post("/api/v1/vms", json={"name": "invalid", **body})
    assert response.status_code == 422, response.text
    assert client.get("/api/v1/vms").json() == []


def test_available_tpm_and_secure_boot_in_api(monkeypatch):
    monkeypatch.setattr(
        FakeDriver,
        "hardware_capabilities",
        lambda self: {
            "tpm": True,
            "secure_boot": True,
            "keymaps": ["default", "th"],
        },
    )
    client = TestClient(create_app(FakeDriver(demo_console=False)))
    response = client.post(
        "/api/v1/vms",
        json={
            "name": "secure",
            "firmware": "uefi",
            "secure_boot": True,
            "tpm": True,
            "keymap": "th",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["secure_boot"] and response.json()["tpm"]
    url = f"/api/v1/vms/{response.json()['uuid']}"
    assert client.patch(url, json={"tpm": False}).json()["tpm"] is False


@pytest.mark.parametrize("route", ["local", "remote", "relay"])
def test_migrate_cpu_topology_all_routes(env, route):
    client, _, _, node2 = env
    if route == "relay":
        add_shared(client)
        node2.unreachable = {"10.9.9.9"}
    response = client.post(
        "/api/v1/migrate/jobs",
        json={
            "source": "pve",
            "vm": "101",
            "node": "node1" if route == "local" else "node2",
            "pool": "shared" if route == "relay" else "default",
            "vcpus": 6,
            "cpu_sockets": 2,
            "cpu_cores": 3,
        },
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["state"] == "done", job
    vm = client.get(f"/api/v1/vms/{job['result']}").json()
    assert (vm["vcpus"], vm["cpu_sockets"], vm["cpu_cores"]) == (6, 2, 3)
    assert bool(job["via"]) == (route == "relay")


def test_migrate_rejects_bad_cpu_product_before_job(env):
    client, *_ = env
    response = client.post(
        "/api/v1/migrate/jobs",
        json={
            "source": "pve",
            "vm": "101",
            "vcpus": 2,
            "cpu_sockets": 2,
            "cpu_cores": 3,
        },
    )
    assert response.status_code == 422
    assert client.get("/api/v1/migrate/jobs").json() == []


def test_old_agent_rejects_topology_without_starting_import():
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"disk_targets": True, "nic_targets": True})

    remote = RemoteImport(
        AgentClient(
            "http://old:8007",
            TOKEN,
            http=httpx.Client(base_url="http://old:8007", transport=httpx.MockTransport(respond)),
        )
    )
    with pytest.raises(InvalidState, match="does not support CPU topology"):
        remote.run(
            Source("s", SourceType.KVM, host="host"), VM, "vm", vcpus=2, cpu_sockets=1, cpu_cores=2
        )
    assert calls == ["/agent/v1/imports/capabilities"]


def test_libvirt_offline_edit_preserves_unrelated_xml(monkeypatch):
    monkeypatch.setattr(lv, "libvirt", types.SimpleNamespace(VIR_DOMAIN_XML_INACTIVE=2))
    spec = VMSpec(name="vm", vcpus=2, cpu_sockets=1, cpu_cores=2)
    root = ET.fromstring(build_domain_xml(spec))
    ET.SubElement(root.find("devices"), "controller", type="scsi", model="virtio-scsi")
    xml = ET.tostring(root, encoding="unicode")
    dom = types.SimpleNamespace(isActive=lambda: False, XMLDesc=lambda flags: xml)
    driver = object.__new__(lv.LibvirtDriver)
    defined = []
    driver._lookup = lambda uuid: dom
    driver._info = lambda domain: VMInfo("uuid", spec, VMState.STOPPED)
    driver._conn = types.SimpleNamespace(defineXML=defined.append)
    driver.update_vm(
        "uuid",
        VMEdit(
            vcpus=4, cpu_sockets=2, cpu_cores=2, keymap="de", serial_port=False, ballooning=False
        ),
    )
    parsed = lv._spec_from_xml(defined[0])
    assert (parsed.vcpus, parsed.cpu_sockets, parsed.cpu_cores) == (4, 2, 2)
    assert parsed.keymap == "de" and not parsed.ballooning and not parsed.serial_port
    assert ET.fromstring(defined[0]).find("devices/controller[@type='scsi']") is not None


def test_legacy_capacity_topology_does_not_break_unrelated_edit():
    root = ET.fromstring(build_domain_xml(VMSpec(name="legacy", vcpus=2)))
    ET.SubElement(root.find("cpu"), "topology", sockets="1", cores="32", threads="1")
    parsed = lv._spec_from_xml(ET.tostring(root, encoding="unicode"))
    assert parsed.cpu_sockets is None
    assert VMEdit(description="keep legacy").apply_to(parsed).vcpus == 2


def test_node_hardware_capabilities_local_and_remote(env):
    client, *_ = env
    for name in ("node1", "node2"):
        response = client.get(f"/api/v1/nodes/{name}/hardware-capabilities")
        assert response.status_code == 200
        assert response.json()["tpm"] is False
        assert response.json()["secure_boot"] is False
        assert "th" not in response.json()["keymaps"]


def test_host_cpu_mode_edit_removes_old_model_and_keeps_numa():
    from ibhics.hypervisor.domain_xml import apply_hardware_xml

    root = ET.fromstring(build_domain_xml(VMSpec(name="vm")))
    cpu = root.find("cpu")
    cpu.set("match", "exact")
    ET.SubElement(cpu, "model", fallback="allow").text = "old-model"
    ET.SubElement(cpu, "feature", policy="require", name="old-feature")
    apply_hardware_xml(root, VMSpec(name="vm", cpu_mode="host-passthrough"), ("cpu_mode",))
    assert cpu.get("mode") == "host-passthrough" and cpu.get("match") is None
    assert cpu.find("model") is None and cpu.find("feature") is None
    assert cpu.find("numa/cell") is not None
