"""Recovery properties of incremental, synthetic, copy and GFS backups."""

import zlib
from datetime import UTC, datetime, timedelta

import pytest
from test_backup import CHUNK, Clock, Inline, image, make_vm

from ibhics.backup import BackupTarget, DirStore, NodeBackup, Repository, TargetType
from ibhics.backup.repository import RepositoryBusy
from ibhics.backup.retention import retained_ids
from ibhics.cluster import Cluster
from ibhics.hypervisor import DiskSpec, VMSpec
from ibhics.hypervisor.backup_cbt import CBTUnavailable, NBDReader
from ibhics.hypervisor.fake import FakeDriver
from ibhics.hypervisor.serialize import spec_to_json
from ibhics.manager.backups import BackupManager, Policy


@pytest.fixture
def setup(tmp_path):
    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC))
    repo = Repository(DirStore(tmp_path / "primary"), CHUNK, clock)
    vm = make_vm()
    data = b"a" * CHUNK + b"b" * CHUNK + b"\0" * CHUNK + b"tail"
    first = repo.backup(vm, [image(tmp_path, "disk", data)], spec_to_json(vm.spec))
    return repo, clock, vm, data, first


def test_synthetic_is_restorable_without_original_manifest_or_chunks(setup, tmp_path):
    repo, clock, vm, data, first = setup
    clock.now += timedelta(days=1)
    full = repo.synthetic_full(first.id)
    assert full.kind == "synthetic_full"
    assert full.source_id == first.id and full.read_bytes == 0
    repo.delete(first.id)
    repo.gc()
    assert repo.store.list("chunks/") == []
    assert repo.store.list("fulls/")
    restored = tmp_path / "restored"
    repo.restore_disk(repo.manifest(full.id), 0, restored)
    assert restored.read_bytes() == data
    repo.delete(full.id)
    repo.gc()
    assert not repo.store.list("fulls/")


@pytest.mark.parametrize("archive", [False, True])
def test_copies_survive_total_source_repository_loss(setup, tmp_path, archive):
    repo, clock, vm, data, first = setup
    destination = Repository(DirStore(tmp_path / "secondary"), CHUNK, clock)
    info = repo.transfer(first.id, destination, "archive" if archive else "copy", archive)
    repo.delete(first.id)
    repo.gc()
    destination.restore_disk(destination.manifest(info.id), 0, tmp_path / "restore")
    assert (tmp_path / "restore").read_bytes() == data
    assert info.kind == ("archive" if archive else "copy")


def test_corruption_never_publishes_synthetic_or_copy_manifest(setup, tmp_path):
    repo, clock, vm, data, first = setup
    repo.store.put(repo.store.list("chunks/")[0], b"invalid compressed data")
    destination = Repository(DirStore(tmp_path / "secondary"), CHUNK)
    with pytest.raises(zlib.error):
        repo.transfer(first.id, destination)
    assert not destination.list()
    assert not destination.store.list("locks/")
    with pytest.raises(zlib.error):
        repo.synthetic_full(first.id)
    assert len(repo.list()) == 1


def test_corrupt_existing_destination_is_not_silently_reused(setup, tmp_path):
    repo, clock, vm, data, first = setup
    destination = Repository(DirStore(tmp_path / "secondary"), CHUNK)
    key = repo.store.list("chunks/")[0]
    destination.store.put(key, b"corrupt")
    with pytest.raises(zlib.error):
        repo.transfer(first.id, destination)
    assert not destination.list()


class Reader:
    def __init__(self, data, changed):
        self.data, self.size, self.changed, self.reads = data, len(data), changed, []

    def changed_chunks(self, size):
        return iter(self.changed)

    def read(self, offset, count):
        self.reads.append((offset, count))
        return self.data[offset : offset + count]


