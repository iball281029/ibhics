"""Owner-thread NBD pipelining and two-slot repository admission."""

import hashlib
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_backup import CHUNK, image, make_vm
from test_backup_progress import wait_until

from ibhics.backup import (
    BackupCancelled,
    BackupTarget,
    DirStore,
    NodeBackup,
    Repository,
    TargetType,
)
from ibhics.backup.store import RepositoryBusy, RepositoryLock
from ibhics.hypervisor import VMSpec
from ibhics.hypervisor.fake import FakeDriver


@pytest.mark.parametrize("incremental", [False, True])
def test_backup_pipeline_reads_ahead_on_owner_and_preserves_manifest(tmp_path, incremental):
    processing, next_read = threading.Event(), threading.Event()
    owner = threading.get_ident()
    data = b"a" * CHUNK + b"b" * CHUNK + b"\0" * CHUNK + b"a" * CHUNK + b"tail"

    class Store(DirStore):
        def exists(self, key):
            if key.startswith("chunks/") and not processing.is_set():
                assert threading.get_ident() != owner
                processing.set()
                assert next_read.wait(3), "Owner did not read ahead during chunk processing"
            return super().exists(key)

    class Reader:
        size = len(data)

        def changed_chunks(self, chunk_size):
            assert threading.get_ident() == owner
            yield from range((self.size + chunk_size - 1) // chunk_size)

        def read(self, offset, count):
            assert threading.get_ident() == owner
            if offset == CHUNK:
                assert processing.wait(3)
                next_read.set()
            return data[offset:offset + count]

    repo = Repository(Store(tmp_path / "repo"), CHUNK)
    parent = {"chunk_size": CHUNK, "disks": [{"size_bytes": len(data), "chunks": [None] * 5}]}
    point = repo.backup_readers(
        make_vm(), [Reader()], {}, {"incremental": incremental},
        parent=parent if incremental else None,
    )
    expected = [
        hashlib.sha256(block).hexdigest() if block.strip(b"\0") else None
        for offset in range(0, len(data), CHUNK) for block in [data[offset:offset + CHUNK]]
    ]
    manifest = repo.manifest(point.id)
    assert manifest["disks"][0]["chunks"] == expected
    assert point.read_bytes == len(data)
    assert point.new_data_bytes == 2 * CHUNK + 4
    repo.restore_disk(manifest, 0, tmp_path / "restored")
    assert (tmp_path / "restored").read_bytes() == data


def test_backup_pipeline_cancel_during_read_drains_workers_without_manifest(tmp_path):
    cancelled = threading.Event()
    reads = []

    class Reader:
        size = 10 * CHUNK

        def changed_chunks(self, chunk_size):
            return range(10)

        def read(self, offset, count):
            reads.append(offset)
            if offset == CHUNK:
                cancelled.set()
            return b"a" * count

    def progress(step, percent):
        if cancelled.is_set():
            raise BackupCancelled("cancelled during NBD read")

    store = DirStore(tmp_path / "repo")
    repo = Repository(store, CHUNK)
    with pytest.raises(BackupCancelled):
        repo.backup_readers(make_vm(), [Reader()], {}, {}, progress=progress)
    assert reads == [0, CHUNK]
    assert repo.list() == []
    assert not any(store.exists(f".repository-backup-{slot}") for slot in range(2))
    assert not store.list("locks/")
    repo.gc()  # All worker writes finished before the shared lease was released.
    assert store.list("chunks/") == []


def test_backup_shared_slots_across_processes(tmp_path):
    code = """
import sys
from pathlib import Path
from ibhics.backup import DirStore
with DirStore(Path(sys.argv[1])).shared():
    print('ready', flush=True)
    sys.stdin.readline()
"""
    root = tmp_path / "repo"
    store = DirStore(root)
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(root)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with store.shared():
            with pytest.raises(RepositoryBusy):
                with DirStore(root).shared():
                    pytest.fail("A third process must not acquire a backup slot")
            with pytest.raises(RepositoryBusy):
                Repository(DirStore(root)).gc()
        child.communicate("done\n", timeout=5)
        assert child.returncode == 0
        with store.exclusive():
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize("holder_shared", [False, True])
def test_backup_and_maintenance_wait_across_independent_stores(tmp_path, holder_shared):
    first, second = DirStore(tmp_path / "repo"), DirStore(tmp_path / "repo")
    attempted, acquired = threading.Event(), threading.Event()
    release = threading.Event()

    def waiter():
        attempted.set()
        with second.waiting(shared=not holder_shared):
            acquired.set()
            assert release.wait(3)

    with ThreadPoolExecutor(1) as pool:
        try:
            with (first.shared() if holder_shared else first.exclusive()):
                future = pool.submit(waiter)
                assert attempted.wait(3)
                assert not acquired.wait(0.1)
            assert acquired.wait(3)
        finally:
            release.set()
        future.result(timeout=3)



def test_backup_prune_waits_for_both_node_jobs_and_backup_waits_for_prune(tmp_path):
    driver = FakeDriver()
    node = NodeBackup(driver, chunk_size=CHUNK)
    target = BackupTarget("repo", TargetType.LOCAL, path=str(tmp_path / "repo"))
    first = driver.create_vm(VMSpec(name="first"))
    second = driver.create_vm(VMSpec(name="second"))
    third = driver.create_vm(VMSpec(name="third"))
    entered = set()
    mutex = threading.Lock()
    release = threading.Event()

    @contextmanager
    def export(uuid):
        with mutex:
            entered.add(uuid)
        assert release.wait(5)
        yield [image(tmp_path, uuid, b"a" * CHUNK)]

    driver.export_disks = export
    with ThreadPoolExecutor(4) as pool:
        try:
            a = pool.submit(node.backup, first.uuid, target)
            b = pool.submit(node.backup, second.uuid, target)
            wait_until(lambda: len(entered) == 2)
            c = pool.submit(node.backup, third.uuid, target)
            wait_until(lambda: node.backup_progress(third.uuid).get("phase") == "waiting")
            assert third.uuid not in entered
            prune = pool.submit(node.prune, target, "first", 1)
            assert not prune.done()
        finally:
            release.set()
        for future in (a, b, c, prune):
            future.result(timeout=5)
    assert entered == {first.uuid, second.uuid, third.uuid}

    prune_entered, prune_release = threading.Event(), threading.Event()
    original = Repository.prune

    def paused_prune(repo, *args, **kwargs):
        prune_entered.set()
        assert prune_release.wait(5)
        return original(repo, *args, **kwargs)

    from unittest.mock import patch

    entered.clear()
    with patch.object(Repository, "prune", paused_prune), ThreadPoolExecutor(2) as pool:
        try:
            prune = pool.submit(node.prune, target, "first", 1)
            assert prune_entered.wait(3)
            backup = pool.submit(node.backup, first.uuid, target)
            wait_until(lambda: node.backup_progress(first.uuid).get("phase") == "waiting")
            assert first.uuid not in entered
        finally:
            prune_release.set()
        prune.result(timeout=5)
        backup.result(timeout=5)


def test_backup_local_admission_third_job_waits_and_pending_maintenance_has_priority():
    lock = RepositoryLock()
    assert lock.backup.acquire(False)
    assert lock.backup.acquire(False)
    assert not lock.backup.acquire(False)
    assert not lock.acquire(False)
    lock.backup.release()
    assert not lock.acquire(False)
    lock.backup.release()
    assert lock.acquire(False)
    assert not lock.backup.acquire(False)
    lock.release()
    assert lock.backup.acquire(False)
    lock.backup.release()


def test_backup_manager_allows_two_vms_but_serializes_same_vm(tmp_path):
    from dataclasses import replace

    from ibhics.backup import BackupInfo
    from ibhics.cluster import Cluster
    from ibhics.manager.backups import BackupManager

    driver = FakeDriver()
    vms = [driver.create_vm(VMSpec(name=f"vm{i}")) for i in range(3)]
    node = NodeBackup(driver, chunk_size=CHUNK)
    cluster = Cluster()
    cluster.add_local("node", driver, node)
    entered, mutex = [], threading.Lock()
    release = threading.Event()
    point = BackupInfo("vm0@20261127T000000Z", "vm0", vms[0].uuid, "", 0, 0, 0)

    def backup(uuid, target, mode):
        with mutex:
            entered.append(uuid)
        assert release.wait(5)
        vm = driver.get_vm(uuid)
        return replace(point, id=f"{vm.spec.name}@20261127T000000Z", vm_name=vm.spec.name)

    node.backup = backup
    with ThreadPoolExecutor(4) as pool:
        manager = BackupManager(cluster, state_path=tmp_path / "state.json", executor=pool)
        target = BackupTarget("repo", TargetType.LOCAL, path=str(tmp_path / "repo"))
        manager.add_target(target)
        try:
            a = manager.start_backup(vms[0].uuid, "repo")
            b = manager.start_backup(vms[1].uuid, "repo")
            wait_until(lambda: len(entered) == 2)
            c = manager.start_backup(vms[2].uuid, "repo")
            duplicate = manager.start_backup(vms[0].uuid, "repo")
            wait_until(lambda: c.state == duplicate.state == "running")
            assert entered.count(vms[0].uuid) == 1
            assert vms[2].uuid not in entered
            with pytest.raises(RepositoryBusy):
                with manager._using(target, wait=False):
                    pytest.fail("Maintenance overlapped manager backup admission")
        finally:
            release.set()
        wait_until(lambda: all(job.state == "done" for job in (a, b, c, duplicate)))
    assert entered.count(vms[0].uuid) == 2


def test_backup_node_rejects_duplicate_vm_without_overwriting_cancel_state(tmp_path):
    driver = FakeDriver()
    vm = driver.create_vm(VMSpec(name="single"))
    node = NodeBackup(driver)
    target = BackupTarget("repo", TargetType.LOCAL, path=str(tmp_path / "repo"))
    with node._locks[target.name], ThreadPoolExecutor(1) as pool:
        first = pool.submit(node.backup, vm.uuid, target)
        wait_until(lambda: node.backup_progress(vm.uuid).get("state") == "running")
        with pytest.raises(RepositoryBusy):
            node.backup(vm.uuid, target)
        assert node.cancel_backup(vm.uuid, target.name)
        with pytest.raises(BackupCancelled):
            first.result(timeout=3)


def test_backup_verify_keeps_repository_exclusive_through_boot_and_evidence(tmp_path):
    from ibhics.hypervisor import DiskSpec

    driver = FakeDriver()
    vm = driver.create_vm(VMSpec(name="verified", disks=[DiskSpec(size_gb=1)]))
    node = NodeBackup(driver, chunk_size=CHUNK)
    target = BackupTarget("repo", TargetType.LOCAL, path=str(tmp_path / "repo"))
    point = node.backup(vm.uuid, target)
    booting, release = threading.Event(), threading.Event()
    node._verify_capacity = lambda *args: None

    def boot_ready(uuid):
        booting.set()
        assert release.wait(5)
        return True

    driver.backup_boot_ready = boot_ready
    with ThreadPoolExecutor(2) as pool:
        try:
            verification = pool.submit(node.verify, target, point.id, "default")
            assert booting.wait(3)
            with pytest.raises(RepositoryBusy):
                with DirStore(tmp_path / "repo").shared():
                    pytest.fail("A backup overlapped isolated boot verification")
            prune = pool.submit(node.prune, target, "verified", 1)
            assert not prune.done()
        finally:
            release.set()
        assert verification.result(timeout=5)["verified"] == "passed"
        prune.result(timeout=5)
    assert Repository(DirStore(tmp_path / "repo")).info(point.id).verified == "passed"
