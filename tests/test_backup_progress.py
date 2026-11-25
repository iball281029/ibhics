"""Observable backup work, safe zero skipping, cancellation and bounded NFS accounting."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import test_backup
from test_backup import CHUNK, make_vm, new_vm

from ibhics.backup import (
    BackupCancelled,
    BackupTarget,
    DirStore,
    NodeBackup,
    Repository,
    TargetType,
    service,
)
from ibhics.hypervisor import VMSpec
from ibhics.hypervisor.backup_cbt import CBTUnavailable, NBDReader
from ibhics.hypervisor.fake import FakeDriver
from ibhics.hypervisor.serialize import spec_to_json

api = test_backup.api


def test_broken_incremental_checkpoint_retries_full_pull_without_exporting_disk(tmp_path):
    driver = FakeDriver()
    vm = driver.create_vm(VMSpec(name="checkpoint-recovery"))
    target = BackupTarget(name="repo", type=TargetType.LOCAL, path=str(tmp_path / "repo"))
    repo = Repository(DirStore(tmp_path / "repo"), CHUNK)
    parent_data = b"a" * CHUNK
    parent = repo.backup_readers(
        vm,
        [NBDReader(Allocation(parent_data, [(CHUNK, 0)]), False)],
        spec_to_json(vm.spec),
        {"checkpoint": "broken", "incremental": False},
    )
    current = b"b" * CHUNK + b"\0" * CHUNK
    calls = []

    @contextmanager
    def capture(uuid, previous):
        calls.append(previous)
        if previous is not None:
            raise CBTUnavailable("missing or broken bitmap")
        yield (
            [NBDReader(Allocation(current, [(CHUNK, 0), (CHUNK, 2)]), False)],
            {"checkpoint": "fresh", "incremental": False},
        )

    @contextmanager
    def export(uuid):
        pytest.fail("Full disk export must not be used when full pull works")
        yield []

    driver.export_backup = capture
    driver.export_disks = export
    node = NodeBackup(driver, chunk_size=CHUNK)
    point = node.backup(vm.uuid, target)
    assert len(calls) == 2 and calls[0]["id"] == parent.id and calls[1] is None
    assert point.read_bytes == CHUNK and point.size_bytes == len(current)
    repo.restore_disk(repo.manifest(point.id), 0, tmp_path / "restored")
    assert (tmp_path / "restored").read_bytes() == current


class Allocation:
    def __init__(self, data, extents, supported=True):
        self.data, self.extents, self.supported, self.reads = data, extents, supported, []

    def get_size(self):
        return len(self.data)

    def can_meta_context(self, context):
        return self.supported and context == "base:allocation"

    def block_status(self, count, offset, callback):
        values = []
        cursor = 0
        for length, flags in self.extents:
            end = cursor + length
            if end > offset:
                values.extend([end - max(cursor, offset), flags])
            cursor = end
        callback("base:allocation", offset, values, 0)

    def pread(self, count, offset):
        self.reads.append((offset, count))
        return self.data[offset : offset + count]


@pytest.mark.parametrize("supported", [True, False])
def test_full_skips_only_proven_zero_and_restores_holes_and_unknown_flags(tmp_path, supported):
    data = b"a" * CHUNK + b"\0" * CHUNK + b"c" * CHUNK + b"tail"
    handle = Allocation(data, [(CHUNK, 0), (CHUNK, 2), (CHUNK, 1), (4, 0x100)], supported)
    repo = Repository(DirStore(tmp_path / "repo"), CHUNK)
    vm, snapshots = make_vm(), []
    info = repo.backup_readers(
        vm,
        [NBDReader(handle, False)],
        spec_to_json(vm.spec),
        {"incremental": False},
        metrics=lambda data: snapshots.append(data),
    )
    restored = tmp_path / "restored"
    repo.restore_disk(repo.manifest(info.id), 0, restored)
    assert restored.read_bytes() == data
    assert info.read_bytes == len(data) - (CHUNK if supported else 0)
    assert ((CHUNK, CHUNK) not in handle.reads) is supported
    assert snapshots[-1]["phase"] == "committing"
    assert snapshots[-1]["scanned_bytes"] == len(data)
    assert 0 <= snapshots[-1]["percent"] < 100


def test_zero_extents_crossing_chunk_boundaries_do_not_hide_partial_data():
    data = b"x" * (CHUNK // 2) + b"\0" * CHUNK + b"y" * (CHUNK // 2)
    handle = Allocation(data, [(CHUNK // 2, 0), (CHUNK, 2), (CHUNK // 2, 0)])
    assert list(NBDReader(handle, False).changed_chunks(CHUNK)) == [0, 1]


def test_all_zero_full_backup_has_no_disk_reads_and_is_restorable(tmp_path):
    handle = Allocation(b"\0" * (CHUNK * 4), [(CHUNK * 4, 3)])
    repo = Repository(DirStore(tmp_path / "repo"), CHUNK)
    vm = make_vm()
    info = repo.backup_readers(vm, [NBDReader(handle, False)], spec_to_json(vm.spec), {})
    assert info.read_bytes == 0 and handle.reads == []
    repo.restore_disk(repo.manifest(info.id), 0, tmp_path / "restored")
    assert (tmp_path / "restored").read_bytes() == handle.data


def test_malformed_allocation_never_publishes_restore_point(tmp_path):
    handle = Allocation(b"x" * CHUNK, [(0, 0)])
    repo = Repository(DirStore(tmp_path / "repo"), CHUNK)
    with pytest.raises(RuntimeError, match="allocation"):
        repo.backup_readers(make_vm(), [NBDReader(handle, False)], {}, {})
    assert repo.list() == []


def wait_until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError("Timed out waiting for backup state")


def test_waiting_backup_is_cancellable_without_stopping_vm_or_scanning_repository(
    tmp_path, monkeypatch
):
    driver = FakeDriver()
    vm = driver.create_vm(VMSpec(name="queued"))
    driver.start_vm(vm.uuid)
    node = NodeBackup(driver, chunk_size=CHUNK)
    target = BackupTarget("repo", TargetType.LOCAL, path=str(tmp_path / "repo"))
    scans = []
    monkeypatch.setattr(service, "dir_bytes", lambda path: scans.append(path) or 100)
    first = node.usage(target)
    assert first["used_bytes"] == 100 and len(scans) == 1
    with node._locks[target.name], ThreadPoolExecutor(1) as pool:
        future = pool.submit(node.backup, vm.uuid, target)
        wait_until(lambda: node.backup_progress(vm.uuid).get("phase") == "waiting")
        for _ in range(3):
            usage = node.usage(target)
            assert usage["accounting_deferred"] and usage["used_bytes"] == 100
        assert len(scans) == 1
        assert node.cancel_backup(vm.uuid, target.name)
        with pytest.raises(BackupCancelled):
            future.result(timeout=3)
    assert node.backup_progress(vm.uuid)["state"] == "cancelled"
    assert driver.get_vm(vm.uuid).state.value == "running"
    assert node.list(target) == []


def test_uncached_usage_during_backup_is_unknown_not_zero(tmp_path, monkeypatch):
    node = NodeBackup(FakeDriver())
    target = BackupTarget("repo", TargetType.LOCAL, path=str(tmp_path / "repo"), quota_gb=1)
    node._progress[("vm", "repo")] = {"state": "running", "target": "repo"}
    monkeypatch.setattr(service, "dir_bytes", lambda root: pytest.fail("busy repository scan"))
    usage = node.usage(target)
    assert usage["used_bytes"] is None and usage["free_bytes"] is None
    assert usage["accounting_deferred"]


def test_cancellation_before_manifest_keeps_previous_restore_point(tmp_path):
    repo = Repository(DirStore(tmp_path / "repo"), CHUNK)
    vm = make_vm()
    disk = tmp_path / "disk"
    original = b"a" * CHUNK
    disk.write_bytes(original)
    base = repo.backup(vm, [disk], spec_to_json(vm.spec))
    disk.write_bytes(b"b" * CHUNK)

    def cancel(data):
        if data["phase"] == "committing":
            raise BackupCancelled("operator")

    with pytest.raises(BackupCancelled):
        repo.backup(vm, [disk], spec_to_json(vm.spec), metrics=cancel)
    assert [point.id for point in repo.list()] == [base.id]
    repo.restore_disk(repo.manifest(base.id), 0, tmp_path / "restore")
    assert (tmp_path / "restore").read_bytes() == original


def test_cancel_queued_job_does_not_execute_or_retry(api):
    client, _, manager, _ = api
    vm = new_vm(client)
    pending = []
    manager._executor = SimpleNamespace(submit=lambda work: pending.append(work))
    job = manager.start_backup(vm["uuid"], "local")
    response = client.post(f"/api/v1/backup/jobs/{job.id}/cancel")
    assert response.status_code == 202 and response.json()["state"] == "cancelled"
    pending[0]()
    assert job.state == "cancelled" and job.retry_at is None
    assert manager.list_backups("local") == []


def test_cancel_running_job_through_api_preserves_vm_and_previous_backups(api):
    client, _, manager, _ = api
    vm = new_vm(client)
    node = manager._cluster.member("node1").backups
    with ThreadPoolExecutor(1) as pool:
        manager._executor = pool
        with node._locks["local"]:
            job = manager.start_backup(vm["uuid"], "local")
            wait_until(lambda: node.backup_progress(vm["uuid"]).get("phase") == "waiting")
            status = client.get("/api/v1/backup/jobs").json()[0]
            assert status["state"] == "running"
            response = client.post(f"/api/v1/backup/jobs/{job.id}/cancel")
            assert response.status_code == 202
            wait_until(lambda: job.state == "cancelled")
        assert job.error is None and job.retry_at is None
    assert manager.list_backups("local") == []
    assert client.get(f"/api/v1/vms/{vm['uuid']}").status_code == 200


def test_commit_stage_refuses_cancellation():
    node = NodeBackup(FakeDriver())
    key = ("vm", "repo")
    node._progress[key] = {"state": "running", "phase": "committing"}
    node._cancel[key] = threading.Event()
    assert not node.cancel_backup(*key)
    assert not node._cancel[key].is_set()


def test_incremental_zero_changes_are_not_skipped_by_allocation_metadata():
    handle = Allocation(b"\0" * (CHUNK * 2), [])
    handle.can_meta_context = lambda context: True

    def status(count, offset, callback):
        callback("base:allocation", 0, [CHUNK * 2, 2], 0)
        callback("qemu:dirty-bitmap:changes", 0, [CHUNK, 0, CHUNK, 1], 0)

    handle.block_status = status
    assert list(NBDReader(handle, True).changed_chunks(CHUNK)) == [1]


def test_cancel_endpoint_requires_backup_permission(tmp_path):
    from fastapi.testclient import TestClient

    from ibhics.manager import create_app
    from ibhics.manager.auth import Auth

    auth = Auth(tmp_path / "users.json")
    password = "Backup-cancel-test-42"
    auth.bootstrap(password, must_change=False)
    auth.add_user("viewer", "viewer", password)
    auth.user("viewer").must_change_password = False
    auth.add_user("operator", "operator", password)
    auth.user("operator").must_change_password = False
    app = create_app(FakeDriver(), auth=auth)
    job = app.state.backups._new_job("backup", "repo", "vm", "node1", None)
    client = TestClient(app)
    endpoint = f"/api/v1/backup/jobs/{job.id}/cancel"
    assert client.post(endpoint).status_code == 401
    client.post("/api/v1/auth/login", json={"username": "viewer", "password": password})
    assert client.post(endpoint).status_code == 403
    assert job.state == "queued"
    client.post("/api/v1/auth/login", json={"username": "operator", "password": password})
    assert client.post(endpoint).status_code == 202
    assert job.state == "cancelled"
