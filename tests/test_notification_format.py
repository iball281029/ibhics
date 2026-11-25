"""Real channel payloads share complete, escaped content and accurate metrics."""

import json
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from test_notifications import Immediate

from ibhics.manager.events import CATALOG
from ibhics.manager.notification_format import event_notice
from ibhics.manager.notifications import Notifications


def event(identifier):
    return dict(
        event_id=identifier,
        category=CATALOG[identifier]["category"],
        level=CATALOG[identifier]["level"],
        time="2026-11-25T08:00:00+07:00",
        message="APP-01 <script>bad()</script> password=do-not-send",
    )


def test_all_catalog_events_render_escaped_html_and_line_flex():
    for identifier in CATALOG:
        notice = event_notice(event(identifier))
        assert "<script>" not in notice.html() and "&lt;script&gt;" in notice.html()
        assert "do-not-send" not in str(notice)
        flex = notice.flex()
        assert flex["type"] == "flex" and flex["contents"]["type"] == "bubble"
        assert "Recommended next step" in str(notice)
        assert len(flex["altText"].encode("utf-16-le")) <= 800
        assert len(json.dumps(flex).encode()) < 30000


def test_backup_ratios_use_actual_unique_bytes_and_reused_nonzero_data():
    notice = event_notice(
        event("backup.succeeded"),
        {
            "vm": "APP-01",
            "report": {
                "new_data_bytes": 4096,
                "stored_bytes": 1024,
                "dedup_saved_bytes": 8192,
                "size_bytes": 1048576,
                "read_bytes": 16384,
                "kind": "incremental",
                "verified": "skipped",
            },
        },
    )
    rows = dict(notice.rows)
    assert rows["Compression"] == "4.00×" and rows["Deduplication"] == "3.00×"
    assert rows["Logical disk size"] == "1.00 MiB"
    missing = dict(event_notice(event("backup.succeeded")).rows)
    assert missing["Compression"] == missing["Deduplication"] == "N/A"
    zero = dict(
        event_notice(
            event("backup.succeeded"),
            {"report": {"new_data_bytes": 0, "stored_bytes": 0, "dedup_saved_bytes": 4096}},
        ).rows
    )
    assert zero["Compression"] == zero["Deduplication"] == "N/A"


def test_email_has_plain_and_html_parts_and_line_has_same_metrics(monkeypatch):
    sent = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr("smtplib.SMTP", SMTP)
    requests = []
    from contextlib import nullcontext

    monkeypatch.setattr(
        "ibhics.manager.notifications.urlopen",
        lambda request, **kwargs: (requests.append(request), nullcontext())[1],
    )
    alerts = Notifications(executor=Immediate())
    alerts.configure(
        {
            "host": "smtp",
            "security": "none",
            "from": "a@example.com",
            "recipients": ["b@example.com"],
            "preferences": {
                "backup.succeeded": {
                    **alerts.config()["preferences"]["backup.succeeded"],
                    "email": True,
                }
            },
            "line": {
                "enabled": True,
                "token": "test-token",
                "recipient": "U" + "a" * 32,
                "events": ["backup.succeeded"],
            },
        }
    )
    alerts.event(
        "backup.succeeded",
        "Backup completed: APP-01",
        "b1",
        details={
            "vm": "APP-01",
            "report": {"new_data_bytes": 4096, "stored_bytes": 1024, "dedup_saved_bytes": 8192},
        },
    )
    assert len(sent) == len(requests) == 1
    assert sent[0].get_body(preferencelist=("plain",)).get_content_type() == "text/plain"
    assert sent[0].get_body(preferencelist=("html",)).get_content_type() == "text/html"
    for ratio in ["4.00×", "3.00×"]:
        assert ratio in sent[0].get_body(preferencelist=("plain",)).get_content()
        assert ratio in json.dumps(json.loads(requests[0].data), ensure_ascii=False)


def test_summary_merges_history_counts_cancelled_and_separates_active_jobs(tmp_path, monkeypatch):
    now = datetime(2026, 11, 25, 8, tzinfo=timezone(timedelta(hours=7)))
    alerts = Notifications(tmp_path / "alerts.json", clock=lambda: now, executor=Immediate())
    alerts.data["backup_days"] = {"2026-11-24": {"old": "done", "failed": "failed"}}
    jobs = [
        SimpleNamespace(
            id="cancel", kind="backup", state="cancelled", finished="2026-11-24T10:00:00+07:00"
        ),
        SimpleNamespace(id="live", kind="backup", state="running", finished=None),
    ]
    backups = SimpleNamespace(jobs=lambda: jobs)
    replications = SimpleNamespace(jobs=lambda: [])
    messages = []
    monkeypatch.setattr(alerts, "deliver", lambda subject, notice, config: messages.append(notice))
    alerts.configure({"daily_summary": True})
    alerts.summary(backups, replications, ["Repository shared: 1.00 TiB free"] * 2)
    notice = messages[0]
    assert "1 succeeded, 1 failed, 1 cancelled" in notice
    assert dict(notice.rows)["Running / Queued now"] == "1"
    assert str(notice).count("Repository shared: 1.00 TiB free") == 1
    alerts.summary(backups, replications, [])
    assert len(messages) == 1


def test_missing_daily_history_is_not_reported_as_zero_success(monkeypatch):
    now = datetime(2026, 11, 25, 8, tzinfo=UTC)
    alerts = Notifications(clock=lambda: now, executor=Immediate())
    alerts.configure({"daily_summary": True})
    messages = []
    monkeypatch.setattr(alerts, "deliver", lambda subject, notice, config: messages.append(notice))
    alerts.summary(SimpleNamespace(jobs=lambda: []), SimpleNamespace(jobs=lambda: []), [])
    assert "No recorded backup results" in messages[0]
    assert dict(messages[0].rows)["Succeeded / Failed"] == "No recorded results"
