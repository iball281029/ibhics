"""Unattended protection, installer interlock and SMTP without external services."""

import fcntl
import json
from concurrent.futures import Future
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import test_backup
from fastapi.testclient import TestClient

from ibhics.backup import RepositoryBusy
from ibhics.backup.upgrade_guard import upgrade_guard
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.auth import Auth
from ibhics.manager.backups import BackupManager, Policy
from ibhics.manager.notifications import Notifications

api = test_backup.api
new_vm = test_backup.new_vm


class Immediate:
    def submit(self, function, *args, **kwargs):
        result = Future()
        result.set_result(function(*args, **kwargs))
        return result


def test_retry_exhaustion_and_recovery(api, monkeypatch):
    client, _, backups, clock = api
    new_vm(client)
    events = []
    backups.notify = lambda *args: events.append(args)
    member = backups._cluster.member("node1")
    original = member.backups.backup
    monkeypatch.setattr(
        member.backups, "backup", lambda *args: (_ for _ in ()).throw(OSError("offline"))
    )
    backups.add_policy(Policy("night", "local", every_hours=24))
    job = backups.tick()[0]
    assert job.state == "retry_wait" and job.step == "Retry 1/2 at 01:15"
    clock.now += timedelta(minutes=14)
    assert backups.tick() == []
    clock.now += timedelta(minutes=1)
    backups.tick()
    assert job.state == "retry_wait" and job.step == "Retry 2/2 at 01:30"
    clock.now += timedelta(minutes=15)
    backups.tick()
    assert job.state == "failed" and job.attempt == 2
    assert len(events) == 1 and events[0][0:2] == ("critical", "backup")
    monkeypatch.setattr(member.backups, "backup", original)
    clock.now += timedelta(days=1)
    assert backups.tick()[0].state == "done"
    assert events[-1][0:2] == ("info", "backup")
    clock.now += timedelta(days=1)
    backups.tick()
    assert len(events) == 2  # no repeated recovery after a healthy run


def test_pending_retry_survives_restart(api, monkeypatch):
    client, _, backups, clock = api
    new_vm(client)
    member = backups._cluster.member("node1")
    monkeypatch.setattr(member.backups, "backup", lambda *args: (_ for _ in ()).throw(OSError()))
    backups.add_policy(Policy("night", "local", every_hours=24))
    job = backups.tick()[0]
    again = BackupManager(backups._cluster, backups._state_path, clock, Immediate())
    again.load()
    clock.now += timedelta(minutes=15)
    again.tick()
    restored = again.jobs()[0]
    assert restored.id == job.id and restored.attempt == 1 and restored.state == "retry_wait"


def test_catch_up_one_job_and_daily_previous_slot(api):
    client, _, backups, clock = api
    new_vm(client)
    backups.add_policy(
        Policy("daily", "local", at="02:00", last_run=(clock.now - timedelta(days=1)).isoformat())
    )
    assert len(backups.tick()) == 1  # missed yesterday's 02:00, manager returned at 01:00
    assert backups.tick() == []
    clock.now += timedelta(days=10)
    assert len(backups.tick()) == 1  # never replay a backlog of old backups
    assert backups.tick() == []


