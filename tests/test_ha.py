"""HA restart: a protected VM on a failed node starts on another node, safely."""

import pytest
from test_migration import Env

from ibhics.hypervisor import InvalidState
from ibhics.manager.ha import HighAvailability


@pytest.fixture
def env(tmp_path):
    env = Env(tmp_path)
    env.add_shared_pool()
    env.ha = HighAvailability(env.cluster, state_path=tmp_path / "ha.json", grace_s=90)
    return env


def beat(env, seconds=0):
    env.clock.now += seconds
    env.cluster.refresh()
    env.ha.tick()


def fail_node2(env):
    env.agents.down.add("http://node2:8007")
    beat(env, 20)  # past the heartbeat timeout, not yet past the grace period
    beat(env, 90)


def test_restart_on_another_node_after_grace_and_cleanup_on_return(env):
    uuid = env.create_vm("db", pool="shared", node="node2")
    env.ha.set_enabled(uuid, True)
    beat(env)
    assert env.ha.status()["vms"][0]["ready"]
    env.agents.down.add("http://node2:8007")
    beat(env, 20)
    assert env.node1.list_vms() == []  # within the grace period nothing moves
    beat(env, 90)
    [vm] = env.node1.list_vms()
    assert vm.uuid == uuid and vm.state == "running"
    assert env.ha.status()["moved"][uuid]["from"] == "node2"
    # node2 comes back with its stale copy: it is removed there, disks untouched on node1
    env.agents.down.discard("http://node2:8007")
    beat(env, 5)
    assert [v.uuid for v in env.node2.list_vms()] == []
    assert [v.uuid for v in env.node1.list_vms()] == [uuid]
    assert env.ha.status()["moved"] == {}


def test_disk_lock_undoes_the_restart_and_retries(env, monkeypatch):
    uuid = env.create_vm("db", pool="shared", node="node2")
    env.ha.set_enabled(uuid, True)
    beat(env)

    def locked(_uuid):
        raise InvalidState('Failed to get "write" lock')

    monkeypatch.setattr(env.node1, "start_vm", locked)
    fail_node2(env)
    assert env.node1.list_vms() == []  # defined, refused, forgotten again
    event = env.ha.status()["events"][-1]
    assert not event["ok"] and "still in use by node2" in event["error"]
    monkeypatch.undo()
    beat(env, 10)
    assert env.node1.list_vms() == []  # waits RETRY_S before trying again
    beat(env, 30)
    assert [v.state for v in env.node1.list_vms()] == ["running"]


def test_stopped_or_unprotected_vms_stay_put(env):
    protected_off = env.create_vm("off", pool="shared", node="node2", start=False)
    env.create_vm("plain", pool="shared", node="node2")
    env.ha.set_enabled(protected_off, True)
    beat(env)
    fail_node2(env)
    assert env.node1.list_vms() == []


def test_no_restart_without_quorum(env):
    uuid = env.create_vm("db", pool="shared", node="node2")
    env.ha.set_enabled(uuid, True)
    beat(env)
    env.agents.down.update({"http://node2:8007", "http://witness:8007"})
    beat(env, 20)
    beat(env, 200)
    assert env.node1.list_vms() == []


def test_local_disks_cannot_be_protected(env):
    uuid = env.create_vm("local", node="node2")
    with pytest.raises(InvalidState, match="not on storage shared"):
        env.ha.set_enabled(uuid, True)


def test_settings_survive_a_manager_restart(env, tmp_path):
    uuid = env.create_vm("db", pool="shared", node="node2")
    env.ha.set_enabled(uuid, True)
    beat(env)
    again = HighAvailability(env.cluster, state_path=tmp_path / "ha.json")
    assert again.enabled == {uuid} and again.records[uuid]["definition"]
