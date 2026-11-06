"""Editing a VM's settings: through the API on the fake driver, and the libvirt XML."""

import pytest
from fastapi.testclient import TestClient

from ibhics.hypervisor import DiskEdit, DiskSpec, NicEdit, NicSpec, VMEdit, VMSpec
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app


def make(client, **kw):
    body = {"name": "app", "disks": [{"size_gb": 10}], "nics": [{"network": "default"}], **kw}
    return client.post("/api/v1/vms", json=body).json()


def test_edit_cpu_memory_boot_and_options():
    client = TestClient(create_app(FakeDriver()))
    vm = make(client)
    resp = client.patch(
        f"/api/v1/vms/{vm['uuid']}",
        json={
            "vcpus": 4,
            "memory_mb": 8192,
            "boot": ["cdrom", "hd"],
            "autostart": True,
            "description": "web tier",
            "os_type": "Ubuntu 24.04",
        },
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["vcpus"], out["memory_mb"], out["boot"], out["autostart"]) == (
        4,
        8192,
        ["cdrom", "hd"],
        True,
    )
    assert out["description"] == "web tier" and out["restart_needed"] is False


def test_running_vm_saves_cpu_for_next_start():
    client = TestClient(create_app(FakeDriver()))
    vm = make(client)
    client.post(f"/api/v1/vms/{vm['uuid']}/start")
    out = client.patch(f"/api/v1/vms/{vm['uuid']}", json={"vcpus": 2}).json()
    assert out["restart_needed"] is True
    client.post(f"/api/v1/vms/{vm['uuid']}/stop")
    assert client.get(f"/api/v1/vms/{vm['uuid']}").json()["restart_needed"] is False


def test_disks_grow_add_remove_and_nics():
    client = TestClient(create_app(FakeDriver()))
    vm = make(client, disks=[{"size_gb": 10}, {"size_gb": 5}], nics=[{}, {}])
    keep_mac = vm["nics"][1]["mac"]
    out = client.patch(
        f"/api/v1/vms/{vm['uuid']}",
        json={
            "disks": [{"index": 0, "size_gb": 20}, {"size_gb": 50}],
            "nics": [{"mac": keep_mac, "network": "lan"}, {"network": "dmz"}],
        },
    ).json()
    assert [d["size_gb"] for d in out["disks"]] == [20, 50]
    assert out["nics"][0] == {"network": "lan", "model": "virtio", "mac": keep_mac}
    assert out["nics"][1]["network"] == "dmz" and out["nics"][1]["mac"]


@pytest.mark.parametrize(
    "body",
    [
        {"disks": [{"index": 0, "size_gb": 5}]},  # shrink
        {"disks": []},
        {"disks": [{"index": 3, "size_gb": 10}]},
        {"nics": [{"mac": "52:54:00:00:00:99"}]},
        {"boot": ["floppy"]},
        {"boot": ["hd", "hd"]},
    ],
)
def test_bad_edits_are_refused(body):
    client = TestClient(create_app(FakeDriver()))
    vm = make(client)
    assert client.patch(f"/api/v1/vms/{vm['uuid']}", json=body).status_code in (400, 422)


def test_apply_to_keeps_unset_fields():
    spec = VMSpec(name="a", vcpus=2, disks=[DiskSpec(size_gb=10)], nics=[NicSpec(mac="m")])
    new = VMEdit(
        memory_mb=2048, disks=[DiskEdit(index=0, size_gb=10)], nics=[NicEdit(mac="m")]
    ).apply_to(spec)
    assert (new.vcpus, new.memory_mb) == (2, 2048)
