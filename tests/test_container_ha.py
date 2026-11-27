"""Container HA gates, agent fencing and detached local POSIX lock lifecycle."""

import fcntl
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from test_container_ownership import nodes

from ibhics.hypervisor import InvalidState, ct_runlock
from ibhics.hypervisor.containers import ContainerInfo, ContainerSpec, lxc_config
from ibhics.manager import container_ha
from ibhics.manager.container_ha import ContainerHA
from ibhics.manager.permissions import category


def ha_env(tmp_path, monkeypatch):
    now = [0.0]
    ct = ContainerInfo(ContainerSpec("app", memory_mb=512, pool="shared"), state="running")
    ct.owner_node = "old"
    calls = []
    members = []
    for name, memory in [("old", 1000), ("small", 128), ("new", 2048), ("maint", 8192)]:
        backend = SimpleNamespace(list=lambda: [], ha_takeover=None)
        member = SimpleNamespace(
            name=name,
            role="compute",
            local=False,
            last_seen=0,
            maintenance=name == "maint",
            memory=memory,
            up=True,
            driver=SimpleNamespace(containers=lambda b=backend: b),
        )
        members.append(member)
    old, small, new, maint = members
    old.driver.containers().list = lambda: [ct] if ct.owner_node == "old" else []

    def takeover(name, source):
        calls.append((name, source))
        ct.owner_node = "new"
        new.driver.containers().list = lambda: [ct]
        return ct

    new.driver.containers().ha_takeover = takeover
    cluster = SimpleNamespace(
        _clock=lambda: now[0],
        has_quorum=lambda: True,
        is_healthy=lambda m: m.up,
        compute_members=lambda healthy_only=True: [m for m in members if m.up or not healthy_only],
    )
    monkeypatch.setattr(container_ha, "_free", lambda m: m.memory)
    ha = ContainerHA(cluster, tmp_path / "container-ha.json")
    ha.enabled.add("app")
    ha.tick()
    return SimpleNamespace(
        ha=ha,
        old=old,
        new=new,
        small=small,
        maint=maint,
        cluster=cluster,
        now=now,
        calls=calls,
        ct=ct,
    )


def test_container_ha_grace_memory_maintenance_and_persistence(tmp_path, monkeypatch):
    e = ha_env(tmp_path, monkeypatch)
    e.old.up = False
    e.now[0] = 89
    e.ha.tick()
    assert e.calls == []
    e.now[0] = 91
    e.ha.tick()
    assert e.calls == [("app", "old")]
    assert e.ha.records["app"]["node"] == "new"
    assert e.ha.events[-1]["ok"]
    restored = ContainerHA(e.cluster, tmp_path / "container-ha.json")
    assert restored.enabled == {"app"}
    assert restored.records["app"]["node"] == "new"
    e.ha.tick()
    assert len(e.calls) == 1


def test_container_ha_no_quorum_or_not_enabled(tmp_path, monkeypatch):
    e = ha_env(tmp_path, monkeypatch)
    e.old.up = False
    e.now[0] = 200
    e.cluster.has_quorum = lambda: False
    e.ha.tick()
    assert e.calls == []
    e.cluster.has_quorum = lambda: True
    e.ha.enabled.clear()
    e.ha.tick()
    assert e.calls == []


def test_container_ha_locked_retry_and_uncertain_destination(tmp_path, monkeypatch):
    e = ha_env(tmp_path, monkeypatch)
    e.old.up = False
    e.now[0] = 100
    attempts = []

    def locked(*args):
        attempts.append(args)
        raise InvalidState("still locked by old")

    original = e.new.driver.containers().ha_takeover
    e.new.driver.containers().ha_takeover = locked
    e.ha.tick()
    assert len(attempts) == 1 and e.calls == []
    assert e.ha.records["app"]["node"] == "old"
    assert "still locked" in e.ha.events[-1]["error"]
    e.now[0] = 129
    e.ha.tick()
    assert len(attempts) == 1
    # A stale stopped listing after an uncertain RPC cannot erase restart intent.
    stopped = ContainerInfo(e.ct.spec, state="stopped", owner_node="new")
    e.new.driver.containers().list = lambda: [stopped]
    e.new.driver.containers().ha_takeover = original
    e.now[0] = 131
    e.ha.tick()
    assert e.calls == [("app", "old")]


