"""Storage telemetry follows its controller when the active manager moves."""

import json

from fastapi.testclient import TestClient
from test_cluster import TOKEN
from test_migration import Env

from ibhics.agent import create_agent_app
from ibhics.manager import create_app
from ibhics.storage_cluster import StorageController


def test_manager_reads_existing_storage_from_peer_without_provisioning(tmp_path, monkeypatch):
    peer = tmp_path / "peer"
    runtime = peer / "virtual-storage-runtime"
    runtime.mkdir(parents=True)
    created = {"spec": {"name": "existing", "nodes": []}, "export": "/srv/existing"}
    (runtime / "created.json").write_text(json.dumps(created))
    resource = {"name": "existing", "status": {"service": "Started", "state": "OK"}}
    monkeypatch.setattr(StorageController, "_gateway", staticmethod(lambda: [resource]))
    import test_cluster

    def agent(name, role, token, driver=None, backups=None):
        return create_agent_app(
            name, role, token, driver, backups, storage_root=peer / "virtual-storage"
        )

    monkeypatch.setattr(test_cluster, "create_agent_app", agent)
    env = Env(tmp_path)
    controller_checks = []

    def probe(role, controller=False):
        controller_checks.append(controller)
        return {"software_ready": not controller, "resources": []}

    monkeypatch.setattr("ibhics.manager.app.software_status", probe)
    local = tmp_path / "new-manager"
    client = TestClient(
        create_app(env.cluster, virtual_storage_path=local / "virtual-storage.json")
    )
    result = client.get("/api/v1/virtual-storage").json()
    assert result["created"] == created
    assert result["resource"] == resource
    assert result["controller_node"] == "node2"
    assert result["phase"] == "ready" and result["active"]
    assert controller_checks == [False]
    assert not (local / "virtual-storage-runtime/created.json").exists()
    # The read endpoint remains protected by the cluster token.
    app = env.agents.apps["http://node2:8007"]
    peer_client = TestClient(app)
    assert peer_client.get("/agent/v1/virtual-storage/manager-status").status_code == 401
    assert (
        peer_client.get(
            "/agent/v1/virtual-storage/manager-status", headers={"X-Ibhics-Token": TOKEN}
        ).json()["created"]
        == created
    )


def test_missing_gateway_keeps_existing_storage_visible_as_degraded(tmp_path, monkeypatch):
    root = tmp_path / "virtual-storage-runtime"
    root.mkdir()
    created = {"spec": {"name": "existing", "nodes": []}}
    (root / "created.json").write_text(json.dumps(created))
    monkeypatch.setattr(
        StorageController, "_gateway", staticmethod(lambda: (_ for _ in ()).throw(OSError()))
    )
    from ibhics.cluster import NodeRole
    from ibhics.hypervisor.fake import FakeDriver

    app = create_agent_app(
        "peer", NodeRole.COMPUTE, TOKEN, FakeDriver(), storage_root=tmp_path / "virtual-storage"
    )
    data = (
        TestClient(app)
        .get("/agent/v1/virtual-storage/manager-status", headers={"X-Ibhics-Token": TOKEN})
        .json()
    )
    assert data["created"] == created and data["resource"] is None
    assert data["error"] == "Storage controller status is unavailable"
    assert json.loads((root / "created.json").read_text()) == created
