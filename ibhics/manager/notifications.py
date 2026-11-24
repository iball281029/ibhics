"""Durable local alerts and optional SMTP delivery using only the standard library."""

from __future__ import annotations

import copy
import json
import re
import smtplib
import ssl
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .events import CATALOG, LEVELS, defaults, legacy_id
from .settings import private_json

CATEGORIES = {
    "backup",
    "rpo",
    "anomaly",
    "verify",
    "replication",
    "node",
    "quorum",
    "capacity",
    "subscription",
    "email",
    "line",
    "summary",
}


class Notifications:
    def __init__(self, path=None, clock=lambda: datetime.now().astimezone(), executor=None):
        self.path, self.clock = path, clock
        self.lock = threading.RLock()
        self.executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="alerts")
        self.data = {
            "events": [],
            "preferences": defaults(),
            "dedupe": {},
            "signals": {},
            "summary_date": None,
            "backup_days": {},
            "line": {"enabled": False, "token": "", "recipient": "", "events": []},
            "smtp": {
                "host": "",
                "port": 587,
                "security": "STARTTLS",
                "username": "",
                "password": "",
                "from": "",
                "recipients": [],
                "levels": ["critical", "warning"],
                "categories": sorted(CATEGORIES - {"email", "summary"}),
                "daily_summary": False,
                "summary_at": "08:00",
            },
        }
        if path and path.exists():
            import json

            saved = json.loads(path.read_text())
            self.data.update({k: v for k, v in saved.items() if k not in ("smtp", "line")})
            self.data["smtp"].update(saved.get("smtp", {}))
            self.data["line"].update(saved.get("line", {}))
        self.data["preferences"] = {**defaults(), **self.data["preferences"]}
        self.data["events"] = self.data["events"][-500:]

    def save(self):
        if self.path:
            private_json(self.path, self.data)

    def view(self):
        with self.lock:
            events = copy.deepcopy(self.data["events"])
            return {"events": list(reversed(events)), "unread": sum(not e["read"] for e in events)}

    def read(self, identifiers):
        with self.lock:
            for event in self.data["events"]:
                if event["id"] in identifiers:
                    event["read"] = True
            self.save()
        return self.view()

    def event(self, event_id, message, key, *, level=None):
        spec = CATALOG[event_id]
        return self.emit(level or spec["level"], spec["category"], message, key, event_id=event_id)

    def emit(self, level, category, message, key, *, email=True, event_id=None):
        event_id = event_id or legacy_id(level, category, message)
        if level not in LEVELS or (category not in CATEGORIES and event_id not in CATALOG):
            raise ValueError("Invalid alert level or category")
        now = self.clock()
        with self.lock:
            preference = self.data["preferences"].get(event_id)
            if preference and preference["level"] != CATALOG[event_id]["level"]:
                level = preference["level"]
            dedupe_key = f"{event_id or category}:{key}"
            previous = self.data["dedupe"].get(dedupe_key)
            if previous is None:
                previous = next(
                    (
                        e
                        for e in reversed(self.data["events"])
                        if e["key"] == key and e.get("event_id") == event_id
                    ),
                    None,
                )
            if (
                previous
                and previous["level"] == level
                and now - datetime.fromisoformat(previous["time"]) < timedelta(hours=1)
            ):
                return None
            event = {
                "id": uuid.uuid4().hex,
                "event_id": event_id,
                "time": now.isoformat(),
                "level": level,
                "category": category,
                "message": message,
                "read": False,
                "key": key,
            }
            self.data["dedupe"][dedupe_key] = {"level": level, "time": event["time"]}
            self.data["dedupe"] = {
                k: v
                for k, v in self.data["dedupe"].items()
                if now - datetime.fromisoformat(v["time"]) < timedelta(hours=1)
            }
            if not preference or preference["in_app"]:
                self.data["events"].append(event)
            self.data["events"] = self.data["events"][-500:]
            self.save()
            config = copy.deepcopy(self.data["smtp"])
            line = copy.deepcopy(self.data["line"])
        if line["enabled"] and event_id in line["events"]:
            self.executor.submit(self.deliver_line, f"ibHiCS {level}: {category}\n{message}", line)
        if (
            email
            and config["host"]
            and (
                preference["email"]
                if preference
                else level in config["levels"] and category in config["categories"]
            )
        ):
            self.executor.submit(self.deliver, f"ibHiCS {level}: {category}", message, config)
        return event

    def config(self):
        with self.lock:
            result = copy.deepcopy(self.data["smtp"])
            result["catalog"] = copy.deepcopy(list(CATALOG.values()))
            result["preferences"] = copy.deepcopy(self.data["preferences"])
            result["password"] = "••• set" if result["password"] else ""
            result["line"] = copy.deepcopy(self.data["line"])
            result["line"]["token"] = "••• set" if result["line"]["token"] else ""
            return result

    def configure(self, body):
        with self.lock:
            body = dict(body)
            line = self._line_config(body.pop("line", {}))
            preferences = copy.deepcopy(self.data["preferences"])
            supplied = body.pop("preferences", {})
            if not isinstance(supplied, dict) or set(supplied) - set(CATALOG):
                raise ValueError("Unknown notification event")
            for key, value in supplied.items():
                if (
                    not isinstance(value, dict)
                    or set(value) != {"in_app", "email", "level"}
                    or type(value["in_app"]) is not bool
                    or type(value["email"]) is not bool
                    or not isinstance(value["level"], str)
                    or value["level"] not in LEVELS
                ):
                    raise ValueError("Invalid event preference")
                preferences[key] = dict(value)
            config = copy.deepcopy(self.data["smtp"])
            if set(body) - set(config):
                raise ValueError("Unknown notification setting")
            if body.get("password") == "••• set":
                body = {k: v for k, v in body.items() if k != "password"}
            config.update(body)
            for key in ("host", "username", "password", "from", "summary_at"):
                if (
                    not isinstance(config[key], str)
                    or len(config[key]) > 2048
                    or "\n" in config[key]
                    or "\r" in config[key]
                ):
                    raise ValueError("Invalid SMTP field")
            if config["security"] not in ("STARTTLS", "SSL", "none"):
                raise ValueError("Choose STARTTLS, SSL or none")
            if type(config["port"]) is not int or not 1 <= config["port"] <= 65535:
                raise ValueError("Invalid SMTP port")
            for key, choices in (("levels", LEVELS), ("categories", CATEGORIES)):
                if not isinstance(config[key], list) or any(
                    not isinstance(v, str) or v not in choices for v in config[key]
                ):
                    raise ValueError("Invalid email filter")
            if not isinstance(config["recipients"], list) or len(config["recipients"]) > 100:
                raise ValueError("Supply up to 100 email recipients")
            for address in [config["from"], *config["recipients"]]:
                if not isinstance(address, str) or (
                    address and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", address)
                ):
                    raise ValueError("Invalid email address")
            if type(config["daily_summary"]) is not bool or not re.fullmatch(
                r"([01]\d|2[0-3]):[0-5]\d", config["summary_at"]
            ):
                raise ValueError("Summary time must be HH:MM")
            self.data["preferences"] = preferences
            self.data["smtp"] = config
            self.data["line"] = line
            self.save()
        return self.config()

    def _line_config(self, body):
        if not isinstance(body, dict) or set(body) - {"enabled", "token", "recipient", "events"}:
            raise ValueError("Unknown LINE setting")
        config = copy.deepcopy(self.data["line"])
        body = dict(body)
        if body.get("token") == "••• set":
            body.pop("token")
        config.update(body)
        if type(config["enabled"]) is not bool:
            raise ValueError("Invalid LINE enabled setting")
        token = config["token"]
        if (
            not isinstance(token, str)
            or not token.isascii()
            or len(token) > 4096
            or any(c.isspace() for c in token)
        ):
            raise ValueError("Invalid LINE channel access token")
        recipient = config["recipient"]
        if not isinstance(recipient, str) or (
            recipient and not re.fullmatch(r"[UCR][0-9a-fA-F]{32}", recipient)
        ):
            raise ValueError("LINE recipient must be a user, group or room ID")
        events = config["events"]
        if not isinstance(events, list) or any(
            not isinstance(event, str) or event not in CATALOG for event in events
        ):
            raise ValueError("Unknown LINE notification event")
        if config["enabled"] and (not token or not recipient):
            raise ValueError("Configure LINE channel access token and recipient first")
        return config

    def deliver_line(self, text, config=None):
        with self.lock:
            config = copy.deepcopy(config or self.data["line"])
        if not config["token"] or not config["recipient"]:
            return {
                "ok": False,
                "message": "Configure LINE channel access token and recipient first",
            }
        request = Request(
            "https://api.line.me/v2/bot/message/push",
            data=json.dumps(
                {
                    "to": config["recipient"],
                    "messages": [
                        {
                            "type": "text",
                            "text": text.encode("utf-16-le")[:10000].decode(
                                "utf-16-le", errors="ignore"
                            ),
                        }
                    ],
                }
            ).encode(),
            headers={
                "Authorization": f"Bearer {config['token']}",
                "Content-Type": "application/json",
                "X-Line-Retry-Key": str(uuid.uuid4()),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=20):
                pass
            return {
                "ok": True,
                "message": "LINE accepted the message; check the configured recipient",
            }
        except HTTPError as exc:
            message = {
                400: "LINE rejected the recipient or message",
                401: "LINE channel access token is invalid",
                403: "LINE channel is not authorized to send",
                429: "LINE rate limit or monthly message quota reached; try later",
            }.get(exc.code, "LINE delivery failed; check the channel settings")
            exc.close()
        except Exception:
            message = "LINE connection failed; check network access"
        # Record delivery failure locally without recursively sending another LINE alert.
        self.emit("warning", "line", message, "line:delivery", email=False)
        return {"ok": False, "message": message}

    def test_line(self):
        return self.executor.submit(self.deliver_line, "Your ibHiCS LINE alerts are working.")

    def _send(self, subject, text, config):
        if not config["host"] or not config["from"] or not config["recipients"]:
            raise ValueError("Configure SMTP host, sender and recipients first")
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = (
            subject,
            config["from"],
            ", ".join(config["recipients"]),
        )
        message.set_content(text)
        factory = smtplib.SMTP_SSL if config["security"] == "SSL" else smtplib.SMTP
        kwargs = {"timeout": 20}
        if config["security"] == "SSL":
            kwargs["context"] = ssl.create_default_context()
        with factory(config["host"], config["port"], **kwargs) as smtp:
            if config["security"] == "STARTTLS":
                smtp.starttls(context=ssl.create_default_context())
            if config["username"]:
                smtp.login(config["username"], config["password"])
            smtp.send_message(message)

    def deliver(self, subject, text, config=None):
        config = config or copy.deepcopy(self.data["smtp"])
        error = ""
        for attempt in range(4):  # initial attempt plus three retries
            try:
                self._send(subject, text, config)
                return {"ok": True, "message": "Email sent"}
            except Exception as exc:
                # SMTP server responses may echo credentials: expose only a controlled description.
                error = {
                    "SMTPAuthenticationError": "SMTP authentication failed",
                    "SMTPRecipientsRefused": "SMTP recipients refused",
                    "SMTPConnectError": "SMTP connection failed",
                    "TimeoutError": "SMTP connection timed out",
                    "ValueError": "Configure SMTP host, sender and recipients first",
                }.get(
                    type(exc).__name__, "SMTP delivery failed; check server and security settings"
                )
                if attempt < 3:
                    time.sleep(1)
        self.emit("warning", "email", error, "email:delivery", email=False)
        return {"ok": False, "message": error}

    def test(self):
        return self.executor.submit(
            self.deliver, "ibHiCS test email", "Your ibHiCS email alerts are working."
        )

    def signal(self, key, healthy, category, name):
        with self.lock:
            previous = self.data["signals"].get(key)
            self.data["signals"][key] = healthy
            self.save()
        if previous != healthy and (previous is not None or not healthy):
            self.emit(
                "info" if healthy else "critical",
                category,
                f"{name}: {'online' if healthy else 'offline'}"
                if category == "node"
                else f"Quorum {'OK' if healthy else 'lost'}",
                key,
            )

    def transition(self, key, healthy, recovered, failed, name):
        with self.lock:
            previous = self.data["signals"].get(key)
            self.data["signals"][key] = healthy
            self.save()
        if previous != healthy and (previous is not None or not healthy):
            self.event(
                recovered if healthy else failed,
                f"{name}: {'healthy' if healthy else 'degraded'}",
                key,
            )

    def monitor(self, cluster, backups, replications, settings):
        backups.check_rpo()
        self.signal("quorum", cluster.has_quorum(), "quorum", "Quorum")
        for member in cluster.members():
            self.signal(f"node:{member.name}", cluster.is_healthy(member), "node", member.name)
        storage_status = getattr(self, "storage_status", None)
        if storage_status:
            try:
                runtime = storage_status()
                resource = runtime.get("resource")
                if runtime.get("created"):
                    status = (resource or {}).get("status", {})
                    self.transition(
                        "vstore",
                        status.get("service") == "Started" and status.get("state") == "OK",
                        "vstore.healthy",
                        "vstore.degraded",
                        "Virtual Storage",
                    )
            except Exception:
                pass
        for member in cluster.compute_members():
            try:
                health = member.driver.host_system().hardware_health()
                for item in health.get("items", []):
                    if item.get("status") in ("warning", "critical"):
                        self.event(
                            "hardware.fault"
                            if item["status"] == "critical"
                            else "hardware.warning",
                            f"{member.name}: {item.get('name', 'Hardware')} — "
                            f"{item.get('detail', '')}",
                            f"hardware:{member.name}:{item.get('source')}:{item.get('name')}",
                        )
            except Exception:
                continue
        capacity = []
        for target in backups.targets():
            for usage in backups.usage(target.name):
                free, used = usage.get("free_bytes"), usage.get("used_bytes")
                if free is None or used is None:
                    continue
                total = (
                    usage.get("quota_bytes") or usage.get("filesystem_total_bytes") or free + used
                )
                capacity.append(f"Repository {target.name}: {free} bytes free")
                if total and free / total < 0.1:
                    self.emit(
                        "warning",
                        "capacity",
                        f"Repository space below 10%: {target.name}",
                        f"capacity:repository:{target.name}:{usage.get('node')}",
                    )
        for member in cluster.compute_members():
            try:
                for pool in member.driver.list_pools():
                    capacity.append(
                        f"Pool {member.name}/{pool.name}: {pool.available_gb:g} GiB free"
                    )
                    if pool.capacity_gb and pool.available_gb / pool.capacity_gb < 0.1:
                        self.emit(
                            "warning",
                            "capacity",
                            f"Pool space below 10%: {member.name}/{pool.name}",
                            f"capacity:pool:{member.name}:{pool.name}",
                        )
            except Exception:
                continue
        for kind, sub in settings.subscription().items():
            if not isinstance(sub, dict) or sub.get("days") is None:
                continue
            days = sub["days"]
            expired = sub.get("status") == "Expired" or days < 0
            if days <= 30:
                self.event(
                    "subscription.expired" if expired else "subscription.expiring",
                    f"{kind.title()} subscription: {days} days remaining",
                    f"subscription:{kind}:{'expired' if expired else '7' if days <= 7 else '30'}",
                    level="critical" if days <= 7 else "warning",
                )
        self.summary(backups, replications, capacity)

    def record_backup(self, job):
        """Keep daily counts beyond the bounded task history, replacing a job only once."""
        day = self.clock().date().isoformat()
        with self.lock:
            days = self.data["backup_days"]
            days.setdefault(day, {})[job.id] = job.state
            for old in sorted(days)[:-3]:
                del days[old]
            self.save()

    def summary(self, backups, replications, capacity):
        now = self.clock()
        with self.lock:
            config = copy.deepcopy(self.data["smtp"])
            if (
                not config["daily_summary"]
                or now.strftime("%H:%M") < config["summary_at"]
                or self.data["summary_date"] == now.date().isoformat()
            ):
                return
            self.data["summary_date"] = now.date().isoformat()
            self.save()
        yesterday = (now - timedelta(days=1)).date()
        jobs = [
            j
            for j in backups.jobs()
            if j.kind == "backup"
            and j.finished
            and datetime.fromisoformat(j.finished).astimezone(now.tzinfo).date() == yesterday
        ]
        with self.lock:
            states = list(self.data["backup_days"].get(yesterday.isoformat(), {}).values())
        if not states:
            states = [job.state for job in jobs]
        succeeded = states.count("done")
        failed = states.count("failed")
        lines = [f"Backup summary for {yesterday}: {succeeded} succeeded, {failed} failed."]
        lines += [f"Replication {j['name']}: {j['state']}" for j in replications.jobs()]
        lines += capacity
        self.executor.submit(
            self.deliver, f"ibHiCS daily summary: {yesterday}", "\n".join(lines), config
        )
