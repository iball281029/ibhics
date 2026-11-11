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
RESOLUTIONS = {
    # window, sample bucket, labelled time-grid interval (seconds)
    "now": (600, 10, 300),
    "5m": (1800, 300, 300),
    "30m": (10800, 1800, 1800),
    "1d": (7 * 86400, 86400, 86400),
    "7d": (30 * 86400, 7 * 86400, 7 * 86400),
}


def percent(used, total):
    return used / total * 100 if total > 0 else None


def unique_storage_pools(pools):
    """Count each NFS export once; local pools remain distinct across nodes."""
    result = {}
    for index, pool in enumerate(pools):
        source = getattr(pool, "source", "")
        shared = getattr(pool, "type", None) == "nfs" and source
        key = ("nfs", source) if shared else ("local", index)
        if key not in result or (not result[key].active and pool.active):
            result[key] = pool
    return list(result.values())


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
        pools = unique_storage_pools([p for _, ps in readings for p in ps])
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

    def history(self, period="now", resolution=None, utc_offset=0):
        if period not in RANGES:
            raise ValueError("choose now, 5m, 30m, 1d, 7d or 30d")
        end = int(self.clock())
        start = end - RANGES[period]
        step = max(
            SAMPLE_SECONDS, math.ceil(RANGES[period] / 180 / SAMPLE_SECONDS) * SAMPLE_SECONDS
        )
        tick = step
        if resolution is not None:
            if resolution not in RESOLUTIONS or not -840 <= utc_offset <= 840:
                raise ValueError("invalid resolution or UTC offset")
            window, step, tick = RESOLUTIONS[resolution]
            start = end - window
        shift = int(utc_offset) * 60
        with closing(self.connect()) as db:
            first = db.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
            rows = db.execute(
                """SELECT CAST((ts + ?) / ? AS INTEGER) * ? - ? AS bucket,
                AVG(cpu), AVG(memory), AVG(storage), MIN(nodes), MAX(expected), COUNT(*),
                MIN(ts), MAX(ts) FROM samples WHERE ts >= ? AND ts <= ?
                GROUP BY bucket ORDER BY bucket""",
                (shift, step, step, shift, start, end),
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
            "resolution": resolution,
            "tick_ms": tick * 1000,
            "utc_offset": utc_offset,
            "start": start * 1000,
            "end": end * 1000,
            "step_ms": step * 1000,
            "sample_seconds": SAMPLE_SECONDS,
            "retention_days": 30,
            "points": [
                dict(
                    at=max(t, first_actual) * 1000,
                    bucket_start=t * 1000,
                    bucket_end=(t + step) * 1000,
                    first_sample=first_actual * 1000,
                    last_sample=last_actual * 1000,
                    cpu=c,
                    memory=m,
                    storage=s,
                    nodes=n,
                    expected=e,
                    samples=count,
                )
                for t, c, m, s, n, e, count, first_actual, last_actual in rows
            ],
        }
