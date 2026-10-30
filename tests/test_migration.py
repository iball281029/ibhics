"""Live migration between the manager's node and a remote agent, on fake hypervisors."""

from concurrent.futures import Executor, Future

import pytest
from fastapi.testclient import TestClient
from test_cluster import TOKEN, Agents, Clock, join

from ibhics.cluster import Cluster, NodeRole
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.migrations import MigrationManager

NFS = {"name": "shared", "type": "nfs", "host": "nas1", "export": "/vms"}


class Inline(Executor):
    def submit(self, fn, *args, **kwargs):
        future = Future()
        fn(*args, **kwargs)
        future.set_result(None)
        return future


class Env:
    def __init__(self, tmp_path, busy=lambda _vm: False):
        self.node1 = FakeDriver(memory_mb=65536, demo_console=False)
        self.node2 = FakeDriver(memory_mb=262144, demo_console=False)
        self.node1.peers = {"node2": self.node2}
        self.node2.peers = {"node1": self.node1}
        self.agents = Agents()
        self.agents.add("http://node2:8007", "node2", NodeRole.COMPUTE, self.node2)
        self.agents.add("http://witness:8007", "witness", NodeRole.WITNESS)
        self.clock = Clock()
        self.cluster = Cluster(
            token=TOKEN,
            heartbeat_timeout=15,
            state_path=tmp_path / "cluster.json",
            client_factory=self.agents.client,
            clock=self.clock,
        )
        self.cluster.add_local("node1", self.node1)
        self.migrations = MigrationManager(
            self.cluster, local_host="node1", busy=busy, executor=Inline()
        )
        self.client = TestClient(create_app(self.cluster, migrations=self.migrations))
        assert join(self.client, "node2", "compute", "http://node2:8007").status_code == 201
        assert join(self.client, "witness", "witness", "http://witness:8007").status_code == 201

    def add_shared_pool(self):
        for node in ("node1", "node2"):
            resp = self.client.post(f"/api/v1/nodes/{node}/pools", json=NFS)
            assert resp.status_code == 201, resp.text

    def create_vm(self, name, pool="default", node="node1", memory_mb=2048, start=True):
        body = {"name": name, "memory_mb": memory_mb, "node": node, "disks": [{"size_gb": 10}]}
        body["disks"][0]["pool"] = pool
        resp = self.client.post("/api/v1/vms", json=body)
        assert resp.status_code == 201, resp.text
        uuid = resp.json()["uuid"]
        if start:
            assert self.client.post(f"/api/v1/vms/{uuid}/start").status_code == 200
        return uuid

    def migrate(self, uuid, **body):
        return self.client.post(f"/api/v1/vms/{uuid}/migrate", json=body)

    def where(self, uuid):
        return self.client.get(f"/api/v1/vms/{uuid}").json()["node"]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_live_migration_on_shared_storage(env):
    env.add_shared_pool()
    uuid = env.create_vm("web", pool="shared")
    env.node1.write_disk(uuid, 0, 0, b"guest data")

    resp = env.migrate(uuid, node="node2")
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["source"] == "node1" and job["target"] == "node2"
    assert job["live"] is True and job["copy_storage"] is False

    assert env.where(uuid) == "node2"
    assert env.client.get(f"/api/v1/vms/{uuid}").json()["state"] == "running"
    assert env.node2.read_disk(uuid, 0).startswith(b"guest data")
    [done] = env.client.get("/api/v1/migrations").json()
    assert done["state"] == "done" and done["finished"]


def test_local_disks_are_copied_unless_refused(env):
    uuid = env.create_vm("db")  # "default" is a local directory pool on each node

    refused = env.migrate(uuid, node="node2", copy_storage=False)
    assert refused.status_code == 409
    assert "local to node1" in refused.json()["detail"]

    resp = env.migrate(uuid, node="node2")
    assert resp.json()["copy_storage"] is True
    assert env.where(uuid) == "node2"