def test_backup_holds_installer_shared_lock(api, tmp_path, monkeypatch):
    from ibhics.backup import upgrade_guard as guard_module

    client, _, backups, _ = api
    vm = new_vm(client)
    path = tmp_path / "upgrade.lock"
    backups.guard_path = path
    monkeypatch.setattr(guard_module, "GUARD_PATH", path)
    node = backups._cluster.member("node1").backups
    original = node._open
    checks = []

    def inspect(target):
        with path.open("a+") as installer:
            with pytest.raises(BlockingIOError):
                fcntl.flock(installer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        checks.append(True)
        return original(target)

    monkeypatch.setattr(node, "_open", inspect)
    job = backups.start_backup(vm["uuid"], "local")
    assert job.state == "done" and checks
    with path.open("a+") as installer:
        fcntl.flock(installer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RepositoryBusy):
            with upgrade_guard(path):
                pytest.fail("Backup started during install")


def test_rpo_uses_restore_points_and_custom_limit(api, monkeypatch):
    client, _, backups, clock = api
    new_vm(client)
    backups.add_policy(Policy("p", "local", every_hours=1))
    events = []
    backups.notify = lambda *args: events.append(args)
    point = SimpleNamespace(
        vm_name="web", suspicious=False, created=(clock.now - timedelta(hours=3)).isoformat()
    )
    monkeypatch.setattr(backups, "list_backups", lambda target: [(None, point)])
    backups.check_rpo()
    assert events[0][0:2] == ("critical", "rpo")
    events.clear()
    backups._policies["p"].rpo_hours = 4
    backups.check_rpo()
    assert not events
    point.suspicious = True
    backups.check_rpo()
    assert len(events) == 1


def test_dedupe_levels_read_and_persistence(tmp_path):
    now = [datetime.now().astimezone()]
    path = tmp_path / "notifications.json"
    alerts = Notifications(path, clock=lambda: now[0])
    first = alerts.emit("warning", "backup", "Failed", "vm:web")
    assert alerts.emit("warning", "backup", "Failed again", "vm:web") is None
    assert alerts.emit("critical", "backup", "Critical", "vm:web")
    now[0] += timedelta(hours=1)
    assert alerts.emit("critical", "backup", "Still critical", "vm:web")
    alerts.read([first["id"]])
    restored = Notifications(path)
    assert restored.view()["unread"] == 2
    assert path.stat().st_mode & 0o777 == 0o600
    for i in range(505):
        restored.emit("info", "node", "Online", str(i))
    assert len(restored.view()["events"]) == 500


@pytest.mark.parametrize("security", ["STARTTLS", "SSL", "none"])
def test_smtp_transport_and_filters(monkeypatch, security):
    calls = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            calls.append((host, port, kwargs["timeout"]))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, **kwargs):
            calls.append("tls")

        def login(self, username, password):
            assert password == "secret-password"
            calls.append("login")

        def send_message(self, message):
            calls.append(message["Subject"])

    monkeypatch.setattr("smtplib.SMTP", SMTP)
    monkeypatch.setattr("smtplib.SMTP_SSL", SMTP)
    alerts = Notifications(executor=Immediate())
    result = alerts.configure(
        {
            "host": "mail.example.com",
            "security": security,
            "username": "ops",
            "password": "secret-password",
            "from": "alerts@example.com",
            "recipients": ["ops@example.com", "admin@example.com"],
        }
    )
    assert result["password"] == "••• set" and "secret-password" not in json.dumps(result)
    assert alerts.test().result()["ok"]
    assert calls[0] == ("mail.example.com", 587, 20)
    assert ("tls" in calls) == (security == "STARTTLS")
    before = len(calls)
    alerts.emit("info", "backup", "Recovered", "backup:1")
    assert len(calls) == before
    alerts.emit("critical", "backup", "Failed", "backup:2")
    assert len(calls) > before


