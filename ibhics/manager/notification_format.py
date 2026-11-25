"""Shared English notification content for plain text, HTML email and LINE Flex."""
# ruff: noqa: E501 -- Inline email styles and notification copy are kept together.

import re
from html import escape

from .events import CATALOG


def clean(value):
    text = str(value)
    return re.sub(
        r"(?i)\b(password|token|secret|authorization)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )[:1200]


def size(value):
    if not isinstance(value, (int, float)) or value < 0:
        return "N/A"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:,.2f} {unit}"
        value /= 1024


def ratio(numerator, denominator):
    return f"{numerator / denominator:.2f}×" if denominator > 0 else "N/A"


class Notice(str):
    """A text-compatible message retaining its structured delivery layout."""

    def __new__(cls, title, level, message, rows=(), note="Review details in the ibHiCS console."):
        rows = [(clean(k), clean(v)) for k, v in rows]
        title, message, note = clean(title), clean(message), clean(note)
        text = f"ibHiCS | {level.upper()}\n{title}\n\n{message}\n\n"
        text += "\n".join(f"{k}: {v}" for k, v in rows)
        text += f"\n\nRecommended next step\n{note}"
        obj = super().__new__(cls, text)
        obj.title, obj.level, obj.message, obj.rows, obj.note = title, level, message, rows, note
        return obj

    @property
    def color(self):
        return {"critical": "#B32638", "warning": "#965F00"}.get(self.level, "#087B55")

    def html(self):
        rows = "".join(
            '<tr><td style="padding:10px 0;border-bottom:1px solid #e4eaf1;color:#53657b;width:40%">'
            f'{escape(k)}</td><td style="padding:10px 0;border-bottom:1px solid #e4eaf1;font-weight:600">{escape(v)}</td></tr>'
            for k, v in self.rows
        )
        return (
            '<!doctype html><html lang="en"><body style="margin:0;background:#eef2f7;font-family:Arial,sans-serif;color:#182b43">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">'
            '<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:white">'
            '<tr><td style="background:#112c48;color:white;padding:24px;font-size:28px;font-weight:bold">ibHiCS'
            '<span style="font-size:12px;font-weight:normal"> · NOTIFICATIONS</span></td></tr>'
            f'<tr><td style="padding:24px"><p style="color:{self.color};font-size:13px;font-weight:bold">{escape(self.level.upper())}</p>'
            f'<h1 style="font-size:24px;line-height:1.4">{escape(self.title)}</h1>'
            f'<p style="line-height:1.6;white-space:pre-wrap">{escape(self.message)}</p>'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="font-size:14px">{rows}</table>'
            f'<div style="margin-top:24px;padding:16px;background:#f3f6fa;border-left:3px solid {self.color};line-height:1.6">'
            f"<b>Recommended next step</b><br>{escape(self.note)}</div></td></tr>"
            '<tr><td style="padding:18px 24px;background:#f7f9fc;color:#53657b;font-size:12px">Automated notification from ibHiCS. Times include the manager timezone.</td></tr>'
            "</table></td></tr></table></body></html>"
        )

    def flex(self):
        def text(value, **kwargs):
            value = (value or "N/A").encode("utf-16-le")[:4000].decode("utf-16-le", errors="ignore")
            return {"type": "text", "text": value, "wrap": True, **kwargs}

        body = [
            text(self.level.upper(), size="xs", weight="bold", color=self.color),
            text(self.title, size="xl", weight="bold", color="#182B43"),
            text(self.message, size="sm", color="#43556C"),
        ]
        for key, value in self.rows[:20]:
            body.append(
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "md",
                    "contents": [
                        text(key, flex=4, size="xs", color="#53657B"),
                        text(value[:160], flex=6, size="sm", weight="bold", color="#182B43"),
                    ],
                }
            )
        body += [
            {"type": "separator", "margin": "lg"},
            text("Recommended next step", size="xs", weight="bold"),
            text(self.note, size="sm"),
        ]
        return {
            "type": "flex",
            "altText": str(self).encode("utf-16-le")[:800].decode("utf-16-le", errors="ignore"),
            "contents": {
                "type": "bubble",
                "size": "mega",
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#112C48",
                    "contents": [
                        text("ibHiCS · NOTIFICATIONS", color="#FFFFFF", weight="bold", size="md")
                    ],
                },
                "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body},
            },
        }


def event_notice(event, details=None):
    event_id = event.get("event_id") or ""
    title = CATALOG.get(event_id, {}).get("description", event["category"].title())
    d = details or {}
    rows = [("Event", event_id or event["category"]), ("Detected at", event["time"])]
    for key, label in [
        ("vm", "Virtual machine"),
        ("node", "Node"),
        ("target", "Repository"),
        ("policy", "Schedule"),
        ("started", "Started at"),
        ("finished", "Finished at"),
        ("duration", "Duration"),
        ("error", "Reported error"),
    ]:
        if d.get(key) is not None:
            rows.append((label, d[key]))
    if event_id in {"backup.succeeded", "backup.recovered"}:
        r = d.get("report") or {}
        new, saved, stored = (
            r.get(k) for k in ("new_data_bytes", "dedup_saved_bytes", "stored_bytes")
        )
        rows += [
            ("Backup type", r.get("kind") or "N/A"),
            ("Logical disk size", size(r.get("size_bytes"))),
            ("Data read", size(r.get("read_bytes"))),
            ("New data before compression", size(new)),
            ("New data stored", size(stored)),
            ("Duplicate data reused", size(saved)),
            (
                "Compression",
                ratio(new, stored) if isinstance(new, int) and isinstance(stored, int) else "N/A",
            ),
            (
                "Deduplication",
                ratio(new + saved, new)
                if isinstance(new, int) and isinstance(saved, int)
                else "N/A",
            ),
            ("Boot verification", r.get("verified") or "Not performed"),
        ]
    notes = {
        "backup": "Review the backup job and restore points. Backup completion and boot verification are separate checks.",
        "restore": "Check the restored VM and validate its applications before returning it to service.",
        "replication": "Review the last successful synchronization and destination status before recovery or cutover.",
        "node": "Check host power and network connectivity, then review VM and HA status.",
        "quorum": "Check cluster connectivity and member health before making cluster changes.",
        "storage": "Review free space and retention settings before the next scheduled job.",
        "vstore": "Review Virtual Storage health and affected resources in the console.",
        "hardware": "Review Device health and the server hardware management console.",
        "auth": "Review the audit log and verify the activity with the account owner.",
        "subscription": "Review subscription details and contact the contract owner.",
        "update": "Review the update status and release notes before scheduling maintenance.",
        "approval": "Review the request and its impact before approving or rejecting it.",
    }
    return Notice(
        title,
        event["level"],
        event["message"],
        rows,
        notes.get(
            event_id.split(".")[0], "Review the event and related task in the ibHiCS console."
        ),
    )
