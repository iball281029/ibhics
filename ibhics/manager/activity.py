"""Every change made through the console or API, kept in SQLite for the Tasks page.

Long jobs (backups, migrations, imports, deploys) keep their own progress records;
this log adds every other action: who did what to which object, the result and how
long it took. History is kept forever unless an admin sets a retention period.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

API = "/api/v1"

# (method, route template) -> (category, action). Templates are FastAPI route paths.
ACTIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("POST", "/vms"): ("VM", "Create VM"),
    ("PATCH", "/vms/{uuid}"): ("VM", "Edit VM settings"),
    ("DELETE", "/vms/{uuid}"): ("VM", "Delete VM"),
    ("POST", "/vms/{uuid}/start"): ("VM", "Power on"),
    ("POST", "/vms/{uuid}/stop"): ("VM", "Shut down"),
    ("POST", "/vms/{uuid}/restart"): ("VM", "Restart"),
    ("POST", "/vms/{uuid}/migrate"): ("VM", "Migrate"),
    ("POST", "/vms/{uuid}/cdrom"): ("VM", "Change CD"),
    ("POST", "/vms/{uuid}/snapshots"): ("VM", "Take snapshot"),
    ("POST", "/vms/{uuid}/clone"): ("VM", "Deploy / clone"),
    ("PUT", "/vms/{uuid}/ha"): ("VM", "Change HA restart"),
    ("POST", "/networks"): ("Network", "Add network"),
    ("DELETE", "/networks/{name}"): ("Network", "Delete network"),
    ("PUT", "/nodes/{name}/network"): ("Network", "Change node network"),
    ("POST", "/nodes/{name}/pools"): ("Storage", "Add storage"),
    ("DELETE", "/nodes/{name}/pools/{pool}"): ("Storage", "Remove storage"),
    ("PATCH", "/nodes/{name}/pools/{pool}"): ("Storage", "Rename storage"),
    ("POST", "/virtual-storage/create"): ("Storage", "Create Virtual Storage"),
    ("POST", "/virtual-storage/cache"): ("Storage", "Change SSD cache"),
    ("POST", "/backup/targets"): ("Backup", "Add repository"),
    ("DELETE", "/backup/targets/{name}"): ("Backup", "Remove repository"),
    ("POST", "/backup/policies"): ("Backup", "Save backup policy"),
    ("DELETE", "/backup/policies/{name}"): ("Backup", "Delete backup policy"),
    ("POST", "/cluster/create"): ("Node", "Create cluster"),
    ("POST", "/cluster/nodes"): ("Node", "Add node"),
    ("DELETE", "/cluster/nodes/{name}"): ("Node", "Remove node"),
    ("PATCH", "/cluster/nodes/{name}"): ("Node", "Rename node"),
    ("POST", "/nodes/{name}/maintenance"): ("Node", "Maintenance mode"),
    ("POST", "/nodes/{name}/evacuate"): ("Node", "Evacuate node"),
    ("POST", "/nodes/{name}/power"): ("Node", "Node power"),
    ("POST", "/users"): ("User", "Add user"),
    ("PATCH", "/users/{name}"): ("User", "Change user"),
    ("DELETE", "/users/{name}"): ("User", "Delete user"),
    ("LOGIN", ""): ("Sign-in", "Sign in"),
    ("POST", "/auth/logout"): ("Sign-in", "Sign out"),
    ("POST", "/auth/password"): ("Sign-in", "Change own password"),
}

_PREFIXES = [
    ("/containers", "Container"),
    ("/vms", "VM"),
    ("/templates", "VM"),
    ("/deploys", "VM"),
    ("/recycle", "VM"),
    ("/networks", "Network"),
    ("/edge", "Network"),
    ("/virtual-storage", "Storage"),
    ("/backup", "Backup"),
    ("/migrate", "Import"),
    ("/imports", "Import"),
    ("/cluster", "Node"),
    ("/nodes", "Node"),
    ("/users", "User"),
    ("/auth", "Sign-in"),
    ("/isos", "Storage"),
]
_VERBS = {"POST": "Change", "PUT": "Change", "PATCH": "Edit", "DELETE": "Delete"}
_PARAM = re.compile(r"\{(\w+)\}")


def describe(method: str, route: str, path: str) -> tuple[str, str, str]:
    """(category, action, object) for one request; route is the FastAPI template."""
    if method == "LOGIN":
        return "Sign-in", "Sign in", path
    short = route.removeprefix(API) if route else path.removeprefix(API)
    category, action = ACTIONS.get((method, short), ("", ""))
    if not category:
        category = next((c for p, c in _PREFIXES if short.startswith(p)), "System")
        words = [s for s in short.split("/") if s and not s.startswith("{")]
        what = " ".join(words[1:]) or (words[0] if words else "")
        action = f"{_VERBS.get(method, method.title())} {what}".strip().replace("-", " ")
    obj = ""
    if route:
        names = _PARAM.findall(route)
        parts = path.removeprefix(API).split("/")
        tmpl = short.split("/")
        values = {
            n: parts[i]
            for i, seg in enumerate(tmpl)
            for n in names
            if seg == "{" + n + "}"
            if i < len(parts)
        }
        obj = " / ".join(values[n] for n in names if n in values)
    return category, action, obj


class ActivityLog:
    def __init__(self, path: Path | None = None, clock=time.time) -> None:
        self._db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        self._clock = clock
        if path:
            try:
                Path(path).chmod(0o600)
            except OSError:
                pass
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL, user TEXT,"
                " category TEXT, action TEXT, object TEXT, method TEXT, path TEXT,"
                " status INTEGER, ok INTEGER, duration_ms INTEGER, address TEXT, note TEXT)"
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS events_at ON events(at)")
            self._db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value)")

    def add(
        self,
        user: str,
        method: str,
        route: str,
        path: str,
        status: int,
        address: str = "",
        duration_ms: int | None = None,
        note: str = "",
    ) -> None:
        category, action, obj = describe(method, route, path)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO events (at, user, category, action, object, method, path, status,"
                " ok, duration_ms, address, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self._clock(),
                    user,
                    category,
                    action,
                    obj,
                    method,
                    path,
                    status,
                    int(status < 400),
                    duration_ms,
                    address,
                    note[:300],
                ),
            )

    @property
    def retention_days(self) -> int:
        row = self._db.execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()
        return int(row[0]) if row else 0

    def set_retention(self, days: int) -> int:
        if days < 0 or days > 3650:
            raise ValueError("retention must be 0 (keep forever) to 3650 days")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO settings VALUES ('retention_days', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (days,),
            )
        self.purge()
        return days

    def purge(self) -> int:
        days = self.retention_days
        if not days:
            return 0
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM events WHERE at < ?", (self._clock() - days * 86400,)
            )
            return cur.rowcount

    def query(
        self,
        category: str = "",
        result: str = "",
        q: str = "",
        days: int = 0,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        where, args = [], []
        if category:
            where.append("category = ?")
            args.append(category)
        if result in ("ok", "failed"):
            where.append("ok = ?")
            args.append(1 if result == "ok" else 0)
        if q:
            where.append(
                "(user || ' ' || action || ' ' || object || ' ' || path || ' ' || note) LIKE ?"
            )
            args.append(f"%{q}%")
        if days:
            where.append("at >= ?")
            args.append(self._clock() - days * 86400)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY at DESC, id DESC LIMIT ? OFFSET ?"
        with self._lock:
            cur = self._db.execute(sql, [*args, max(1, min(limit, 5000)), max(0, offset)])
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        for row in rows:
            row["at"] = datetime.fromtimestamp(row["at"]).astimezone().isoformat(timespec="seconds")
            row["ok"] = bool(row["ok"])
        return rows

    def to_csv(self, rows: list[dict]) -> str:
        out = io.StringIO()
        fields = [
            "at",
            "user",
            "category",
            "action",
            "object",
            "ok",
            "status",
            "duration_ms",
            "address",
            "method",
            "path",
            "note",
        ]
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # Spreadsheet formula injection: cells must not start with = + - @
            writer.writerow({k: _safe_cell(row.get(k)) for k in fields})
        return out.getvalue()


def _safe_cell(value):
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value
