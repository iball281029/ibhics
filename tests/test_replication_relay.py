"""Local manager and remote agents exchange replica state using fake hypervisors."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_cluster import TOKEN, Agents, Clock, join
from test_migration import NFS
from test_replication import VM, Inline, Provider

from ibhics.agent.app import create_agent_app
from ibhics.cluster import Cluster, NodeRole
from ibhics.hypervisor import VMSpec
from ibhics.hypervisor.fake import FakeDriver
from ibhics.importer import Converted, Source, SourceType
from ibhics.manager.app import create_app
from ibhics.manager.imports import ImportManager
from ibhics.manager.replications import ReplicationManager
from ibhics.replication import runtime as runtime_module


@pytest.fixture
def env(tmp_path, monkeypatch):
    drivers = {n: FakeDriver(memory_mb=65536, demo_console=False) for n in ("node1", "node2")}
    for driver in drivers.values():
        driver.files_root = tmp_path / "shared-mount"
    agents = Agents()
    agents.apps["http://node2:8007"] = create_agent_app(
        "node2",
        NodeRole.COMPUTE,
        TOKEN,
        drivers["node2"],
        replication_root=tmp_path / "agent-state",
        replication_guard=tmp_path / "agent-upgrade.lock",
    )
    agents.add("http://witness:8007", "witness", NodeRole.WITNESS)
    cluster = Cluster(token=TOKEN, client_factory=agents.client, clock=Clock())
    cluster.add_local("node1", drivers["node1"])
    imports = ImportManager(cluster, discover=lambda source: [VM])
    imports.add_source(Source("src", SourceType.KVM, host="host", password="SECRET"))
    # Discovery is also offline; no connected host is ever contacted by these tests.
    imports.source_vms = lambda name: [VM]
    manager = ReplicationManager(imports, tmp_path / "replications.json", executor=Inline())
    manager.guard_path = tmp_path / "manager-upgrade.lock"
    client = TestClient(create_app(cluster, imports=imports, replications=manager))
    assert join(client, "node2", "compute", "http://node2:8007").status_code == 201
    assert join(client, "witness", "witness", "http://witness:8007").status_code == 201
    for node in drivers:
        assert client.post(f"/api/v1/nodes/{node}/pools", json=NFS).status_code == 201
    reads = []

    def provider(source, vm):
        reads.append((source.host, vm.ref))
        return Provider(b"aaaabbbb")

    class Converter:
        def __init__(self, images):
            self.images = images

        def convert(self, source, vm, name, output, **kwargs):
            for image in self.images:
                assert image.read_bytes() == b"aaaabbbb"
                image.write_bytes(b"converted")
            return Converted(self.images, vm.firmware)

    monkeypatch.setattr(runtime_module, "KVMPullProvider", provider)
    monkeypatch.setattr(runtime_module, "ReplicaConverter", Converter)
    return client, manager, drivers, reads


@pytest.mark.parametrize("destination", ["node1", "node2"])
def test_relay_sync_and_test_on_destination(env, destination):
    client, manager, drivers, reads = env
    existing = drivers[destination].create_vm(VMSpec(name="already-running-workload"))
    drivers[destination].unreachable = {"host"}
    via = "node2" if destination == "node1" else "node1"
    response = client.post(
        "/api/v1/migrate/replications",
        json={
            "source": "src",
            "vm": "vm",
            "name": "replica",
            "node": destination,
            "pool": "shared",
            "network": ["default"],
            "schedule": {"interval_minutes": 5},
        },
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["via"] == via and job["state"] == "idle", job
    url = f"/api/v1/migrate/replications/{job['id']}"
    manifest = Path(drivers[destination].pool_path("shared")) / ".ibhics-replicas" / job["id"]
    raw = next(manifest.rglob("*.raw"))
    before = raw.read_bytes()
    test = client.post(url + "/test-failover", json={}).json()
    assert test["state"] == "idle" and test["test_vm"], test
    vm = drivers[destination].get_vm(test["test_vm"])
    assert vm.spec.nics == [] and str(vm.state) == "running"
    assert raw.read_bytes() == before
    assert drivers[via].list_vms() == []
    cleaned = client.post(url + "/test-cleanup").json()
    assert cleaned["test_vm"] is None, cleaned
    assert [v.uuid for v in drivers[destination].list_vms()] == [existing.uuid]
    done = client.post(url + "/cutover", json={"source_powered_off": True}).json()
    assert done["state"] == "done", done
    assert drivers[destination].get_vm(done["result"]).spec.nics[0].network == "default"
    assert len(reads) == 2
    assert client.delete(url + "?confirm=true").status_code == 204
    assert drivers[destination].get_vm(done["result"])
    assert drivers[destination].get_vm(existing.uuid).spec == existing.spec


def test_no_shared_pool_refuses_relay(env):
    client, manager, drivers, reads = env
    drivers["node2"].unreachable = {"host"}
    response = client.post(
        "/api/v1/migrate/replications",
        json={
            "source": "src",
            "vm": "vm",
            "node": "node2",
            "pool": "default",
        },
    )
    assert response.status_code == 409
    assert reads == []
