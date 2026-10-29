import pytest
from fastapi.testclient import TestClient

from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app


@pytest.fixture
def client():
    return TestClient(create_app(FakeDriver()))


def create(client, name="web-01", **extra):
    body = {"name": name, "vcpus": 2, "memory_mb": 2048, "disks": [{"size_gb": 40}], **extra}
    return client.post("/api/v1/vms", json=body)


def test_health(client):
    assert client.get("/api/v1/health").json()["status"] == "ok"


def test_host(client):
    host = client.get("/api/v1/host").json()
    assert host["hypervisor"] == "fake"
    assert host["cpus"] > 0


def test_console_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ibHiCS Console" in resp.text


def test_vm_lifecycle(client):
    resp = create(client)
    assert resp.status_code == 201
    vm = resp.json()
    assert vm["state"] == "stopped"
    assert vm["disks"] == [{"size_gb": 40, "backend": "file", "pool": "default"}]

    assert client.post(f"/api/v1/vms/{vm['uuid']}/start").json()["state"] == "running"
    # Running VMs must be stopped before delete.
    assert client.delete(f"/api/v1/vms/{vm['uuid']}").status_code == 409

    stopped = client.post(f"/api/v1/vms/{vm['uuid']}/stop", json={"force": True}).json()
    assert stopped["state"] == "stopped"
    assert client.delete(f"/api/v1/vms/{vm['uuid']}").status_code == 204
    assert client.get(f"/api/v1/vms/{vm['uuid']}").status_code == 404


def test_duplicate_name_conflicts(client):
    assert create(client).status_code == 201
    assert create(client).status_code == 409


def test_list_vms(client):
    create(client, "a")
    create(client, "b")
    names = sorted(vm["name"] for vm in client.get("/api/v1/vms").json())
    assert names == ["a", "b"]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "bad name"},
        {"name": "ok", "vcpus": 0},
        {"name": "ok", "memory_mb": 64},
        {"name": "ok", "disks": [{"size_gb": 0}]},
        {"name": "ok", "disks": [{"size_gb": 10, "backend": "nfs"}]},
    ],
)
def test_invalid_input_rejected(client, body):
    assert client.post("/api/v1/vms", json=body).status_code == 422


def test_start_twice_conflicts(client):
    uuid = create(client).json()["uuid"]
    client.post(f"/api/v1/vms/{uuid}/start")
    assert client.post(f"/api/v1/vms/{uuid}/start").status_code == 409


def test_unknown_vm(client):
    assert client.post("/api/v1/vms/does-not-exist/start").status_code == 404
