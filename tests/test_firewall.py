"""Firewall rules by VM and group, port lists, flow simulation and safe apply."""

from fastapi.testclient import TestClient

from ibhics.cluster import Cluster
from ibhics.hypervisor import DiskSpec, NetworkSpec, NicSpec, VMSpec
from ibhics.hypervisor.edge import EdgeConfig, FirewallRule, render_nft
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.edge import SAFE_SECONDS, EdgeManager


def vm(driver, name, mac, network="default"):
    info = driver.create_vm(
        VMSpec(name=name, disks=[DiskSpec(size_gb=1)], nics=[NicSpec(network=network, mac=mac)])
    )
    driver.start_vm(info.uuid)
    return info


def test_vm_ends_render_by_mac_in_bridge_and_ip_everywhere():
    rule = FirewallRule(
        "web-db",
        "allow",
        src="vm:web",
        dst="vm:db",
        proto="tcp",
        ports="5432,6000-6010",
        src_match=["mac:52:54:00:00:00:01", "ip:10.0.0.5"],
        dst_match=["mac:52:54:00:00:00:02"],
    )
    nft = render_nft(EdgeConfig(rules=[rule]), {"default": "br0"}, "br0", False)
    bridge = nft.split("table bridge ibhics_fw {")[1]
    inet = nft.split("table inet ibhics_fw {")[1].split("table bridge")[0]
    assert "ether saddr { 52:54:00:00:00:01 } ether daddr { 52:54:00:00:00:02 }" in bridge
    assert "ip saddr { 10.0.0.5 } ether daddr" in bridge
    assert "th dport { 5432, 6000-6010 }" in bridge
    assert "ether" not in inet  # no MACs once routed, and the db has no known IP yet
    rule.validate({"default"})


def test_vm_rules_resolve_and_simulate(tmp_path):
    driver = FakeDriver(demo_console=False)
    driver.create_network(NetworkSpec("db", 20))
    vm(driver, "web", "52:54:00:aa:00:01")
    vm(driver, "db", "52:54:00:aa:00:02", "db")
    client = TestClient(create_app(driver, edge_state=tmp_path / "edge.json"))
    rules = [
        {
            "name": "web-sql",
            "action": "allow",
            "src": "vm:web",
            "dst": "vm:db",
            "proto": "tcp",
            "ports": "5432",
        },
        {"name": "no-db", "action": "deny", "src": "any", "dst": "db"},
    ]
    resp = client.put("/api/v1/edge/rules", json=rules)
    assert resp.status_code == 200, resp.text
    pushed = driver.edge[0]["rules"][0]
    assert "mac:52:54:00:aa:00:01" in pushed["src_match"]
    assert resp.json()["rules"][0]["src_match"] == []  # the saved rule stays by name
    ok = client.post(
        "/api/v1/edge/simulate", json={"src": "web", "dst": "db", "proto": "tcp", "port": 5432}
    ).json()
    assert ok == {"allowed": True, "rule": "web-sql", "index": 1, "src": "web", "dst": "db"}
    ssh = client.post(
        "/api/v1/edge/simulate", json={"src": "web", "dst": "db", "proto": "tcp", "port": 22}
    ).json()
    assert ssh["allowed"] is False and ssh["rule"] == "no-db"
    assert (
        client.post("/api/v1/edge/simulate", json={"src": "nope", "dst": "db"}).status_code == 409
    )


def test_safe_change_rolls_back_unless_confirmed():
    driver = FakeDriver(demo_console=False)
    cluster = Cluster(token="t")
    cluster.add_local("node1", driver)
    now = [0.0]
    edge = EdgeManager(cluster, clock=lambda: now[0])
    edge.set_rules([FirewallRule("keep", "deny", src="10.0.0.0/8")])
    view = edge.set_rules([FirewallRule("risky", "deny")], safe=True)
    assert view["pending_confirm_seconds"] == SAFE_SECONDS
    now[0] = SAFE_SECONDS + 1
    edge._expire()
    assert [r.name for r in edge.config.rules] == ["keep"]
    assert driver.edge[0]["rules"][0]["name"] == "keep"
    edge.set_rules([FirewallRule("fine", "deny", src="10.1.0.0/16")], safe=True)
    edge.confirm()
    now[0] = 10 * SAFE_SECONDS
    edge._expire()
    assert [r.name for r in edge.config.rules] == ["fine"]


def test_refresh_follows_new_vms():
    driver = FakeDriver(demo_console=False)
    cluster = Cluster(token="t")
    cluster.add_local("node1", driver)
    edge = EdgeManager(cluster)
    edge.set_rules([FirewallRule("lock-app", "deny", dst="vm:app")])
    assert driver.edge[0]["rules"][0]["dst_match"] == []
    assert edge.refresh() is False
    vm(driver, "app", "52:54:00:bb:00:01")
    assert edge.refresh() is True
    assert "mac:52:54:00:bb:00:01" in driver.edge[0]["rules"][0]["dst_match"]


def test_network_edit_and_sync(tmp_path):
    from test_migration import Env

    env = Env(tmp_path)
    env.node1.create_network(NetworkSpec("lab", 30))  # only on one node
    client = TestClient(create_app(env.cluster, edge_state=tmp_path / "edge.json"))
    synced = client.post("/api/v1/networks/lab/sync")
    assert synced.status_code == 200, synced.text
    assert sorted(synced.json()["nodes"]) == ["node1", "node2"]
    moved = client.put("/api/v1/networks/lab", json={"name": "lab2", "vlan": 31})
    assert moved.status_code == 200, moved.text
    for node in (env.node1, env.node2):
        assert {(n.name, n.vlan) for n in node.list_networks()} >= {("lab2", 31)}
        assert "lab" not in {n.name for n in node.list_networks()}
    vm(env.node2, "web", "52:54:00:cc:00:01", "lab2")
    busy = client.put("/api/v1/networks/lab2", json={"name": "lab3", "vlan": 32})
    assert busy.status_code == 409 and "move its VMs" in busy.json()["detail"]
    assert client.put("/api/v1/networks/default", json={"name": "x", "vlan": 5}).status_code == 409


def test_nic_move_to_switch_missing_on_node(tmp_path):
    from test_migration import Env

    env = Env(tmp_path)
    env.node1.create_network(NetworkSpec("lab", 30))  # node2 lacks it
    client = TestClient(create_app(env.cluster, edge_state=tmp_path / "edge.json"))
    info = vm(env.node2, "web", "52:54:00:dd:00:01")
    resp = client.patch(f"/api/v1/vms/{info.uuid}", json={"nics": [{"network": "lab"}]})
    assert resp.status_code == 409, resp.text
    assert "not on node2" in resp.json()["detail"]


def test_vlans_reach_every_node_by_themselves(tmp_path):
    from test_migration import Env

    env = Env(tmp_path)
    env.node1.create_network(NetworkSpec("lab", 30))  # made while node2 was away
    client = TestClient(create_app(env.cluster, edge_state=tmp_path / "edge.json"))
    sync = client.app.state.sync_networks
    assert sync() == ["lab@node2"]
    assert "lab" in {n.name for n in env.node2.list_networks()}
    assert sync() == []
    # Deleted on purpose: a node that still has it does not bring it back.
    assert client.delete("/api/v1/networks/lab").status_code == 204
    env.node1.create_network(NetworkSpec("lab", 30))
    assert sync() == []
    assert "lab" not in {n.name for n in env.node2.list_networks()}
    # Made again through the API: it is wanted everywhere once more.
    client.delete("/api/v1/networks/lab")
    assert client.post("/api/v1/networks", json={"name": "lab", "vlan": 30}).status_code == 201
