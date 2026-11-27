"""v0.7 network: virtual router (gateway, NAT, DHCP), distributed firewall, ping/trace."""

import pytest
from fastapi.testclient import TestClient

from ibhics.hypervisor import NetworkSpec
from ibhics.hypervisor.edge import (
    EdgeConfig,
    FirewallRule,
    RouterSpec,
    apply,
    net_test,
    render_dnsmasq,
    render_nft,
)
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app

BRIDGES = {"default": "br0", "web": "ibr10", "db": "ibr20"}


def test_router_defaults_and_rules():
    r = RouterSpec("web", "10.0.10.1/24")
    r.validate()
    assert [str(a) for a in r.dhcp_range()] == ["10.0.10.100", "10.0.10.200"]
    small = RouterSpec("db", "10.0.20.1/28")
    assert [str(a) for a in small.dhcp_range()] == ["10.0.20.8", "10.0.20.14"]
    for bad in (
        RouterSpec("default", "10.0.0.1/24"),  # the LAN has its own gateway
        RouterSpec("web", "10.0.10.0/24"),  # network address
        RouterSpec("web", "10.0.10.1/30"),
        RouterSpec("web", "10.0.10.1/24", dhcp_start="10.0.10.1", dhcp_end="10.0.10.9"),
        RouterSpec("web", "10.0.10.1/24", dhcp_start="10.0.11.5", dhcp_end="10.0.11.9"),
    ):
        with pytest.raises(ValueError):
            bad.validate()
    overlap = EdgeConfig([r, RouterSpec("db", "10.0.10.129/25", dhcp=False)])
    with pytest.raises(ValueError, match="overlaps"):
        overlap.validate(set(BRIDGES))


def test_firewall_rules_render_for_routed_and_bridged_traffic():
    cfg = EdgeConfig(
        routers=[RouterSpec("web", "10.0.10.1/24"), RouterSpec("db", "10.0.20.1/24", nat=False)],
        rules=[
            FirewallRule("web-to-db-sql", "allow", "web", "db", "tcp", 5432),
            FirewallRule("web-to-db", "deny", "web", "db"),
            FirewallRule("no-ssh-in-web", "deny", "any", "web", "tcp", 22),
            FirewallRule("off", "deny", "any", "any", enabled=False),
        ],
    )
    cfg.validate(set(BRIDGES))
    nft = render_nft(cfg, BRIDGES, "br0", router=True)
    inet = nft.split("table inet ibhics_fw {")[1].split("table bridge")[0]
    assert 'iifname "ibr10" oifname "ibr20" meta l4proto tcp th dport 5432 counter accept' in inet
    assert inet.index("web-to-db-sql") < inet.index('"web-to-db"')
    bridge = nft.split("table bridge ibhics_fw {")[1].split("table ip")[0]
    assert "web-to-db" not in bridge  # never on one bridge
    assert 'meta obrname "ibr10" meta l4proto tcp th dport 22 counter drop' in bridge
    assert '"off"' not in nft
    assert 'ip saddr 10.0.10.0/24 oifname "br0" masquerade' in nft
    assert "10.0.20.0/24" not in nft.split("table ip ibhics_nat")[-1]  # db: routed, no NAT
    assert "masquerade" not in render_nft(cfg, BRIDGES, "br0", router=False)


def test_bad_rules():
    for rule in (
        FirewallRule("x", "deny", "nowhere", "any"),
        FirewallRule("x", "deny", "any", "any", "icmp", 80),
        FirewallRule("x", "drop"),
    ):
        with pytest.raises(ValueError):
            rule.validate(set(BRIDGES))


def test_dnsmasq_conf():
    conf = render_dnsmasq(RouterSpec("web", "10.0.10.1/24", dns="1.1.1.1"), "ibr10")
    assert "interface=ibr10" in conf and "port=0" in conf
    assert "dhcp-range=10.0.10.100,10.0.10.200,255.255.255.0,12h" in conf
    assert "option:router,10.0.10.1" in conf and "dns-server,1.1.1.1" in conf


def test_apply_on_a_node(tmp_path):
    ran = []

    def run(cmd):
        ran.append(cmd)
        return ""

    cfg = EdgeConfig([RouterSpec("web", "10.0.10.1/24")])
    apply(cfg, BRIDGES, run, router=True, root=str(tmp_path))
    assert ["ip", "addr", "add", "10.0.10.1/24", "dev", "ibr10"] in ran
    assert not any("br0" in c and c[:2] == ["ip", "-4"] for c in ran)  # the LAN is untouched
    assert ["nft", "-f", str(tmp_path / "etc/ibhics/edge.nft")] in ran
    assert (tmp_path / "etc/ibhics/dnsmasq/web.conf").is_file()
    assert (tmp_path / "etc/sysctl.d/90-ibhics-router.conf").is_file()
    # Router gone: address flushed, DHCP stopped, forwarding file removed.
    (tmp_path / "run").mkdir()
    (tmp_path / "run/ibhics-dhcp-web.pid").write_text("4242")
    ran.clear()
    apply(EdgeConfig(), BRIDGES, run, router=True, root=str(tmp_path))
    assert ["kill", "4242"] in ran
    assert not (tmp_path / "etc/ibhics/dnsmasq/web.conf").exists()
    assert not (tmp_path / "etc/sysctl.d/90-ibhics-router.conf").exists()