def test_stopped_vm_moves_only_on_shared_storage(env):
    env.add_shared_pool()
    shared = env.create_vm("cold-shared", pool="shared", start=False)
    local = env.create_vm("cold-local", start=False)

    resp = env.migrate(shared, node="node2")
    assert resp.json()["live"] is False
    assert env.where(shared) == "node2"

    resp = env.migrate(local, node="node2")
    assert resp.status_code == 409
    assert "start it first" in resp.json()["detail"]


def test_destination_needs_the_pool_and_memory(env):
    env.client.post("/api/v1/nodes/node1/pools", json=NFS)  # only on node1
    uuid = env.create_vm("app", pool="shared")
    resp = env.migrate(uuid, node="node2")
    assert resp.status_code == 409
    assert "node2 has no storage pool 'shared'" in resp.json()["detail"]

    big = env.create_vm("big", memory_mb=300000)
    resp = env.migrate(big, node="node2")
    assert resp.status_code == 409
    assert "not enough free memory" in resp.json()["detail"]


def test_destination_is_chosen_when_omitted(env):
    uuid = env.create_vm("auto")
    assert env.migrate(uuid).json()["target"] == "node2"
    resp = env.migrate(uuid, node="node2")
    assert resp.status_code == 409
    assert "already on node2" in resp.json()["detail"]
    # back again, through node2's agent
    assert env.migrate(uuid).json()["target"] == "node1"
    assert env.where(uuid) == "node1"


def test_failed_migration_is_recorded_and_vm_stays(env):
    env.node1.peers = {}  # node1's libvirtd cannot reach node2
    uuid = env.create_vm("stuck")
    assert env.migrate(uuid, node="node2").status_code == 202
    [job] = env.client.get("/api/v1/migrations").json()
    assert job["state"] == "failed"
    assert "cannot connect to qemu+tls://node2/system" in job["error"]
    assert env.where(uuid) == "node1"


def test_evacuate_moves_what_it_can_and_explains_the_rest(env):
    env.add_shared_pool()
    running = env.create_vm("r1", node="node2")
    shared_off = env.create_vm("s1", pool="shared", node="node2", start=False)
    local_off = env.create_vm("l1", node="node2", start=False)

    resp = env.client.post("/api/v1/nodes/node2/evacuate")
    assert resp.status_code == 202
    jobs = {j["vm"]: j for j in resp.json()}
    assert jobs["r1"]["target"] == "node1" and jobs["r1"]["state"] == "done"
    assert jobs["s1"]["state"] == "done"
    assert jobs["l1"]["state"] == "failed" and "start it first" in jobs["l1"]["error"]
    assert env.where(running) == env.where(shared_off) == "node1"
    assert env.where(local_off) == "node2"


def test_migration_needs_quorum(env):
    uuid = env.create_vm("q")
    env.agents.down |= {"http://node2:8007", "http://witness:8007"}
    env.clock.now += 60
    env.cluster.refresh()
    assert env.migrate(uuid, node="node2").status_code == 503


def test_vm_being_backed_up_does_not_move(tmp_path):
    env = Env(tmp_path, busy=lambda vm: vm == "busy")
    uuid = env.create_vm("busy")
    resp = env.migrate(uuid, node="node2")
    assert resp.status_code == 409
    assert "backup" in resp.json()["detail"]


def test_transport_uris(env):
    node1, node2 = env.cluster.members()[:2]
    assert env.migrations.uri(node2) == "qemu+tls://node2/system"
    ssh = MigrationManager(env.cluster, transport="ssh", local_host="10.0.0.11")
    assert ssh.uri(node1) == "qemu+ssh://root@10.0.0.11/system"
    with pytest.raises(ValueError):
        MigrationManager(env.cluster, transport="telnet")


def test_agent_rejects_other_destinations(env):
    agent = env.agents.client("http://node2:8007", TOKEN)
    resp = agent.request("POST", "/vms/x/migrate", json={"dest_uri": "file:///etc/passwd"})
    assert resp.status_code == 422
