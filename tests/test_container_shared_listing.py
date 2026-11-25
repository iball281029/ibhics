"""Shared container observations must resolve to their running owner, never a stopped alias."""

from copy import deepcopy

from fastapi.testclient import TestClient

from ibhics.cluster import Cluster, NodeUnavailable
from ibhics.hypervisor.containers import ContainerInfo, ContainerSpec
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app


def setup_shared():
    cluster = Cluster()
    first, second = FakeDriver(), FakeDriver()
    cluster.add_local("first", first)
    cluster.add_local("second", second)
    spec = ContainerSpec("shared", mac="00:16:3e:11:22:33")
    stopped = ContainerInfo(spec, state="stopped", path="/shared/containers/shared")
    running = deepcopy(stopped)
    running.state = "running"
    running.addresses = ["10.0.0.8"]
    first.containers()._cts["shared"] = stopped
    second.containers()._cts["shared"] = running
    return TestClient(create_app(cluster)), first.containers(), second.containers()


def test_shared_listing_and_actions_use_running_owner_not_first_stopped_alias():
    client, first, second = setup_shared()
    rows = client.get("/api/v1/containers").json()
    assert len(rows) == 1 and rows[0]["node"] == "second" and rows[0]["addresses"] == ["10.0.0.8"]
    assert client.get("/api/v1/containers/shared").json()["node"] == "second"
    assert client.delete("/api/v1/containers/shared").status_code == 409
    assert first.get("shared") and second.get("shared").state == "running"
    assert (
        client.post("/api/v1/containers/shared/action", json={"action": "stop"}).json()["node"]
        == "second"
    )
    assert second.get("shared").state == "stopped"


def test_same_name_different_local_containers_are_not_silently_merged():
    client, first, second = setup_shared()
    second.get("shared").spec.mac = "00:16:3e:44:55:66"
    assert len(client.get("/api/v1/containers").json()) == 2
    assert client.delete("/api/v1/containers/shared").status_code == 409
    assert "shared" in first._cts and "shared" in second._cts


def test_unreachable_node_prevents_destructive_operation(monkeypatch):
    client, first, second = setup_shared()

    def unavailable():
        raise NodeUnavailable("offline")

    monkeypatch.setattr(second, "list", unavailable)
    assert client.delete("/api/v1/containers/shared").status_code == 503
    assert "shared" in first._cts


def test_two_running_owners_are_rejected_without_deleting_either():
    client, first, second = setup_shared()
    first.get("shared").state = "running"
    assert client.delete("/api/v1/containers/shared").status_code == 409
    assert first.get("shared").state == second.get("shared").state == "running"


def test_delete_rechecks_state_after_waiting_for_task_lock(monkeypatch):
    client, first, second = setup_shared()
    second.get("shared").state = "stopped"

    def started_while_waiting(kind, detail, name, node, uuid, work):
        first.get("shared").state = "running"
        return work()

    monkeypatch.setattr(client.app.state.backups, "run_task", started_while_waiting)
    assert client.delete("/api/v1/containers/shared").status_code == 409
    assert "shared" in first._cts and "shared" in second._cts
