import os
import stat
from concurrent.futures import Executor, Future
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ibhics.backup import (
    BackupNotFound,
    BackupTarget,
    DirStore,
    Mounter,
    NodeBackup,
    Repository,
    RepositoryBusy,
    S3Store,
    TargetType,
)
from ibhics.backup.store import open_store
from ibhics.cluster import Cluster
from ibhics.hypervisor import DiskSpec, VMInfo, VMSpec, VMState
from ibhics.hypervisor.fake import FAKE_BYTES_PER_GB, FakeDriver
from ibhics.hypervisor.serialize import spec_to_json
from ibhics.manager import create_app
from ibhics.manager.backups import BackupManager, Policy

CHUNK = 16 * 1024
IQN = "iqn.2004-04.com.example:backup"


class Inline(Executor):
    """Runs jobs as they are submitted, so tests see the finished job."""

    def submit(self, fn, *args, **kwargs):
        future = Future()
        fn(*args, **kwargs)
        future.set_result(None)
        return future


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


# Targets


@pytest.mark.parametrize(
    "target",
    [
        BackupTarget(name="local", type=TargetType.LOCAL),
        BackupTarget(name="nas", type=TargetType.NFS, host="10.0.0.50", export="/backup"),
        BackupTarget(name="win", type=TargetType.SMB, host="fs01", share="Backup$", username="u"),
        BackupTarget(name="usb", type=TargetType.USB, device="/dev/disk/by-label/BACKUP"),
        BackupTarget(name="san", type=TargetType.ISCSI, host="10.0.0.60", iqn=IQN, lun=2),
        BackupTarget(name="fc", type=TargetType.FC, device="/dev/mapper/mpathb"),
        BackupTarget(
            name="minio",
            type=TargetType.S3,
            endpoint="https://minio.local:9000",
            bucket="vm-backup",
            access_key="AK",
            secret_key="SK",
        ),
    ],
)
def test_valid_targets(target):
    target.validate()


@pytest.mark.parametrize(
    "target",
    [
        BackupTarget(name="bad name", type=TargetType.LOCAL),
        BackupTarget(name="root", type=TargetType.LOCAL, path="/"),
        BackupTarget(name="nas", type=TargetType.NFS, host="10.0.0.50"),
        BackupTarget(name="win", type=TargetType.SMB, host="fs01"),
        BackupTarget(name="win", type=TargetType.SMB, host="fs01", share="b", password="a,b"),
        BackupTarget(name="usb", type=TargetType.USB, device="/etc/fstab"),
        BackupTarget(name="san", type=TargetType.ISCSI, host="10.0.0.60", iqn="nope"),
        BackupTarget(name="san", type=TargetType.ISCSI, iqn=IQN),
        BackupTarget(name="fc", type=TargetType.FC, device="/dev/sdb; reboot"),
        BackupTarget(name="s3", type=TargetType.S3, endpoint="https://x", bucket="Bad_Bucket"),
        BackupTarget(name="s3", type=TargetType.S3, endpoint="https://x", bucket="ok-bucket"),
        BackupTarget(name="esc", type=TargetType.NFS, host="h", export="/e", prefix="../../etc"),
    ],
)
def test_invalid_targets(target):
    with pytest.raises(ValueError):
        target.validate()


def test_secrets_are_masked():
    target = BackupTarget(
        name="s3",
        type=TargetType.S3,
        endpoint="https://s3.local",
        bucket="b-1",
        access_key="AK",
        secret_key="very-secret",
    )
    public = target.public_dict()
    assert public["secret_key"] == "********" and public["access_key"] == "AK"
    assert BackupTarget.from_dict(target.to_dict()) == target


# Repository


def make_vm(name="web", disks=1):
    spec = VMSpec(name=name, disks=[DiskSpec(size_gb=1) for _ in range(disks)])
    return VMInfo(uuid=f"uuid-{name}", spec=spec, state=VMState.STOPPED)


