"""Manager + agents end to end, with agents served in-process through TestClient."""

from concurrent.futures import Executor, Future

import pytest
from fastapi.testclient import TestClient

from ibhics.agent import create_agent_app
from ibhics.cluster import AgentClient, Cluster, NodeRole
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app

TOKEN = "s3cret"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Agents:
    """Agent apps keyed by URL; a node can be switched off to simulate failure."""

    def __init__(self):
        self.apps = {}
        self.down = set()

    def add(self, address, name, role, driver=None, backups=None):
        self.apps[address] = create_agent_app(name, role, TOKEN, driver, backups)

    def client(self, address, token):
        agents = self

        class Switchable(TestClient):
            def request(self, *args, **kwargs):
                if address in agents.down:
                    import httpx

                    raise httpx.ConnectError("node is down")
                kwargs.pop("timeout", None)  # TestClient doesn't support per-request timeouts
                return super().request(*args, **kwargs)

        return AgentClient(address, token, http=Switchable(self.apps[address], base_url=address))


@pytest.fixture
def env(tmp_path):
    agents = Agents()
    agents.add("http://node2:8007", "node2", NodeRole.COMPUTE, FakeDriver(memory_mb=262144))
    agents.add("http://witness:8007", "witness", NodeRole.WITNESS)
    clock = Clock()
    cluster = Cluster(
        token=TOKEN,
        heartbeat_timeout=15,
        state_path=tmp_path / "cluster.json",
        client_factory=agents.client,
        clock=clock,
    )
    cluster.add_local("node1", FakeDriver(memory_mb=65536))
    client = TestClient(create_app(cluster))
    return client, cluster, agents, clock


def join(client, name, role, address):
    return client.post(
        "/api/v1/cluster/nodes", json={"name": name, "role": role, "address": address}
    )


def build_two_node_witness(client):
    assert join(client, "node2", "compute", "http://node2:8007").status_code == 201
    assert join(client, "witness", "witness", "http://witness:8007").status_code == 201


def test_two_node_witness_topology(env):
    client, *_ = env
    build_two_node_witness(client)
    status = client.get("/api/v1/cluster").json()
    assert status["topology"] == "two-node-witness"
    assert status["quorum"] and status["ha_capable"]
    assert {m["name"]: m["healthy"] for m in status["members"]} == {
        "node1": True,
        "node2": True,
        "witness": True,
    }


def test_join_checks_identity_and_duplicates(env):
    client, *_ = env
    resp = join(client, "wrong-name", "compute", "http://node2:8007")
    assert resp.status_code == 400
    assert join(client, "node2", "witness", "http://node2:8007").status_code == 400
    build_two_node_witness(client)
    assert join(client, "node2", "compute", "http://node2:8007").status_code == 400


def test_join_unreachable_node(env):
    client, _, agents, _ = env
    agents.down.add("http://node2:8007")
    assert join(client, "node2", "compute", "http://node2:8007").status_code == 503


def test_vm_placed_on_node_with_most_free_memory(env):
    client, *_ = env
    build_two_node_witness(client)
    vm = client.post("/api/v1/vms", json={"name": "web-01"}).json()
    assert vm["node"] == "node2"  # node2 has 256 GB, node1 64 GB

    pinned = client.post("/api/v1/vms", json={"name": "db-01", "node": "node1"}).json()
    assert pinned["node"] == "node1"

    listed = {v["name"]: v["node"] for v in client.get("/api/v1/vms").json()}
    assert listed == {"web-01": "node2", "db-01": "node1"}


def test_vm_lifecycle_on_remote_node(env):
    client, *_ = env
    build_two_node_witness(client)
    uuid = client.post("/api/v1/vms", json={"name": "app", "node": "node2"}).json()["uuid"]
    assert client.post(f"/api/v1/vms/{uuid}/start").json()["state"] == "running"
    assert client.post(f"/api/v1/vms/{uuid}/start").status_code == 409
    assert (
        client.post(f"/api/v1/vms/{uuid}/stop", json={"force": True}).json()["state"] == "stopped"
    )
    assert client.delete(f"/api/v1/vms/{uuid}").status_code == 204
    assert client.get(f"/api/v1/vms/{uuid}").status_code == 404


def test_vm_names_unique_across_nodes(env):
    client, *_ = env
    build_two_node_witness(client)
    client.post("/api/v1/vms", json={"name": "app", "node": "node2"})
    assert client.post("/api/v1/vms", json={"name": "app", "node": "node1"}).status_code == 409


def test_losing_one_member_keeps_quorum(env):
    client, cluster, agents, clock = env
    build_two_node_witness(client)
    agents.down.add("http://witness:8007")
    clock.now += 30
    cluster.refresh()
    status = client.get("/api/v1/cluster").json()
    assert status["quorum"] is True
    assert client.post("/api/v1/vms", json={"name": "still-ok"}).status_code == 201


def test_losing_majority_blocks_changes_but_not_reads(env):
    client, cluster, agents, clock = env
    build_two_node_witness(client)
    uuid = client.post("/api/v1/vms", json={"name": "local", "node": "node1"}).json()["uuid"]

    agents.down.update({"http://witness:8007", "http://node2:8007"})
    clock.now += 30
    cluster.refresh()

    status = client.get("/api/v1/cluster").json()
    assert status["quorum"] is False
    assert client.get("/api/v1/vms").status_code == 200
    assert client.post("/api/v1/vms", json={"name": "new"}).status_code == 503
    assert client.post(f"/api/v1/vms/{uuid}/start").status_code == 503

    agents.down.clear()
    cluster.refresh()
    assert client.post(f"/api/v1/vms/{uuid}/start").status_code == 200


