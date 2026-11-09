"""Persistent, bounded server-side resource history, independent of browser sessions."""

from __future__ import annotations

import math
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

RANGES = {"now": 600, "5m": 300, "30m": 1800, "1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
RETENTION = 30 * 86400
SAMPLE_SECONDS = 10


def percent(used, total):
    return used / total * 100 if total > 0 else None


class UsageHistory:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER PRIMARY KEY, cpu REAL, memory REAL, storage REAL,
                nodes INTEGER NOT NULL, expected INTEGER NOT NULL)""")
            db.execute("DELETE FROM samples WHERE ts < ?", (int(clock()) - RETENTION,))

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def record(self, ts, cpu, memory, storage, nodes=1, expected=1):
        def clean(value):
            return (
                min(100, max(0, float(value)))
                if value is not None and math.isfinite(value)
                else None
            )

        ts = int(ts) // SAMPLE_SECONDS * SAMPLE_SECONDS
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?)",
                (ts, clean(cpu), clean(memory), clean(storage), nodes, expected),
            )
            db.execute("DELETE FROM samples WHERE ts < ?", (int(self.clock()) - RETENTION,))

    def collect(self, cluster):
        expected = len(cluster.compute_members(healthy_only=False))

        def read_node(member):
            summary = None
            pools = []
            try:
                summary = member.driver.host_system().summary()
            except Exception:
                pass  # missing readings become gaps, never zeroes
            try:
                pools = member.driver.list_pools()
            except Exception:
                pass
            return summary, pools

        with ThreadPoolExecutor(max_workers=4) as pool:
            readings = list(pool.map(read_node, cluster.compute_members()))
        summaries = [s for s, _ in readings if s is not None]
        cpu = [s for s in summaries if math.isfinite(s.cpu_percent) and s.threads > 0]
        pools = [p for _, ps in readings for p in ps]
        self.record(
            self.clock(),
            sum(s.cpu_percent * s.threads for s in cpu) / sum(s.threads for s in cpu)
            if cpu
            else None,
            percent(sum(s.memory_used_mb for s in summaries), sum(s.memory_mb for s in summaries)),
            percent(sum(p.allocation_gb for p in pools), sum(p.capacity_gb for p in pools)),
            len(summaries),
            expected,
        )

    def history(self, period="now"):
        if period not in RANGES:
            raise ValueError("choose now, 5m, 30m, 1d, 7d or 30d")
        end = int(self.clock())
        start = end - RANGES[period]
        step = max(
            SAMPLE_SECONDS, math.ceil(RANGES[period] / 180 / SAMPLE_SECONDS) * SAMPLE_SECONDS
        )
        with closing(self.connect()) as db:
            first = db.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
            rows = db.execute(
                """SELECT AVG(ts), AVG(cpu), AVG(memory), AVG(storage),
                MIN(nodes), MAX(expected), COUNT(*) FROM samples WHERE ts >= ? AND ts <= ?
                GROUP BY CAST((ts - ?) / ? AS INTEGER) ORDER BY MIN(ts)""",
                (start, end, start, step),
            ).fetchall()
        disk_bytes = 0
        for path in (self.path, Path(str(self.path) + "-wal")):
            try:
                disk_bytes += path.stat().st_size
            except FileNotFoundError:
                pass
        return {
            "recording_since": first * 1000 if first is not None else None,
            "storage_bytes": disk_bytes,
            "range": period,
            "start": start * 1000,
            "end": end * 1000,
            "step_ms": step * 1000,
            "sample_seconds": SAMPLE_SECONDS,
            "retention_days": 30,
            "points": [
                dict(
                    at=round(t * 1000),
                    cpu=c,
                    memory=m,
                    storage=s,
                    nodes=n,
                    expected=e,
                    samples=count,
                )
                for t, c, m, s, n, e, count in rows
            ],
        }
