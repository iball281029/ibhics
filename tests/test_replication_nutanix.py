"""Prism mock: export snapshot clone disks only; cleanup precedes publication."""

import json
from pathlib import Path

import httpx
import pytest

from ibhics.importer import SourceVM
from ibhics.importer.nutanix import NutanixError, Prism
from ibhics.replication.nutanix import NutanixSnapshotProvider
from ibhics.replication.providers import capability
from ibhics.replication.store import ReplicaStore
from tests.test_nutanix import SOURCE


def entity(ids, *, power="OFF", nics=None):
    return {
        "status": {
            "state": "COMPLETE",
            "resources": {
                "power_state": power,
                "nic_list": nics or [],
                "disk_list": [
                    {
                        "uuid": identity,
                        "disk_size_bytes": 8,
                        "device_properties": {
                            "device_type": "DISK",
                            "disk_address": {"adapter_type": "SCSI", "device_index": i},
                        },
                    }
                    for i, identity in enumerate(ids)
                ],
            },
        }
    }


class PrismMock:
    def __init__(self, fail=None):
        self.fail = fail
        self.deleted = []
        self.snapshot = self.clone = None
        self.images = []
        self.image_ids = {}
        self.calls = []
        self.data = [b"aaaabbbb", b"ccccdddd"]

    def handle(self, request):
        path = request.url.path.removeprefix("/api/nutanix/v3")
        method = request.method
        self.calls.append((method, path))
        if method == "GET" and path in self.deleted:
            return httpx.Response(404)
        if method == "DELETE":
            self.deleted.append(path)
            if self.fail == "cleanup" and path.startswith("/vms/"):
                return httpx.Response(500)
            return httpx.Response(202, json={"task_uuid": "delete"})
        if method == "GET" and path == "/vms/live":
            return httpx.Response(200, json=entity(["live-0", "live-1"], power="ON"))
        if method == "POST" and path == "/vm_snapshots":
            data = json.loads(request.content)
            self.snapshot = data["metadata"]["uuid"]
            assert data["spec"]["name"].startswith("ibhics-repl-")
            assert data["spec"]["snapshot_type"] == "CRASH_CONSISTENT"
            assert data["spec"]["resources"] == {"entity_uuid": "live"}
            if self.fail == "snapshot":
                raise httpx.ReadTimeout("secret-pw")
            return httpx.Response(
                202, json={"metadata": {"uuid": self.snapshot}, "task_uuid": "create"}
            )
        if method == "GET" and path == f"/vm_snapshots/{self.snapshot}":
            return httpx.Response(200, json={"status": {"state": "COMPLETE"}})
        if method == "POST" and path == "/vms":
            data = json.loads(request.content)
            self.clone = data["metadata"]["uuid"]
            resources = data["spec"]["resources"]
            assert resources["parent_reference"] == {"kind": "vm_snapshot", "uuid": self.snapshot}
            assert resources["power_state"] == "OFF" and resources["nic_list"] == []
            assert data["spec"]["name"].startswith("ibhics-repl-")
            if self.fail == "clone":
                raise httpx.ReadTimeout("secret-pw")
            return httpx.Response(
                202, json={"metadata": {"uuid": self.clone}, "task_uuid": "create"}
            )
        if method == "GET" and path == f"/vms/{self.clone}":
            ids = ["live-0", "live-1"] if self.fail == "live" else ["frozen-0", "frozen-1"]
            return httpx.Response(
                200, json=entity(ids, power="ON" if self.fail == "power" else "OFF")
            )
        if method == "POST" and path == "/images":
            data = json.loads(request.content)
            disk = data["spec"]["resources"]["data_source_reference"]["uuid"]
            assert disk.startswith("frozen-")  # never export a live disk
            assert data["spec"]["name"].startswith("ibhics-repl-")
            index = int(disk[-1])
            self.images.append(index)
            image_id = data["metadata"]["uuid"]
            self.image_ids[image_id] = index
            if self.fail == "image-create":
                raise httpx.ReadTimeout("secret-pw")
            return httpx.Response(
                202,
                json={
                    "metadata": {"uuid": image_id},
                    "status": {"execution_context": {"task_uuid": "image"}},
                },
            )
        if method == "GET" and path.endswith("/file"):
            index = self.image_ids[path.split("/")[-2]]
            if self.fail == "download" and index == 1:
                return httpx.Response(500)
            return httpx.Response(200, content=self.data[index])
        if path.startswith("/tasks/"):
            if self.fail == "task" and path == "/tasks/image":
                return httpx.Response(200, json={"status": "FAILED", "error_detail": "secret-pw"})
            return httpx.Response(200, json={"status": "SUCCEEDED"})
        return httpx.Response(404)