def test_offline_node_rejects_pinned_placement(env):
    client, cluster, agents, clock = env
    build_two_node_witness(client)
    agents.down.add("http://node2:8007")
    clock.now += 30
    cluster.refresh()
    assert client.post("/api/v1/vms", json={"name": "x", "node": "node2"}).status_code == 503
    assert client.post("/api/v1/vms", json={"name": "x"}).json()["node"] == "node1"


def test_remove_node_and_persistence(env, tmp_path):
    client, _, agents, clock = env
    build_two_node_witness(client)
    assert client.delete("/api/v1/cluster/nodes/node1").status_code == 400  # manager's own node

    restored = Cluster(
        token=TOKEN,
        state_path=tmp_path / "cluster.json",
        client_factory=agents.client,
        clock=clock,
    )
    restored.add_local("node1", FakeDriver())
    restored.load()
    assert sorted(m.name for m in restored.members()) == ["node1", "node2", "witness"]

    assert client.delete("/api/v1/cluster/nodes/witness").status_code == 204
    assert client.get("/api/v1/cluster").json()["topology"] == "incomplete"


def test_agent_requires_token():
    app = create_agent_app("n", NodeRole.COMPUTE, TOKEN, FakeDriver())
    client = TestClient(app)
    assert client.get("/agent/v1/health").status_code == 401
    assert client.get("/agent/v1/health", headers={"X-Ibhics-Token": "nope"}).status_code == 401
    ok = client.get("/agent/v1/health", headers={"X-Ibhics-Token": TOKEN})
    assert ok.json()["role"] == "compute"


def test_witness_exposes_no_vm_api():
    client = TestClient(create_agent_app("w", NodeRole.WITNESS, TOKEN))
    assert client.get("/agent/v1/vms", headers={"X-Ibhics-Token": TOKEN}).status_code == 404


def test_cluster_without_token_cannot_join(tmp_path):
    client = TestClient(create_app(FakeDriver()))
    resp = join(client, "node2", "compute", "http://node2:8007")
    assert resp.status_code == 400
    assert "token" in resp.json()["detail"]


def test_pools_on_remote_node(env):
    client, *_ = env
    build_two_node_witness(client)
    resp = client.post(
        "/api/v1/nodes/node2/pools",
        json={"name": "fc", "type": "lvm", "device": "/dev/mapper/mpatha", "confirm_wipe": True},
    )
    assert resp.status_code == 201
    assert resp.json()["node"] == "node2"
    pools = {(p["node"], p["name"]) for p in client.get("/api/v1/pools").json()}
    assert pools == {("node1", "default"), ("node2", "default"), ("node2", "fc")}

    vm = client.post(
        "/api/v1/vms",
        json={"name": "db", "node": "node2", "disks": [{"size_gb": 10, "pool": "fc"}]},
    )
    assert vm.json()["disks"][0]["backend"] == "block"

    missing = client.post(
        "/api/v1/vms",
        json={"name": "x", "node": "node1", "disks": [{"size_gb": 10, "pool": "fc"}]},
    )
    assert missing.status_code == 404
    assert client.delete("/api/v1/nodes/node2/pools/fc").status_code == 409
    assert client.delete("/api/v1/nodes/node2/pools/ghost").status_code == 404


def test_backup_and_restore_on_remote_node(env, tmp_path):
    from ibhics.backup import NodeBackup
    from ibhics.manager.backups import BackupManager

    class Inline(Executor):
        def submit(self, fn, *args, **kwargs):
            future = Future()
            fn(*args, **kwargs)
            future.set_result(None)
            return future

    client, cluster, agents, _ = env
    node2 = FakeDriver(memory_mb=262144)
    agents.add("http://node2:8007", "node2", NodeRole.COMPUTE, node2, NodeBackup(node2))
    build_two_node_witness(client)
    client = TestClient(create_app(cluster, backups=BackupManager(cluster, executor=Inline())))

    # In this test both nodes' "local" directory is the same one, so each node lists it.
    shared = tmp_path / "nas"
    client.post(
        "/api/v1/backup/targets", json={"name": "nas", "type": "local", "path": str(shared)}
    )
    vm = client.post("/api/v1/vms", json={"name": "app", "node": "node2"}).json()
    node2.write_disk(vm["uuid"], 0, 0, b"payload")

    job = client.post(f"/api/v1/vms/{vm['uuid']}/backup", json={"target": "nas"}).json()
    assert (job["state"], job["node"]) == ("done", "node2"), job["error"]
    listed = client.get("/api/v1/backup/targets/nas/backups").json()
    assert sorted(b["node"] for b in listed) == ["node1", "node2"]
    backup = listed[0]

    url = f"/api/v1/backup/targets/nas/backups/{backup['id']}/restore"
    job = client.post(url, json={"source_node": "node2", "name": "app-copy"}).json()
    assert (job["state"], job["node"]) == ("done", "node2"), job["error"]
    assert node2.read_disk(job["result"], 0)[:7] == b"payload"

    missing = client.post(url.replace("app@", "ghost@"), json={"source_node": "node2"}).json()
    assert missing["state"] == "failed" and "BackupNotFound" in missing["error"]


def test_guest_inventory_from_remote_node(env):
    client, *_ = env
    build_two_node_witness(client)
    body = {
        "name": "far",
        "vcpus": 1,
        "memory_mb": 1024,
        "node": "node2",
        "disks": [{"size_gb": 10}],
        "nics": [{"network": "default"}],
    }
    vm = client.post("/api/v1/vms", json=body).json()
    client.post(f"/api/v1/vms/{vm['uuid']}/start")
    (guest,) = client.get("/api/v1/vms/guests").json()
    assert guest["uuid"] == vm["uuid"] and guest["hostname"] == "far"
    assert client.get(f"/api/v1/vms/{vm['uuid']}/guest").json()["agent"] is True
