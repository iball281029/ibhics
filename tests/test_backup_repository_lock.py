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
