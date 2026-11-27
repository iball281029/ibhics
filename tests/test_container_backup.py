"""Container filesystem backup/restore shares repository guarantees without VM restore paths."""

import zlib

import pytest
import test_backup

from ibhics.backup import (
    BackupCancelled,
    BackupTarget,
    DirStore,
    NodeBackup,
    Repository,
    TargetType,
)
from ibhics.backup.container_image import RawImageReader
from ibhics.hypervisor import InvalidState
from ibhics.hypervisor.containers import ContainerSpec
from ibhics.hypervisor.fake import FakeDriver

api = test_backup.api


def test_raw_sparse_reader_matches_full_image(tmp_path):
    image = tmp_path / "disk.img"
    with image.open("wb") as file:
        file.write(b"a" * 4096)
        file.seek(1024 * 1024)
        file.write(b"tail")
        file.truncate(2 * 1024 * 1024)
    reader = RawImageReader(image)
    rebuilt = bytearray(reader.size)
    try:
        for index in reader.changed_chunks(16384):
            offset = index * 16384
            data = reader.read(offset, min(16384, reader.size - offset))
            rebuilt[offset : offset + len(data)] = data
    finally:
        reader.close()
    assert bytes(rebuilt) == image.read_bytes()


def test_stopped_or_explicit_temporary_stop_backup_and_restore(api):
    client, driver, backups, _ = api
    source = driver.containers().create(ContainerSpec("source"))
    driver.containers()._rootfs["source"] = b"important container contents" * 2000
    denied = client.post("/api/v1/containers/source/backup", json={"target": "local"})
    assert denied.status_code == 409 and source.state == "running"
    response = client.post(
        "/api/v1/containers/source/backup", json={"target": "local", "allow_stop": True}
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["state"] == "done" and job["entity_type"] == "container", job
    assert source.state == "running"
    assert job["report"]["entity_type"] == "container"
    points = client.get("/api/v1/backup/targets/local/backups").json()
    assert len(points) == 1 and points[0]["entity_type"] == "container"
    point = points[0]["id"]
    denied = client.post(
        f"/api/v1/backup/targets/local/backups/{point}/restore-container",
        json={"name": "source", "source_node": "node1"},
    )
    assert denied.status_code == 409
    response = client.post(
        f"/api/v1/backup/targets/local/backups/{point}/restore-container",
        json={"name": "restored", "source_node": "node1", "pool": "default"},
    )
    assert response.status_code == 202 and response.json()["state"] == "done", response.text
    restored = driver.containers().get("restored")
    assert restored.state == "stopped" and not restored.spec.autostart
    assert restored.spec.mac != source.spec.mac
    assert driver.containers()._rootfs["restored"] == driver.containers()._rootfs["source"]
    member = backups._cluster.member("node1")
    with pytest.raises(InvalidState, match="container restore"):
        member.backups.restore(backups.target("local"), point, "wrong-vm")
    assert not driver.list_vms()


def test_backup_failure_and_cancellation_restart_original_without_publishing(tmp_path, monkeypatch):
    driver = FakeDriver()
    source = driver.containers().create(ContainerSpec("source"))
    node = NodeBackup(driver, chunk_size=4096)
    target = BackupTarget(name="repo", type=TargetType.LOCAL, path=str(tmp_path / "repo"))
    first = node.backup("ct:source", target, "container_stop")
    repo = Repository(DirStore(tmp_path / "repo"))
    cancelled = []

    def cancel(step, percent):
        if node.backup_progress("ct:source", "repo").get("read_bytes", 0) and not cancelled:
            cancelled.append(node.cancel_backup("ct:source", "repo"))

    with pytest.raises(BackupCancelled):
        node.backup("ct:source", target, "container_stop", progress=cancel)
    assert source.state == "running" and cancelled == [True]
    assert [point.id for point in repo.list()] == [first.id]

    def fail(*args, **kwargs):
        raise OSError("Storage failed")

    monkeypatch.setattr(Repository, "backup_readers", fail)
    with pytest.raises(OSError, match="Storage failed"):
        node.backup("ct:source", target, "container_stop")
    assert source.state == "running"


def test_restoring_corrupt_point_does_not_publish_container(tmp_path):
    driver = FakeDriver()
    driver.containers().create(ContainerSpec("source"))
    node = NodeBackup(driver, chunk_size=4096)
    target = BackupTarget(name="repo", type=TargetType.LOCAL, path=str(tmp_path / "repo"))
    point = node.backup("ct:source", target, "container_stop")
    store = DirStore(tmp_path / "repo")
    store.put(store.list("chunks/")[0], b"corrupt")
    with pytest.raises((zlib.error, ValueError)):
        node.restore_container(target, point.id, "restored")
    assert [c.spec.name for c in driver.containers().list()] == ["source"]


def test_lxc_restore_is_atomic_owned_stopped_and_preserves_template(tmp_path, monkeypatch):
    from test_container_ownership import nodes

    first, peer, states, calls = nodes(tmp_path, monkeypatch)
    spec = ContainerSpec("restored", pool="pool", autostart=True, password="discard-me")

    def write(path):
        path.write_bytes(b"filesystem bytes")

    result = first.import_rootfs(
        spec, write, ["/usr/share/lxc/config/ubuntu.common.conf", "/tmp/unsafe.conf"]
    )
    home = tmp_path / "pool/containers/restored"
    assert result.state == "stopped" and result.owner_node == "node1"
    assert peer.list() == [] and not result.spec.autostart
    assert result.spec.password == ""
    config = (home / "config").read_text()
    assert "ubuntu.common.conf" in config and "unsafe.conf" not in config
    assert f"loop:{home}/rootfs.img" in config
    assert not any(argv[0] in ("lxc-start", "lxc-create") for _, argv in calls)
    with pytest.raises(InvalidState, match="already exists"):
        first.import_rootfs(spec, write)
    assert (home / "rootfs.img").read_bytes() == b"filesystem bytes"

    def fail(path):
        path.write_bytes(b"partial")
        raise OSError("Interrupted restore")

    with pytest.raises(OSError):
        first.import_rootfs(ContainerSpec("failed", pool="pool"), fail)
    assert not (home.parent / "failed").exists()
    assert not list(home.parent.glob(".failed-restore-*"))


def test_lxc_backup_validates_actual_rootfs_and_always_restarts(tmp_path, monkeypatch):
    from test_container_ownership import nodes

    from ibhics.hypervisor.containers import ContainerRestartFailed

    first, _, states, _ = nodes(tmp_path, monkeypatch)
    first.create(ContainerSpec("app", pool="pool"))
    home = tmp_path / "pool/containers/app"
    with first.backup_source("app", allow_stop=True) as (image, definition):
        assert states["node1"]["app"] == "STOPPED"
        assert image.read_bytes() == b"root filesystem contents"
        assert definition["entity_type"] == "container"
        assert "password" not in definition["container"]
    assert states["node1"]["app"] == "RUNNING"
    (home / "config").write_text("lxc.rootfs.path = dir:/some/other/rootfs")
    with pytest.raises(InvalidState, match="does not match"):
        with first.backup_source("app", allow_stop=True):
            pytest.fail("Mismatched rootfs must not be backed up")
    assert states["node1"]["app"] == "RUNNING"
    (home / "config").write_text(f"lxc.rootfs.path = loop:{home}/rootfs.img")
    action = first.action

    def cannot_restart(name, act):
        if act == "start":
            raise OSError("Start failed")
        return action(name, act)

    monkeypatch.setattr(first, "action", cannot_restart)
    with pytest.raises(ContainerRestartFailed):
        with first.backup_source("app", allow_stop=True):
            pass


def test_seek_hole_error_never_silently_discards_data(tmp_path, monkeypatch):
    import errno
    import os

    path = tmp_path / "image"
    path.write_bytes(b"contents")
    reader = RawImageReader(path)

    def seek(fd, offset, whence):
        if whence == os.SEEK_DATA:
            return 0
        raise OSError(errno.ENXIO, "Invalid hole query")

    monkeypatch.setattr(os, "lseek", seek)
    try:
        with pytest.raises(OSError):
            list(reader.changed_chunks(4096))
    finally:
        reader.close()


def test_remote_agent_container_roundtrip(tmp_path):
    from fastapi.testclient import TestClient
    from test_cluster import TOKEN, Agents, join
    from test_migration import Inline

    from ibhics.cluster import Cluster, NodeRole
    from ibhics.manager import create_app
    from ibhics.manager.backups import BackupManager

    source = FakeDriver()
    source.containers().create(ContainerSpec("remote"))
    source.containers()._rootfs["remote"] = b"remote data" * 1000
    agents = Agents()
    agents.add("http://remote:8007", "node2", NodeRole.COMPUTE, source, NodeBackup(source))
    cluster = Cluster(token=TOKEN, client_factory=agents.client)
    cluster.add_local("node1", FakeDriver())
    jobs = BackupManager(cluster, executor=Inline())
    client = TestClient(create_app(cluster, backups=jobs))
    assert join(client, "node2", "compute", "http://remote:8007").status_code == 201
    assert (
        client.post(
            "/api/v1/backup/targets",
            json={"name": "repo", "type": "local", "path": str(tmp_path / "repo")},
        ).status_code
        == 201
    )
    response = client.post(
        "/api/v1/containers/remote/backup", json={"target": "repo", "allow_stop": True}
    )
    job = response.json()
    assert response.status_code == 202 and job["state"] == "done", job
    assert job["node"] == "node2" and source.containers().get("remote").state == "running"
    point = next(
        p for p in client.get("/api/v1/backup/targets/repo/backups").json() if p["node"] == "node2"
    )
    assert point["entity_type"] == "container"
    response = client.post(
        f"/api/v1/backup/targets/repo/backups/{point['id']}/restore-container",
        json={"name": "copy", "source_node": "node2"},
    )
    assert response.status_code == 202 and response.json()["state"] == "done", response.text
    assert source.containers().get("copy").state == "stopped"
    assert source.containers()._rootfs["copy"] == source.containers()._rootfs["remote"]


def test_viewer_cannot_backup_or_restore_container(tmp_path):
    from fastapi.testclient import TestClient
    from test_auth import as_user

    from ibhics.manager import create_app
    from ibhics.manager.auth import Auth

    auth = Auth(state_path=tmp_path / "users.json")
    client = TestClient(create_app(FakeDriver(), auth=auth))
    viewer = as_user((client, auth), "viewer", "viewer")
    assert (
        viewer.post(
            "/api/v1/containers/name/backup", json={"target": "repo", "allow_stop": True}
        ).status_code
        == 403
    )
    assert (
        viewer.post(
            "/api/v1/backup/targets/repo/backups/point/restore-container", json={"name": "copy"}
        ).status_code
        == 403
    )


def test_remote_cancel_is_reported_as_cancelled_not_json_error(tmp_path, monkeypatch):
    from test_cluster import TOKEN, Agents

    from ibhics.cluster import NodeRole
    from ibhics.cluster.remote import RemoteBackup

    driver = FakeDriver()
    node = NodeBackup(driver)

    def cancelled(*args, **kwargs):
        raise BackupCancelled("Cancelled by operator")

    monkeypatch.setattr(node, "backup", cancelled)
    agents = Agents()
    agents.add("http://node:8007", "node", NodeRole.COMPUTE, driver, node)
    remote = RemoteBackup(agents.client("http://node:8007", TOKEN))
    target = BackupTarget(name="repo", type=TargetType.LOCAL, path=str(tmp_path))
    with pytest.raises(BackupCancelled, match="Cancelled by operator"):
        remote.backup("ct:source", target, "container_stop")


def test_container_daily_schedule_tick_retention_and_edit(api, monkeypatch):
    from datetime import timedelta

    from ibhics.manager.backups import BackupManager

    client, driver, backups, clock = api
    source = driver.containers().create(ContainerSpec("scheduled"))
    vm = test_backup.new_vm(client, "not-selected")
    body = {
        "name": "ct-nightly",
        "target": "local",
        "entity_type": "container",
        "vms": ["scheduled"],
        "at": "02:00",
        "keep_last": 1,
        "allow_stop": True,
    }
    response = client.post("/api/v1/backup/policies", json=body)
    assert response.status_code == 201, response.text
    assert backups.tick() == []
    clock.now += timedelta(hours=1)
    [job] = backups.tick()
    assert job.state == "done" and job.entity_type == "container" and job.scheduled
    assert source.state == "running" and job.vm_uuid == "ct:scheduled"
    assert backups.tick() == []
    assert all(
        point.vm_uuid != vm["uuid"]
        for point in backups._cluster.member("node1").backups.list(backups.target("local"))
    )
    last_run = backups.policies()[0].last_run
    response = client.put(
        "/api/v1/backup/policies/ct-nightly", json={**body, "every_hours": 2, "at": None}
    )
    assert response.status_code == 200 and response.json()["last_run"] == last_run
    # Advance the repository timestamp for a distinct restore-point ID.
    import ibhics.backup.repository as repository

    real_datetime = repository.datetime

    class Later(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(seconds=10)

    monkeypatch.setattr(repository, "datetime", Later)
    clock.now += timedelta(hours=2)
    [again] = backups.tick()
    assert again.state == "done"
    assert len(backups._cluster.member("node1").backups.list(backups.target("local"))) == 1
    loaded = BackupManager(backups._cluster, backups._state_path, clock)
    loaded.load()
    assert loaded.policies()[0].entity_type == "container" and loaded.policies()[0].allow_stop
    client.delete("/api/v1/backup/policies/ct-nightly")
    clock.now += timedelta(days=1)
    assert backups.tick() == []


def test_container_schedule_retry_preserves_stop_permission(api, monkeypatch):
    from datetime import timedelta

    client, driver, backups, clock = api
    source = driver.containers().create(ContainerSpec("scheduled"))
    member = backups._cluster.member("node1")
    original = member.backups.backup
    attempts = []

    def transient(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            raise OSError("Temporary storage error")
        return original(*args, **kwargs)

    monkeypatch.setattr(member.backups, "backup", transient)
    body = {
        "name": "retry",
        "target": "local",
        "entity_type": "container",
        "vms": ["scheduled"],
        "every_hours": 24,
        "allow_stop": True,
        "retry_delay_minutes": 1,
    }
    assert client.post("/api/v1/backup/policies", json=body).status_code == 201
    [job] = backups.tick()
    assert job.state == "retry_wait"
    clock.now += timedelta(minutes=2)
    [again] = backups.tick()
    assert again.state == "done" and again.id == job.id
    assert all(args[0] == "ct:scheduled" and args[2] == "container_stop" for args in attempts)
    assert source.state == "running"
