"""Offline replication contract, integrity, lifecycle and API tests."""

from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ibhics.cluster import Cluster
from ibhics.hypervisor import Firmware, InvalidState
from ibhics.hypervisor.fake import FakeDriver
from ibhics.importer import Source, SourceType, SourceVM
from ibhics.manager.app import create_app
from ibhics.manager.imports import ImportManager
from ibhics.manager.replications import ReplicationManager, validate_schedule
from ibhics.replication.providers import ChangeProvider, Disk, Point, capability
from ibhics.replication.store import ReplicaStore


def test_sparse_clone_preserves_holes_and_falls_back_without_seek_data(tmp_path, monkeypatch):
    import errno
    import subprocess

    from ibhics.replication import store as storage

    source = tmp_path / "source"
    with source.open("wb") as image:
        image.write(b"head")
        image.seek(1024 * 1024)
        image.write(b"tail")
        image.truncate(2 * 1024 * 1024)
    monkeypatch.setattr(
        storage.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1)
    )
    expected = source.read_bytes()
    fast = tmp_path / "fast"
    storage.sparse_copy(source, fast, chunk=4096)
    assert fast.read_bytes() == expected

    def unsupported(*args):
        raise OSError(errno.EINVAL, "SEEK_DATA unavailable")

    monkeypatch.setattr(storage.os, "lseek", unsupported)
    fallback = tmp_path / "fallback"
    storage.sparse_copy(source, fallback, chunk=4096)
    assert fallback.read_bytes() == expected


class Provider(ChangeProvider):
    def __init__(self, data, *, fail=False, source_hash=True, fail_begin=False, fail_end=False):
        self.data = data
        self.fail, self.source_hash = fail, source_hash
        self.fail_begin, self.fail_end = fail_begin, fail_end
        self.reads = []
        self.ended = False

    def begin(self):
        if self.fail_begin:
            raise RuntimeError("snapshot setup failed")
        return Point(str(hash(self.data)), (Disk("disk0", len(self.data)),))

    def hash(self, disk, offset, length):
        import hashlib

        return (
            hashlib.sha256(self.data[offset : offset + length]).hexdigest()
            if (self.source_hash)
            else None
        )

    def read(self, disk, offset, length):
        self.reads.append(offset)
        if self.fail and offset >= 4:
            raise RuntimeError("source connection failed")
        return self.data[offset : offset + length]

    def end(self):
        self.ended = True
        if self.fail_end:
            raise RuntimeError("snapshot cleanup failed")


def store(tmp_path):
    return ReplicaStore(tmp_path / "metadata", [tmp_path / "pool"], chunk=4)


def test_hash_delta_and_sparse_zero(tmp_path):
    replica = store(tmp_path)
    first = Provider(b"aaaa" + bytes(4) + b"bbbb")
    initial = replica.sync(first)
    assert first.ended and first.reads == [0, 8]
    assert initial["bytes_transferred"] == 8
    second = Provider(b"aaaa" + bytes(4) + b"cccc")
    result = replica.sync(second)
    assert second.ended and second.reads == [8]
    assert result["bytes_transferred"] == 4 and result["changed_bytes"] == 4
    assert Path(result["disks"][0]["path"]).read_bytes() == second.data
    assert not Path(initial["disks"][0]["path"]).exists()
    third = Provider(bytes(12))
    result = replica.sync(third)
    assert third.reads == []
    assert Path(result["disks"][0]["path"]).read_bytes() == bytes(12)


def test_stream_fallback_checks_hash_locally(tmp_path):
    replica = store(tmp_path)
    replica.sync(Provider(b"aaaabbbb"))
    provider = Provider(b"aaaabbbb", source_hash=False)
    result = replica.sync(provider)
    assert provider.reads == [0, 4]
    assert result["bytes_transferred"] == 8 and result["changed_bytes"] == 0


@pytest.mark.parametrize("failure", ["fail", "fail_begin", "fail_end"])
def test_failed_cycle_keeps_old_replica_and_hashes(tmp_path, failure):
    replica = store(tmp_path)
    before = replica.sync(Provider(b"aaaabbbb"))
    provider = Provider(b"ccccdddd", **{failure: True})
    with pytest.raises(RuntimeError):
        replica.sync(provider)
    assert provider.ended
    assert replica.current() == before
    assert Path(before["disks"][0]["path"]).read_bytes() == b"aaaabbbb"
    assert len(list((tmp_path / "pool").glob("*.raw"))) == 1


def test_test_clone_is_independent(tmp_path):
    replica = store(tmp_path)
    before = replica.sync(Provider(b"aaaabbbb"))
    [clone] = replica.clone(tmp_path / "test")
    clone.write_bytes(b"converted")
    assert Path(before["disks"][0]["path"]).read_bytes() == b"aaaabbbb"
    assert replica.current() == before