def test_incremental_reads_only_changed_chunks_including_new_zeros(setup, tmp_path):
    repo, clock, vm, data, first = setup
    reader = Reader(b"a" * CHUNK + b"\0" * CHUNK + b"c" * CHUNK + b"tail", [1, 2])
    clock.now += timedelta(hours=1)
    info = repo.backup_readers(
        vm,
        [reader],
        spec_to_json(vm.spec),
        {"incremental": True, "checkpoint": "next"},
        repo.manifest(first.id),
    )
    assert reader.reads == [(CHUNK, CHUNK), (CHUNK * 2, CHUNK)]
    assert info.kind == "incremental" and info.read_bytes == 2 * CHUNK
    repo.delete(first.id)
    repo.gc()
    repo.restore_disk(repo.manifest(info.id), 0, tmp_path / "restore")
    assert (tmp_path / "restore").read_bytes() == reader.data


def test_incremental_after_synthetic_does_not_depend_on_its_private_chunks(setup, tmp_path):
    repo, clock, vm, data, first = setup
    clock.now += timedelta(hours=1)
    full = repo.synthetic_full(first.id)
    clock.now += timedelta(hours=1)
    reader = Reader(data, [])
    inc = repo.backup_readers(
        vm, [reader], spec_to_json(vm.spec), {"incremental": True}, repo.manifest(full.id)
    )
    repo.delete(first.id)
    repo.delete(full.id)
    repo.gc()
    repo.restore_disk(repo.manifest(inc.id), 0, tmp_path / "restore")
    assert (tmp_path / "restore").read_bytes() == data and not reader.reads


def test_incremental_refuses_changed_disk_size_without_publishing(setup):
    repo, clock, vm, data, first = setup
    with pytest.raises(ValueError, match="size changed"):
        repo.backup_readers(
            vm,
            [Reader(data + b"extra", [])],
            spec_to_json(vm.spec),
            {"incremental": True},
            repo.manifest(first.id),
        )
    assert len(repo.list()) == 1


def test_gfs_keeps_distinct_calendar_buckets_and_deduplicates_synthetic_points():
    def point(id, date, **extra):
        return {"id": id, "vm_uuid": "vm", "created": date, **extra}

    points = [
        point("old", "2024-12-31T02:00:00+00:00"),
        point("jan", "2025-01-31T02:00:00+00:00"),
        point("feb", "2025-02-28T02:00:00+00:00"),
        point("new", "2025-03-07T02:00:00+00:00"),
        point("synthetic", "2025-03-07T03:00:00+00:00", data_created="2025-03-07T02:00:00+00:00"),
    ]
    assert retained_ids(points, 1, monthly=3, yearly=2) == {"old", "jan", "feb", "synthetic"}


