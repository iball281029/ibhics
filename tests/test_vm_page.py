"""VM groups and live VM load, behind the VM list."""

import pytest
from fastapi.testclient import TestClient

from ibhics.hypervisor.fake import FakeDriver
from ibhics.hypervisor.vmstats import memory_used_mb, rates, sample
from ibhics.manager.app import create_app
from ibhics.manager.vmgroups import GroupNotFound, VMGroups


@pytest.fixture
def client():
    return TestClient(create_app(FakeDriver()))


def create(client, name):
    body = {"name": name, "vcpus": 2, "memory_mb": 2048, "disks": [{"size_gb": 10}]}
    return client.post("/api/v1/vms", json=body).json()


def test_stats_of_running_vms(client):
    vm = create(client, "web-01")
    create(client, "db-01")
    assert client.get("/api/v1/vms/stats").json() == []
    client.post(f"/api/v1/vms/{vm['uuid']}/start")
    [s] = client.get("/api/v1/vms/stats").json()
    assert s["uuid"] == vm["uuid"]
    assert 0 <= s["cpu_pct"] <= 100
    assert 0 < s["mem_used_mb"] <= 2048


def test_restart(client):
    vm = create(client, "web-01")
    assert client.post(f"/api/v1/vms/{vm['uuid']}/restart").status_code == 409
    client.post(f"/api/v1/vms/{vm['uuid']}/start")
    r = client.post(f"/api/v1/vms/{vm['uuid']}/restart", json={"force": True})
    assert r.status_code == 200 and r.json()["state"] == "running"


def test_rates_from_two_samples():
    raw = {
        "cpu.time": 0,
        "vcpu.current": 2,
        "block.count": 1,
        "block.0.rd.bytes": 0,
        "net.count": 1,
        "net.0.rx.bytes": 0,
        "balloon.available": 2097152,
        "balloon.usable": 1048576,
    }
    first = sample(raw, at=10.0)
    later = dict(
        raw, **{"cpu.time": 1_000_000_000, "block.0.rd.bytes": 4096, "net.0.rx.bytes": 2000}
    )
    s = rates(first, sample(later, at=11.0))
    assert s.cpu_pct == 50.0  # one second of CPU over two vCPUs in one second
    assert s.disk_read_bps == 4096
    assert s.net_rx_bps == 2000
    assert s.mem_used_mb == 1024
    assert rates(None, first).cpu_pct is None


def test_memory_unknown_without_balloon_driver():
    assert memory_used_mb({"balloon.current": 2097152}) is None
    assert memory_used_mb({"balloon.available": 2097152, "balloon.unused": 524288}) == 1536


def test_counter_reset_is_not_negative():
    a = sample({"cpu.time": 5_000_000_000, "block.count": 1, "block.0.wr.bytes": 10_000}, 1.0)
    b = sample({"cpu.time": 1_000, "block.count": 1, "block.0.wr.bytes": 0}, 2.0)
    s = rates(a, b)
    assert s.cpu_pct == 0 and s.disk_write_bps == 0


def test_groups_tree_and_members(client):
    web, db = create(client, "web-01"), create(client, "db-01")
    prod = client.post("/api/v1/vm-groups", json={"name": "Production"}).json()
    apps = client.post("/api/v1/vm-groups", json={"name": "Apps", "parent": prod["id"]}).json()
    assert (
        client.post("/api/v1/vm-groups", json={"name": "apps", "parent": prod["id"]}).status_code
        == 422
    )
    r = client.put(
        "/api/v1/vm-groups/members", json={"vms": [web["uuid"], db["uuid"]], "group": apps["id"]}
    )
    assert r.json()["members"] == {web["uuid"]: apps["id"], db["uuid"]: apps["id"]}
    # Renaming and moving
    assert (
        client.patch(f"/api/v1/vm-groups/{apps['id']}", json={"name": "Web"}).json()["name"]
        == "Web"
    )
    assert (
        client.patch(f"/api/v1/vm-groups/{prod['id']}", json={"parent": apps["id"]}).status_code
        == 422
    )
    # Deleting a group moves its VMs up to the parent
    assert client.delete(f"/api/v1/vm-groups/{apps['id']}").status_code == 204
    data = client.get("/api/v1/vm-groups").json()
    assert [g["name"] for g in data["groups"]] == ["Production"]
    assert set(data["members"].values()) == {prod["id"]}
    # Ungrouping, and deleting a VM forgets it
    client.put("/api/v1/vm-groups/members", json={"vms": [web["uuid"]], "group": ""})
    client.delete(f"/api/v1/vms/{db['uuid']}")
    assert client.get("/api/v1/vm-groups").json()["members"] == {}
    assert (
        client.put("/api/v1/vm-groups/members", json={"vms": ["nope"], "group": ""}).status_code
        == 404
    )
    assert client.delete("/api/v1/vm-groups/nope").status_code == 404


def test_groups_saved(tmp_path):
    path = tmp_path / "groups.json"
    g = VMGroups(path)
    top = g.create("Branch offices")
    sub = g.create("Chiang Mai", top.id)
    g.assign(["vm-1"], sub.id)
    again = VMGroups(path)
    again.load()
    assert [x.name for x in again.groups()] == ["Branch offices", "Chiang Mai"]
    assert again.members() == {"vm-1": sub.id}
    with pytest.raises(GroupNotFound):
        again.assign(["vm-2"], "missing")


def test_groups_nest_limit():
    g = VMGroups()
    parent = ""
    for i in range(4):
        parent = g.create(f"level {i}", parent).id
    with pytest.raises(ValueError):
        g.create("too deep", parent)


def test_delete_keeps_sibling_names_unique():
    g = VMGroups()
    a = g.create("A")
    g.create("Lab")
    g.create("Lab", a.id)
    g.delete(a.id)
    assert sorted(x.name for x in g.groups()) == ["Lab", "Lab (2)"]