def image(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return path


@pytest.fixture
def repo(tmp_path):
    clock = Clock(datetime(2026, 10, 29, 2, 0, tzinfo=UTC))
    repo = Repository(DirStore(tmp_path / "repo"), chunk_size=CHUNK, clock=clock)
    repo.clock = clock
    return repo


def chunk_files(repo):
    return repo.store.list("chunks/")


def test_backup_dedups_and_skips_zeros(repo, tmp_path):
    vm = make_vm()
    data = bytearray(CHUNK * 8)  # eight chunks, all zero except two
    data[0:5] = b"hello"
    data[CHUNK * 5 : CHUNK * 5 + 5] = b"world"
    first = repo.backup(vm, [image(tmp_path, "d0", bytes(data))], spec_to_json(vm.spec))
    assert first.size_bytes == CHUNK * 8
    assert len(chunk_files(repo)) == 2  # zero chunks are not stored
    assert first.stored_bytes > 0

    repo.clock.now += timedelta(days=1)
    second = repo.backup(vm, [image(tmp_path, "d1", bytes(data))], spec_to_json(vm.spec))
    assert second.stored_bytes == 0  # nothing changed, nothing new stored

    data[CHUNK * 3] = 1
    repo.clock.now += timedelta(days=1)
    third = repo.backup(vm, [image(tmp_path, "d2", bytes(data))], spec_to_json(vm.spec))
    assert len(chunk_files(repo)) == 3  # only the changed chunk is new
    assert [b.id for b in repo.list("web")] == [first.id, second.id, third.id]

    out = tmp_path / "restored"
    repo.restore_disk(repo.manifest(third.id), 0, out)
    assert out.read_bytes() == bytes(data)


def test_restore_keeps_odd_sizes(repo, tmp_path):
    vm = make_vm()
    data = b"x" * (CHUNK + 123) + b"\0" * 7
    info = repo.backup(vm, [image(tmp_path, "d", data)], spec_to_json(vm.spec))
    out = tmp_path / "out"
    repo.restore_disk(repo.manifest(info.id), 0, out)
    assert out.read_bytes() == data


def test_ids_unique_within_a_second(repo, tmp_path):
    vm = make_vm()
    a = repo.backup(vm, [image(tmp_path, "d", b"a")], {})
    b = repo.backup(vm, [image(tmp_path, "d", b"b")], {})
    assert a.id == "web@20261029T020000Z" and b.id == "web@20261029T020000Z-2"


def test_prune_and_gc(repo, tmp_path):
    vm = make_vm()
    for i in range(4):
        repo.clock.now += timedelta(hours=1)
        repo.backup(vm, [image(tmp_path, "d", bytes([i + 1]) * CHUNK)], {})
    assert len(chunk_files(repo)) == 4
    removed = repo.prune("web", keep_last=2)
    assert len(removed) == 2 and len(repo.list("web")) == 2
    assert repo.gc() == 2
    assert len(chunk_files(repo)) == 2


def test_gc_waits_for_running_backup(repo):
    repo.store.put("locks/web@20261029T015900Z", repo.clock.now.isoformat().encode())
    with pytest.raises(RepositoryBusy):
        repo.gc()
    repo.clock.now += timedelta(hours=13)  # a lock this old is from a crashed job
    assert repo.gc() == 0


def test_corrupt_chunk_detected(repo, tmp_path):
    vm = make_vm()
    info = repo.backup(vm, [image(tmp_path, "d", b"z" * CHUNK)], {})
    (key,) = chunk_files(repo)
    import zlib

    repo.store.put(key, zlib.compress(b"y" * CHUNK))
    with pytest.raises(RuntimeError, match="corrupt"):
        repo.restore_disk(repo.manifest(info.id), 0, tmp_path / "out")


def test_unknown_backup(repo):
    with pytest.raises(BackupNotFound):
        repo.manifest("web@20200101T000000Z")
    with pytest.raises(BackupNotFound):
        repo.delete("../../etc/passwd")


# Mounting external devices


class Recorder:
    def __init__(self, outputs=None, fail=()):
        self.calls = []
        self.smb_credentials = None
        self.outputs = outputs or {}  # command name -> stdout
        self.fail = fail  # command names that fail

    def __call__(self, cmd, ok_codes=(0,)):
        self.calls.append(cmd)
        if cmd[0] in self.fail:
            from ibhics.backup import TargetUnavailable

            raise TargetUnavailable(f"{cmd[0]} failed")
        if "cifs" in cmd:
            creds = cmd[-1].split(",")[0].removeprefix("credentials=")
            self.smb_credentials = open(creds).read()
            assert stat.S_IMODE(os.stat(creds).st_mode) == 0o600
        return self.outputs.get(cmd[0], "")


def test_nfs_mounted_for_job_and_shared(tmp_path):
    run = Recorder()
    mounter = Mounter(run=run, root=str(tmp_path / "mnt"))
    target = BackupTarget(name="nas", type=TargetType.NFS, host="10.0.0.50", export="/backup")
    point = str(tmp_path / "mnt" / "nas")
    with mounter.mounted(target), mounter.mounted(target):
        assert run.calls == [["mount", "-t", "nfs", "10.0.0.50:/backup", point]]
    assert run.calls[-1] == ["umount", point]
    assert len(run.calls) == 2


def test_smb_credentials_stay_off_the_command_line(tmp_path):
    run = Recorder()
    target = BackupTarget(
        name="win", type=TargetType.SMB, host="fs01", share="Backup", username="bk",
        password="p@ss", domain="CORP",
    )  # fmt: skip
    with Mounter(run=run, root=str(tmp_path)).mounted(target):
        pass
    mount = run.calls[0]
    assert mount[:4] == ["mount", "-t", "cifs", "//fs01/Backup"]
    assert "p@ss" not in " ".join(mount)
    assert run.smb_credentials == "username=bk\npassword=p@ss\ndomain=CORP\n"
    assert not os.path.exists(mount[-1].split(",")[0].removeprefix("credentials="))


def test_usb_store_uses_prefix_directory(tmp_path):
    run = Recorder()
    target = BackupTarget(name="usb", type=TargetType.USB, device="/dev/sdc1", prefix="vms")
    with open_store(target, Mounter(run=run, root=str(tmp_path))) as store:
        store.put("a/b", b"1")
    assert (tmp_path / "usb" / "vms" / "a" / "b").read_bytes() == b"1"
    assert run.calls[0] == ["mount", "/dev/sdc1", str(tmp_path / "usb")]


def test_iscsi_lun_logged_in_formatted_once_and_logged_out(tmp_path):
    run = Recorder()  # blkid prints nothing: the LUN is empty
    target = BackupTarget(
        name="san", type=TargetType.ISCSI, host="10.0.0.60", iqn=IQN, lun=1, confirm_wipe=True
    )
    with open_store(target, Mounter(run=run, root=str(tmp_path))) as store:
        store.put("x", b"1")
    device = f"/dev/disk/by-path/ip-10.0.0.60:3260-iscsi-{IQN}-lun-1"
    portal = "10.0.0.60:3260"
    assert run.calls == [
        ["iscsiadm", "-m", "discovery", "-t", "sendtargets", "-p", portal],
        ["iscsiadm", "-m", "node", "-T", IQN, "-p", portal, "--login"],
        ["udevadm", "settle"],
        ["blkid", "-p", "-o", "value", "-s", "TYPE", device],
        ["mkfs.xfs", "-q", device],
        ["mount", device, str(tmp_path / "san")],
        ["umount", str(tmp_path / "san")],
        ["iscsiadm", "-m", "node", "-T", IQN, "-p", portal, "--logout"],
    ]
    assert not list((tmp_path / ".iscsi-sessions").iterdir())  # nothing left to clean up


def test_cleanup_detaches_what_a_crash_left(tmp_path):
    root = tmp_path / "mnt"
    run = Recorder(outputs={"blkid": "xfs"})
    mounter = Mounter(run=run, root=str(root))
    target = BackupTarget(name="san", type=TargetType.ISCSI, host="10.0.0.60", iqn=IQN)
    mounter._mount(target, root / "san")  # the node dies before the job ends...
    mounts = tmp_path / "mounts"
    mounts.write_text(
        f"/dev/sda1 / ext4 rw 0 0\n/dev/sdx {root}/san xfs rw 0 0\n"
        f"10.0.0.50:/b {root}/my\\040nas nfs rw 0 0\n"
    )
    run.calls.clear()
    fresh = Mounter(run=run, root=str(root))  # ...and comes back
    assert fresh.cleanup(str(mounts)) == [f"{root}/san", f"{root}/my nas", IQN]
    assert run.calls == [
        ["umount", "-l", f"{root}/san"],
        ["umount", "-l", f"{root}/my nas"],
        ["iscsiadm", "-m", "node", "-T", IQN, "-p", "10.0.0.60:3260", "--logout"],
    ]
    assert fresh.cleanup(str(mounts))[-1] != IQN  # session record is gone


def test_lun_with_filesystem_is_not_formatted(tmp_path):
    run = Recorder(outputs={"blkid": "xfs\n"})
    target = BackupTarget(name="fc", type=TargetType.FC, device="/dev/mapper/mpathb")
    with Mounter(run=run, root=str(tmp_path)).mounted(target):
        pass
    assert [c[0] for c in run.calls] == ["blkid", "mount", "umount"]


def test_empty_lun_needs_confirm_wipe(tmp_path):
    from ibhics.backup import TargetUnavailable

    run = Recorder()
    target = BackupTarget(name="san", type=TargetType.ISCSI, host="10.0.0.60", iqn=IQN)
    with pytest.raises(TargetUnavailable, match="confirm_wipe"):
        with Mounter(run=run, root=str(tmp_path)).mounted(target):
            pass
    assert "mkfs.xfs" not in [c[0] for c in run.calls]
    assert run.calls[-1][-1] == "--logout"  # no session left behind


def test_failed_mount_logs_out(tmp_path):
    run = Recorder(outputs={"blkid": "xfs"}, fail=("mount",))
    target = BackupTarget(name="san", type=TargetType.ISCSI, host="10.0.0.60", iqn=IQN)
    mounter = Mounter(run=run, root=str(tmp_path))
    with pytest.raises(Exception, match="mount failed"):
        with mounter.mounted(target):
            pass
    assert run.calls[-1][-1] == "--logout"
    # The failed attempt left nothing counted as mounted: the next job mounts again.
    run.fail = ()
    with mounter.mounted(target):
        pass
    assert run.calls[-2][0] == "umount"


# S3


class FakeS3:
    class exceptions:  # noqa: N801 - mirrors boto3's client.exceptions
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        body = self.objects[(Bucket, Key)]
        return {"Body": type("B", (), {"read": lambda self: body})()}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000):  # noqa: N803
        keys = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys[:MaxKeys]]}

    def get_paginator(self, _name):
        s3 = self

        class Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                yield s3.list_objects_v2(Bucket, Prefix)

        return Paginator()

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop((Bucket, Key), None)


