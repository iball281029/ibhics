"""A shared-storage handover preserves identity and fences uncertain completion."""

import pytest
from test_container_ownership import nodes

from ibhics.hypervisor import InvalidState
from ibhics.hypervisor import container_migration as migration
from ibhics.hypervisor.containers import ContainerSpec, LxcContainers


def prepare(tmp_path, monkeypatch, running=True):
    first, second, states, calls = nodes(tmp_path, monkeypatch)
    first.create(ContainerSpec("app", pool="pool"))
    first.action("app", "start" if running else "stop")
    # Test runner uses one known include; don't depend on the developer Mac having LXC.
    home = tmp_path / "pool/containers/app"
    (home / "config").write_text("")
    return first, second, states, home


def test_handover_keeps_data_mac_and_only_one_owner(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    before = first.get("app")
    (home / "snaps/proof").mkdir(parents=True)
    original = (home / "rootfs.img").read_bytes()
    receipt = first.migration("prepare", "app", {"node": "node2", "allow_stop": True})
    assert first.get("app").migration_pending
    with pytest.raises(InvalidState, match="fenced"):
        first.action("app", "start")
    assert second.migration("probe", "app", receipt)["ready"]
    first.migration("commit", "app", receipt)
    assert first.list() == [] and len(second.list()) == 1
    with pytest.raises(InvalidState, match="Ownership changed"):
        first.migration("abort", "app", receipt)
    result = second.migration("finish", "app", receipt)
    assert result["state"] == "running" and result["mac"] == before.spec.mac
    assert not result["migration_pending"] and (home / "rootfs.img").read_bytes() == original
    assert (home / "snaps/proof").is_dir()
    assert states["node1"]["app"] == "STOPPED" and states["node2"]["app"] == "RUNNING"


def test_private_storage_cannot_pass_shared_receipt_and_abort_restarts_source(
    tmp_path, monkeypatch
):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    receipt = first.migration("prepare", "app", {"node": "node2", "allow_stop": True})
    other = LxcContainers(
        lambda p: str(tmp_path / "different" / p),
        lambda n: "br0",
        first._run,
        lambda: ["pool"],
        node_name="node2",
    )
    with pytest.raises(InvalidState, match="Shared storage"):
        other.migration("probe", "app", receipt)
    first.migration("abort", "app", receipt)
    assert first.get("app").state == "running" and second.list() == []


def test_stopped_guest_stays_stopped_and_interrupted_prepare_can_recover(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch, running=False)
    receipt = first.migration("prepare", "app", {"node": "node2"})
    first.migration("recover", "app", {})
    assert first.get("app").state == "stopped" and not (home / migration.MARKER).exists()
    receipt = first.migration("prepare", "app", {"node": "node2"})
    second.migration("probe", "app", receipt)
    first.migration("commit", "app", receipt)
    assert second.migration("recover", "app", {})["state"] == "stopped"


def test_incomplete_owner_commit_can_recover_safely_on_source(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    receipt = first.migration("prepare", "app", {"node": "node2", "allow_stop": True})
    second.migration("probe", "app", receipt)
    original = migration.atomic

    def fail_owner(path, data):
        if path.name == ".ibhics-owner.json":
            raise OSError("write interrupted")
        original(path, data)

    monkeypatch.setattr(migration, "atomic", fail_owner)
    with pytest.raises(OSError):
        first.migration("commit", "app", receipt)
    assert len(first.list()) == 1 and second.list() == []
    monkeypatch.setattr(migration, "atomic", original)
    first.migration("commit", "app", receipt)
    assert second.migration("recover", "app", {})["state"] == "running"


def test_running_guest_requires_explicit_stop_permission(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    with pytest.raises(InvalidState, match="temporary stop"):
        first.migration("prepare", "app", {"node": "node2"})
    assert first.get("app").state == "running"
    assert not (home / migration.MARKER).exists()


def test_manager_migration_route_and_recovery_keep_inventory_single(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from ibhics.cluster import Cluster
    from ibhics.hypervisor.fake import FakeDriver
    from ibhics.manager import create_app

    first, second, states, home = prepare(tmp_path, monkeypatch)
    cluster = Cluster()
    for name, backend in [("node1", first), ("node2", second)]:
        driver = FakeDriver()
        monkeypatch.setattr(driver, "containers", lambda backend=backend: backend)
        cluster.add_local(name, driver)
    client = TestClient(create_app(cluster))
    assert client.post("/api/v1/containers/app/migrate", json={"node": "node2"}).status_code == 409
    result = client.post(
        "/api/v1/containers/app/migrate", json={"node": "node2", "allow_stop": True}
    )
    assert result.status_code == 200, result.text
    assert result.json()["node"] == "node2"
    inventory = client.get("/api/v1/containers").json()
    assert len(inventory) == 1 and inventory[0]["node"] == "node2"
    assert not inventory[0]["migration_pending"]
    assert (
        client.post(
            "/api/v1/containers/app/migrate", json={"node": "node2", "allow_stop": True}
        ).status_code
        == 409
    )