def test_smtp_retry_and_sanitized_failure(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("secret-password token=hidden")

    monkeypatch.setattr("smtplib.SMTP", fail)
    monkeypatch.setattr("ibhics.manager.notifications.time.sleep", lambda seconds: None)
    alerts = Notifications(executor=Immediate())
    alerts.configure(
        {
            "host": "smtp",
            "from": "a@example.com",
            "recipients": ["b@example.com"],
            "password": "secret-password",
        }
    )
    result = alerts.test().result()
    assert not result["ok"] and len(calls) == 4
    assert "secret-password" not in json.dumps(result)
    assert len(alerts.view()["events"]) == 1
    assert alerts.view()["events"][0]["category"] == "email"


def test_api_hides_password_and_requires_admin(tmp_path):
    auth = Auth(tmp_path / "users.json")
    password = "Notification-password-42"
    auth.bootstrap(password, must_change=False)
    app = create_app(FakeDriver(), auth=auth)
    client = TestClient(app)
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": password}
        ).status_code
        == 200
    )
    result = client.put("/api/v1/settings/notifications", json={"password": "smtp-secret"})
    assert result.status_code == 200 and result.json()["password"] == "••• set"
    result = client.get("/api/v1/settings/notifications")
    assert "smtp-secret" not in result.text and result.headers["cache-control"] == "no-store"
    config = result.json()
    assert {event["id"] for event in config["catalog"]} == set(config["preferences"])
    preference = {**config["preferences"]["backup.succeeded"], "email": True}
    assert (
        client.put(
            "/api/v1/settings/notifications", json={"preferences": {"backup.succeeded": preference}}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/v1/settings/notifications").json()["preferences"]["backup.succeeded"]
        == preference
    )
    auth.add_user("reader", "viewer", password)
    auth.user("reader").must_change_password = False
    reader = TestClient(app)
    reader.post("/api/v1/auth/login", json={"username": "reader", "password": password})
    assert reader.get("/api/v1/settings/notifications").status_code == 403
    assert reader.put("/api/v1/settings/notifications", json={}).status_code == 403
    event = app.state.notifications.emit("info", "node", "Online", "node:a")
    assert reader.post("/api/v1/notifications/read", json={"ids": [event["id"]]}).status_code == 200


@pytest.mark.parametrize(
    "operation,args",
    [
        ("backup", ("vm", None)),
        ("restore", (None, "point")),
        ("verify", (None, "point", "pool")),
    ],
)
def test_node_operations_refuse_installer_exclusive_lock(tmp_path, monkeypatch, operation, args):
    from ibhics.backup import NodeBackup
    from ibhics.backup import upgrade_guard as guard_module

    path = tmp_path / "installer.lock"
    monkeypatch.setattr(guard_module, "GUARD_PATH", path)
    node = NodeBackup(FakeDriver(demo_console=False))
    with path.open("a+") as installer:
        fcntl.flock(installer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RepositoryBusy):
            getattr(node, operation)(*args)


def test_monitor_node_quorum_capacity_subscription_and_summary(tmp_path, monkeypatch):
    now = [datetime.now().astimezone().replace(hour=9)]
    alerts = Notifications(tmp_path / "alerts.json", clock=lambda: now[0], executor=Immediate())
    node = SimpleNamespace(
        name="node1",
        driver=SimpleNamespace(
            list_pools=lambda: [SimpleNamespace(name="data", capacity_gb=100, available_gb=9)]
        ),
    )
    healthy = [False]
    cluster = SimpleNamespace(
        has_quorum=lambda: healthy[0],
        members=lambda: [node],
        is_healthy=lambda member: healthy[0],
        compute_members=lambda: [node],
    )
    rpo_checks = []
    backups = SimpleNamespace(
        check_rpo=lambda: rpo_checks.append(True),
        targets=lambda: [SimpleNamespace(name="local")],
        usage=lambda name: [{"free_bytes": 9, "used_bytes": 91, "quota_bytes": 100}],
        jobs=lambda: [],
    )
    replications = SimpleNamespace(jobs=lambda: [{"name": "db", "state": "failed"}])
    days = [30]
    settings = SimpleNamespace(subscription=lambda: {"update": {"days": days[0]}})
    alerts.monitor(cluster, backups, replications, settings)
    events = alerts.view()["events"]
    assert len(events) == 5 and rpo_checks
    assert {e["category"] for e in events} == {"node", "quorum", "capacity", "System"}
    alerts.monitor(cluster, backups, replications, settings)
    assert len(alerts.view()["events"]) == 5
    healthy[0] = True
    days[0] = 7
    alerts.monitor(cluster, backups, replications, settings)
    assert len(alerts.view()["events"]) == 8
    assert alerts.view()["events"][0]["level"] == "critical"
    assert len([e for e in alerts.view()["events"] if e["level"] == "info"]) == 2

    delivered = []
    monkeypatch.setattr(
        alerts, "deliver", lambda subject, message, config: delivered.append((subject, message))
    )
    # Daily counters survive the bounded task history and a manager restart.
    for i in range(205):
        alerts.record_backup(SimpleNamespace(id=str(i), state="done"))
    alerts.record_backup(SimpleNamespace(id="failed", state="failed"))
    alerts.configure({"daily_summary": True, "summary_at": "08:00"})
    now[0] += timedelta(days=1)
    restored = Notifications(alerts.path, clock=lambda: now[0], executor=Immediate())
    monkeypatch.setattr(restored, "deliver", alerts.deliver)
    restored.summary(backups, replications, ["Pool data: 9 GiB free"])
    restored.summary(backups, replications, [])
    assert len(delivered) == 1
    assert "205 succeeded, 1 failed" in delivered[0][1]
    assert "Replication db: failed" in delivered[0][1]
    assert "Pool data: 9 GiB free" in delivered[0][1]


def test_first_missed_daily_slot_after_restart(api):
    _, _, backups, clock = api
    backups.add_policy(Policy("first", "local", at="02:00"))
    assert not backups.policies()[0].due(clock.now)
    again = BackupManager(backups._cluster, backups._state_path, clock, Immediate())
    again.load()
    clock.now += timedelta(days=1)
    assert again.policies()[0].due(clock.now)


def test_verify_and_anomaly_alerts(api, monkeypatch):
    from dataclasses import replace

    client, _, backups, _ = api
    new_vm(client)
    member = backups._cluster.member("node1")
    original = member.backups.backup
    monkeypatch.setattr(
        member.backups, "backup", lambda *args: replace(original(*args), suspicious=True)
    )
    monkeypatch.setattr(
        member.backups, "verify", lambda *args: {"verified": "failed", "reason": "No heartbeat"}
    )
    backups.add_policy(
        Policy("verified", "local", every_hours=24, verify_after_backup=True, verify_pool="default")
    )
    assert backups.tick()[0].state == "done"
    events = client.get("/api/v1/notifications").json()["events"]
    assert {(e["category"], e["level"]) for e in events} == {
        ("anomaly", "warning"),
        ("verify", "critical"),
        ("Backup", "info"),
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_success_email_preference_and_independent_in_app(tmp_path, monkeypatch, enabled):
    from ibhics.manager.events import defaults

    alerts = Notifications(tmp_path / "alerts.json", executor=Immediate())
    preference = {**defaults()["backup.succeeded"], "email": enabled, "in_app": False}
    alerts.configure({"host": "smtp", "preferences": {"backup.succeeded": preference}})
    # The saved per-event switch overrides legacy info/category filters.
    restored = Notifications(alerts.path, executor=Immediate())
    delivered = []
    monkeypatch.setattr(restored, "deliver", lambda *args: delivered.append(args))
    restored.event("backup.succeeded", "Completed", "backup:one")
    assert len(delivered) == int(enabled)
    assert restored.view()["events"] == []
    assert restored.event("backup.succeeded", "Duplicate", "backup:one") is None
    assert len(delivered) == int(enabled)
    restarted = Notifications(alerts.path, executor=Immediate())
    assert restarted.event("backup.succeeded", "Duplicate after restart", "backup:one") is None


def test_catalog_defaults_validation_and_ui_contract(tmp_path):
    from pathlib import Path

    from ibhics.manager.events import CATALOG, defaults

    alerts = Notifications(tmp_path / "alerts.json")
    config = alerts.config()
    assert {e["id"] for e in config["catalog"]} == set(CATALOG) == set(config["preferences"])
    for key, spec in CATALOG.items():
        assert spec["id"] == key
        assert spec["email"] == (spec["level"] in ("critical", "warning"))
        assert spec["in_app"] == (not key.startswith("vm."))
    script = Path("ibhics/manager/static/settings.js").read_text()
    assert "renderEventPreferences(config.catalog, config.preferences)" in script
    assert "coming soon" in script and "event.hooked" in script
    with pytest.raises(ValueError):
        alerts.configure({"preferences": {"unknown.event": defaults()["backup.succeeded"]}})
    with pytest.raises(ValueError):
        alerts.configure({"preferences": {"backup.succeeded": {"in_app": "yes"}}})
    assert alerts.config()["preferences"] == defaults()


def test_success_hook_and_email_on_real_backup(api, monkeypatch):
    client, _, backups, _ = api
    vm = new_vm(client)
    alerts = client.app.state.notifications
    delivered = []
    alerts.executor = Immediate()
    monkeypatch.setattr(alerts, "deliver", lambda *args: delivered.append(args))
    alerts.configure({"host": "smtp"})
    assert backups.start_backup(vm["uuid"], "local").state == "done"
    assert not delivered
    preference = alerts.config()["preferences"]["backup.succeeded"]
    preference["email"] = True
    alerts.configure({"preferences": {"backup.succeeded": preference}})
    assert backups.start_backup(vm["uuid"], "local").state == "done"
    assert len(delivered) == 1
    assert any(e["event_id"] == "backup.succeeded" for e in alerts.view()["events"])


def test_level_override_and_other_event_preferences():
    from ibhics.manager.events import defaults

    alerts = Notifications()
    alerts.configure(
        {"preferences": {"node.offline": {**defaults()["node.offline"], "level": "warning"}}}
    )
    assert alerts.event("node.offline", "Offline", "node:one")["level"] == "warning"
    assert alerts.event("vm.started", "Started", "vm:one")
    assert not any(e["event_id"] == "vm.started" for e in alerts.view()["events"])


def test_hardware_and_storage_monitor_transitions():
    alerts = Notifications()
    state = {"service": "Stopped", "state": "Degraded"}
    alerts.storage_status = lambda: {"created": True, "resource": {"status": state}}
    health = {
        "items": [
            {"status": "critical", "name": "disk", "source": "SMART", "detail": "fault"},
            {"status": "warning", "name": "fan", "source": "IPMI", "detail": "slow"},
        ]
    }
    node = SimpleNamespace(
        name="n",
        driver=SimpleNamespace(
            host_system=lambda: SimpleNamespace(hardware_health=lambda: health),
            list_pools=lambda: [],
        ),
    )
    cluster = SimpleNamespace(
        has_quorum=lambda: True, members=lambda: [], compute_members=lambda: [node]
    )
    backups = SimpleNamespace(check_rpo=lambda: None, targets=lambda: [])
    settings = SimpleNamespace(subscription=lambda: {})
    alerts.monitor(cluster, backups, None, settings)
    assert {e["event_id"] for e in alerts.view()["events"]} == {
        "vstore.degraded",
        "hardware.fault",
        "hardware.warning",
    }
    alerts.monitor(cluster, backups, None, settings)
    assert len(alerts.view()["events"]) == 3
    state.update(service="Started", state="OK")
    alerts.monitor(cluster, backups, None, settings)
    assert alerts.view()["events"][0]["event_id"] == "vstore.healthy"


def test_ui_renders_server_catalog_and_reset_defaults():
    import shutil
    import subprocess
    from pathlib import Path

    from ibhics.manager.events import CATALOG, defaults

    if not shutil.which("node"):
        pytest.skip("Node is required to execute notification UI")
    source = Path("ibhics/manager/static/settings.js").read_text()
    functions = source[
        source.index("let notificationCatalog") : source.index(
            "async function renderNotificationConfig"
        )
    ]
    harness = (
        r"""const assert = require("node:assert/strict");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const elements = {};
const document = {querySelectorAll: () => []};
const $ = id => elements[id] ||= {innerHTML:"", dataset:{}};
const esc = value => String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;");
"""
        + functions
        + r"""
renderEventPreferences(input.catalog, input.preferences);
const html = $("#notification-events").innerHTML;
assert.deepEqual(
  Array.from(html.matchAll(/data-event-id="([^"]+)"/g), match => match[1]),
  input.catalog.map(event => event.id));
assert.equal((html.match(/role="switch"/g) || []).length, input.catalog.length * 3);
assert.equal((html.match(/coming soon/g) || []).length,
  input.catalog.filter(event => !event.hooked).length);
$("#notification-defaults").onclick();
assert.equal($("#notification-config").dataset.dirty, "true");
assert.equal($("#notification-events").innerHTML, html);
"""
    )
    result = subprocess.run(
        ["node", "-e", harness],
        input=json.dumps({"catalog": list(CATALOG.values()), "preferences": defaults()}),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_subscription_thresholds_and_expiry():
    alerts = Notifications()
    cluster = SimpleNamespace(
        has_quorum=lambda: True, members=lambda: [], compute_members=lambda: []
    )
    backups = SimpleNamespace(check_rpo=lambda: None, targets=lambda: [])
    subscription = {"days": 30, "status": "Active"}
    settings = SimpleNamespace(subscription=lambda: {"update": subscription})
    for days, status in [(30, "Active"), (7, "Active"), (0, "Active"), (0, "Expired")]:
        subscription.update(days=days, status=status)
        alerts.monitor(cluster, backups, None, settings)
    events = alerts.view()["events"]
    assert [e["event_id"] for e in events] == [
        "subscription.expired",
        "subscription.expiring",
        "subscription.expiring",
    ]
    assert [e["level"] for e in events] == ["critical", "critical", "warning"]


def test_line_selection_secret_masking_and_persistence(tmp_path, monkeypatch):
    path = tmp_path / "notifications.json"
    alerts = Notifications(path, executor=Immediate())
    requests = []
    from contextlib import nullcontext

    def send(request, **kwargs):
        requests.append(request)
        return nullcontext()

    monkeypatch.setattr("ibhics.manager.notifications.urlopen", send)
    config = alerts.configure(
        {
            "line": {
                "enabled": True,
                "token": "line-secret",
                "recipient": "C" + "a" * 32,
                "events": ["backup.failed", "replication.failed"],
            }
        }
    )
    assert config["line"]["token"] == "••• set"
    assert path.stat().st_mode & 0o777 == 0o600
    alerts.event("backup.succeeded", "Healthy", "backup:1")
    assert requests == []
    alerts.event("backup.failed", "Failed", "backup:2")
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://api.line.me/v2/bot/message/push"
    assert request.get_header("Authorization") == "Bearer line-secret"
    assert json.loads(request.data)["to"] == "C" + "a" * 32
    assert "Failed" in json.loads(request.data)["messages"][0]["text"]
    alerts.configure({"line": {"token": "••• set"}})
    again = Notifications(path, executor=Immediate())
    assert again.data["line"]["token"] == "line-secret"
    assert again.test_line().result()["ok"]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_line_failure_is_local_and_redacted(monkeypatch, status):
    from urllib.error import HTTPError

    alerts = Notifications(executor=Immediate())
    alerts.configure(
        {
            "line": {
                "enabled": True,
                "token": "line-secret",
                "recipient": "U" + "a" * 32,
                "events": ["replication.failed"],
            }
        }
    )

    def fail(*args, **kwargs):
        raise HTTPError("https://api.line.me", status, "line-secret", {}, None)

    monkeypatch.setattr("ibhics.manager.notifications.urlopen", fail)
    event = alerts.event("replication.failed", "Replica failed", "replication:1")
    assert event["event_id"] == "replication.failed"
    assert "line-secret" not in json.dumps(alerts.view())
    result = alerts.test_line().result()
    assert not result["ok"]
    assert "line-secret" not in json.dumps(result)
    if status == 429:
        assert "quota" in result["message"]


def test_line_configuration_validation_and_test_permissions(tmp_path):
    alerts = Notifications(executor=Immediate())
    for body in (
        {"enabled": True},
        {"token": "a\nb"},
        {"recipient": "display-name"},
        {"events": ["unknown"]},
    ):
        with pytest.raises(ValueError):
            alerts.configure({"line": body})
    auth = Auth(tmp_path / "users.json")
    password = "Notification-password-42"
    auth.bootstrap(password, must_change=False)
    app = create_app(FakeDriver(), auth=auth)
    app.state.notifications.executor = Immediate()
    client = TestClient(app)
    assert client.post("/api/v1/settings/notifications/test-line").status_code == 401
    client.post("/api/v1/auth/login", json={"username": "admin", "password": password})
    result = client.post("/api/v1/settings/notifications/test-line")
    assert result.status_code == 200 and not result.json()["ok"]
    auth.add_user("reader", "viewer", password)
    auth.user("reader").must_change_password = False
    client.post("/api/v1/auth/login", json={"username": "reader", "password": password})
    assert client.post("/api/v1/settings/notifications/test-line").status_code == 403
