"""Incremental cycles must not copy or read unchanged baseline data."""

import hashlib
import shutil
import threading
from pathlib import Path

import pytest
from test_replication import Provider

from ibhics.replication.cow import CowReplicaStore
from ibhics.replication.runtime import ClusterRuntime


class Delta(Provider):
    tracking = "cbt"

    def __init__(self, data, ranges, **kwargs):
        super().__init__(data, **kwargs)
        self.ranges = ranges

    def list_changed(self, token):
        return {"disk0": self.ranges} if self.ranges is not None else None

    def hash(self, *args):
        raise AssertionError("A CBT cycle must not hash-scan the source")


def test_zero_changes_does_not_clone_or_read_baseline(tmp_path, monkeypatch):
    store = CowReplicaStore(tmp_path / "meta", [tmp_path / "disk"], chunk=4096)
    before = store.sync(Provider(b"a" * 8192))
    path = Path(before["disks"][0]["path"])
    inode = path.stat().st_ino

    def forbidden(*args):
        raise AssertionError("An unchanged sync must not copy the baseline")

    monkeypatch.setattr("ibhics.replication.store.sparse_copy", forbidden)
    monkeypatch.setattr(store, "_layer", forbidden)
    provider = Delta(b"a" * 8192, [])
    result = store.sync(provider)
    assert provider.reads == [] and provider.ended
    assert result["bytes_transferred"] == result["source_read_bytes"] == 0
    assert Path(store.current()["disks"][0]["path"]).stat().st_ino == inode
    assert path.read_bytes() == b"a" * 8192
    assert len(list((tmp_path / "disk").iterdir())) == 1


def test_missing_cbt_never_silently_starts_full_scan(tmp_path):
    store = CowReplicaStore(tmp_path / "meta", [tmp_path / "disk"], chunk=4096)
    before = store.sync(Provider(b"a" * 8192))
    provider = Delta(b"b" * 8192, None)
    with pytest.raises(ValueError, match="full scan not started"):
        store.sync(provider)
    assert store.current() == before
    assert provider.reads == [] and provider.ended


@pytest.mark.skipif(
    not shutil.which("qemu-img") or not shutil.which("qemu-io"), reason="QEMU tools"
)
def test_changed_chunk_overlay_zero_and_failure_preserve_old_points(tmp_path, monkeypatch):
    store = CowReplicaStore(tmp_path / "meta", [tmp_path / "disk"], chunk=4096)
    original = b"a" * 4096 + b"b" * 4096
    before = store.sync(Provider(original))
    base = Path(before["disks"][0]["path"])
    initial_hash = hashlib.sha256(base.read_bytes()).hexdigest()

    def forbidden(*args):
        raise AssertionError("No full copy for incremental sync")

    monkeypatch.setattr("ibhics.replication.store.sparse_copy", forbidden)
    changed = Delta(b"a" * 4096 + b"c" * 4096, [(4096, 4096)])
    second = store.sync(changed)
    assert changed.reads == [4096]
    assert second["source_read_bytes"] == second["changed_bytes"] == 4096
    assert second["disks"][0]["format"] == "qcow2"
    assert hashlib.sha256(base.read_bytes()).hexdigest() == initial_hash
    previous_layer = Path(store.current()["disks"][0]["path"])
    layer_hash = hashlib.sha256(previous_layer.read_bytes()).hexdigest()
    [clone] = store.clone(tmp_path / "test-second")
    assert clone.read_bytes() == changed.data

    zero = Delta(b"a" * 4096 + bytes(4096), [(4096, 4096)])
    store.sync(zero)
    [clone] = store.clone(tmp_path / "test-zero")
    assert clone.read_bytes() == zero.data
    assert hashlib.sha256(previous_layer.read_bytes()).hexdigest() == layer_hash
    committed = store.current()
    existing = set((tmp_path / "disk").iterdir())
    failing = Delta(b"d" * 8192, [(0, 8192)], fail=True)
    with pytest.raises(RuntimeError):
        store.sync(failing)
    assert store.current() == committed
    assert set((tmp_path / "disk").iterdir()) == existing
    [clone] = store.clone(tmp_path / "test-after-failure")
    assert clone.read_bytes() == zero.data


def test_remote_stop_is_forwarded_before_first_data_byte():
    runtime = object.__new__(ClusterRuntime)
    released = threading.Event()
    calls = []

    def call(member, action, job, **kwargs):
        calls.append(action)
        if action == "sync":
            assert released.wait(2), "Stop was not forwarded while progress was zero"
            return {}
        if action == "stop":
            released.set()
            return {"ok": True}
        return {"done": 0, "total": 0, "running": True}

    class Source:
        def to_dict(self):
            return {}

    class SyncStopped(Exception):
        pass

    def stop(done, total):
        raise SyncStopped

    runtime._call = call
    try:
        with pytest.raises(SyncStopped):
            runtime._remote_sync(None, None, Source(), stop, every=0.01)
    finally:
        released.set()
    assert "stop" in calls
