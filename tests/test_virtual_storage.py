import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_migration import Env

from ibhics.manager import create_app
from ibhics.storage import VirtualStoragePlan, readiness


def plan():
    return {
        "name": "hybrid01",
        "nodes": [
            {"name": "node1", "address": "172.16.12.104", "device": "/dev/vg/data"},
            {"name": "node2", "address": "172.16.12.103", "device": "/dev/vg/data"},
        ],
        "arbiter_name": "witness",
        "arbiter_address": "172.16.12.105",
        "virtual_ip": "172.16.12.106",
        "prefix": 24,
        "size_gib": 3500,
    }


def test_configuration_has_two_replicas_and_diskless_arbiter():
    conf = VirtualStoragePlan(**plan()).drbd_config()
    assert conf.count("meta-disk internal;") == 2
    assert "disk none;" in conf and "quorum majority;" in conf
    assert "allow-two-primaries no;" in conf
    assert "on-no-quorum suspend-io;" in conf
    assert "hosts node1 node2 witness;" in conf


@pytest.mark.parametrize(
    "change",
    [
        {"virtual_ip": "172.16.12.104"},
        {"virtual_ip": "172.16.13.106"},
        {"arbiter_name": "node1"},
        {"name": "bad; rm -rf /"},
        {"virtual_ip": "172.16.12.255"},
    ],
)
def test_invalid_address_or_name_is_refused(change):
    with pytest.raises(ValidationError):
        VirtualStoragePlan(**(plan() | change))


def test_saving_plan_does_not_publish_live_storage(tmp_path):
    env = Env(tmp_path)
    path = tmp_path / "virtual-storage.json"
    client = TestClient(create_app(env.cluster, virtual_storage_path=path))
    response = client.put("/api/v1/virtual-storage/plan", json=plan())
    assert response.status_code == 200, response.text
    assert not response.json()["active"]
    assert json.loads(path.read_text())["name"] == "hybrid01"
    assert path.stat().st_mode & 0o777 == 0o600
    assert client.get("/api/v1/virtual-storage/configuration").text.startswith("resource hybrid01")
    assert [p.name for p in env.node1.list_pools()] == ["default"]


def test_refuses_plan_while_vm_uses_source_pool(tmp_path):
    env = Env(tmp_path)
    env.add_shared_pool()
    env.create_vm("keep", pool="shared")
    path = tmp_path / "plan.json"
    client = TestClient(create_app(env.cluster, virtual_storage_path=path))
    data = plan() | {"name": "shared"}
    assert client.put("/api/v1/virtual-storage/plan", json=data).status_code == 409
    assert not path.exists()


def test_refuses_old_drbd_kernel(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tool")
    run = Mock(return_value=Mock(stdout="8.4.11", returncode=0))
    result = readiness(run)
    assert not result["module_available"] and "DRBD 9" in result["error"]
    assert run.call_count == 1
