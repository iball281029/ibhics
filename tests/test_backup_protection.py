"""Protection contracts using disposable fake VMs and local/mock object stores only."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_backup import CHUNK, Clock, Inline, image, make_vm

from ibhics.backup import BackupTarget, DirStore, NodeBackup, Repository, S3Store, TargetType
from ibhics.cluster import Cluster
from ibhics.hypervisor import DiskSpec, InvalidState, NicSpec, VMSpec, VMState
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.backups import BackupManager, Policy


@pytest.fixture
def protection(tmp_path, monkeypatch):
    # Never invoke filesystem attribute tools, even on a Linux test host.
    monkeypatch.setattr(DirStore, "protect", lambda *args: False)
    monkeypatch.setattr(DirStore, "release", lambda *args: None)
    driver = FakeDriver()
    node = NodeBackup(driver, chunk_size=CHUNK, scratch_dir=str(tmp_path))
    target = BackupTarget("local", TargetType.LOCAL, path=str(tmp_path / "repository"))
    vm = driver.create_vm(
        VMSpec(
            name="production",
            disks=[DiskSpec(size_gb=1)],
            nics=[NicSpec(network="default")],
            autostart=True,
        )
    )
    point = node.backup(vm.uuid, target)
    return driver, node, target, vm, point


@pytest.mark.parametrize("failure", [None, "boot", "probe", "screenshot", "import"])
def test_verify_isolates_and_always_removes_temporary_vm(protection, monkeypatch, failure):
    driver, node, target, original, point = protection
    seen = []
    start = driver.start_vm

    def boot(uuid):
        spec = driver.get_vm(uuid).spec
        seen.append(spec)
        assert spec.nics == [] and not spec.autostart
        assert spec.name.startswith("ibhics-verify-")
        if failure == "boot":
            raise RuntimeError("boot failed")
        return start(uuid)

    driver.start_vm = boot
    driver.backup_boot_ready = Mock(return_value=True)
    if failure == "probe":
        driver.backup_boot_ready.side_effect = RuntimeError("probe failed")
    driver.backup_screenshot = Mock(return_value=(b"png-evidence", "image/png"))
    if failure == "screenshot":
        driver.backup_screenshot.side_effect = RuntimeError("no console")
    if failure == "import":
        monkeypatch.setattr(driver, "import_disk", Mock(side_effect=RuntimeError("import failed")))
    result = node.verify(target, point.id, "default", timeout=1)
    assert result["verified"] == ("failed" if failure in ("boot", "probe", "import") else "passed")
    assert [v.uuid for v in driver.list_vms()] == [original.uuid]
    assert driver.get_vm(original.uuid).spec.nics
    restored = node.list(target)[0]
    assert restored.verification == result
    assert restored.verified == result["verified"]
    if failure is None:
        assert seen and node.evidence(target, point.id)["data"]
    if failure == "screenshot":
        assert result["screenshot_error"] == "no console"


def test_verify_timeout_and_missing_probe_are_truthful(protection, monkeypatch):
    driver, node, target, vm, point = protection
    driver.backup_boot_ready = Mock(return_value=False)
    ticks = iter([0, 2, 0])
    monkeypatch.setattr(
        "ibhics.backup.service.time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None),
    )
    driver.backup_screenshot = Mock(return_value=(b"png", "image/png"))
    result = node.verify(target, point.id, "default", timeout=1)
    assert result["verified"] == "failed" and "timeout" in result["reason"]
    driver.backup_boot_ready.side_effect = NotImplementedError
    assert node.verify(target, point.id, "default")["verified"] == "skipped"
    assert len(driver.list_vms()) == 1


def test_cleanup_failure_records_vm_for_operator(protection):
    driver, node, target, vm, point = protection
    driver.backup_boot_ready = Mock(return_value=True)
    driver.backup_screenshot = Mock(return_value=(b"png", "image/png"))
    driver.delete_vm = Mock(side_effect=RuntimeError("storage offline"))
    result = node.verify(target, point.id, "default", timeout=1)
    assert result["verified"] == "failed"
    assert result["cleanup_vm_uuid"] != vm.uuid
    assert "cleanup failed" in result["reason"]
    driver.delete_vm.assert_called_once()


def test_verify_queue_serializes_even_for_different_points(protection):
    driver, node, target, vm, point = protection
    entered, release = threading.Event(), threading.Event()
    count = 0

    def probe(uuid):
        nonlocal count
        count += 1
        entered.set()
        assert release.wait(5)
        return True

    driver.backup_boot_ready = probe
    driver.backup_screenshot = Mock(return_value=(b"png", "image/png"))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(node.verify, target, point.id, "default", 1)
        assert entered.wait(5)
        second = executor.submit(node.verify, target, point.id, "default", 1)
        assert count == 1 and len(driver.list_vms()) == 2
        release.set()
        assert first.result()["verified"] == second.result()["verified"] == "passed"
    assert count == 2 and len(driver.list_vms()) == 1


def test_immutable_delete_prune_and_expiry(protection, tmp_path):
    driver, node, target, vm, point = protection
    clock = Clock(datetime.now(UTC))
    repo = Repository(DirStore(tmp_path / "locked"), CHUNK, clock, immutability_days=7)
    data = image(tmp_path, "raw", b"a" * CHUNK)
    first = repo.backup(make_vm(), [data], {})
    clock.now += timedelta(days=1)
    second = repo.backup(make_vm(), [data], {})
    assert first.lock_status == "soft"
    with pytest.raises(InvalidState, match="immutable"):
        repo.delete(first.id)
    assert repo.prune("web", 1) == []
    assert repo.gc() == 0
    # Turning the repository setting off does not unlock existing points.
    repo.immutability_days = 0
    with pytest.raises(InvalidState):
        repo.delete(first.id)
    clock.now += timedelta(days=7)
    assert repo.prune("web", 1) == [first.id]
    assert repo.list()[0].id == second.id


def test_s3_compliance_retains_specific_version_and_never_shortens():
    client = Mock()
    client.get_object_lock_configuration.return_value = {
        "ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}
    }
    later = datetime.now(UTC) + timedelta(days=30)
    client.head_object.return_value = {"VersionId": "v1", "ObjectLockRetainUntilDate": later}
    store = S3Store(client, "bucket", "prefix")
    assert store.protect("chunks/aa/test", datetime.now(UTC) + timedelta(days=7))
    client.put_object_retention.assert_called_once_with(
        Bucket="bucket",
        Key="prefix/chunks/aa/test",
        VersionId="v1",
        Retention={"Mode": "COMPLIANCE", "RetainUntilDate": later},
    )
    client.put_object_retention.side_effect = RuntimeError("Object Lock unavailable")
    assert not store.protect("backups/web/point.json", later)


def test_native_lock_covers_shared_chunks_and_manifest(protection, tmp_path, monkeypatch):
    retained = []
    monkeypatch.setattr(DirStore, "protect", lambda self, key, until: retained.append(key) or True)
    repo = Repository(DirStore(tmp_path / "native"), CHUNK, immutability_days=7)
    point = repo.backup(make_vm(), [image(tmp_path, "disk", b"a" * CHUNK)], {})
    assert point.lock_status == repo.list()[0].lock_status == "native"
    assert any(k.startswith("backups/") for k in retained)
    assert any(k.startswith("chunks/") for k in retained)


def test_anomaly_uses_vm_history_and_requires_both_thresholds(protection, tmp_path):
    clock = Clock(datetime.now(UTC))
    repo = Repository(DirStore(tmp_path / "anomaly"), CHUNK, clock)
    vm = make_vm()
    for _ in range(4):
        clock.now += timedelta(hours=1)
        point = repo.backup(vm, [image(tmp_path, "disk", b"a" * CHUNK * 10)], {})
        assert not point.suspicious
    # First run had one unique chunk; following stable runs had none.
    clock.now += timedelta(hours=1)
    data = b"".join(bytes([i + 10]) * CHUNK for i in range(10))
    point = repo.backup(vm, [image(tmp_path, "disk", data)], {})
    assert point.suspicious and point.anomaly_reason
    other = repo.backup(make_vm("other"), [image(tmp_path, "other", data)], {})
    assert not other.suspicious


@pytest.fixture
def manager(protection, tmp_path):
    driver, node, target, vm, point = protection
    cluster = Cluster()
    cluster.add_local("node1", driver, node)
    backups = BackupManager(cluster, state_path=tmp_path / "state.json", executor=Inline())
    backups.add_target(target)
    return TestClient(create_app(cluster, backups=backups)), backups, protection


def test_schedule_verification_api_and_backup_vm_lock(manager):
    client, backups, (driver, node, target, vm, point) = manager
    driver.backup_boot_ready = Mock(return_value=True)
    driver.backup_screenshot = Mock(return_value=(b"png", "image/png"))
    verify = node.verify

    def locked(*args):
        assert not backups.vm_busy(vm.uuid).acquire(blocking=False)
        return verify(*args)

    node.verify = locked
    body = {
        "name": "nightly",
        "target": target.name,
        "at": "02:00",
        "verify_after_backup": True,
        "verify_pool": "default",
        "verify_timeout": 1,
    }
    response = client.post("/api/v1/backup/policies", json=body)
    assert response.status_code == 201 and response.json()["verify_after_backup"]
    jobs = client.post("/api/v1/backup/policies/nightly/run").json()
    assert jobs[0]["state"] == "done"
    points = client.get("/api/v1/backup/targets/local/backups").json()
    verified = next(b for b in points if b["verified"] == "passed")
    evidence = client.get(
        f"/api/v1/backup/targets/local/backups/{verified['id']}/evidence?node=node1"
    )
    assert evidence.status_code == 200 and evidence.json()["mime"] == "image/png"
    assert any(j.kind == "verify" and j.state == "done" for j in backups.jobs())
    persisted = json.loads(backups._state_path.read_text())
    assert persisted["policies"][0]["verify_pool"] == "default"


def test_api_immutable_delete_is_blocked_for_admin(manager):
    client, backups, (driver, node, target, vm, point) = manager
    target.immutability_days = 7
    locked = node.backup(vm.uuid, target)
    response = client.delete(f"/api/v1/backup/targets/local/backups/{locked.id}?node=node1")
    assert response.status_code == 409
    assert any(b.id == locked.id for b in node.list(target))


def test_recovery_fallback_reports_actual_mode_and_never_writes_source(manager):
    client, backups, (driver, node, target, vm, point) = manager
    root = __import__("pathlib").Path(target.path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    response = client.post(
        f"/api/v1/backup/targets/local/backups/{point.id}/recover",
        json={"name": "recovered", "pool": "default", "source_node": "node1"},
    )
    assert response.status_code == 202
    job = response.json()
    assert job["state"] == "done" and job["kind"] == "recovery"
    assert job["report"]["instant"] is False
    assert job["report"]["elapsed_seconds"] >= 0
    assert job["report"]["finalize_required"] is False
    assert driver.get_vm(job["result"]).state == VMState.RUNNING
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after
    capability = client.get("/api/v1/backup/capabilities").json()
    assert capability["recovery"]["instant"] is False


def test_policy_requires_pool_and_bounds_timeout():
    with pytest.raises(ValueError, match="pool"):
        Policy("test", "local", at="02:00", verify_after_backup=True).validate()
    with pytest.raises(ValueError, match="timeout"):
        Policy("test", "local", at="02:00", verify_timeout=0).validate()


def test_console_ppm_is_converted_to_browser_viewable_png():
    import struct
    import zlib

    from ibhics.backup.console import console_png

    # Preserve whitespace-valued first pixels instead of stripping the binary raster.
    pixels = b"\n\x20\x09\xff\x00\x01"
    png = console_png(b"P6\n# libvirt console\n2 1\n255\n" + pixels, "image/x-portable-pixmap")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack("!2I", png[16:24]) == (2, 1)
    offset = png.index(b"IDAT")
    length = struct.unpack("!I", png[offset - 4 : offset])[0]
    assert zlib.decompress(png[offset + 4 : offset + 4 + length]) == b"\0" + pixels
    with pytest.raises(ValueError, match="length"):
        console_png(b"P6\n2 1\n255\nshort", "image/x-portable-pixmap")
    with pytest.raises(ValueError, match="dimensions"):
        console_png(b"P6\n9000 9000\n255\n", "image/x-portable-pixmap")


def test_remote_agent_preserves_protection_and_evidence(protection):
    from test_cluster import Agents

    from ibhics.cluster import NodeRole
    from ibhics.cluster.remote import RemoteBackup

    driver, node, target, vm, point = protection
    target.immutability_days = 7
    driver.backup_boot_ready = Mock(return_value=True)
    driver.backup_screenshot = Mock(
        return_value=(b"P6\n1 1\n255\n\x00\x01\xff", "image/x-portable-pixmap")
    )
    agents = Agents()
    agents.add("http://fixture-node:8007", "fixture-node", NodeRole.COMPUTE, driver, node)
    remote = RemoteBackup(agents.client("http://fixture-node:8007", "s3cret"))
    locked = remote.backup(vm.uuid, target)
    assert locked.immutable_until and locked.lock_status == "soft"
    assert remote.verify(target, locked.id, "default", timeout=1)["verified"] == "passed"
    assert next(p for p in remote.list(target) if p.id == locked.id).verified == "passed"
    assert remote.evidence(target, locked.id)["mime"] == "image/png"
    assert remote.usage(target)["free_bytes"] > 0
    with pytest.raises(InvalidState, match="immutable"):
        remote.delete(target, locked.id)


def test_anomaly_does_not_flag_small_change_or_short_history(protection, tmp_path):
    repo = Repository(DirStore(tmp_path / "thresholds"), CHUNK)
    vm = make_vm()
    path = image(tmp_path, "disk", b"a" * CHUNK + b"\0" * CHUNK * 9)
    for _ in range(4):
        assert not repo.backup(vm, [path], {}).suspicious
    path.write_bytes(b"b" * CHUNK + b"\0" * CHUNK * 9)
    assert not repo.backup(vm, [path], {}).suspicious  # 10% is below the absolute threshold
    fresh = make_vm("new-vm")
    path.write_bytes(b"".join(bytes([n]) * CHUNK for n in range(1, 11)))
    assert not repo.backup(fresh, [path], {}).suspicious  # no historical baseline


def test_copy_uses_destination_retention_and_does_not_inherit_boot_evidence(protection, tmp_path):
    driver, node, target, vm, point = protection
    driver.backup_boot_ready = Mock(return_value=True)
    driver.backup_screenshot = Mock(return_value=(b"png", "image/png"))
    node.verify(target, point.id, "default", timeout=1)
    destination = BackupTarget(
        "offsite", TargetType.LOCAL, path=str(tmp_path / "offsite"), immutability_days=14
    )
    copied = node.copy(target, destination, point.id)
    assert copied.immutable_until and copied.verified == "skipped"
    with pytest.raises(InvalidState, match="immutable"):
        node.delete(destination, copied.id)


def test_verification_evidence_does_not_modify_locked_manifest(protection):
    from pathlib import Path

    driver, node, target, vm, point = protection
    target.immutability_days = 7
    point = node.backup(vm.uuid, target)
    store = DirStore(Path(target.path))
    manifest_keys = store.list("backups/")
    before = {key: store.get(key) for key in manifest_keys + store.list("chunks/")}
    driver.backup_boot_ready = Mock(return_value=True)
    driver.backup_screenshot = Mock(return_value=(b"png", "image/png"))
    assert node.verify(target, point.id, "default", timeout=1)["verified"] == "passed"
    assert all(store.get(key) == data for key, data in before.items())


def test_verify_skips_before_cloning_when_memory_is_exhausted(protection):
    driver, node, target, vm, point = protection
    host = driver.host_info()
    host.memory_mb = vm.spec.memory_mb
    driver.host_info = Mock(return_value=host)
    driver.create_vm = Mock(side_effect=AssertionError("Must not create a VM"))
    result = node.verify(target, point.id, "default", timeout=1)
    assert result["verified"] == "skipped" and "memory" in result["reason"]
    driver.create_vm.assert_not_called()


def test_capacity_monitor_does_not_attach_on_demand_target(protection):
    driver, node, target, vm, point = protection
    target = BackupTarget("nas", TargetType.NFS, host="fixture-nas", export="/backup")
    node._open = Mock(side_effect=AssertionError("Capacity monitor must not mount NAS/SAN/USB"))
    assert node.usage(target)["free_bytes"] is None
    node._open.assert_not_called()


def test_skipped_verification_does_not_turn_successful_backup_into_failure(manager):
    client, backups, (driver, node, target, vm, point) = manager
    host = driver.host_info()
    host.memory_mb = vm.spec.memory_mb
    driver.host_info = Mock(return_value=host)
    backups.add_policy(
        Policy("verify", target.name, at="02:00", verify_after_backup=True, verify_pool="default")
    )
    backup = backups.run_policy("verify")[0]
    assert backup.state == "done"
    verification = next(j for j in backups.jobs() if j.kind == "verify")
    assert verification.state == "skipped"
    assert backup.report["verified"] == "skipped"
    assert "memory" in verification.error