def test_container_ha_no_memory_does_not_takeover(tmp_path, monkeypatch):
    e = ha_env(tmp_path, monkeypatch)
    e.old.up = False
    e.new.memory = 100
    e.now[0] = 100
    e.ha.tick()
    assert not e.calls and "memory" in e.ha.events[-1]["error"]


def test_agent_takeover_marker_lock_and_owner(tmp_path, monkeypatch):
    first, second, states, calls = nodes(tmp_path, monkeypatch)
    first.create(ContainerSpec("app", pool="pool"))
    home = tmp_path / "pool/containers/app"
    path_of = second._pool_path
    monkeypatch.setattr(second, "_pools", lambda: ["block", "pool"])
    monkeypatch.setattr(second, "_pool_path", lambda p: None if p == "block" else path_of(p))
    monkeypatch.setattr(second, "_shared_home", lambda h: True)
    acquired = []
    monkeypatch.setattr(second, "_runlock_acquire", lambda h: acquired.append(h))
    marker = home / ".ibhics-migration.json"
    marker.write_text("{}")
    with pytest.raises(InvalidState, match="migration marker"):
        second.ha_takeover("app", "node1")
    assert not acquired
    marker.unlink()

    def locked(h):
        raise RuntimeError("locked")

    monkeypatch.setattr(second, "_runlock_acquire", locked)
    before = len(calls)
    with pytest.raises(InvalidState, match="still locked by node1"):
        second.ha_takeover("app", "node1")
    assert len(calls) == before
    assert first._owner(home) == "node1"
    monkeypatch.setattr(second, "_runlock_acquire", lambda h: acquired.append(h))
    result = second.ha_takeover("app", "node1")
    assert acquired == [home]
    assert result.state == "running" and result.owner_node == "node2"
    assert first.list() == []
    assert json.loads((home / "ibhics.json").read_text())["owner_node"] == "node2"


@pytest.mark.parametrize("reason", ["owner", "locked", "relock", "alive"])
def test_self_fence_uses_local_state_even_when_owner_hidden(tmp_path, monkeypatch, reason):
    first, second, states, calls = nodes(tmp_path, monkeypatch)
    first.create(ContainerSpec("app", pool="pool"))
    home = tmp_path / "pool/containers/app"
    if reason == "owner":
        from ibhics.hypervisor.container_migration import atomic

        data = json.loads((home / "ibhics.json").read_text())
        data["owner_node"] = "node2"
        atomic(home / "ibhics.json", data)
        atomic(home / ".ibhics-owner.json", {"node": "node2"})
    original = first._run

    def run(argv):
        if argv[0] == "lxc-info" and "-sH" in argv:
            return 0, "RUNNING\n"
        return original(argv)

    def acquire(h):
        if reason == "locked":
            raise RuntimeError("run lock unavailable")
        relocked.append(h)

    relocked = []
    monkeypatch.setattr(first, "_run", run)
    monkeypatch.setattr(ct_runlock, "alive", lambda h: reason == "alive")
    monkeypatch.setattr(first, "_runlock_acquire", acquire)
    first.self_fence()
    killed = any(cmd[0] == "lxc-stop" and "-k" in cmd for node, cmd in calls)
    if reason in ("owner", "locked"):
        assert killed and states["node1"]["app"] == "STOPPED"
    else:
        # A stale heartbeat or a pre-HA container is re-locked, never killed.
        assert not killed
        assert relocked == ([home] if reason == "relock" else [])


def test_runlock_hooks_and_permission_category():
    config = lxc_config(ContainerSpec("app"), "br0", "/shared/containers/app/rootfs.img")
    assert "lxc.hook.pre-start = " in config
    assert "ibhics.hypervisor.ct_runlock acquire /shared/containers/app" in config
    assert "ibhics.hypervisor.ct_runlock release /shared/containers/app" in config
    assert category("/api/v1/containers/app/ha") == category("/api/v1/vms/id/ha")