def setup(tmp_path, fail=None):
    fake = PrismMock(fail)
    http = httpx.Client(base_url="https://prism:9440", transport=httpx.MockTransport(fake.handle))
    prism = Prism(SOURCE, http=http, sleep=lambda _: None)
    vm = SourceVM(ref="live", name="vm", disk_paths=["live-0", "live-1"])
    provider = NutanixSnapshotProvider(SOURCE, vm, prism=prism, temp_root=tmp_path)
    store = ReplicaStore(tmp_path / "metadata", [tmp_path / "0", tmp_path / "1"], chunk=4)
    return fake, provider, store


def test_nutanix_snapshot_full_diff_and_cleanup(tmp_path):
    fake, provider, store = setup(tmp_path)
    first = store.sync(provider)
    assert first["change_tracking"] == "full-diff"
    assert "retained reference" in first["fallback_reason"]
    assert [d["key"] for d in first["disks"]] == ["live-0", "live-1"]
    assert [Path(d["path"]).read_bytes() for d in first["disks"]] == fake.data
    assert fake.deleted[-2:] == [f"/vms/{fake.clone}", f"/vm_snapshots/{fake.snapshot}"]
    assert fake.deleted[:2] == ["/images/" + identity for identity in fake.image_ids]
    assert not list(tmp_path.glob("ibhics-repl-*"))
    fake2, provider2, _ = setup(tmp_path)
    fake2.data[0] = b"aaaaxxxx"
    second = store.sync(provider2)
    assert second["changed_bytes"] == 4
    assert second["bytes_transferred"] == 16  # full export/scan, no false CRT claim
    assert capability(SOURCE)["supported"] is True
    assert capability(SOURCE)["change_tracking"] == "full-diff"


@pytest.mark.parametrize(
    "fail", ["snapshot", "clone", "download", "task", "live", "power", "cleanup", "image-create"]
)
def test_nutanix_failure_always_cleans_owned_resources_and_preserves_manifest(tmp_path, fail):
    _, initial, store = setup(tmp_path)
    baseline = store.sync(initial)
    fake, provider, _ = setup(tmp_path, fail)
    with pytest.raises(NutanixError) as error:
        store.sync(provider)
    assert "secret-pw" not in str(error.value)
    assert f"/vm_snapshots/{provider.snapshot}" in fake.deleted
    if provider.clone_attempted:
        assert f"/vms/{provider.clone}" in fake.deleted
    for identity in fake.image_ids:
        assert f"/images/{identity}" in fake.deleted
    assert store.current() == baseline
    assert not list(tmp_path.glob("ibhics-repl-*"))


def test_nutanix_low_space_rejected_before_snapshot(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ibhics.replication import nutanix

    fake, provider, store = setup(tmp_path)
    monkeypatch.setattr(nutanix.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(NutanixError):
        store.sync(provider)
    assert fake.snapshot is None and fake.clone is None
    assert store.current() == {}
    assert not list(tmp_path.glob("ibhics-repl-*"))


def test_nutanix_qcow2_export_is_flattened_before_chunk_reads(tmp_path):
    from types import SimpleNamespace

    fake, provider, store = setup(tmp_path)
    fake.data[0] = b"QFI\xfb" + b"xxxx"
    commands = []

    def convert(args, **kwargs):
        commands.append(args)
        Path(args[-1]).write_bytes(b"aaaabbbb")
        return SimpleNamespace(returncode=0)

    provider.run = convert
    result = store.sync(provider)
    assert commands[0][:6] == ["qemu-img", "convert", "-f", "qcow2", "-O", "raw"]
    assert Path(result["disks"][0]["path"]).read_bytes() == b"aaaabbbb"
    assert fake.deleted[-1] == f"/vm_snapshots/{fake.snapshot}"
