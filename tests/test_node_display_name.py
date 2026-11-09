"""Node labels are persistent metadata, never membership keys or Linux hostnames."""

import json

import pytest
from fastapi.testclient import TestClient

from ibhics.cluster import Cluster
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.auth import Auth, Role


def test_display_name_survives_restart_without_changing_vm_references(tmp_path):
    path = tmp_path / "cluster.json"
    driver = FakeDriver()
    cluster = Cluster.standalone("node1", driver, state_path=path)
    client = TestClient(create_app(cluster))
    vm = client.post("/api/v1/vms", json={"name": "web"}).json()
    settings_before = client.get("/api/v1/nodes/node1/settings").json()
    result = client.patch(
        "/api/v1/cluster/nodes/node1", json={"display_name": "  เซิร์ฟเวอร์ Production 01  "}
    )
    assert result.status_code == 200, result.text
    assert result.json()["name"] == "node1"
    assert result.json()["display_name"] == "เซิร์ฟเวอร์ Production 01"
    assert client.get(f"/api/v1/vms/{vm['uuid']}").json()["node"] == "node1"
    assert (
        client.get("/api/v1/nodes/node1/settings").json()["hostname"] == settings_before["hostname"]
    )
    assert json.loads(path.read_text())["display_names"] == {"node1": "เซิร์ฟเวอร์ Production 01"}
    restored = Cluster.standalone("node1", driver, state_path=path)
    restored.load()
    assert restored.member("node1").display_name == "เซิร์ฟเวอร์ Production 01"
    client = TestClient(create_app(restored))
    assert (
        client.get("/api/v1/cluster").json()["members"][0]["display_name"]
        == "เซิร์ฟเวอร์ Production 01"
    )
    assert (
        client.patch("/api/v1/cluster/nodes/node1", json={"display_name": ""}).json()[
            "display_name"
        ]
        == "node1"
    )
    assert json.loads(path.read_text())["display_names"] == {}


@pytest.mark.parametrize("label", ["a" * 81, "a\nb", "a\x00b", "a\u202eb"])
def test_invalid_display_name_is_rejected(label):
    client = TestClient(create_app(Cluster.standalone("node1", FakeDriver())))
    assert client.patch(
        "/api/v1/cluster/nodes/node1", json={"display_name": label}
    ).status_code in (400, 422)
    assert client.get("/api/v1/cluster").json()["members"][0]["display_name"] == "node1"


def test_unknown_node_and_legacy_state(tmp_path):
    path = tmp_path / "cluster.json"
    path.write_text('{"members": [], "maintenance": []}')
    cluster = Cluster.standalone("node1", FakeDriver(), state_path=path)
    cluster.load()
    client = TestClient(create_app(cluster))
    assert client.get("/api/v1/cluster").json()["members"][0]["display_name"] == "node1"
    assert (
        client.patch("/api/v1/cluster/nodes/missing", json={"display_name": "new"}).status_code
        == 400
    )


def test_failed_save_keeps_previous_label(tmp_path, monkeypatch):
    cluster = Cluster.standalone("node1", FakeDriver(), state_path=tmp_path / "cluster.json")
    cluster.set_display_name("node1", "Original")

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(cluster, "_save", fail_save)
    with pytest.raises(OSError):
        cluster.set_display_name("node1", "New")
    assert cluster.member("node1").display_name == "Original"


@pytest.mark.parametrize(
    "role, expected", [(Role.VIEWER, 403), (Role.OPERATOR, 403), (Role.ADMIN, 200)]
)
def test_display_name_requires_admin(role, expected):
    auth = Auth()
    auth.add_user("review-user", role, "Review-password-1")
    auth.change_password("review-user", "Review-password-1", "Review-password-2")
    client = TestClient(create_app(Cluster.standalone("node1", FakeDriver()), auth=auth))
    login = client.post(
        "/api/v1/auth/login", json={"username": "review-user", "password": "Review-password-2"}
    )
    assert login.status_code == 200
    assert (
        client.patch("/api/v1/cluster/nodes/node1", json={"display_name": "New"}).status_code
        == expected
    )