def test_multi_disk_failure_never_commits_first_disk(tmp_path):
    class Two(Provider):
        def begin(self):
            return Point("two", (Disk("one", 8), Disk("two", 8)))

        def read(self, disk, offset, length):
            if disk.key == "two":
                raise RuntimeError("disk two fails")
            return super().read(disk, offset, length)

    replica = ReplicaStore(tmp_path / "metadata", [tmp_path / "a", tmp_path / "b"], 4)
    provider = Two(b"aaaabbbb", source_hash=False)
    with pytest.raises(RuntimeError):
        replica.sync(provider)
    assert replica.current() == {} and provider.ended
    assert not list(tmp_path.rglob("*.raw"))


class Inline:
    def submit(self, fn):
        fn()
        result = Future()
        result.set_result(None)
        return result


class Deferred:
    def __init__(self):
        self.work = []

    def submit(self, fn):
        self.work.append(fn)
        return Future()


class Runtime:
    def __init__(self, root):
        self.root = root
        self.fail = False
        self.calls = []

    def store(self, job):
        return store(self.root)

    def sync(self, job, source, vm, progress):
        self.calls.append("sync")
        if self.fail:
            raise RuntimeError("password=SECRET token=SECRET")
        return self.store(job).sync(Provider(b"aaaabbbb"), progress)

    def materialize(self, job, source, vm, *, test, networks):
        self.calls.append((test, networks))
        before = self.store(job).current()
        images = self.store(job).clone(self.root / ("test" if test else "cutover"))
        images[0].write_bytes(b"converted")
        assert self.store(job).current() == before
        return "test-vm" if test else "new-vm"

    def cleanup_test(self, job):
        self.calls.append("cleanup")

    def delete(self, job):
        self.calls.append("delete")


VM = SourceVM("vm", "web", 2, 2048, Firmware.BIOS, [1], 1, state="running")


@pytest.fixture
def env(tmp_path):
    cluster = Cluster.standalone("node1", FakeDriver())
    imports = ImportManager(cluster, discover=lambda source: [VM])
    imports.add_source(Source("kvm", SourceType.KVM, host="host", password="SECRET"))
    runtime = Runtime(tmp_path)
    now = [datetime(2026, 11, 21, 12, tzinfo=UTC)]
    manager = ReplicationManager(
        imports, tmp_path / "jobs.json", runtime=runtime, executor=Inline(), clock=lambda: now[0]
    )
    manager.guard_path = tmp_path / "upgrade.lock"
    client = TestClient(create_app(cluster, imports=imports, replications=manager))
    return client, manager, runtime, now


