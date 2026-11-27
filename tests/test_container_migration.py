"""A shared-storage handover preserves identity and fences uncertain completion."""

import pytest
from test_container_ownership import nodes

from ibhics.hypervisor import InvalidState
from ibhics.hypervisor import container_migration as migration
from ibhics.hypervisor.containers import ContainerNotFound, ContainerSpec, LxcContainers


def prepare(tmp_path, monkeypatch, running=True):
    first, second, states, calls = nodes(tmp_path, monkeypatch)
    first.create(ContainerSpec("app", pool="pool"))
    first.action("app", "start" if running else "stop")
    # Test runner uses one known include; don't depend on the developer Mac having LXC.
    home = tmp_path / "pool/containers/app"
    (home / "config").write_text("")
    monkeypatch.setattr(migration.shutil, "which", lambda _: None)
    return first, second, states, home


def test_handover_keeps_data_mac_and_only_one_owner(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    before = first.get("app")
    (home / "snaps/proof").mkdir(parents=True)
    original = (home / "rootfs.img").read_bytes()
    receipt = first.migration("prepare", "app", {"node": "node2", "allow_stop": True})
    assert first.get("app").migration_pending
    with pytest.raises(InvalidState, match="fenced"):
        first.action("app", "start")
    assert second.migration("probe", "app", receipt)["ready"]
    first.migration("commit", "app", receipt)
    assert first.list() == [] and len(second.list()) == 1
    with pytest.raises(InvalidState, match="Ownership changed"):
        first.migration("abort", "app", receipt)
    result = second.migration("finish", "app", receipt)
    assert result["state"] == "running" and result["mac"] == before.spec.mac
    assert not result["migration_pending"] and (home / "rootfs.img").read_bytes() == original
    assert (home / "snaps/proof").is_dir()
    assert states["node1"]["app"] == "STOPPED" and states["node2"]["app"] == "RUNNING"


def test_private_storage_cannot_pass_shared_receipt_and_abort_restarts_source(
    tmp_path, monkeypatch
):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    receipt = first.migration("prepare", "app", {"node": "node2", "allow_stop": True})
    other = LxcContainers(
        lambda p: str(tmp_path / "different" / p),
        lambda n: "br0",
        first._run,
        lambda: ["pool"],
        node_name="node2",
    )
    with pytest.raises(InvalidState, match="Shared storage"):
        other.migration("probe", "app", receipt)
    first.migration("abort", "app", receipt)
    assert first.get("app").state == "running" and second.list() == []


def test_stopped_guest_stays_stopped_and_interrupted_prepare_can_recover(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch, running=False)
    receipt = first.migration("prepare", "app", {"node": "node2"})
    first.migration("recover", "app", {})
    assert first.get("app").state == "stopped" and not (home / migration.MARKER).exists()
    receipt = first.migration("prepare", "app", {"node": "node2"})
    second.migration("probe", "app", receipt)
    first.migration("commit", "app", receipt)
    assert second.migration("recover", "app", {})["state"] == "stopped"


def test_incomplete_owner_commit_can_recover_safely_on_source(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    receipt = first.migration("prepare", "app", {"node": "node2", "allow_stop": True})
    second.migration("probe", "app", receipt)
    original = migration.atomic

    def fail_owner(path, data):
        if path.name == ".ibhics-owner.json":
            raise OSError("write interrupted")
        original(path, data)

    monkeypatch.setattr(migration, "atomic", fail_owner)
    with pytest.raises(OSError):
        first.migration("commit", "app", receipt)
    assert len(first.list()) == 1 and second.list() == []
    monkeypatch.setattr(migration, "atomic", original)
    first.migration("commit", "app", receipt)
    assert second.migration("recover", "app", {})["state"] == "running"


def test_running_guest_requires_explicit_stop_permission(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    with pytest.raises(InvalidState, match="temporary stop"):
        first.migration("prepare", "app", {"node": "node2"})
    assert first.get("app").state == "running"
    assert not (home / migration.MARKER).exists()


@pytest.mark.parametrize("live_available", [False, True])
def test_manager_migration_route_and_recovery_keep_inventory_single(
    tmp_path, monkeypatch, live_available
):
    from fastapi.testclient import TestClient

    from ibhics.cluster import Cluster
    from ibhics.hypervisor.fake import FakeDriver
    from ibhics.manager import create_app

    first, second, states, home = prepare(tmp_path, monkeypatch)
    if live_available:
        checkpoint_lab(monkeypatch, states)
        states["node2"]["_restore_here"] = True
    cluster = Cluster()
    for name, backend in [("node1", first), ("node2", second)]:
        driver = FakeDriver()
        monkeypatch.setattr(driver, "containers", lambda backend=backend: backend)
        cluster.add_local(name, driver)
    client = TestClient(create_app(cluster))
    assert client.post(
        "/api/v1/containers/app/migrate", json={"node": "node2", "mode": "offline"}
    ).status_code == 409
    result = client.post(
        "/api/v1/containers/app/migrate", json={"node": "node2"}
    )
    assert result.status_code == 200, result.text
    assert result.json()["node"] == "node2"
    expected = ("Live (memory kept)" if live_available else
                "Restarted on destination: criu is not installed on node1")
    assert result.json()["result"] == expected
    job = client.get("/api/v1/backup/jobs").json()[-1]
    assert job["kind"] == "ct-migrate" and job["state"] == "done"
    assert job["result"] == result.json()["result"]
    inventory = client.get("/api/v1/containers").json()
    assert len(inventory) == 1 and inventory[0]["node"] == "node2"
    assert not inventory[0]["migration_pending"]
    assert (
        client.post(
            "/api/v1/containers/app/migrate", json={"node": "node2", "allow_stop": True}
        ).status_code
        == 409
    )


def checkpoint_lab(monkeypatch, states, dump_error=False, restore_error=False,
                   partial_restore=False):
    """Exercise the subprocess wrapper without executing LXC or CRIU."""
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(migration.shutil, "which", lambda _: "/usr/sbin/criu")

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 120
        assert kwargs["stdin"] == migration.subprocess.DEVNULL
        assert kwargs["stdout"] == migration.subprocess.DEVNULL
        assert not isinstance(kwargs["stderr"], int)  # a file, never PIPE
        restore = "-r" in argv
        node = "node2" if states["node2"].get("_restore_here") else "node1"
        directory = migration.Path(argv[argv.index("-D") + 1])
        assert directory.stat().st_mode & 0o777 == 0o700
        if not restore:
            node = "node1"
        failed = restore_error if restore else dump_error
        if failed:
            (directory / ("restore.log" if restore else "dump.log")).write_text(
                "Info: start\nError: cgroup namespace unsupported\n"
            )
            if restore and partial_restore:
                states[node]["app"] = "FROZEN"
        else:
            states[node]["app"] = "RUNNING" if restore else "STOPPED"
        return SimpleNamespace(returncode=1 if failed else 0)

    monkeypatch.setattr(migration.subprocess, "run", run)
    return calls


def complete(first, second, receipt):
    second.migration("probe", "app", receipt)
    first.migration("commit", "app", receipt)
    return second.migration("finish", "app", receipt)


def test_live_keeps_memory_and_fence_until_running(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    calls = checkpoint_lab(monkeypatch, states)
    receipt = first.migration("prepare", "app", {"node": "node2", "mode": "live"})
    assert receipt["checkpoint"] and receipt["live"]
    assert states["node1"]["app"] == "STOPPED"
    with pytest.raises(InvalidState, match="fenced"):
        first.action("app", "start")
    states["node2"]["_restore_here"] = True
    result = complete(first, second, receipt)
    assert result["method"] == "live" and result["downtime_seconds"] >= 0
    assert len(calls) == 2 and "-s" in calls[0] and "-r" in calls[1]
    assert not (home / migration.CHECKPOINT).exists()
    with pytest.raises(ContainerNotFound):
        first.action("app", "start")


def test_dump_failure_stops_source_and_restarts_destination(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    calls = checkpoint_lab(monkeypatch, states, dump_error=True)
    receipt = first.migration("prepare", "app", {"node": "node2", "mode": "live"})
    assert not receipt["checkpoint"] and "cgroup" in receipt["live_error"]
    assert states["node1"]["app"] == "STOPPED"
    result = complete(first, second, receipt)
    assert result["method"] == "restart" and "cgroup" in result["live_error"]
    assert len(calls) == 1 and not (home / migration.CHECKPOINT).exists()


def test_restore_failure_kills_partial_guest_before_cold_start(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    checkpoint_lab(monkeypatch, states, restore_error=True, partial_restore=True)
    receipt = first.migration("prepare", "app", {"node": "node2", "mode": "live"})
    states["node2"]["_restore_here"] = True
    original = second._lxc
    calls = []

    def lxc(*args, **kwargs):
        assert (home / migration.MARKER).exists()
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(second, "_lxc", lxc)
    result = complete(first, second, receipt)
    assert result["method"] == "restart" and "cgroup" in result["live_error"]
    assert [c[0] for c in calls if c[0] != "info"] == ["stop", "start"]
    assert "-k" in next(c for c in calls if c[0] == "stop")
    assert not (home / migration.CHECKPOINT).exists()


@pytest.mark.parametrize("missing_on", ["source", "destination"])
def test_live_without_criu_falls_back(tmp_path, monkeypatch, missing_on):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    if missing_on == "destination":
        checkpoint_lab(monkeypatch, states)
    receipt = first.migration("prepare", "app", {"node": "node2", "mode": "live"})
    monkeypatch.setattr(migration.shutil, "which", lambda _: None)
    result = complete(first, second, receipt)
    assert result["method"] == "restart"
    node = "node1" if missing_on == "source" else "node2"
    assert result["live_error"] == f"criu is not installed on {node}"
    assert not (home / migration.CHECKPOINT).exists()


@pytest.mark.parametrize("restore_error", [False, True])
def test_abort_restores_or_cold_starts_source(tmp_path, monkeypatch, restore_error):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    checkpoint_lab(monkeypatch, states, restore_error=restore_error)
    first.migration("prepare", "app", {"node": "node2", "mode": "live"})
    result = first.migration("recover", "app", {})
    assert result["method"] == ("restart" if restore_error else "live")
    assert states["node1"]["app"] == "RUNNING" and not second.list()
    assert not (home / migration.CHECKPOINT).exists()


def test_checkpoint_timeout_reports_stderr_and_falls_back(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(migration.shutil, "which", lambda _: "/usr/sbin/criu")

    def run(argv, **kwargs):
        kwargs["stderr"].write(b"checkpoint timed out")
        raise migration.subprocess.TimeoutExpired(argv, 120)

    monkeypatch.setattr(migration.subprocess, "run", run)
    receipt = first.migration("prepare", "app", {"node": "node2", "mode": "live"})
    result = complete(first, second, receipt)
    assert result["method"] == "restart" and "timed out" in result["live_error"]


def test_lxc_config_disables_console_and_ttys():
    from ibhics.hypervisor.containers import lxc_config

    config = lxc_config(ContainerSpec("app"), "br0")
    assert "lxc.tty.max = 0\n" in config
    assert "lxc.console.path = none\n" in config


def test_failed_destination_start_remains_fenced_and_can_recover(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    checkpoint_lab(monkeypatch, states, restore_error=True)
    receipt = first.migration("prepare", "app", {"node": "node2", "mode": "live"})
    second.migration("probe", "app", receipt)
    first.migration("commit", "app", receipt)
    original = second._lxc

    def fail_start(*args, **kwargs):
        if args[0] == "start":
            raise InvalidState("start failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(second, "_lxc", fail_start)
    with pytest.raises(InvalidState, match="start failed"):
        second.migration("finish", "app", receipt)
    assert (home / migration.MARKER).exists()
    with pytest.raises(InvalidState, match="fenced"):
        second.action("app", "start")
    with pytest.raises(InvalidState, match="Ownership changed"):
        first.migration("abort", "app", receipt)
    assert states["node1"]["app"] == "STOPPED"
    monkeypatch.setattr(second, "_lxc", original)
    result = second.migration("recover", "app", {})
    assert result["method"] == "restart" and result["state"] == "running"
    assert not (home / migration.MARKER).exists()
    assert not (home / migration.CHECKPOINT).exists()


def test_explicit_offline_never_checkpoints_even_with_criu(tmp_path, monkeypatch):
    first, second, states, home = prepare(tmp_path, monkeypatch)
    calls = checkpoint_lab(monkeypatch, states)
    receipt = first.migration(
        "prepare", "app", {"node": "node2", "mode": "offline", "allow_stop": True}
    )
    result = complete(first, second, receipt)
    assert result["method"] == "restart" and not calls and not receipt["live"]
