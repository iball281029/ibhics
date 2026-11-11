from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_migration import Env

from ibhics.hypervisor import DiskSpec, VMSpec
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.poollabels import PoolLabels


def test_label_does_not_change_vm_pool_and_survives_restart(tmp_path):
    driver = FakeDriver()
    vm = driver.create_vm(VMSpec(name="keep", disks=[DiskSpec(size_gb=1)]))
    path = tmp_path / "labels.json"
    client = TestClient(create_app(driver, pool_labels_path=path))
    node = client.get("/api/v1/pools").json()[0]["node"]
    res = client.patch(f"/api/v1/nodes/{node}/pools/default", json={"display_name": "พื้นที่ VM"})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "default"
    assert res.json()["display_name"] == "พื้นที่ VM"
    assert driver.get_vm(vm.uuid).spec.disks[0].pool == "default"
    fresh = TestClient(create_app(driver, pool_labels_path=path))
    assert fresh.get("/api/v1/pools").json()[0]["display_name"] == "พื้นที่ VM"


def test_rename_remote_pool_is_visible_through_agent(tmp_path):
    env = Env(tmp_path)
    res = env.client.patch("/api/v1/nodes/node2/pools/default", json={"display_name": "Node 2 SSD"})
    assert res.status_code == 200, res.text
    pools = env.client.get("/api/v1/pools").json()
    assert next(p for p in pools if p["node"] == "node2")["display_name"] == "Node 2 SSD"
    assert next(p for p in pools if p["node"] == "node1")["display_name"] == ""


def test_reject_control_characters_and_clear_label(tmp_path):
    labels = PoolLabels(tmp_path / "labels.json")
    with pytest.raises(ValueError):
        labels.set("default", "bad\nname")
    labels.set("default", "SSD")
    labels.set("default", "")
    assert labels.get("default") == ""


def test_manager_and_agent_updates_preserve_other_labels(tmp_path):
    path = tmp_path / "labels.json"
    stores = [PoolLabels(path), PoolLabels(path)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda i: stores[i % 2].set(f"pool{i}", f"Label {i}"), range(30)))
    assert all(stores[0].get(f"pool{i}") == f"Label {i}" for i in range(30))
