"""Per-NIC import targets, without contacting a hypervisor or source host."""

from dataclasses import replace

import httpx
import pytest
from test_import_relay import TOKEN, VM, add_shared
from test_import_relay import env as relay_env  # noqa: F401

from ibhics.cluster.remote import AgentClient, RemoteImport
from ibhics.hypervisor import InvalidState, NicSpec, VMSpec
from ibhics.hypervisor.fake import FakeDriver
from ibhics.importer import NodeImport, Source, SourceType
from ibhics.importer.convert import FakeConverter

env = relay_env
SOURCE_MAC = "52:54:00:11:22:33"
CUSTOM_MAC = "52:54:00:aa:bb:cc"


@pytest.fixture
def converted_macs(monkeypatch):
    original = FakeConverter.convert

    def convert(self, source, vm, *args, **kwargs):
        result = original(self, source, vm, *args, **kwargs)
        result.macs = list(vm.macs)
        return result

    monkeypatch.setattr(FakeConverter, "convert", convert)


@pytest.mark.parametrize("route", ["local", "remote", "relay"])
@pytest.mark.parametrize("mode", ["keep", "new", "custom"])
def test_api_nics_all_routes(env, converted_macs, route, mode):
    client, imports, _, node2 = env
    imports._discover = lambda s: [replace(VM, macs=[SOURCE_MAC])]
    assert client.post("/api/v1/networks", json={"name": "vlan-20", "vlan": 20}).status_code == 201
    if route == "relay":
        add_shared(client)
        node2.unreachable = {"10.9.9.9"}
    target = {
        "network": "vlan-20",
        "mac_mode": mode,
        "mac": CUSTOM_MAC if mode == "custom" else None,
    }
    response = client.post(
        "/api/v1/migrate/jobs",
        json={
            "source": "pve",
            "vm": "101",
            "node": "node1" if route == "local" else "node2",
            "pool": "shared" if route == "relay" else "default",
            "nics": [target],
        },
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["state"] == "done", job
    assert job["nics"] == [target]
    assert bool(job["via"]) == (route == "relay")
    nic = client.get(f"/api/v1/vms/{job['result']}").json()["nics"][0]
    assert nic["network"] == "vlan-20"
    if mode == "new":
        assert nic["mac"] not in (SOURCE_MAC, CUSTOM_MAC, None)
    else:
        assert nic["mac"] == (SOURCE_MAC if mode == "keep" else CUSTOM_MAC)


@pytest.mark.parametrize("mode", ["keep", "new", "custom"])
def test_node_uses_final_mac_for_linux_names(tmp_path, converted_macs, mode):
    driver = FakeDriver(demo_console=False)
    converter = FakeConverter()
    names = []
    converter.keep_nic_names = lambda images, macs: names.extend(macs)
    created = NodeImport(driver, converter, str(tmp_path)).run(
        Source("s", SourceType.OVA, path="/unused"),
        replace(VM, macs=[SOURCE_MAC], guest_os="Linux"),
        "imported",
        nics=[
            {
                "network": "default",
                "mac_mode": mode,
                "mac": CUSTOM_MAC if mode == "custom" else None,
            }
        ],
    )
    assert names == [created.spec.nics[0].mac]
    if mode != "new":
        assert names == [SOURCE_MAC if mode == "keep" else CUSTOM_MAC]
    else:
        assert names[0] != SOURCE_MAC


@pytest.mark.parametrize(
    "mac",
    [
        "bad",
        "52-54-00-11-22-33",
        "01:00:00:00:00:01",
        "ff:ff:ff:ff:ff:ff",
        "00:00:00:00:00:00",
        None,
    ],
)
def test_api_rejects_invalid_custom_mac(env, mac):
    client, *_ = env
    response = client.post(
        "/api/v1/migrate/jobs",
        json={
            "source": "pve",
            "vm": "101",
            "nics": [{"network": "default", "mac_mode": "custom", "mac": mac}],
        },
    )
    assert response.status_code == 422, response.text


def test_api_rejects_duplicate_macs_in_request(env):
    client, imports, *_ = env
    imports._discover = lambda s: [replace(VM, nics=2)]
    response = client.post(
        "/api/v1/migrate/jobs",
        json={
            "source": "pve",
            "vm": "101",
            "nics": [
                {"network": "default", "mac_mode": "custom", "mac": CUSTOM_MAC},
                {"network": "default", "mac_mode": "custom", "mac": CUSTOM_MAC.upper()},
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert not imports.jobs()


@pytest.mark.parametrize("mode", ["custom", "keep"])
def test_api_rejects_mac_used_on_other_node(env, mode):
    client, imports, _, node2 = env
    imports._discover = lambda s: [replace(VM, macs=[CUSTOM_MAC])]
    node2.create_vm(VMSpec(name="existing", nics=[NicSpec(mac=CUSTOM_MAC)]))
    response = client.post(
        "/api/v1/migrate/jobs",
        json={
            "source": "pve",
            "vm": "101",
            "node": "node1",
            "nics": [
                {
                    "network": "default",
                    "mac_mode": mode,
                    "mac": CUSTOM_MAC if mode == "custom" else None,
                }
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert not imports.jobs()


def test_pending_jobs_reserve_source_and_custom_macs(env):
    client, imports, *_ = env
    imports._discover = lambda s: [replace(VM, macs=[SOURCE_MAC])]
    imports._executor = type("Queued", (), {"submit": lambda *args: None})()
    target = {"network": "default", "mac_mode": "keep"}
    assert (
        client.post(
            "/api/v1/migrate/jobs", json={"source": "pve", "vm": "101", "nics": [target]}
        ).status_code
        == 202
    )
    target = {"network": "default", "mac_mode": "custom", "mac": SOURCE_MAC}
    response = client.post(
        "/api/v1/migrate/jobs",
        json={"source": "pve", "vm": "101", "name": "other", "nics": [target]},
    )
    assert response.status_code == 422, response.text


def test_running_source_cannot_keep_mac(env):
    client, imports, *_ = env
    imports._discover = lambda s: [replace(VM, state="running", macs=[SOURCE_MAC])]
    response = client.post(
        "/api/v1/migrate/jobs",
        json={"source": "pve", "vm": "101", "nics": [{"network": "default", "mac_mode": "keep"}]},
    )
    assert response.status_code == 422, response.text


def test_old_agent_rejects_nic_targets():
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"disk_targets": True})

    remote = RemoteImport(
        AgentClient(
            "http://old:8007",
            TOKEN,
            http=httpx.Client(base_url="http://old:8007", transport=httpx.MockTransport(respond)),
        )
    )
    with pytest.raises(InvalidState, match="does not support NIC targets; upgrade"):
        remote.run(
            Source("s", SourceType.KVM, host="host"),
            VM,
            "vm",
            nics=[{"network": "default", "mac_mode": "new"}],
        )
    assert calls == ["/agent/v1/imports/capabilities"]


@pytest.mark.parametrize("mac", ["bad", "01:00:00:00:00:01", CUSTOM_MAC])
def test_node_rejects_invalid_or_existing_mac(tmp_path, mac):
    driver = FakeDriver(demo_console=False)
    driver.create_vm(VMSpec(name="existing", nics=[NicSpec(mac=CUSTOM_MAC)]))
    with pytest.raises(ValueError, match="MAC|unicast"):
        NodeImport(driver, scratch_dir=str(tmp_path)).run(
            Source("s", SourceType.OVA, path="/unused"),
            VM,
            "new",
            nics=[{"network": "default", "mac_mode": "custom", "mac": mac}],
        )
    assert len(driver.list_vms()) == 1


def test_discovery_preserves_mac_positions():
    from ibhics.importer.sources import parse_vmware_xml

    vm = parse_vmware_xml("""<domain><name>vm</name><memory>1024</memory><devices>
      <interface><source bridge="LAN"/></interface>
      <interface><source bridge="WAN"/><mac address="52:54:00:AA:BB:CC"/></interface>
      </devices></domain>""")
    assert vm.macs == ["", CUSTOM_MAC]
    assert vm.networks == ["LAN", "WAN"]
    assert type(vm).from_dict(vm.to_dict()).macs == vm.macs


def test_legacy_request_omits_new_fields_for_old_agent():
    import json

    from ibhics.manager.schemas import VMOut

    info = FakeDriver(demo_console=False).create_vm(VMSpec(name="vm"))
    calls = []

    def respond(request):
        calls.append(request.url.path)
        body = json.loads(request.content)
        assert "nics" not in body
        assert "macs" not in body["vm"]
        assert body["network"] == ["default"]
        return httpx.Response(200, json=VMOut.from_info(info).model_dump(mode="json"))

    remote = RemoteImport(
        AgentClient(
            "http://old:8007",
            TOKEN,
            http=httpx.Client(base_url="http://old:8007", transport=httpx.MockTransport(respond)),
        )
    )
    assert (
        remote.run(
            Source("s", SourceType.KVM, host="host"),
            replace(VM, macs=[SOURCE_MAC]),
            "vm",
            network=["default"],
        ).uuid
        == info.uuid
    )
    assert calls == ["/agent/v1/imports/run"]