def test_repository_on_s3(tmp_path):
    s3 = FakeS3()
    repo = Repository(S3Store(s3, "vm-backup", "site1"), chunk_size=CHUNK)
    vm = make_vm()
    info = repo.backup(vm, [image(tmp_path, "d", b"q" * CHUNK * 2)], {})
    assert all(key.startswith("site1/") for _, key in s3.objects)
    assert [b.id for b in repo.list()] == [info.id]
    out = tmp_path / "out"
    repo.restore_disk(repo.manifest(info.id), 0, out)
    assert out.read_bytes() == b"q" * CHUNK * 2
    repo.delete(info.id)
    assert repo.gc() == 1


# Policies


def test_daily_policy_due_once_per_day():
    policy = Policy(name="nightly", target="t", at="02:00")
    day = datetime(2026, 10, 29, 1, 59)
    assert not policy.due(day)
    assert policy.due(day.replace(hour=2, minute=0))
    policy.last_run = day.replace(hour=2, minute=0).isoformat()
    assert not policy.due(day.replace(hour=23))
    assert policy.due(day + timedelta(days=1, minutes=1))


def test_interval_policy():
    policy = Policy(name="hourly", target="t", every_hours=4)
    now = datetime(2026, 10, 29, 12, 0)
    assert policy.due(now)
    policy.last_run = now.isoformat()
    assert not policy.due(now + timedelta(hours=3))
    assert policy.due(now + timedelta(hours=4))