class NBD:
    def get_size(self):
        return 4 * CHUNK

    def can_meta_context(self, context):
        return True

    def block_status(self, count, offset, callback):
        # Replies may cover only part of the requested range; dirty extents need
        # not align with repository chunks, and zero writes are dirty too.
        if offset == 0:
            callback("qemu:dirty-bitmap:changes", 0, [CHUNK // 2, 0, CHUNK, 1], 0)
        else:
            callback("qemu:dirty-bitmap:changes", offset, [4 * CHUNK - offset, 0], 0)


def test_cbt_maps_partial_dirty_extents_to_repository_chunk_boundaries():
    assert list(NBDReader(NBD(), True).changed_chunks(CHUNK)) == [0, 1]


def test_cbt_refuses_missing_metadata_instead_of_claiming_zero_changes():
    handle = NBD()
    handle.can_meta_context = lambda context: False
    with pytest.raises(CBTUnavailable):
        list(NBDReader(handle, True).changed_chunks(CHUNK))
    handle.can_meta_context = lambda context: True
    handle.block_status = lambda *args: None
    with pytest.raises(CBTUnavailable):
        list(NBDReader(handle, True).changed_chunks(CHUNK))


def test_scheduled_full_synthetic_copy_archive_and_persistence(tmp_path):
    driver = FakeDriver()
    vm = driver.create_vm(VMSpec(name="web", disks=[DiskSpec(size_gb=1)]))
    cluster = Cluster()
    cluster.add_local("node", driver, NodeBackup(driver, chunk_size=CHUNK))
    state = tmp_path / "policies.json"
    clock = Clock(datetime(2026, 1, 4, 2, tzinfo=UTC))  # Sunday
    manager = BackupManager(cluster, state, clock, Inline())
    for name in ("primary", "copy", "archive"):
        manager.add_target(BackupTarget(name, TargetType.LOCAL, path=str(tmp_path / name)))
    manager.add_policy(
        Policy(
            "nightly",
            "primary",
            vms=["web"],
            every_hours=1,
            full_weekday=6,
            synthetic_weekday=6,
            copy_target="copy",
            archive_target="archive",
        )
    )
    jobs = manager.run_policy("nightly")
    assert jobs[0].state == "done", jobs[0].error
    assert {j.kind for j in manager.jobs()} == {
        "backup",
        "synthetic_full",
        "backup_copy",
        "archive",
    }
    assert [b.kind for _, b in manager.list_backups("primary")] == ["synthetic_full"]
    assert [b.kind for _, b in manager.list_backups("copy")] == ["copy"]
    assert [b.kind for _, b in manager.list_backups("archive")] == ["archive"]
    restored = manager.start_restore(
        "archive", manager.list_backups("archive")[0][1].id, name="recovered", source_node="node"
    )
    assert restored.state == "done", restored.error
    reloaded = BackupManager(cluster, state, clock, Inline())
    reloaded.load()
    assert reloaded._maintenance == manager._maintenance
    clock.now += timedelta(hours=1)
    assert reloaded.run_policy("nightly")[0].state == "done"
    assert len(reloaded.list_backups("archive")) == 1  # once on the selected day
    assert len(reloaded.list_backups("primary")) == 2
    assert driver.get_vm(vm.uuid).spec.name == "web"


@pytest.mark.parametrize(
    "fields",
    [
        dict(keep_monthly=-1),
        dict(full_weekday=9),
        dict(copy_target="primary"),
        dict(mode="pretend"),
    ],
)
def test_invalid_policy_is_rejected(fields):
    with pytest.raises(ValueError):
        Policy("p", "primary", at="02:00", **fields).validate()


def test_full_fallback_is_explicit_on_non_cbt_driver(tmp_path):
    driver = FakeDriver()
    vm = driver.create_vm(VMSpec(name="web", disks=[DiskSpec(size_gb=1)]))
    backup = NodeBackup(driver, chunk_size=CHUNK)
    info = backup.backup(vm.uuid, BackupTarget("repo", TargetType.LOCAL, path=str(tmp_path / "r")))
    assert info.kind == "full" and info.read_bytes > 0 and "CBT" in info.note


def test_rest_api_synthetic_copy_archive_restore_and_policy(tmp_path):
    from fastapi.testclient import TestClient

    from ibhics.manager import create_app

    driver = FakeDriver()
    vm = driver.create_vm(VMSpec(name="vm", disks=[DiskSpec(size_gb=1)]))
    driver.write_disk(vm.uuid, 0, 20, b"recover this data")
    cluster = Cluster()
    cluster.add_local("node", driver, NodeBackup(driver, chunk_size=CHUNK))
    manager = BackupManager(cluster, executor=Inline())
    client = TestClient(create_app(cluster, backups=manager))
    for name in ("primary", "secondary"):
        assert (
            client.post(
                "/api/v1/backup/targets",
                json={"name": name, "type": "local", "path": str(tmp_path / name)},
            ).status_code
            == 201
        )
    first = client.post(
        f"/api/v1/vms/{vm.uuid}/backup", json={"target": "primary", "mode": "active_full"}
    ).json()
    assert first["state"] == "done", first
    url = f"/api/v1/backup/targets/primary/backups/{first['result']}"
    synth = client.post(url + "/synthetic", json={"node": "node"}).json()
    assert synth["kind"] == "synthetic_full" and synth["state"] == "done", synth
    copied = client.post(
        url + "/copy", json={"node": "node", "destination": "secondary", "archive": True}
    ).json()
    assert copied["kind"] == "archive" and copied["state"] == "done", copied
    points = client.get("/api/v1/backup/targets/secondary/backups").json()
    assert points[0]["kind"] == "archive"
    client.delete(url + "?node=node")
    restored = client.post(
        f"/api/v1/backup/targets/secondary/backups/{copied['result']}/restore",
        json={"name": "recovered", "source_node": "node"},
    ).json()
    assert restored["state"] == "done", restored
    assert driver.read_disk(restored["result"], 0) == driver.read_disk(vm.uuid, 0)
    policy = {
        "name": "nightly",
        "target": "primary",
        "at": "02:00",
        "mode": "incremental",
        "synthetic_weekday": 6,
        "keep_monthly": 12,
        "copy_target": "secondary",
    }
    response = client.post("/api/v1/backup/policies", json=policy)
    assert response.status_code == 201, response.text
    assert response.json()["keep_monthly"] == 12
    assert client.delete("/api/v1/backup/targets/secondary").status_code == 422


def test_checkpoint_pull_capture_establishes_base_and_incremental(monkeypatch, tmp_path):
    import sys
    import types
    import xml.etree.ElementTree as ET

    from ibhics.hypervisor import libvirt_driver
    from ibhics.hypervisor.backup_cbt import capture

    handles = []

    class Handle(NBD):
        def __init__(self):
            self.contexts, self.closed = [], False
            handles.append(self)

        def set_export_name(self, name):
            self.export = name

        def add_meta_context(self, context):
            self.contexts.append(context)

        def connect_unix(self, socket):
            self.socket = socket

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "nbd", types.SimpleNamespace(NBD=Handle))
    monkeypatch.setattr(libvirt_driver, "_qemu_may_write", lambda path: None)

    class Domain:
        def isActive(self):
            return True

        def XMLDesc(self, flags):
            return (
                "<domain><devices><disk device='disk'><driver type='qcow2'/>"
                "<target dev='vda'/></disk></devices></domain>"
            )

        def backupBegin(self, xml, checkpoint):
            self.xml, self.cp = xml, checkpoint

        def abortJob(self):
            self.aborted = True

        def checkpointLookupByName(self, name, flags):
            return object()

    dom = Domain()
    driver = types.SimpleNamespace(
        _lookup=lambda uuid: dom,
        _disk_path=lambda d: "/disk.qcow2",
        _volume=lambda d: types.SimpleNamespace(info=lambda: (0, 4 * CHUNK)),
        scratch_dir=str(tmp_path),
    )
    with capture(driver, "uuid") as (readers, baseline):
        assert not baseline["incremental"]
        assert ET.fromstring(dom.xml).get("mode") == "pull"
        assert ET.fromstring(dom.cp).find("name").text == baseline["checkpoint"]
        assert ET.fromstring(dom.xml).find("incremental") is None
    assert dom.aborted and handles[-1].closed
    with capture(driver, "uuid", baseline) as (readers, inc):
        assert inc["incremental"]
        assert ET.fromstring(dom.xml).find("incremental").text == baseline["checkpoint"]
        assert handles[-1].contexts == ["qemu:dirty-bitmap:changes"]
    with capture(driver, "uuid", {**baseline, "disk_fingerprint": "changed"}) as (_, fresh):
        assert not fresh["incremental"]

    def missing(*args):
        raise RuntimeError("checkpoint gone")

    dom.checkpointLookupByName = missing
    with capture(driver, "uuid", baseline) as (_, fresh):
        assert not fresh["incremental"]


def test_s3_archive_class_only_applies_to_chunk_payloads():
    from types import SimpleNamespace

    from ibhics.backup import S3Store

    writes = []
    store = S3Store(
        SimpleNamespace(put_object=lambda **kw: writes.append(kw)), "bucket", "prefix", "GLACIER_IR"
    )
    store.put("chunks/aa/aaa", b"chunk")
    store.put("fulls/vm@time/aa/aaa", b"full")
    store.put("backups/vm/time.json", b"manifest")
    store.put("locks/job", b"lock")
    assert [w.get("StorageClass") for w in writes] == ["GLACIER_IR", "GLACIER_IR", None, None]


@pytest.mark.parametrize("profile", ["fast", "balanced", "maximum"])
def test_compression_metrics_are_measured_and_chunks_remain_readable(tmp_path, profile):
    from ibhics.backup.compression import codec

    repo = Repository(DirStore(tmp_path / profile), CHUNK, compression=profile)
    vm = make_vm()
    block = b"compressible real payload " * (CHUNK // 26)
    block = block.ljust(CHUNK, b".")
    data = block + block + b"\0" * CHUNK
    first = repo.backup(vm, [image(tmp_path, "input", data)], spec_to_json(vm.spec))
    assert first.new_data_bytes == CHUNK
    assert first.dedup_saved_bytes == CHUNK  # all-zero block is not counted as dedup
    assert first.compression == codec(profile)
    assert first.stored_bytes < first.new_data_bytes
    assert sum(len(repo.store.get(k)) for k in repo.store.list("chunks/")) == first.stored_bytes
    repo.restore_disk(repo.manifest(first.id), 0, tmp_path / "restore")
    assert (tmp_path / "restore").read_bytes() == data
    second = repo.backup(vm, [tmp_path / "input"], spec_to_json(vm.spec))
    assert second.new_data_bytes == second.stored_bytes == 0
    assert second.dedup_saved_bytes == 2 * CHUNK


def test_report_csv_json_and_persistence_survive_backup_deletion(tmp_path):
    import csv
    import io

    from fastapi.testclient import TestClient

    from ibhics.manager import create_app

    driver = FakeDriver()
    vm = driver.create_vm(VMSpec(name="web", disks=[DiskSpec(size_gb=2)]))
    driver.write_disk(vm.uuid, 0, 0, b"data" * 300)
    cluster = Cluster()
    cluster.add_local("node", driver, NodeBackup(driver, chunk_size=CHUNK))
    path = tmp_path / "backup.json"
    manager = BackupManager(cluster, path, executor=Inline())
    manager.add_target(BackupTarget("repo", TargetType.LOCAL, path=str(tmp_path / "repo")))
    client = TestClient(create_app(cluster, backups=manager))
    first = manager.start_backup(vm.uuid, "repo")
    assert first.state == "done"
    response = client.get(f"/api/v1/backup/jobs/{first.id}/report")
    assert response.status_code == 200
    report = response.json()
    assert report["dedup_ratio"] == 1
    assert report["compression_ratio"] == round(
        first.report["new_data_bytes"] / first.report["stored_bytes"], 4
    )
    csv_response = client.get(f"/api/v1/backup/jobs/{first.id}/report?format=csv")
    assert "attachment" in csv_response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(csv_response.text.lstrip("\ufeff"))))
    assert rows[0]["backup_id"] == first.result
    assert float(rows[0]["compression_ratio"]) == report["compression_ratio"]
    second = manager.start_backup(vm.uuid, "repo")
    report2 = client.get(f"/api/v1/backup/jobs/{second.id}/report").json()
    assert report2["all_data_reused"] is True
    assert report2["dedup_ratio"] is None and report2["compression_ratio"] is None
    manager.delete_backup("repo", first.result, node="node")
    loaded = BackupManager(cluster, path, executor=Inline())
    loaded.load()
    reloaded = TestClient(create_app(cluster, backups=loaded))
    assert reloaded.get(f"/api/v1/backup/jobs/{first.id}/report").json() == report
    new = loaded.start_backup(vm.uuid, "repo")
    assert int(new.id) > int(second.id)
    assert client.get("/api/v1/backup/jobs/missing/report").status_code == 404
    assert client.get(f"/api/v1/backup/jobs/{first.id}/report?format=bad").status_code == 422


