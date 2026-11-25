"""Storage loss must fail before bulk data can land on the system disk."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ibhics.hypervisor import InvalidState
from ibhics.storage_paths import require_data_storage


def test_missing_mount_never_creates_root_directories(tmp_path):
    target = tmp_path / "missing-mount" / "uploads"
    with pytest.raises(InvalidState, match="on root"):
        require_data_storage(target)
    assert not target.parent.exists()


def test_storage_link_back_to_root_is_rejected(tmp_path):
    target = tmp_path / "uploads"
    target.symlink_to("/", target_is_directory=True)
    with pytest.raises(InvalidState, match="on root"):
        require_data_storage(target)


def test_insufficient_space_fails_before_creating_work(tmp_path, monkeypatch):
    import ibhics.storage_paths as paths

    original = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda p, **kw: SimpleNamespace(st_dev=-123) if p == tmp_path else original(p, **kw),
    )
    monkeypatch.setattr(Path, "is_dir", lambda p: p in (tmp_path, Path("/")))
    monkeypatch.setattr(paths.shutil, "disk_usage", lambda p: SimpleNamespace(free=5))
    target = tmp_path / "work"
    with pytest.raises(InvalidState, match="free space"):
        require_data_storage(target, required_bytes=6)
    assert not target.exists()


def test_open_file_on_root_is_rejected_even_when_mount_is_back(tmp_path, monkeypatch):
    import ibhics.storage_paths as paths

    root_device = Path("/").stat().st_dev
    original = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda p, **kw: SimpleNamespace(st_dev=-123) if p == tmp_path else original(p, **kw),
    )
    monkeypatch.setattr(Path, "is_dir", lambda p: p in (tmp_path, Path("/")))
    monkeypatch.setattr(paths, "fstat", lambda fd: SimpleNamespace(st_dev=root_device))
    with pytest.raises(InvalidState, match="Refusing to write"):
        require_data_storage(tmp_path, file_fd=123)


def test_upload_guard_stops_before_consuming_request(tmp_path):
    import asyncio

    from ibhics.manager.app import receive_file

    async def chunks():
        pytest.fail("Request body must not be read when storage is unavailable")
        yield b"data"

    def reject(*args, **kwargs):
        raise InvalidState("storage missing")

    target = tmp_path / "missing" / "disk.raw"
    with pytest.raises(InvalidState, match="storage missing"):
        asyncio.run(receive_file(SimpleNamespace(stream=chunks), target, storage_guard=reject))
    assert not target.parent.exists()


def test_upload_open_file_guard_cleans_partial_without_writing(tmp_path):
    import asyncio

    from ibhics.manager.app import receive_file

    async def chunks():
        pytest.fail("Request body must not be read on the root filesystem")
        yield b"data"

    def reject_fd(directory, *, file_fd=None):
        if file_fd is not None:
            raise InvalidState("root filesystem")

    target = tmp_path / "disk.raw"
    with pytest.raises(InvalidState, match="root filesystem"):
        asyncio.run(receive_file(SimpleNamespace(stream=chunks), target, storage_guard=reject_fd))
    assert list(tmp_path.iterdir()) == []


def test_upload_source_on_root_is_rejected_before_mkdir(tmp_path):
    from ibhics.manager.imports import ImportManager

    manager = ImportManager.__new__(ImportManager)
    manager._cluster = SimpleNamespace(
        compute_members=lambda: [SimpleNamespace(local=True, driver=object())]
    )
    manager.upload_dir = tmp_path / "uploads"
    with pytest.raises(InvalidState, match="on root"):
        manager.source("uploads")
    assert not manager.upload_dir.exists()


def test_p2v_storage_guard_runs_before_session_directory_is_created(tmp_path):
    from ibhics.manager.p2v import P2VSessions

    sessions = P2VSessions()
    sessions.storage_guard = require_data_storage
    with pytest.raises(InvalidState, match="on root"):
        sessions.open("missing", tmp_path / "missing-mount")
    assert list(tmp_path.iterdir()) == []


def test_guestfs_cache_cannot_redirect_writes_to_root(tmp_path):
    from ibhics.importer.convert import ConversionFailed, V2VConverter

    (tmp_path / ".libguestfs-cache").symlink_to("/", target_is_directory=True)
    with pytest.raises(ConversionFailed, match="must not be a symlink"):
        V2VConverter._guestfs_env(tmp_path)


def test_upload_full_storage_preserves_existing_file_and_removes_partial(tmp_path, monkeypatch):
    import asyncio
    import errno

    from ibhics.manager.app import receive_file

    async def chunks():
        yield b"replacement"

    async def full_disk(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    target = tmp_path / "disk.raw"
    target.write_bytes(b"original")
    monkeypatch.setattr(asyncio, "to_thread", full_disk)
    with pytest.raises(OSError) as error:
        asyncio.run(receive_file(SimpleNamespace(stream=chunks), target))
    assert error.value.errno == errno.ENOSPC
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]


def test_replica_full_storage_preserves_previous_generation(tmp_path, monkeypatch):
    import errno

    from test_replication import Provider

    from ibhics.replication import store as storage

    replica = storage.ReplicaStore(tmp_path / "metadata", [tmp_path / "disks"], chunk=4)
    before = replica.sync(Provider(b"original"))
    original = Path(before["disks"][0]["path"])

    def full_disk(source, target, chunk):
        target.write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(storage, "sparse_copy", full_disk)
    with pytest.raises(OSError) as error:
        replica.sync(Provider(b"modified"))
    assert error.value.errno == errno.ENOSPC
    assert replica.current() == before
    assert original.read_bytes() == b"original"
    assert list((tmp_path / "disks").iterdir()) == [original]


def test_long_guestfs_workspace_uses_short_socket_path_and_cleans_temp(monkeypatch):
    import socket
    import tempfile

    from ibhics.importer.convert import V2VConverter

    with tempfile.TemporaryDirectory(prefix="ibgt-", dir=Path("/tmp").resolve()) as directory:
        storage = Path(directory)
        workspace = storage / ("nested-" + "x" * 100) / "conversion"
        workspace.mkdir(parents=True)
        monkeypatch.setattr(V2VConverter, "_guestfs_tmp_root", staticmethod(lambda _: storage))
        env = V2VConverter._guestfs_env(workspace)
        temp = Path(env["TMPDIR"])
        assert temp.parent == storage
        assert len(str(temp).encode()) <= 65
        assert env["LIBGUESTFS_TMPDIR"] == str(temp)
        assert Path(env["LIBGUESTFS_CACHEDIR"]) == workspace / ".libguestfs-cache"
        socket_dir = temp / "v2v.123456"
        socket_dir.mkdir()
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(socket_dir / "in0"))
        V2VConverter.cleanup_workspace(workspace)
        assert not temp.exists()
        assert workspace.exists()


def test_failed_migration_cleans_short_guestfs_temp(monkeypatch):
    import tempfile

    from ibhics.hypervisor.fake import FakeDriver
    from ibhics.importer import Source, SourceType, SourceVM
    from ibhics.importer.convert import ConversionFailed, V2VConverter
    from ibhics.importer.node import NodeImport

    with tempfile.TemporaryDirectory(prefix="ibgt-", dir=Path("/tmp").resolve()) as directory:
        storage = Path(directory)
        scratch = storage / ("workspace-" + "x" * 80)
        untouched = storage / "unrelated"
        untouched.write_text("keep")
        monkeypatch.setattr(V2VConverter, "_guestfs_tmp_root", staticmethod(lambda _: storage))
        observed = []

        class FailingConverter:
            def convert(self, source, vm, name, work, *args, **kwargs):
                observed.append(Path(V2VConverter._guestfs_env(work)["TMPDIR"]))
                raise ConversionFailed("fixture failure")

        importer = NodeImport(FakeDriver(), FailingConverter(), scratch_dir=str(scratch))
        with pytest.raises(ConversionFailed, match="fixture failure"):
            importer.run(Source("fixture", SourceType.DISK), SourceVM("disk", "fixture"), "test")
        assert observed and not observed[0].exists()
        assert list(scratch.iterdir()) == []
        assert untouched.read_text() == "keep"
