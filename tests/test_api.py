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


def test_standalone_cluster(client):
    status = client.get("/api/v1/cluster").json()
    assert status["topology"] == "standalone"
    assert status["quorum"] is True
    assert status["ha_capable"] is False
    (member,) = status["members"]
    assert member["local"] and member["healthy"]

    host = client.get(f"/api/v1/nodes/{member['name']}/host").json()
    assert host["hypervisor"] == "fake"


def test_console_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<title>ibHiCS</title>" in resp.text
    for asset in ("app.js", "app.css", "login.html", "logo.svg"):
        assert client.get(f"/{asset}").status_code == 200


def test_vm_lifecycle(client):
    resp = create(client)
    assert resp.status_code == 201
    vm = resp.json()
    assert vm["state"] == "stopped"
    assert vm["disks"] == [{"size_gb": 40, "backend": "file", "pool": "default", "used_gb": None}]

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


def test_iso_library(tmp_path):
    client = TestClient(create_app(FakeDriver(), iso_dir=tmp_path / "iso"))
    assert client.get("/api/v1/isos").json() == []  # the folder appears with the first upload

    resp = client.put("/api/v1/isos/debian-13.0.0-amd64-netinst.iso", content=b"x" * 10)
    assert resp.status_code == 200, resp.text
    path = str(tmp_path / "iso" / "debian-13.0.0-amd64-netinst.iso")
    name = "debian-13.0.0-amd64-netinst.iso"
    assert resp.json() == {"name": name, "path": path, "size_bytes": 10}
    assert [i["path"] for i in client.get("/api/v1/isos").json()] == [path]
    for bad in ("x.exe", ".hidden.iso", "a$b.iso"):
        assert client.put(f"/api/v1/isos/{bad}", content=b"x").status_code == 422, bad

    vm = create(client, iso=path).json()
    resp = client.delete("/api/v1/isos/debian-13.0.0-amd64-netinst.iso")
    assert resp.status_code == 422 and "web-01" in resp.json()["detail"]
    client.post(f"/api/v1/vms/{vm['uuid']}/cdrom", json={"iso": None})
    assert client.delete("/api/v1/isos/debian-13.0.0-amd64-netinst.iso").status_code == 204
    assert client.delete("/api/v1/isos/debian-13.0.0-amd64-netinst.iso").status_code == 404
