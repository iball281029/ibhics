"""v1.0 containers: templates, the LXC config and first-boot script, the API, the agent."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from ibhics.hypervisor.containers import (
    ContainerSpec,
    LxcContainers,
    catalog,
    first_boot_script,
    lxc_config,
)
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app


def test_catalog_has_os_and_app_templates():
    ids = {t["id"] for t in catalog()}
    assert {"debian-13", "ubuntu-24.04", "app:nginx", "app:docker"} <= ids


def test_spec_validation():
    ContainerSpec("web-1", ip="10.0.0.5/24", gateway="10.0.0.1", dns="1.1.1.1").validate()
    for bad in (
        ContainerSpec("Web_1"),
        ContainerSpec("web", template="windows"),
        ContainerSpec("web", ip="10.0.0.5"),
        ContainerSpec("web", ip="10.0.0.5/24", gateway="10.9.0.1"),
        ContainerSpec("web", ssh_key="not a key"),
    ):
        with pytest.raises(ValueError):
            bad.validate()
    docker = ContainerSpec("d", template="app:docker")
    docker.validate()
    assert docker.nesting and docker.distro == ("debian", "trixie")


def test_config_is_unprivileged_with_limits():
    spec = ContainerSpec("web", vcpus=2, memory_mb=1024, ip="10.0.0.5/24", gateway="10.0.0.1")
    cfg = lxc_config(spec, "br0", "/p/containers/web/rootfs.img")
    assert "lxc.idmap = u 0 100000 65536" in cfg
    assert "lxc.net.0.link = br0" in cfg and "lxc.cgroup2.memory.max = 1024M" in cfg
    assert "lxc.cgroup2.cpu.max = 200000 100000" in cfg
    assert "lxc.net.0.ipv4.address = 10.0.0.5/24" in cfg
    assert "rootfs.path = loop:/p/containers/web/rootfs.img" in cfg
    script = first_boot_script(
        ContainerSpec("web", template="app:nginx", password="p'w", ssh_key="ssh-ed25519 AAA x")
    )
    assert "chpasswd" in script and "'p'\"'\"'w'" in script and "install -y nginx" in script


def test_lxc_create_runs_download_template(tmp_path):
    calls = []

    def run(cmd):
        calls.append(cmd)
        if cmd[0] == "lxc-info":
            return 0, "State: RUNNING\nIP: 10.0.0.9\nMemory use: 52428800\n"
        return 0, ""

    lxc = LxcContainers(lambda p: str(tmp_path / p), lambda n: "br0", run, lambda: ["default"])
    info = lxc.create(ContainerSpec("web", password="secret", disk_gb=4))
    create = next(c for c in calls if c[0] == "lxc-create")
    assert create[create.index("--fssize") + 1] == "4G" and "trixie" in create
    assert info.state == "running" and info.addresses == ["10.0.0.9"]
    meta = json.loads((tmp_path / "default/containers/web/ibhics.json").read_text())
    assert meta["password"] == ""  # never stored
    assert any(c[:1] == ["timeout"] and "chpasswd" in c[-1] for c in calls)
    with pytest.raises(Exception, match="shrink"):
        lxc.update("web", None, None, 2, None, None)


def wait_for(client, name):
    for _ in range(50):
        r = client.get(f"/api/v1/containers/{name}")
        if r.status_code == 200:
            return r.json()
        time.sleep(0.02)
    raise AssertionError(f"{name} was not created")


def test_container_api_lifecycle():
    client = TestClient(create_app(FakeDriver()))
    assert client.get("/api/v1/containers/templates").status_code == 200
    r = client.post("/api/v1/containers", json={"name": "web", "template": "app:nginx"})
    assert r.status_code == 202
    ct = wait_for(client, "web")
    assert ct["state"] == "running" and "password" not in ct and ct["node"]
    assert client.post("/api/v1/containers", json={"name": "web"}).status_code == 409
    r = client.post("/api/v1/containers/web/action", json={"action": "stop"})
    assert r.json()["state"] == "stopped"
    r = client.patch("/api/v1/containers/web", json={"memory_mb": 2048, "disk_gb": 20})
    assert r.json()["memory_mb"] == 2048 and r.json()["disk_gb"] == 20
    assert client.patch("/api/v1/containers/web", json={"disk_gb": 2}).status_code == 409
    client.post("/api/v1/containers/web/action", json={"action": "start"})
    r = client.post("/api/v1/containers/web/exec", json={"command": "uptime"})
    assert r.json()["code"] == 0
    snaps = client.post("/api/v1/containers/web/snapshots", json={"name": "s1"}).json()
    assert snaps["snapshots"] == ["s1"]
    assert client.post("/api/v1/containers/web/snapshots/s1/restore").status_code == 200
    kinds = {j["kind"] for j in client.get("/api/v1/backup/jobs").json()}
    assert {"container-create", "snapshot"} <= kinds
    assert client.delete("/api/v1/containers/web").status_code == 204
    assert client.get("/api/v1/containers/web").status_code == 404


def test_containers_through_node_agents(tmp_path):
    from test_migration import Env

    env = Env(tmp_path)
    client = TestClient(create_app(env.cluster))
    r = client.post("/api/v1/containers", json={"name": "db", "node": "node2"})
    assert r.status_code == 202 and r.json()["node"] == "node2"
    assert wait_for(client, "db")["node"] == "node2"
    r = client.post("/api/v1/containers/db/snapshots", json={"name": "before"})
    assert r.json()["snapshots"] == ["before"]
    assert client.delete("/api/v1/containers/db/snapshots/nope").status_code == 409
    assert client.delete("/api/v1/containers/db").status_code == 204
