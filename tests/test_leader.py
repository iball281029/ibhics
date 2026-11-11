"""Active/standby managers through a lock and state copies on shared storage."""

import json
import sqlite3
import subprocess
import sys
import textwrap

from fastapi.testclient import TestClient

from ibhics.cluster import Cluster
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager.leader import Leadership, Replicator, replicate, restore, standby_app


def contender(shared, node="node2"):
    """Try the lock from another process (POSIX locks are per process)."""
    code = textwrap.dedent(f"""
        from ibhics.manager.leader import Leadership
        print(Leadership({str(shared)!r}, {node!r}, "10.0.0.2").try_acquire())
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    return out.stdout.strip()


def test_only_one_manager_holds_the_lock(tmp_path):
    a = Leadership(tmp_path, "node1", "10.0.0.1")
    assert a.try_acquire()
    assert contender(tmp_path) == "False"
    assert a.current()["node"] == "node1"
    import os

    os.close(a.fd)  # the active manager is gone
    assert contender(tmp_path) == "True"


def test_state_is_copied_out_and_restored_on_takeover(tmp_path):
    local, shared, other = tmp_path / "n1", tmp_path / "shared", tmp_path / "n2"
    local.mkdir()
    (local / "users.json").write_text('{"users": [1]}')
    db = sqlite3.connect(local / "activity.sqlite3")
    db.execute("create table t (x)")
    db.execute("insert into t values (42)")
    db.commit()
    files = [local / "users.json", local / "activity.sqlite3", local / "missing.json"]
    seen = {}
    assert replicate(files, shared, seen) == 2
    assert replicate(files, shared, seen) == 0  # unchanged files are not copied again
    other.mkdir()
    (other / "users.json").write_text('{"users": ["old"]}')
    restored = restore([other / "users.json", other / "activity.sqlite3"], shared, other / "bk")
    assert sorted(restored) == ["activity.sqlite3", "users.json"]
    assert json.loads((other / "users.json").read_text()) == {"users": [1]}
    assert sqlite3.connect(other / "activity.sqlite3").execute("select x from t").fetchone() == (
        42,
    )
    [kept] = list((other / "bk").iterdir())
    assert json.loads((kept / "users.json").read_text()) == {"users": ["old"]}


def test_active_manager_steps_down_when_replaced(tmp_path):
    a = Leadership(tmp_path, "node1", "10.0.0.1")
    a.try_acquire()
    stopped = []
    r = Replicator(a, [], on_replaced=lambda: stopped.append(True))
    r.step()
    assert stopped == []
    (tmp_path / "leader.json").write_text(json.dumps({"node": "node2", "address": "10.0.0.2"}))
    r.step()
    assert stopped == [True]


def test_standby_page_points_to_the_active_manager(tmp_path):
    a = Leadership(tmp_path, "node1", "10.0.0.1")
    a.try_acquire()
    standby = Leadership(tmp_path, "node2", "10.0.0.2")
    client = TestClient(standby_app(standby))
    health = client.get("/api/v1/health")
    assert health.status_code == 503 and health.json()["active_url"] == "https://10.0.0.1:8888"
    assert "https://10.0.0.1:8888" in client.get("/").text


def test_cluster_state_includes_this_node_for_a_standby(tmp_path):
    c = Cluster(token="tok1", state_path=tmp_path / "cluster.json")
    c.add_local("node1", FakeDriver(demo_console=False))
    c.local_agent = "http://10.0.0.1:8007"
    c.create_cluster("lab")
    saved = json.loads((tmp_path / "cluster.json").read_text())
    assert {
        "name": "node1",
        "role": "compute",
        "address": "http://10.0.0.1:8007",
        "agent_token": "tok1",
    } in saved["members"]
    # node2 taking over: node1 comes back as a remote member, node2 stays local
    d = Cluster(
        token="tok2",
        state_path=tmp_path / "cluster.json",
        client_factory=lambda a, t: type("C", (), {"address": a, "token": t})(),
    )
    d.add_local("node2", FakeDriver(demo_console=False))
    d.load()
    names = {m.name: m.local for m in d.members()}
    assert names == {"node2": True, "node1": False}