@pytest.mark.parametrize(
    "policy",
    [
        Policy(name="p", target="t"),
        Policy(name="p", target="t", at="02:00", every_hours=1),
        Policy(name="p", target="t", at="25:00"),
        Policy(name="p", target="t", every_hours=1, keep_last=0),
    ],
)
def test_invalid_policies(policy):
    with pytest.raises(ValueError):
        policy.validate()


# API, standalone node


@pytest.fixture
def api(tmp_path):
    driver = FakeDriver()
    cluster = Cluster()
    cluster.add_local("node1", driver, NodeBackup(driver, chunk_size=CHUNK))
    clock = Clock(datetime(2026, 10, 29, 1, 0).astimezone())
    backups = BackupManager(
        cluster, state_path=tmp_path / "backup.json", clock=clock, executor=Inline()
    )
    client = TestClient(create_app(cluster, backups=backups))
    target = {"name": "local", "type": "local", "path": str(tmp_path / "backups")}
    assert client.post("/api/v1/backup/targets", json=target).status_code == 201
    return client, driver, backups, clock


def new_vm(client, name="web", size_gb=2):
    return client.post("/api/v1/vms", json={"name": name, "disks": [{"size_gb": size_gb}]}).json()


def test_backup_and_restore_vm(api):
    client, driver, *_ = api
    vm = new_vm(client)
    driver.write_disk(vm["uuid"], 0, 1000, b"important data")

    job = client.post(f"/api/v1/vms/{vm['uuid']}/backup", json={"target": "local"}).json()
    assert job["state"] == "done", job["error"]
    assert job["node"] == "node1"

    (backup,) = client.get("/api/v1/backup/targets/local/backups").json()
    assert backup["id"] == job["result"] and backup["vm_name"] == "web"
    assert backup["size_bytes"] == 2 * FAKE_BYTES_PER_GB and backup["node"] == "node1"

    # Restoring over a VM that still exists is refused; a copy under a new name works.
    url = f"/api/v1/backup/targets/local/backups/{backup['id']}/restore"
    assert client.post(url, json={"source_node": "node1"}).status_code == 409
    job = client.post(url, json={"source_node": "node1", "name": "web-copy"}).json()
    assert job["state"] == "done", job["error"]
    restored = client.get(f"/api/v1/vms/{job['result']}").json()
    assert restored["name"] == "web-copy"
    assert driver.read_disk(job["result"], 0) == driver.read_disk(vm["uuid"], 0)

    # Lost the original: restore it as it was.
    client.delete(f"/api/v1/vms/{vm['uuid']}")
    job = client.post(url, json={"source_node": "node1"}).json()
    assert job["state"] == "done"
    assert client.get(f"/api/v1/vms/{job['result']}").json()["name"] == "web"

    jobs = client.get("/api/v1/backup/jobs").json()
    assert [j["kind"] for j in jobs] == ["restore", "restore", "backup"]


