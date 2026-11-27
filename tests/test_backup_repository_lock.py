"""Repository serialization prevents GC from removing unpublished backup chunks."""

import pytest
from test_backup import FakeS3, image, make_vm

from ibhics.backup import DirStore, Repository
from ibhics.backup.store import RepositoryBusy, S3Store, TargetUnavailable


@pytest.fixture(params=["directory", "s3"])
def stores(request, tmp_path):
    if request.param == "directory":
        return DirStore(tmp_path / "repository"), DirStore(tmp_path / "repository")
    client = FakeS3()
    return S3Store(client, "bucket", "repository"), S3Store(client, "bucket", "repository")


def test_exclusive_lease_is_reentrant_and_blocks_other_nodes(stores):
    first, second = stores
    with first.exclusive():
        with first.exclusive():
            with pytest.raises(RepositoryBusy):
                with second.exclusive():
                    pytest.fail("Two independent nodes acquired the same repository")
        assert first.exists(".repository-lease")
    with second.exclusive():
        assert second.exists(".repository-lease")
    assert not first.exists(".repository-lease")


def test_orphan_lease_prevents_gc_without_time_based_expiry(stores):
    first, second = stores
    first.put(".repository-lease", b"orphan operation; requires operator review")
    first.put("chunks/ab/orphan", b"not safe to delete while ownership is unknown")
    with pytest.raises(RepositoryBusy):
        Repository(second).gc()
    assert first.exists("chunks/ab/orphan")


def test_gc_and_a_new_backup_cannot_overlap(stores, tmp_path):
    first, second = stores
    a, b = Repository(first, 4096), Repository(second, 4096)
    first.put("chunks/ab/orphan", b"unreferenced")
    vm = make_vm()
    disk = image(tmp_path, "disk", b"important data" * 1000)
    attempts = []
    original_list = first.list

    def interleave(prefix):
        if prefix == "chunks/" and not attempts:
            attempts.append(True)
            with pytest.raises(RepositoryBusy):
                b.backup(vm, [disk], {})
        return original_list(prefix)

    first.list = interleave
    assert a.gc() == 1
    assert attempts
    point = b.backup(vm, [disk], {})
    out = tmp_path / "restored"
    b.restore_disk(b.manifest(point.id), 0, out)
    assert out.read_bytes() == disk.read_bytes()


def test_delete_cannot_remove_a_point_in_use_by_another_node(stores, tmp_path):
    first, second = stores
    a, b = Repository(first, 4096), Repository(second, 4096)
    point = a.backup(make_vm(), [image(tmp_path, "disk", b"payload")], {})
    with a.reading():
        with pytest.raises(RepositoryBusy):
            b.delete(point.id)
        assert b.manifest(point.id)["id"] == point.id


def test_unsupported_s3_conditional_writes_fail_closed():
    class OldClient:
        def put_object(self, Bucket, Key, Body):  # noqa: N803
            pytest.fail("An unconditional repository lease must not be written")

    with pytest.raises(TargetUnavailable, match="conditional S3"):
        with S3Store(OldClient(), "bucket").exclusive():
            pytest.fail("Unsupported locking must not permit repository mutation")


def test_backup_shared_limit_reentrancy_and_orphans(stores):
    first, second = stores
    # Independent clients exercise directory and S3 conditional admission.
    with first.shared(), first.shared(), second.shared():
        assert first.exists(".repository-backup-0")
        assert first.exists(".repository-backup-1")
        with pytest.raises(RepositoryBusy):
            with first.exclusive():
                pytest.fail("Lease upgrades must fail without releasing backup admission")
    first.put(".repository-backup-1", b"crashed node; requires review")
    with pytest.raises(RepositoryBusy):
        Repository(second).gc()
    with second.shared():
        assert second.exists(".repository-backup-0")


def test_backup_chunk_publish_does_not_replace_a_peers_protected_object(stores):
    first, second = stores
    key = "chunks/ab/same-digest"
    first.put_chunk(key, b"first compressed representation")
    second.put_chunk(key, b"another valid representation of the same content")
    assert first.get(key) == b"first compressed representation"


def test_waiting_backup_cancel_releases_cross_node_slot(stores):
    from ibhics.backup import BackupCancelled

    first, second = stores
    cancelled = False

    def check_cancel():
        if cancelled:
            raise BackupCancelled("Cancelled while acquiring repository admission")

    # Cancellation immediately after successful admission must still release the slot.
    with pytest.raises(BackupCancelled):
        with second.waiting(shared=True, check_cancel=check_cancel):
            cancelled = True
            check_cancel()
    with first.exclusive():
        assert not first.exists(".repository-backup-0")


def test_third_backup_waits_until_one_cross_node_slot_is_released(stores):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    first, second = stores
    attempted, acquired, release = threading.Event(), threading.Event(), threading.Event()

    def third():
        attempted.set()
        with first.waiting(shared=True):
            acquired.set()
            assert release.wait(3)

    with ThreadPoolExecutor(1) as pool, first.shared():
        try:
            with second.shared():
                future = pool.submit(third)
                assert attempted.wait(3)
                assert not acquired.wait(0.1)
            assert acquired.wait(3)
        finally:
            release.set()
        future.result(timeout=3)


def test_backup_concurrent_duplicate_chunk_counts_only_the_actual_writer(stores, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    first, second = stores
    barrier = threading.Barrier(2)
    data = b"same block" * 400

    class Reader:
        size = len(data)

        def changed_chunks(self, chunk_size):
            return [0]

        def read(self, offset, count):
            return data[offset:offset + count]

    def synchronized_publish(original):
        def publish(key, payload):
            barrier.wait(timeout=3)
            return original(key, payload)
        return publish

    for store in stores:
        monkeypatch.setattr(store, "put_chunk", synchronized_publish(store.put_chunk))

    def backup(store, name):
        with store.waiting(shared=True):
            return Repository(store, 4096).backup_readers(make_vm(name), [Reader()], {}, {})

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(backup, first, "first")
        b = pool.submit(backup, second, "second")
        points = [a.result(timeout=5), b.result(timeout=5)]
    assert sum(point.new_data_bytes for point in points) == len(data)
    assert sum(point.dedup_saved_bytes for point in points) == len(data)
    assert sum(point.stored_bytes for point in points) == sum(
        len(first.get(key)) for key in first.list("chunks/")
    )
