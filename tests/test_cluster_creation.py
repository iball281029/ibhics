from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from ibhics.cluster import Cluster
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.auth import Auth, Role


def test_create_cluster_keeps_this_node_and_vms_and_survives_restart(tmp_path):
    driver = FakeDriver()
    path = tmp_path / "cluster.json"
    cluster = Cluster.standalone("node1", driver, state_path=path)
    cluster.set_display_name("node1", "ibhics-Node1")
    client = TestClient(create_app(cluster))
    vm = client.post("/api/v1/vms", json={"name": "keep-me"}).json()
    result = client.post("/api/v1/cluster/create", json={"name": "ibHiCS Lab"})
    assert result.status_code == 201, result.text
    data = result.json()
    UUID(data["cluster_id"])
    assert data["name"] == "ibHiCS Lab"
    assert [m["name"] for m in data["members"]] == ["node1"]
    assert not data["ha_capable"]
    assert client.get("/api/v1/vms").json()[0]["uuid"] == vm["uuid"]
    restored = Cluster.standalone("node1", driver, state_path=path)
    restored.load()
    assert restored.cluster_id == data["cluster_id"]
    assert restored.cluster_name == "ibHiCS Lab"
    assert restored.members()[0].display_name == "ibhics-Node1"
    assert client.post("/api/v1/cluster/create", json={"name": "Other"}).status_code == 400
    assert cluster.cluster_name == "ibHiCS Lab"


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, "cluster\nname"])
def test_invalid_name_does_not_create_cluster(name):
    cluster = Cluster.standalone("node1", FakeDriver())
    client = TestClient(create_app(cluster))
    assert client.post("/api/v1/cluster/create", json={"name": name}).status_code in (400, 422)
    assert not cluster.cluster_id


def test_save_failure_does_not_leave_cluster_created(monkeypatch):
    cluster = Cluster.standalone("node1", FakeDriver())

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(cluster, "_save", fail)
    with pytest.raises(OSError):
        cluster.create_cluster("test")
    assert not cluster.cluster_id
    assert not cluster.cluster_name


def test_only_admin_can_create_cluster(tmp_path):
    auth = Auth(state_path=tmp_path / "users.json")
    auth.bootstrap("admin-test-password", must_change=False)
    auth.add_user("reader", Role.VIEWER, "reader-test-password")
    auth.add_user("operator", Role.OPERATOR, "operator-test-password")
    cluster = Cluster.standalone("node1", FakeDriver())
    client = TestClient(create_app(cluster, auth=auth))
    assert client.post("/api/v1/cluster/create", json={"name": "test"}).status_code == 401
    for name in ("reader", "operator"):
        login = client.post(
            "/api/v1/auth/login", json={"username": name, "password": name + "-test-password"}
        )
        assert login.status_code == 200
        assert client.post("/api/v1/cluster/create", json={"name": "test"}).status_code == 403
    client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-test-password"})
    assert client.post("/api/v1/cluster/create", json={"name": "test"}).status_code == 201


def test_independently_installed_node_uses_own_pairing_key_and_persists(tmp_path):
    from ibhics.agent import create_agent_app
    from ibhics.cluster import AgentClient, NodeRole

    remote_app = create_agent_app("node2", NodeRole.COMPUTE, "independent-agent-key", FakeDriver())

    def factory(address, token):
        return AgentClient(address, token, http=TestClient(remote_app, base_url=address))

    path = tmp_path / "cluster.json"
    cluster = Cluster.standalone(
        "node1", FakeDriver(), token="primary-key", state_path=path, client_factory=factory
    )
    client = TestClient(create_app(cluster))
    client.post("/api/v1/cluster/create", json={"name": "Lab"})
    request = {
        "name": "node2",
        "role": "compute",
        "address": "http://node2:8007",
        "agent_token": "independent-agent-key",
    }
    added = client.post("/api/v1/cluster/nodes", json=request)
    assert added.status_code == 201, added.text
    assert "independent-agent-key" not in added.text
    assert "independent-agent-key" not in client.get("/api/v1/cluster").text
    assert path.stat().st_mode & 0o777 == 0o600
    restored = Cluster.standalone(
        "node1", FakeDriver(), token="primary-key", state_path=path, client_factory=factory
    )
    restored.load()
    restored.refresh()
    assert restored.member("node2").client.health()["name"] == "node2"


def test_pairing_key_is_admin_only_and_standalone_only(tmp_path):
    auth = Auth(state_path=tmp_path / "users.json")
    auth.bootstrap("admin-test-password", must_change=False)
    auth.add_user("reader", Role.VIEWER, "reader-test-password")
    cluster = Cluster.standalone("node2", FakeDriver(), token="private-agent-key")
    client = TestClient(create_app(cluster, auth=auth))
    assert client.get("/api/v1/cluster/pairing").status_code == 401
    client.post(
        "/api/v1/auth/login", json={"username": "reader", "password": "reader-test-password"}
    )
    assert client.get("/api/v1/cluster/pairing").status_code == 403
    client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-test-password"})
    response = client.get("/api/v1/cluster/pairing")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["agent_token"] == "private-agent-key"
    assert "private-agent-key" not in client.get("/api/v1/cluster").text
    cluster.create_cluster("other")
    assert client.get("/api/v1/cluster/pairing").status_code == 400