def test_failed_restore_leaves_no_vm(api, monkeypatch):
    client, driver, *_ = api
    vm = new_vm(client)
    backup_id = client.post(f"/api/v1/vms/{vm['uuid']}/backup", json={"target": "local"}).json()[
        "result"
    ]

    def broken(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(driver, "import_disk", broken)
    url = f"/api/v1/backup/targets/local/backups/{backup_id}/restore"
    job = client.post(url, json={"source_node": "node1", "name": "copy"}).json()
    assert job["state"] == "failed" and "disk full" in job["error"]
    assert [v["name"] for v in client.get("/api/v1/vms").json()] == ["web"]


def test_policy_runs_on_schedule_and_keeps_last(api):
    client, _, backups, clock = api
    new_vm(client, "web")
    new_vm(client, "db")
    policy = {"name": "nightly", "target": "local", "vms": ["web"], "at": "02:00", "keep_last": 2}
    assert client.post("/api/v1/backup/policies", json=policy).status_code == 201

    assert backups.tick() == []  # 01:00, not yet
    results = []
    for day in range(3):
        clock.now = datetime(2026, 10, 29 + day, 2, 0, 30).astimezone()
        jobs = backups.tick()
        assert [(j.vm, j.state) for j in jobs] == [("web", "done")]
        results.append(jobs[0].result)
        assert backups.tick() == []  # once per day

    listed = client.get("/api/v1/backup/targets/local/backups").json()
    assert [b["id"] for b in listed] == results[1:]


def test_policy_run_now(api):
    client, *_ = api
    new_vm(client, "web")
    client.post(
        "/api/v1/backup/policies", json={"name": "all", "target": "local", "every_hours": 6}
    )
    jobs = client.post("/api/v1/backup/policies/all/run").json()
    assert [j["state"] for j in jobs] == ["done"]
    assert client.get("/api/v1/backup/policies").json()[0]["last_run"]


def test_target_rules(api):
    client, *_ = api
    body = {"name": "local", "type": "local"}
    assert client.post("/api/v1/backup/targets", json=body).status_code == 409
    bad = {"name": "nas", "type": "nfs", "host": "10.0.0.5"}
    assert client.post("/api/v1/backup/targets", json=bad).status_code == 422
    client.post("/api/v1/backup/policies", json={"name": "p", "target": "local", "every_hours": 1})
    assert client.delete("/api/v1/backup/targets/local").status_code == 422  # policy uses it
    client.delete("/api/v1/backup/policies/p")
    assert client.delete("/api/v1/backup/targets/local").status_code == 204
    assert client.get("/api/v1/backup/targets/local/backups").status_code == 404


def test_policy_needs_known_target(api):
    client, *_ = api
    body = {"name": "p", "target": "nowhere", "every_hours": 1}
    assert client.post("/api/v1/backup/policies", json=body).status_code == 404


def test_delete_backup(api):
    client, *_ = api
    vm = new_vm(client)
    backup_id = client.post(f"/api/v1/vms/{vm['uuid']}/backup", json={"target": "local"}).json()[
        "result"
    ]
    url = f"/api/v1/backup/targets/local/backups/{backup_id}"
    assert client.delete(url).status_code == 422  # local target: which node?
    assert client.delete(url, params={"node": "node1"}).status_code == 204
    assert client.get("/api/v1/backup/targets/local/backups").json() == []
    assert client.delete(url, params={"node": "node1"}).status_code == 404


def test_unreachable_target_fails_the_job(api, tmp_path):
    client, _, backups, _ = api
    vm = new_vm(client)
    nas = {"name": "nas", "type": "nfs", "host": "10.0.0.50", "export": "/backup"}
    client.post("/api/v1/backup/targets", json=nas)
    member = backups._cluster.members()[0]

    def fail(_cmd):
        from ibhics.backup import TargetUnavailable

        raise TargetUnavailable("mount failed: connection refused")

    member.backups._mounter = Mounter(run=fail, root=str(tmp_path / "mnt"))
    job = client.post(f"/api/v1/vms/{vm['uuid']}/backup", json={"target": "nas"}).json()
    assert job["state"] == "failed" and "connection refused" in job["error"]
    assert client.get("/api/v1/backup/targets/nas/backups").status_code == 502


def test_state_saved_privately_and_secrets_hidden(api, tmp_path):
    client, _, backups, _ = api
    s3 = {
        "name": "minio",
        "type": "s3",
        "endpoint": "https://minio.local:9000",
        "bucket": "vm-backup",
        "access_key": "AK",
        "secret_key": "topsecret",
    }
    assert client.post("/api/v1/backup/targets", json=s3).status_code == 201
    shown = {t["name"]: t for t in client.get("/api/v1/backup/targets").json()}
    assert shown["minio"]["settings"]["secret_key"] == "********"
    assert "topsecret" not in client.get("/api/v1/backup/targets").text

    path = tmp_path / "backup.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = BackupManager(backups._cluster, state_path=path)
    again.load()
    assert again.target("minio").secret_key == "topsecret"


def test_exclusive_target_blocks_listing_while_a_job_runs(api):
    client, _, backups, _ = api
    target = BackupTarget(name="fc", type=TargetType.FC, device="/dev/mapper/mpathb")
    backups._targets["fc"] = target
    with backups._using(target, wait=False):
        resp = client.get("/api/v1/backup/targets/fc/backups")
        assert resp.status_code == 409 and "try again" in resp.json()["detail"]
    shared = backups.target("local")
    with backups._using(shared, wait=False), backups._using(shared, wait=False):
        pass  # directories and NAS shares are never locked