def create(client):
    response = client.post(
        "/api/v1/migrate/replications",
        json={"source": "kvm", "vm": "vm", "name": "web-copy", "schedule": {"interval_minutes": 5}},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_api_lifecycle_cutover_and_cleanup(env):
    client, manager, runtime, now = env
    job = create(client)
    assert job["state"] == "idle" and job["changed_pct"] == 100
    url = f"/api/v1/migrate/replications/{job['id']}"
    for body in ({}, {"source_powered_off": False}, {"source_powered_off": "true"}):
        assert client.post(url + "/cutover", json=body).status_code == 422
    assert runtime.calls == ["sync"]
    before = runtime.store(None).current()
    assert client.post(url + "/test-failover", json={}).status_code == 202
    assert runtime.calls[-1] == (True, [])
    assert runtime.store(None).current() == before
    assert client.post(url + "/cutover", json={"source_powered_off": True}).status_code == 409
    assert client.delete(url + "?confirm=true").status_code == 409
    assert client.post(url + "/test-cleanup").status_code == 202
    assert client.get(url).json()["test_vm"] is None
    assert client.patch(url, json={"paused": True}).json()["state"] == "paused"
    assert client.post(url + "/sync").status_code == 409
    assert client.patch(url, json={"paused": False}).json()["state"] == "idle"
    now[0] += timedelta(minutes=5)
    assert len(manager.tick()) == 1
    assert manager.tick() == []
    assert (
        client.post(url + "/cutover", json={"source_powered_off": True}).json()["state"] == "done"
    )
    assert runtime.calls[-2:] == ["sync", (False, None)]
    assert client.delete(url).status_code == 422
    assert client.delete(url + "?confirm=true").status_code == 204
    assert client.get(url).status_code == 404


def test_sync_and_cutover_resolve_unique_source_case_change(env):
    client, manager, runtime, _ = env
    job = create(client)
    url = f"/api/v1/migrate/replications/{job['id']}"
    manager.imports.remove_source("kvm")
    manager.imports.add_source(Source("KVM", SourceType.KVM, host="host", password="SECRET"))
    manager.imports.add_source(Source("Kvm", SourceType.KVM, host="other-host", password="SECRET"))

    failed = client.post(url + "/sync").json()
    assert failed["state"] == "failed"
    assert "matches multiple configured names" in failed["error"]
    assert runtime.calls == ["sync"]

    manager.imports.remove_source("Kvm")
    assert client.post(url + "/sync").json()["state"] == "idle"
    result = client.post(url + "/cutover", json={"source_powered_off": True})
    assert result.json()["state"] == "done"
    assert runtime.calls == ["sync", "sync", "sync", (False, None)]


def test_failover_uses_last_replica_after_failed_sync(env):
    client, manager, runtime, now = env
    job = create(client)
    url = f"/api/v1/migrate/replications/{job['id']}"
    previous = runtime.store(None).current()
    manager.imports.remove_source("kvm")
    failed = client.post(url + "/sync").json()
    assert failed["state"] == "failed"
    assert "source 'kvm' is missing" in failed["error"]

    response = client.post(url + "/test-failover", json={})
    assert response.status_code == 202
    tested = response.json()
    assert tested["test_vm"] == "test-vm"
    assert tested["state"] == "failed"
    assert tested["error"] == failed["error"]
    assert runtime.calls == ["sync", (True, [])]
    assert runtime.store(None).current() == previous


def test_schedule_pause_rpo_and_restart(env):
    client, manager, runtime, now = env
    job = create(client)
    url = f"/api/v1/migrate/replications/{job['id']}"
    now[0] += timedelta(minutes=2)
    assert client.get(url).json()["rpo_seconds"] == 120
    assert client.patch(url, json={"schedule": {"at": "13:00"}}).status_code == 200
    assert manager.tick() == []
    now[0] += timedelta(hours=1)
    assert len(manager.tick()) == 1 and manager.tick() == []
    manager.job(job["id"]).state = "syncing"
    manager._save()
    manager.load()
    assert manager.job(job["id"]).state == "failed"
    assert "Interrupted" in manager.job(job["id"]).error
    text = manager.state_path.read_text()
    assert "SECRET" not in text


def test_busy_rejected_and_failed_sync_does_not_expose_secrets(env):
    client, manager, runtime, now = env
    job = create(client)
    url = f"/api/v1/migrate/replications/{job['id']}"
    runtime.fail = True
    assert client.post(url + "/sync").json()["state"] == "failed"
    assert "SECRET" not in client.get(url).text
    notifications = client.get("/api/v1/notifications").json()["events"]
    assert any(e["event_id"] == "replication.synced" for e in notifications)
    notifications = [e for e in notifications if e["event_id"] == "replication.failed"]
    assert len(notifications) == 1
    assert notifications[0]["category"] == "replication"
    assert notifications[0]["level"] == "critical"
    assert "SECRET" not in str(notifications)
    executor = Deferred()
    manager.executor = executor
    assert client.post(url + "/sync").status_code == 202
    assert client.post(url + "/sync").status_code == 409
    # Pause during a running sync asks it to stop at the next chunk.
    assert client.patch(url, json={"paused": True}).json()["stop_requested"] is True
    assert client.delete(url + "?confirm=true").status_code == 409
    executor.work.pop()()
    assert manager.job(job["id"]).state == "failed"


@pytest.mark.parametrize(
    "schedule",
    [
        {},
        {"interval_minutes": 0},
        {"interval_minutes": True},
        {"at": "24:00"},
        {"at": "12:00", "interval_minutes": 5},
        {"cron": "* * * * *"},
    ],
)
def test_invalid_schedule(schedule):
    with pytest.raises(ValueError):
        validate_schedule(schedule)


@pytest.mark.parametrize("kind", list(SourceType))
def test_capabilities_are_explicit(kind):
    cap = capability(Source("src", kind))
    assert cap["change_tracking"] in {
        "cbt",
        "dirty-bitmap",
        "crt",
        "rct",
        "hash-delta",
        "full",
        "full-diff",
    }
    assert isinstance(cap["live"], bool)
    if kind in {SourceType.DISK, SourceType.OVA, SourceType.UPLOAD, SourceType.P2V}:
        assert not cap["supported"] and not cap["live"]
    if kind == SourceType.NUTANIX:
        assert cap["change_tracking"] == "full-diff" and cap["supported"]
    if kind == SourceType.SANGFOR:
        assert cap["change_tracking"] == "full" and not cap["supported"]


def test_capability_api_and_invalid_requests(env):
    client, manager, runtime, now = env
    assert client.get("/api/v1/migrate/sources/kvm/replication-capability").json()["live"] is True
    assert (
        client.get("/api/v1/migrate/sources/uploads/replication-capability").json()["supported"]
        is False
    )
    assert client.get("/api/v1/migrate/sources/missing/replication-capability").status_code == 404
    assert (
        client.post(
            "/api/v1/migrate/replications", json={"source": "uploads", "vm": "x"}
        ).status_code
        == 422
    )


def test_upgrade_lock_excludes_new_sync(env):
    import fcntl

    client, manager, runtime, now = env
    job = create(client)
    with manager.guard_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(InvalidState, match="upgrade"):
            manager.sync(job["id"])
    assert manager.job(job["id"]).state == "idle"


def test_changed_ranges_use_previous_token_and_skip_source_reads(tmp_path):
    replica = store(tmp_path)
    first = replica.sync(Provider(b"aaaabbbb"))

    class Ranges(Provider):
        def list_changed(self, prev_token):
            assert prev_token == first["token"]
            return {"disk0": [(5, 1)]}

    provider = Ranges(b"aaaacccc")
    result = replica.sync(provider)
    assert provider.reads == [4] and result["bytes_transferred"] == 4
    assert Path(result["disks"][0]["path"]).read_bytes() == provider.data


def test_hash_read_mismatch_never_advances_token(tmp_path):
    replica = store(tmp_path)
    before = replica.sync(Provider(b"aaaabbbb"))

    class Inconsistent(Provider):
        def read(self, disk, offset, length):
            return b"xxxx"

    with pytest.raises(ValueError, match="point changed"):
        replica.sync(Inconsistent(b"ccccdddd"))
    assert replica.current() == before


def test_generation_lock_prevents_overlap(tmp_path):
    from ibhics.replication.store import operation_lock

    replica = store(tmp_path)
    with operation_lock(tmp_path / "replication.lock"):
        with pytest.raises(InvalidState):
            replica.sync(Provider(b"aaaabbbb"))
    assert replica.current() == {}


def test_api_rejects_nonboolean_shutdown_acknowledgement(env):
    client, manager, runtime, now = env
    job = create(client)
    url = f"/api/v1/migrate/replications/{job['id']}/cutover"
    for value in (1, 0, "true", None):
        assert client.post(url, json={"source_powered_off": value}).status_code == 422
    assert runtime.calls == ["sync"]


def test_rpo_includes_sync_duration(env):
    client, manager, runtime, now = env
    original = runtime.sync

    def slow_sync(*args):
        now[0] += timedelta(minutes=2)
        return original(*args)

    runtime.sync = slow_sync
    job = create(client)
    assert job["rpo_seconds"] == 120
    assert job["last_sync"] != job["last_point"]


def test_create_on_a_node_that_already_has_vms(env):
    from ibhics.hypervisor import DiskSpec, VMSpec

    client, manager, runtime, now = env
    driver = client.app.state.cluster.member("node1").driver
    driver.create_vm(VMSpec(name="web-copy", disks=[DiskSpec(size_gb=1)]))
    taken = client.post(
        "/api/v1/migrate/replications",
        json={"source": "kvm", "vm": "vm", "name": "web-copy", "schedule": {"interval_minutes": 5}},
    )
    assert taken.status_code == 409, taken.text
    ok = client.post(
        "/api/v1/migrate/replications",
        json={
            "source": "kvm",
            "vm": "vm",
            "name": "web-copy2",
            "schedule": {"interval_minutes": 5},
        },
    )
    assert ok.status_code == 202, ok.text


def test_actual_tracking_and_fallback_persist_in_job_and_sync_history(env):
    client, manager, runtime, now = env
    original = runtime.sync
    tracking = ["hash-delta", "CBT baseline requires a full hash scan"]

    def tracked_sync(*args):
        result = original(*args)
        return {**result, "change_tracking": tracking[0], "fallback_reason": tracking[1]}

    runtime.sync = tracked_sync
    job = create(client)
    assert job["change_tracking"] == "hash-delta"
    assert "baseline" in job["fallback_reason"]
    assert job["history"][-1]["fallback_reason"] == job["fallback_reason"]
    tracking[:] = ["cbt", None]
    url = f"/api/v1/migrate/replications/{job['id']}"
    updated = client.post(url + "/sync").json()
    assert updated["change_tracking"] == updated["capability"]["change_tracking"] == "cbt"
    assert updated["fallback_reason"] is None
    assert [h["change_tracking"] for h in updated["history"]] == ["hash-delta", "cbt"]
    manager.load()
    assert client.get(url).json()["history"] == updated["history"]
    runtime.fail = True
    failed = client.post(url + "/sync").json()
    assert failed["history"][-1]["change_tracking"] is None
    assert failed["change_tracking"] == "cbt"  # latest committed cycle remains visible