def test_net_test_commands():
    seen = []

    def run(cmd):
        seen.append(cmd)
        return 1, "100% packet loss"

    ok, out = net_test("ping", "10.0.10.5", run, "ibr10")
    assert not ok and seen[-1] == ["ping", "-c", "4", "-W", "2", "-n", "-I", "ibr10", "10.0.10.5"]
    net_test("trace", "example.com", run)
    assert seen[-1][0] == "tracepath"
    for bad in ("-f", "a b", "x;reboot"):
        with pytest.raises(ValueError):
            net_test("ping", bad, run)


def test_gateway_api_removed_but_firewall_persistence_and_diagnostics_work(tmp_path):
    driver = FakeDriver(demo_console=False)
    driver.create_network(NetworkSpec("web", 10))
    driver.create_network(NetworkSpec("db", 20))
    client = TestClient(create_app(driver, edge_state=tmp_path / "edge.json"))
    assert client.put("/api/v1/edge/routers/web", json={"gateway": "10.0.10.1/24"}).status_code in (
        404,
        405,
    )
    assert client.delete("/api/v1/edge/routers/web").status_code in (404, 405)
    assert client.put("/api/v1/edge/router-node", json={"node": "node2"}).status_code in (404, 405)
    rules = [{"name": "web-db", "action": "deny", "src": "web", "dst": "db"}]
    response = client.put("/api/v1/edge/rules", json=rules)
    assert response.status_code == 200 and response.json()["rules"][0]["name"] == "web-db"
    assert "routers" not in response.json() and "router_node" not in response.json()
    assert driver.edge[1] is False and driver.edge[0]["routers"] == []
    assert client.put("/api/v1/edge/rules", json=[{"name": "x", "src": "mars"}]).status_code >= 400
    test = client.post("/api/v1/edge/test", json={"target": "10.0.10.5", "network": "web"})
    assert test.json()["ok"] and "-I ibr10" in test.json()["output"]
    again = TestClient(create_app(driver, edge_state=tmp_path / "edge.json"))
    assert again.get("/api/v1/edge").json()["rules"][0]["name"] == "web-db"


def test_firewall_reaches_all_nodes_without_enabling_a_gateway(tmp_path):
    from test_migration import Env

    env = Env(tmp_path)
    for node in (env.node1, env.node2):
        node.create_network(NetworkSpec("web", 10))
    client = TestClient(create_app(env.cluster, edge_state=tmp_path / "edge.json"))
    response = client.put("/api/v1/edge/rules", json=[{"name": "isolate-web", "src": "web"}])
    assert response.status_code == 200
    for node in (env.node1, env.node2):
        assert node.edge[1] is False and node.edge[0]["routers"] == []
        assert node.edge[0]["rules"][0]["name"] == "isolate-web"
    test = client.post("/api/v1/edge/test", json={"kind": "trace", "target": "1.1.1.1"})
    assert test.status_code == 200 and test.json()["node"]


def test_agent_rejects_gateway_commands_but_accepts_firewall():
    from ibhics.agent.app import create_agent_app
    from ibhics.cluster import NodeRole

    driver = FakeDriver(demo_console=False)
    client = TestClient(
        create_agent_app(name="node", role=NodeRole.COMPUTE, token="test-token", driver=driver)
    )
    headers = {"X-ibhics-token": "test-token"}
    assert (
        client.put(
            "/agent/v1/edge",
            headers=headers,
            json={"router": True, "config": {"routers": [], "rules": []}},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/agent/v1/edge",
            headers=headers,
            json={
                "router": False,
                "config": {"routers": [{"network": "web", "gateway": "10.0.10.1/24"}], "rules": []},
            },
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/agent/v1/edge",
            headers=headers,
            json={"router": False, "config": {"routers": [], "rules": []}},
        ).status_code
        == 204
    )


def test_legacy_edge_state_keeps_firewall_without_reenabling_gateway(tmp_path):
    import json

    from ibhics.cluster import Cluster
    from ibhics.manager.edge import EdgeManager

    driver = FakeDriver(demo_console=False)
    path = tmp_path / "edge.json"
    path.write_text(
        json.dumps(
            {
                "routers": [{"network": "web", "gateway": "10.0.10.1/24"}],
                "router_node": "old",
                "rules": [{"name": "deny-all", "action": "deny"}],
            }
        )
    )
    manager = EdgeManager(Cluster.standalone("node", driver), path)
    manager.push()
    assert driver.edge[1] is False and driver.edge[0]["routers"] == []
    assert manager.view()["rules"][0]["name"] == "deny-all"