def test_directory_repository_can_be_reached_through_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    store = DirStore(alias)
    store.put("backups/vm/manifest.json", b"{}")
    assert store.list("backups/") == ["backups/vm/manifest.json"]


def test_copy_reports_actual_reuse_and_synthetic_dedup(setup, tmp_path):
    repo, clock, vm, data, first = setup
    dest = Repository(DirStore(tmp_path / "copy"), CHUNK, clock)
    copied = repo.transfer(first.id, dest)
    assert copied.new_data_bytes > 0
    repeated = repo.transfer(first.id, dest)
    assert repeated.new_data_bytes == repeated.stored_bytes == 0
    assert repeated.dedup_saved_bytes == copied.new_data_bytes + copied.dedup_saved_bytes
    synthetic = repo.synthetic_full(first.id)
    assert synthetic.new_data_bytes == copied.new_data_bytes
    assert synthetic.dedup_saved_bytes == copied.dedup_saved_bytes


def test_gc_refuses_malformed_and_long_running_operation_locks(setup):
    repo, clock, vm, data, first = setup
    with repo.reading():
        clock.now += timedelta(days=3)
        with pytest.raises(RepositoryBusy):
            repo.gc()
    repo.store.put("locks/unknown", b"incomplete timestamp")
    with pytest.raises(RepositoryBusy):
        repo.gc()