def test_runlock_detached_acquire_exclusive_reuse_release(tmp_path):
    home = tmp_path / "app"
    home.mkdir()
    runtime = tmp_path / "runtime"
    other = tmp_path / "other-runtime"
    try:
        ct_runlock.acquire(home, runtime)
        data = json.loads((runtime / "app.pid").read_text())
        assert data["pid"] != os.getpid()
        assert ct_runlock.healthy(home, runtime)
        ct_runlock.acquire(home, runtime)
        assert json.loads((runtime / "app.pid").read_text())["pid"] == data["pid"]
        with (home / ".ibhics-run.lock").open("r+b") as stream:
            with pytest.raises(OSError):
                fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="still running on another node"):
            ct_runlock.acquire(home, other)
        ct_runlock.release(home, runtime)
        assert not ct_runlock.healthy(home, runtime)
        ct_runlock.acquire(home, other)
        assert ct_runlock.healthy(home, other)
        assert (home / ".ibhics-run.lock").stat().st_size == 1
    finally:
        ct_runlock.release(home, runtime)
        ct_runlock.release(home, other)


def test_runlock_survives_caller_exit_and_release_by_new_process(tmp_path):
    home = tmp_path / "app"
    home.mkdir()
    runtime = tmp_path / "runtime"
    script = (
        "from ibhics.hypervisor.ct_runlock import acquire; "
        f"acquire({str(home)!r}, {str(runtime)!r})"
    )
    try:
        subprocess.run([sys.executable, "-c", script], check=True, timeout=10)
        assert ct_runlock.healthy(home, runtime)
    finally:
        ct_runlock.release(home, runtime)


def test_runlock_acquire_timeout_is_bounded(tmp_path, monkeypatch):
    home = tmp_path / "app"
    home.mkdir()
    popen = subprocess.Popen

    def stalled(*args, **kwargs):
        return popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(ct_runlock.subprocess, "Popen", stalled)
    start = time.monotonic()
    with pytest.raises(RuntimeError):
        ct_runlock.acquire(home, tmp_path / "runtime", timeout=0.1)
    assert time.monotonic() - start < 3
    assert not (tmp_path / "runtime/app.request").exists()


def test_live_migration_lock_order_dump_release_acquire_restore(tmp_path, monkeypatch):
    from test_container_migration import checkpoint_lab, complete, prepare

    from ibhics.hypervisor import container_migration as migration

    first, second, states, home = prepare(tmp_path, monkeypatch)
    checkpoint_lab(monkeypatch, states)
    order = []
    checkpoint = migration.checkpoint

    def record_checkpoint(backend, base, name, restore=False):
        order.append("restore" if restore else "dump")
        return checkpoint(backend, base, name, restore)

    monkeypatch.setattr(migration, "checkpoint", record_checkpoint)
    monkeypatch.setattr(first, "_runlock_release", lambda h: order.append("release"))
    monkeypatch.setattr(second, "_runlock_acquire", lambda h: order.append("acquire"))
    receipt = first.migration("prepare", "app", {"node": "node2", "mode": "live"})
    states["node2"]["_restore_here"] = True
    result = complete(first, second, receipt)
    assert result["method"] == "live"
    assert order == ["dump", "release", "acquire", "restore"]


def test_manager_api_shared_only_list_status_disable_and_persistence(tmp_path):
    from test_migration import Env

    e = Env(tmp_path)
    e.add_shared_pool()
    e.node1.containers().create(ContainerSpec("app", pool="shared"))
    e.node1.containers().create(ContainerSpec("local", pool="default"))
    resp = e.client.put("/api/v1/containers/local/ha", json={"enabled": True})
    assert resp.status_code == 409
    resp = e.client.put("/api/v1/containers/app/ha", json={"enabled": True})
    assert resp.status_code == 200 and resp.json()["enabled"]
    result = e.client.get("/api/v1/containers/app").json()
    assert result["ha_enabled"]
    status = e.client.get("/api/v1/containers/ha/status").json()
    assert status["containers"][0]["name"] == "app"
    assert e.client.put("/api/v1/containers/app/ha", json={"enabled": False}).status_code == 200
    assert not e.client.get("/api/v1/containers/app").json()["ha_enabled"]
