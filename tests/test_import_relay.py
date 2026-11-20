"""An import onto a node that cannot reach the source converts on a node that can."""

from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient
from test_cluster import TOKEN, Agents, Clock, join
from test_migration import NFS

from ibhics.cluster import Cluster, NodeRole
from ibhics.cluster.remote import AgentClient, RemoteImport
from ibhics.hypervisor.base import Firmware
from ibhics.hypervisor.fake import FakeDriver
from ibhics.importer import Source, SourceType, SourceVM
from ibhics.manager.app import create_app
from ibhics.manager.imports import ImportManager, source_ports
from ibhics.manager.migrations import MigrationManager


class Inline:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        future = Future()
        future.set_result(None)
        return future


VM = SourceVM(
    ref="101",
    name="win",
    vcpus=2,
    memory_mb=2048,
    firmware=Firmware.BIOS,
    disks_gb=[10],
    nics=1,
    state="stopped",
)


@pytest.fixture
def env(tmp_path):
    node1 = FakeDriver(memory_mb=65536, demo_console=False)
    node2 = FakeDriver(memory_mb=262144, demo_console=False)
    node1.peers, node2.peers = {"node2": node2}, {"node1": node1}
    agents = Agents()
    agents.add("http://node2:8007", "node2", NodeRole.COMPUTE, node2)
    agents.add("http://witness:8007", "witness", NodeRole.WITNESS)
    cluster = Cluster(
        token=TOKEN,
        client_factory=agents.client,
        clock=Clock(),
        state_path=tmp_path / "cluster.json",
    )
    cluster.add_local("node1", node1)
    migrations = MigrationManager(cluster, local_host="node1", executor=Inline())
    imports = ImportManager(
        cluster, executor=Inline(), discover=lambda s: [VM], upload_dir=tmp_path / "uploads"
    )
    imports._sleep = lambda s: None
    client = TestClient(create_app(cluster, migrations=migrations, imports=imports))
    assert join(client, "node2", "compute", "http://node2:8007").status_code == 201
    assert join(client, "witness", "witness", "http://witness:8007").status_code == 201
    imports.add_source(
        Source("pve", SourceType.KVM, host="10.9.9.9", username="root", password="x")
    )
    return client, imports, node1, node2


def add_shared(client):
    for node in ("node1", "node2"):
        assert client.post(f"/api/v1/nodes/{node}/pools", json=NFS).status_code == 201


def test_ports_per_source_type():
    assert source_ports(Source("a", SourceType.VMWARE, host="h", transport="ssh")) == [443, 22]
    assert source_ports(Source("a", SourceType.VMWARE, host="h", path="DC/host/c/e")) == [443]
    assert source_ports(Source("a", SourceType.NUTANIX, host="h")) == [9440]
    assert source_ports(Source("a", SourceType.OVA, path="/x")) == []


def test_converts_on_a_node_that_reaches_then_moves(env):
    client, imports, node1, node2 = env
    add_shared(client)
    node2.unreachable = {"10.9.9.9"}
    resp = client.post(
        "/api/v1/migrate/jobs",
        json={"source": "pve", "vm": "101", "node": "node2", "pool": "shared", "start": True},
    )
    assert resp.status_code == 202, resp.text
    [job] = client.get("/api/v1/migrate/jobs").json()
    assert job["state"] == "done", job
    assert job["node"] == "node2" and job["via"] == "node1"
    vm = client.get(f"/api/v1/vms/{job['result']}").json()
    assert vm["node"] == "node2" and vm["state"] == "running"


def test_no_node_asked_imports_where_the_source_is_reachable(env):
    client, imports, node1, node2 = env
    add_shared(client)
    node2.unreachable = {"10.9.9.9"}
    job = client.post(
        "/api/v1/migrate/jobs", json={"source": "pve", "vm": "101", "pool": "shared"}
    ).json()
    assert job["node"] == "node1" and job["via"] == ""


def test_local_pool_cannot_be_relayed(env):
    client, imports, node1, node2 = env
    node2.unreachable = {"10.9.9.9"}
    resp = client.post("/api/v1/migrate/jobs", json={"source": "pve", "vm": "101", "node": "node2"})
    assert resp.status_code == 409
    assert "cannot reach the source" in resp.json()["detail"]