def test_remote_agent_synthetic_copy_archive_and_restore(tmp_path):
    from test_cluster import Agents

    from ibhics.cluster import NodeRole
    from ibhics.cluster.remote import RemoteBackup

    driver = FakeDriver()
    node = NodeBackup(driver, chunk_size=CHUNK)
    agents = Agents()
    agents.add("http://backup-node:8007", "backup-node", NodeRole.COMPUTE, driver, node)
    remote = RemoteBackup(agents.client("http://backup-node:8007", "s3cret"))
    source = BackupTarget("primary", TargetType.LOCAL, path=str(tmp_path / "primary"))
    destination = BackupTarget("offsite", TargetType.LOCAL, path=str(tmp_path / "offsite"))
    vm = driver.create_vm(VMSpec(name="agent-vm", disks=[DiskSpec(size_gb=1)]))
    driver.write_disk(vm.uuid, 0, 0, b"remote verified backup")
    first = remote.backup(vm.uuid, source, mode="active_full")
    full = remote.synthetic_full(source, first.id)
    assert full.kind == "synthetic_full" and full.read_bytes == 0
    copied = remote.copy(source, destination, full.id)
    archived = remote.copy(source, destination, full.id, archive=True)
    assert copied.kind == "copy" and archived.kind == "archive"
    remote.delete(source, first.id)
    remote.delete(source, full.id)
    restored = remote.restore(destination, archived.id, name="agent-restored")
    assert driver.read_disk(restored.uuid, 0) == driver.read_disk(vm.uuid, 0)
    assert remote.prune(destination, vm.spec.name, 1, weekly=1, monthly=1, yearly=1) == [copied.id]


