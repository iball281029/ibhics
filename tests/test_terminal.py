"""v0.8 Terminal: allowlisted diagnostic commands on a node."""

import pytest
from fastapi.testclient import TestClient

from ibhics.hypervisor.fake import FakeDriver
from ibhics.hypervisor.terminal import parse, run_line
from ibhics.manager import create_app


def test_allowed_commands():
    assert parse("ping 8.8.8.8") == ["ping", "-c", "4", "8.8.8.8"]
    assert parse("traceroute 1.1.1.1") == ["tracepath", "-n", "1.1.1.1"]
    assert parse("ip -br addr") == ["ip", "-br", "addr"]
    assert parse("nft list ruleset") == ["nft", "list", "ruleset"]
    assert parse("getent hosts example.com")[0] == "getent"
    assert parse("virsh list --all") == ["virsh", "list", "--all"]
    assert "ping" in run_line("help", lambda c: (1, ""))[1]


@pytest.mark.parametrize(
    "line",
    [
        "rm -rf /",
        "bash",
        "ping 1.1.1.1; reboot",
        "ping 1.1.1.1 | tee x",
        "ping $(id)",
        "ping -f 1.1.1.1",
        "ping -c 1000 1.1.1.1",
        "ip route add 10.0.0.0/8 via 1.1.1.1",
        "ip link set eno1 down",
        "ip netns exec x sh",
        "nft flush ruleset",
        "virsh destroy vm1",
        "ss -K dst 1.1.1.1",
        "arping -U 10.0.0.1",
        "ethtool -s eno1 speed 10",
        "hostname evil",
        "",
    ],
)
def test_refused(line):
    with pytest.raises(ValueError):
        parse(line)


def test_terminal_api_local_and_remote(tmp_path):
    client = TestClient(create_app(FakeDriver(demo_console=False)))
    name = client.get("/api/v1/cluster").json()["members"][0]["name"]
    out = client.post(f"/api/v1/nodes/{name}/terminal", json={"command": "ping 10.0.0.1"})
    assert out.status_code == 200 and "ping -c 4 10.0.0.1" in out.json()["output"]
    bad = client.post(f"/api/v1/nodes/{name}/terminal", json={"command": "reboot"})
    assert bad.status_code == 422 and "not allowed" in bad.text

    from test_migration import Env

    env = Env(tmp_path)
    remote = TestClient(create_app(env.cluster))
    out = remote.post("/api/v1/nodes/node2/terminal", json={"command": "ip route"})
    assert out.json() == {"node": "node2", "code": 0, "output": "(fake node) ip route"}
    assert remote.post("/api/v1/nodes/node2/terminal", json={"command": "sh"}).status_code == 422


def test_terminal_is_admin_only_and_audited(tmp_path):
    from test_auth import admin, as_user, env

    e = env.__wrapped__(tmp_path)
    client, auth, *_ = e
    admin(client, auth)
    name = client.get("/api/v1/cluster").json()["members"][0]["name"]
    assert client.post(f"/api/v1/nodes/{name}/terminal", json={"command": "uptime"}).is_success
    targets = [x["target"] for x in client.get("/api/v1/audit").json()]
    assert f"/api/v1/nodes/{name}/terminal uptime" in targets
    op = as_user(e, "op1", "operator")
    assert op.post(f"/api/v1/nodes/{name}/terminal", json={"command": "uptime"}).status_code == 403