@pytest.mark.parametrize(
    "kind", [SourceType.VMWARE, SourceType.KVM, SourceType.SANGFOR, SourceType.NUTANIX]
)
def test_source_vms_relayed_when_manager_cannot_reach(env, monkeypatch, kind):
    client, imports, node1, _ = env
    source = Source("remote", kind, host="10.9.9.9", username="root", password="secret")
    imports.add_source(source)
    node1.unreachable = {source.host}
    seen = []

    def discover(received):
        seen.append(received)
        return [VM]

    monkeypatch.setattr("ibhics.agent.app.discover_source_vms", discover)
    imports._discover = lambda s: pytest.fail("must discover on the remote node")
    response = client.get("/api/v1/migrate/sources/remote/vms")
    assert response.status_code == 200, response.text
    assert response.json()[0]["ref"] == VM.ref
    assert seen == [source]  # credentials and connector settings survive serialization
    assert "secret" not in response.text


@pytest.mark.parametrize("saved", [False, True])
def test_source_test_relay_reports_node(env, monkeypatch, saved):
    client, imports, node1, _ = env
    node1.unreachable = {"10.9.9.9"}
    source = imports.source("pve")
    seen = []

    def diagnose(received):
        seen.append(received)
        return {"checks": [{"step": "Login", "ok": True, "detail": "OK", "hint": ""}]}

    monkeypatch.setattr("ibhics.agent.app.test_source", diagnose)
    imports._tester = lambda s: pytest.fail("must test on the remote node")
    path = "/api/v1/migrate/sources/pve/test" if saved else "/api/v1/migrate/sources/test"
    response = client.post(path, **({} if saved else {"json": source.to_dict()}))
    assert response.status_code == 200, response.text
    assert response.json()["node"] == "node2"
    assert "node2" in response.json()["checks"][0]["detail"]
    assert seen == [source]


def test_source_query_no_reachable_node(env):
    client, imports, node1, node2 = env
    node1.unreachable = node2.unreachable = {"10.9.9.9"}
    # The connection test still runs here, so it can show which step fails.
    imports._tester = lambda s: {"checks": [{"step": "tcp", "ok": False, "detail": "closed"}]}
    assert imports.test_source("pve")["checks"][0]["ok"] is False
    response = client.get("/api/v1/migrate/sources/pve/vms")
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "node1" in detail and "node2" in detail
    assert "10.9.9.9" in detail and "22" in detail
    assert "Cannot reach" in detail


def test_reachable_manager_queries_locally(env, monkeypatch):
    _, imports, _, _ = env
    monkeypatch.setattr(
        "ibhics.agent.app.discover_source_vms", lambda s: pytest.fail("must discover locally")
    )
    monkeypatch.setattr("ibhics.agent.app.test_source", lambda s: pytest.fail("must test locally"))
    imports._tester = lambda s: {"checks": []}
    assert imports.source_vms("pve") == [VM]
    assert imports.test_source("pve") == {"checks": []}


@pytest.mark.parametrize(
    "kind", [SourceType.OVA, SourceType.DISK, SourceType.UPLOAD, SourceType.P2V]
)
def test_local_sources_are_not_relayed(env, monkeypatch, kind):
    _, imports, node1, node2 = env
    node1.unreachable = node2.unreachable = {"10.9.9.9"}
    monkeypatch.setattr(
        imports, "_reaches", lambda *a: pytest.fail("local sources must not probe nodes")
    )
    seen = []
    imports._tester = lambda s: seen.append(s) or {"checks": []}
    if kind == SourceType.UPLOAD:
        source = imports.uploads_source
    elif kind == SourceType.P2V:
        source = imports.physical_source
        assert imports.source_vms(source.name) == []
    else:
        source = imports.add_source(Source("folder", kind, path="/exports"))
    if kind != SourceType.P2V:
        assert imports.source_vms(source.name) == [VM]
    assert imports.test_source(source) == {"checks": []}
    assert seen == [source]


@pytest.mark.parametrize("operation", ["source_vms", "test_source"])
def test_old_agent_source_queries_report_upgrade(env, operation):
    import httpx

    from ibhics.importer import SourceUnavailable

    _, imports, _, _ = env
    http = httpx.Client(
        base_url="http://old-node:8007",
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
    )
    remote = RemoteImport(AgentClient("http://old-node:8007", TOKEN, http=http))
    with pytest.raises(SourceUnavailable, match="does not support source queries; upgrade"):
        getattr(remote, operation)(imports.source("pve"))
