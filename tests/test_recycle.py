"""Recycle bin: deleted VMs are kept for a while and can be restored."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ibhics.hypervisor import DiskSpec, VMSpec
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app


@pytest.fixture
def env():
    d = FakeDriver(demo_console=False)
    app = create_app(d, recycle_days=7)
    vm = d.create_vm(VMSpec(name="old-app", disks=[DiskSpec(size_gb=1)]))
    return d, TestClient(app), app, vm.uuid


def test_delete_goes_to_bin_and_restores(env):
    d, api, _, uuid = env
    assert api.delete(f"/api/v1/vms/{uuid}").status_code == 204
    vm = d.get_vm(uuid)
    assert vm.spec.recycled  # kept, with its disks
    assert api.post(f"/api/v1/vms/{uuid}/start").status_code == 409
    resp = api.post("/api/v1/vms", json={"name": "old-app"})
    assert resp.status_code == 409 and "recycle bin" in resp.json()["detail"]

    bin_ = api.get("/api/v1/recycle-bin").json()
    assert bin_["days"] == 7 and [r["vm"]["name"] for r in bin_["vms"]] == ["old-app"]
    assert bin_["vms"][0]["expires"]

    assert api.post(f"/api/v1/recycle-bin/{uuid}/restore").json()["recycled"] == ""
    assert api.post(f"/api/v1/vms/{uuid}/start").status_code == 200


def test_empty_one_and_permanent_delete(env):
    d, api, _, uuid = env
    api.delete(f"/api/v1/vms/{uuid}")
    assert api.delete(f"/api/v1/recycle-bin/{uuid}").status_code == 204
    assert d.list_vms() == []
    other = d.create_vm(VMSpec(name="tmp", disks=[DiskSpec(size_gb=1)]))
    assert api.delete(f"/api/v1/vms/{other.uuid}?permanent=true").status_code == 204
    assert d.list_vms() == []


def test_running_vm_is_not_deleted(env):
    d, api, _, uuid = env
    d.start_vm(uuid)
    assert api.delete(f"/api/v1/vms/{uuid}").status_code == 409


def test_expired_vms_are_purged(env):
    d, api, app, uuid = env
    api.delete(f"/api/v1/vms/{uuid}")
    long_ago = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    d.set_recycled(uuid, long_ago)
    keep = d.create_vm(VMSpec(name="keep", disks=[DiskSpec(size_gb=1)]))
    api.delete(f"/api/v1/vms/{keep.uuid}")
    from ibhics.manager.recycle import RecycleBin

    bin_ = RecycleBin(app.state.cluster, days=7)
    assert bin_.purge_expired() == ["old-app"]
    assert [v.spec.name for v in d.list_vms()] == ["keep"]


def test_bin_off_deletes_at_once():
    d = FakeDriver(demo_console=False)
    api = TestClient(create_app(d))
    vm = d.create_vm(VMSpec(name="x", disks=[DiskSpec(size_gb=1)]))
    assert api.delete(f"/api/v1/vms/{vm.uuid}").status_code == 204
    assert d.list_vms() == []
    assert api.get("/api/v1/recycle-bin").json() == {"days": 0, "vms": []}