def test_short_read_never_publishes_a_restore_point(setup):
    repo, clock, vm, data, first = setup
    reader = Reader(data, [0])
    reader.read = lambda offset, count: b"short"
    with pytest.raises(RuntimeError, match="Short read"):
        repo.backup_readers(vm, [reader], {}, {"incremental": True}, repo.manifest(first.id))
    assert [b.id for b in repo.list()] == [first.id]
    assert not repo.store.list("locks/")


@pytest.mark.parametrize("body_fails", [False, True])
def test_pull_job_is_aborted_even_when_nbd_close_fails(monkeypatch, tmp_path, body_fails):
    import sys
    import types

    from ibhics.hypervisor import libvirt_driver
    from ibhics.hypervisor.backup_cbt import capture

    class Handle:
        def set_export_name(self, name):
            pass

        def connect_unix(self, socket):
            pass

        def get_size(self):
            return CHUNK

        def close(self):
            raise RuntimeError("close failed")

    class Domain:
        aborted = False

        def isActive(self):
            return True

        def XMLDesc(self, flags):
            return (
                "<domain><devices><disk device='disk'><driver type='qcow2'/>"
                "<target dev='vda'/></disk></devices></domain>"
            )

        def backupBegin(self, xml, checkpoint):
            pass

        def abortJob(self):
            self.aborted = True

    dom = Domain()
    monkeypatch.setitem(sys.modules, "nbd", types.SimpleNamespace(NBD=Handle))
    monkeypatch.setattr(libvirt_driver, "_qemu_may_write", lambda path: None)
    driver = types.SimpleNamespace(
        _lookup=lambda uuid: dom,
        _disk_path=lambda d: "/test.qcow2",
        _volume=lambda d: types.SimpleNamespace(info=lambda: (0, CHUNK)),
        scratch_dir=str(tmp_path),
    )
    with pytest.raises(ValueError if body_fails else RuntimeError) as error:
        with capture(driver, "u"):
            if body_fails:
                raise ValueError("original read failed")
    assert dom.aborted
    if body_fails:
        assert str(error.value) == "original read failed"
        assert any("close failed" in note for note in error.value.__notes__)
