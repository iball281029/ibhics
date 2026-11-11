"""Stopping queued and running jobs from the Tasks page."""

import threading
import time
from concurrent.futures import Executor, Future

import pytest
from test_migration import Env

from ibhics.hypervisor import InvalidState
from ibhics.importer.convert import ConversionFailed, V2VConverter


class Later(Executor):
    """Holds submitted work until run_all(), so jobs stay queued."""

    def __init__(self):
        self.work = []

    def submit(self, fn, *args, **kwargs):
        self.work.append((fn, args, kwargs))
        f = Future()
        f.set_result(None)
        return f

    def run_all(self):
        for fn, args, kwargs in self.work:
            fn(*args, **kwargs)


def test_queued_migration_can_be_cancelled_and_never_runs(tmp_path):
    env = Env(tmp_path)
    later = Later()
    env.migrations._executor = later
    uuid = env.create_vm("q1")
    job_id = env.migrate(uuid, node="node2").json()["id"]
    resp = env.client.post(f"/api/v1/migrations/{job_id}/cancel")
    assert resp.status_code == 200 and resp.json()["state"] == "cancelled"
    later.run_all()
    assert env.where(uuid) == "node1"
    # a VM whose job was cancelled can be migrated again
    env.migrations._executor = type(env.migrations._executor)()
    with pytest.raises(InvalidState):
        env.migrations.cancel(job_id)


def test_running_conversion_is_stopped(tmp_path):
    conv = V2VConverter(log_dir=tmp_path)
    errors = []

    def go():
        try:
            conv._run_streaming(["sleep", "30"], None, "vm1", lambda *a: None)
        except ConversionFailed as exc:
            errors.append(str(exc))

    t = threading.Thread(target=go)
    t.start()
    for _ in range(50):
        if "vm1" in V2VConverter.running:
            break
        time.sleep(0.05)
    started = time.monotonic()
    assert V2VConverter.cancel("vm1") is True
    t.join(10)
    assert errors == ["cancelled by the operator"] and time.monotonic() - started < 10
    assert "vm1" not in V2VConverter.running


def test_cancel_unknown_or_finished_jobs(tmp_path):
    env = Env(tmp_path)
    assert env.client.post("/api/v1/migrations/99/cancel").status_code == 404
    assert env.client.post("/api/v1/migrate/jobs/99/cancel").status_code == 404
    uuid = env.create_vm("d1")
    job_id = env.migrate(uuid, node="node2").json()["id"]  # runs inline: done
    assert env.client.post(f"/api/v1/migrations/{job_id}/cancel").status_code == 409
