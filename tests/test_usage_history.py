import math
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ibhics.cluster import Cluster
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.usage import RETENTION, UsageHistory


def test_history_survives_browser_and_store_recreation(tmp_path):
    now = 1800000000
    path = tmp_path / "usage.sqlite3"
    first = UsageHistory(path, clock=lambda: now)
    first.record(now - 20, 12.5, 25, 50)
    other = UsageHistory(path, clock=lambda: now)
    cluster = Cluster.standalone("node1", FakeDriver())
    a = TestClient(create_app(cluster, usage=first))
    b = TestClient(create_app(cluster, usage=other))
    assert (
        a.get("/api/v1/usage/history").json()["points"]
        == b.get("/api/v1/usage/history").json()["points"]
    )
    point = other.history()["points"][0]
    assert (point["cpu"], point["memory"], point["storage"]) == (12.5, 25, 50)


def test_retention_enforced_on_write_and_on_restart(tmp_path):
    now = 1800000000
    path = tmp_path / "usage.sqlite3"
    store = UsageHistory(path, clock=lambda: now)
    store.record(now - RETENTION - 10, 1, 2, 3)
    store.record(now - 20, 4, 5, 6)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    later = UsageHistory(path, clock=lambda: now + RETENTION + 10)
    assert not later.history("30d")["points"]


def test_ranges_are_bounded_and_averages_preserve_null_gaps(tmp_path):
    now = 1800000000
    store = UsageHistory(tmp_path / "usage.sqlite3", clock=lambda: now)
    for i in range(400):
        store.record(now - i * 10, float(i % 100), None, 50)
    assert len(store.history("5m")["points"]) <= 31
    long = store.history("1d")
    assert len(long["points"]) < 181
    assert long["step_ms"] > 10000
    assert all(p["memory"] is None for p in long["points"])
    assert all(0 <= p["cpu"] <= 100 for p in long["points"])
    client = TestClient(create_app(FakeDriver(), usage=store))
    assert client.get("/api/v1/usage/history?range=forever").status_code == 422


def test_collector_uses_weighted_real_node_values(tmp_path):
    store = UsageHistory(tmp_path / "usage.sqlite3", clock=lambda: 1800000000)
    a, b = FakeDriver(), FakeDriver()
    sa, sb = a.host_system().summary(), b.host_system().summary()
    a.host_system().summary = lambda: replace(
        sa, cpu_percent=20, threads=4, memory_mb=100, memory_used_mb=50
    )
    b.host_system().summary = lambda: replace(
        sb, cpu_percent=80, threads=12, memory_mb=300, memory_used_mb=30
    )
    a.list_pools = lambda: [SimpleNamespace(capacity_gb=100, allocation_gb=10)]
    b.list_pools = lambda: [SimpleNamespace(capacity_gb=300, allocation_gb=30)]
    cluster = Cluster.standalone("a", a)
    cluster.add_local("b", b)
    store.collect(cluster)
    p = store.history()["points"][0]
    assert p["cpu"] == 65
    assert p["memory"] == 20
    assert p["storage"] == 10
    assert p["nodes"] == p["expected"] == 2


def test_missing_metrics_are_not_zero_and_nonfinite_values_are_null(tmp_path):
    store = UsageHistory(tmp_path / "usage.sqlite3", clock=lambda: 1800000000)
    driver = FakeDriver()

    def failed():
        raise RuntimeError("node unavailable")

    driver.host_system().summary = failed
    driver.list_pools = failed
    store.collect(Cluster.standalone("n", driver))
    p = store.history()["points"][0]
    assert p["cpu"] is p["memory"] is p["storage"] is None
    assert p["nodes"] == 0 and p["expected"] == 1
    store.record(1800000000, math.nan, math.inf, None)
    p = store.history()["points"][0]
    assert p["cpu"] is p["memory"] is None


def test_collection_runs_without_a_browser_visit(tmp_path):
    store = UsageHistory(tmp_path / "usage.sqlite3")
    original = store.collect
    done = threading.Event()

    def collect(cluster):
        original(cluster)
        done.set()

    store.collect = collect
    with TestClient(create_app(FakeDriver(), usage=store)):
        assert done.wait(2)
        assert store.history()["points"]
