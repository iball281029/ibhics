import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_migration import Env
from test_storage_create import fixture as disk_fixture

from ibhics.manager import create_app
from ibhics.storage_cache import CacheModeIn
from ibhics.storage_cluster import StorageController
from ibhics.storage_jobs import StorageJobs


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    env = Env(tmp_path)
    spec, disks = disk_fixture()
    spec.arbiter = "witness"
    next(
        m for m in env.cluster.members() if m.name == "witness"
    ).address = "http://172.16.12.105:8007"
    for name, address in (("node1", "172.16.12.104"), ("node2", "172.16.12.103")):
        disks[name]["addresses"] = [{"address": address, "prefix": 24, "interface": "br0"}]
    monkeypatch.setattr("ibhics.storage_cluster.inventories", lambda _: disks)
    monkeypatch.setattr(
        "ibhics.storage_cluster.software_status",
        lambda *a, **kw: {"software_ready": True, "creation_policy": "rf2-aggregate-v1"},
    )
    monkeypatch.setattr(
        "ibhics.storage_cluster.remote",
        lambda *a, **kw: {"software_ready": True, "creation_policy": "rf2-aggregate-v1"},
    )
    monkeypatch.setattr(StorageController, "_gateway", staticmethod(lambda: []))
    monkeypatch.setattr(StorageController, "_linstor_list", staticmethod(lambda _: []))
    monkeypatch.setattr(
        "ibhics.storage_cluster.subprocess",
        SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(returncode=0)),
    )
    controller = StorageController(env.cluster, tmp_path / "storage-runtime")
    return env, controller, spec, disks


def wait_done(jobs):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        job = jobs.current()
        if job and job["state"] in ("done", "failed"):
            return job
        time.sleep(0.01)
    raise AssertionError("Job did not finish")


def test_cache_changes_both_nodes_and_requires_healthy_replicas(workflow, monkeypatch):
    _, controller, spec, _ = workflow
    state = {"created": {"spec": spec.model_dump()}, "resource": {"status": {"state": "Degraded"}}}
    monkeypatch.setattr(controller, "status", lambda: state)
    rows = [{"node": n, "mode": "writethrough"} for n in ("node1", "node2")]
    monkeypatch.setattr(controller, "cache", lambda: rows)
    body = CacheModeIn(name=spec.name, mode="writeback")
    with pytest.raises(ValueError, match="healthy"):
        controller.change_cache(body)
    assert controller.jobs.current() is None
    state["resource"]["status"]["state"] = "OK"
    calls = []

    def local(body, **kwargs):
        calls.append(("local", body.mode))
        rows[0]["mode"] = body.mode

    def peer(member, method, path, **kwargs):
        calls.append((member.name, method, path, kwargs["json"]["mode"]))
        return {"id": "cache-test"}

    monkeypatch.setattr("ibhics.storage_cluster.set_cache_mode", local)
    monkeypatch.setattr("ibhics.storage_cluster.remote", peer)
    monkeypatch.setattr(controller, "_wait", lambda *a: rows[1].update(mode="writeback"))
    controller.change_cache(body)
    assert wait_done(controller.jobs)["state"] == "done"
    assert all(r["mode"] == "writeback" for r in rows)
    assert calls == [
        ("local", "writeback"),
        ("node2", "POST", "/virtual-storage/cache", "writeback"),
    ]


def test_review_and_confirm_use_one_time_receipt(workflow, monkeypatch):
    _, controller, spec, _ = workflow
    calls = []
    monkeypatch.setattr(
        controller,
        "_create",
        lambda spec, review, report: calls.append(spec.name) or {"created": True},
    )
    receipt = controller.review(spec)
    job = controller.create(receipt["token"], spec.name, True)
    assert job["kind"] == "create"
    assert wait_done(controller.jobs)["state"] == "done"
    assert calls == ["hybrid01"]
    with pytest.raises(ValueError, match="no longer available"):
        controller.create(receipt["token"], spec.name, True)


def test_changed_disk_after_review_never_starts_creation(workflow, monkeypatch):
    _, controller, spec, disks = workflow
    calls = []
    monkeypatch.setattr(controller, "_create", lambda *args: calls.append(args))
    receipt = controller.review(spec)
    disks["node2"]["fingerprint"] = "b" * 64
    with pytest.raises(ValueError, match="changed"):
        controller.create(receipt["token"], spec.name, True)
    assert not calls and controller.jobs.current() is None


def test_expired_review_and_wrong_confirmation_are_rejected(workflow):
    _, controller, spec, _ = workflow
    receipt = controller.review(spec)
    with pytest.raises(ValueError, match="type the exact"):
        controller.create(receipt["token"], "other", True)
    with pytest.raises(ValueError, match="Confirm disk erasure"):
        controller.create(receipt["token"], spec.name, False)
    path = controller.root / "reviews" / (receipt["token"] + ".json")
    saved = json.loads(path.read_text())
    saved["expires"] = 0
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="expired"):
        controller.create(receipt["token"], spec.name, True)


