"""Every change is recorded for the Tasks page: object, user, result, duration."""

from fastapi.testclient import TestClient

from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.activity import ActivityLog, describe


def test_describe_known_and_unknown_routes():
    assert describe("POST", "/api/v1/backup/targets", "/api/v1/backup/targets") == (
        "Backup",
        "Add repository",
        "",
    )
    assert describe("POST", "/api/v1/vms/{uuid}/start", "/api/v1/vms/u1/start") == (
        "VM",
        "Power on",
        "u1",
    )
    category, action, obj = describe(
        "POST", "/api/v1/containers/{name}/start", "/api/v1/containers/web/start"
    )
    assert category == "Container" and action == "Change start" and obj == "web"


def test_changes_and_failures_are_logged_with_duration(tmp_path):
    log = ActivityLog(tmp_path / "a.sqlite3")
    client = TestClient(create_app(FakeDriver(demo_console=False), activity=log))
    vm = client.post("/api/v1/vms", json={"name": "a", "memory_mb": 256, "disks": [{"size_gb": 1}]})
    uuid = vm.json()["uuid"]
    client.post(f"/api/v1/vms/{uuid}/start")
    client.post(f"/api/v1/vms/{uuid}/start")  # already running: refused
    client.get("/api/v1/vms")  # reads are not changes
    events = client.get("/api/v1/activity").json()["events"]
    assert [e["action"] for e in events] == ["Power on", "Power on", "Create VM"]
    assert events[0]["ok"] is False and events[0]["status"] == 409
    assert events[1]["ok"] is True and events[1]["object"] == uuid
    assert all(isinstance(e["duration_ms"], int) for e in events)
    failed = client.get("/api/v1/activity", params={"result": "failed"}).json()["events"]
    assert len(failed) == 1
    assert (
        client.get("/api/v1/activity", params={"q": "create"}).json()["events"][0]["action"]
        == "Create VM"
    )


def test_csv_export_and_formula_cells(tmp_path):
    log = ActivityLog(tmp_path / "a.sqlite3")
    log.add("=cmd", "POST", "/api/v1/networks", "/api/v1/networks", 201, "10.0.0.5", 12)
    client = TestClient(create_app(FakeDriver(demo_console=False), activity=log))
    resp = client.get("/api/v1/activity.csv")
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.splitlines()
    assert lines[0].startswith("at,user,category,action")
    assert "'=cmd,Network,Add network" in lines[1]


def test_history_kept_forever_by_default_and_retention_purges(tmp_path):
    now = [1_000_000.0]
    log = ActivityLog(tmp_path / "a.sqlite3", clock=lambda: now[0])
    log.add("u", "POST", "/api/v1/users", "/api/v1/users", 201)
    now[0] += 400 * 86400
    assert log.purge() == 0 and len(log.query()) == 1  # nothing is deleted by default
    log.set_retention(30)
    assert log.query() == []
    again = ActivityLog(tmp_path / "a.sqlite3")
    assert again.retention_days == 30  # the setting is stored with the history


def test_created_object_name_is_recorded(tmp_path):
    log = ActivityLog(tmp_path / "a.sqlite3")
    client = TestClient(create_app(FakeDriver(demo_console=False), activity=log))
    client.post("/api/v1/vms", json={"name": "web01", "memory_mb": 256, "disks": [{"size_gb": 1}]})
    [event] = client.get("/api/v1/activity").json()["events"]
    assert event["note"] == "name: web01"