def test_vip_collision_and_unrelated_linstor_node_block_review(workflow, monkeypatch):
    _, controller, spec, _ = workflow
    monkeypatch.setattr(
        "ibhics.storage_cluster.subprocess",
        SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(returncode=1)),
    )
    with pytest.raises(ValueError, match="VIP is in use"):
        controller.review(spec)
    monkeypatch.setattr(
        controller,
        "_linstor_list",
        lambda kind: [{"name": "someone-else", "net_interfaces": []}] if kind == "node" else [],
    )
    with pytest.raises(ValueError, match="unrelated node"):
        controller.review(spec)


def test_creation_api_validates_body_and_review_before_queueing(workflow, tmp_path, monkeypatch):
    env, _, spec, _ = workflow
    calls = []
    monkeypatch.setattr(
        StorageController,
        "_create",
        lambda self, spec, review, report: calls.append(spec.name) or {"created": True},
    )
    app = create_app(env.cluster, virtual_storage_path=tmp_path / "virtual-storage.json")
    client = TestClient(app)
    receipt = client.post("/api/v1/virtual-storage/review", json=spec.model_dump())
    assert receipt.status_code == 200, receipt.text
    response = client.post(
        "/api/v1/virtual-storage/create",
        json={"token": receipt.json()["token"], "confirm_name": spec.name, "confirm_wipe": False},
    )
    assert response.status_code == 422 and not calls
    response = client.post(
        "/api/v1/virtual-storage/create",
        json={"token": receipt.json()["token"], "confirm_name": spec.name, "confirm_wipe": True},
    )
    assert response.status_code == 202, response.text
    jobs = StorageJobs(tmp_path / "virtual-storage-runtime/manager-jobs")
    assert wait_done(jobs)["state"] == "done" and calls == [spec.name]


def test_storage_job_excludes_concurrent_jobs_and_records_failure(tmp_path):
    jobs = StorageJobs(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def work(report):
        report("Checking disks")
        entered.set()
        release.wait(2)
        raise RuntimeError("test failure before provisioning")

    jobs.start("create", {}, work)
    assert entered.wait(1)
    try:
        with pytest.raises(ValueError, match="Another storage operation"):
            jobs.start("create", {}, lambda _: {})
    finally:
        release.set()
    job = wait_done(jobs)
    assert job["state"] == "failed" and "test failure" in job["error"]
    assert (tmp_path / "current.json").stat().st_mode & 0o777 == 0o600


def test_agent_provision_parses_body_and_requires_matching_node(tmp_path, monkeypatch):
    from ibhics.agent import create_agent_app
    from ibhics.cluster import NodeRole
    from ibhics.hypervisor.fake import FakeDriver

    calls = []
    monkeypatch.setattr(
        "ibhics.agent.app.provision",
        lambda driver, body, **kwargs: (
            calls.append(body.selection.node) or {"provider": "test/thin"}
        ),
    )
    root = tmp_path / "agent"
    app = create_agent_app(
        "node2", NodeRole.COMPUTE, "test-token", FakeDriver(demo_console=False), storage_root=root
    )
    client = TestClient(app, headers={"X-Ibhics-Token": "test-token"})
    spec, _ = disk_fixture()
    body = {
        "name": spec.name,
        "selection": spec.nodes[0].model_dump(),
        "size_gib": spec.size_gib,
        "confirm_wipe": True,
    }
    response = client.post("/agent/v1/virtual-storage/provision", json=body)
    assert response.status_code == 422 and not calls and not root.exists()
    body["selection"] = spec.nodes[1].model_dump()
    response = client.post("/agent/v1/virtual-storage/provision", json=body)
    assert response.status_code == 202, response.text
    assert wait_done(StorageJobs(root / "node-jobs"))["state"] == "done"
    assert calls == ["node2"]


def test_export_waits_for_started_service(workflow, monkeypatch):
    _, controller, _, _ = workflow
    states = iter(
        [
            [{"name": "example", "status": {"state": "Degraded", "service": "Stopped"}}],
            [
                {
                    "name": "example",
                    "status": {"state": "OK", "service": "Started", "primary": "node1"},
                }
            ],
        ]
    )
    monkeypatch.setattr(controller, "_gateway", lambda: next(states))
    monkeypatch.setattr(
        "ibhics.storage_cluster.time", SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None)
    )
    messages = []
    result = controller._wait_export("example", messages.append)
    assert messages and result["status"]["service"] == "Started"
