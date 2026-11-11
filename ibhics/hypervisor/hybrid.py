"""Hybrid pools (v1.3): HDDs (or an md RAID) hold the data, an SSD caches it.

The pool is a thin (XFS) pool on one LVM volume, data, in a volume group of its own
(ibhc-<pool>). The SSD is an LVM cache volume (dm-cache) on it: reads that repeat and,
in writeback mode, writes are served from the SSD. Writethrough (the default) writes to
the HDD at once, so losing the SSD loses nothing; writeback is faster for writes but
dirty blocks live only on the SSD until flushed, so use a mirrored SSD for it.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

from .base import InvalidState

CACHE_MODES = ("writethrough", "writeback")
HYBRID_DEV = re.compile(r"^/dev/(ibhc-[A-Za-z0-9_.+-]+)/data$")
STAT_FIELDS = (
    "cache_mode",
    "cache_total_blocks",
    "cache_used_blocks",
    "cache_dirty_blocks",
    "cache_read_hits",
    "cache_read_misses",
    "cache_write_hits",
    "cache_write_misses",
    "chunk_size",
)
GIB = 2**30

Run = Callable[[list[str]], str]


def run_lvm(cmd: list[str]) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if done.returncode:
        raise RuntimeError(f"{cmd[0]} failed: {(done.stderr or done.stdout).strip()[-400:]}")
    return done.stdout


def vg_name(pool: str) -> str:
    return f"ibhc-{pool}"


def vg_of(device: str) -> str | None:
    """The volume group of a hybrid pool's device, or None for any other device."""
    m = HYBRID_DEV.match(device or "")
    return m.group(1) if m else None


def build(run: Run, pool: str, data: list[str], cache: list[str], mode: str,
          cache_gb: int | None) -> str:  # fmt: skip
    """The volume group, its data volume on the HDDs and the SSD cache; returns the data
    volume's device, which the pool formats XFS and mounts."""
    vg = vg_name(pool)
    for dev in [*data, *cache]:
        run(["pvcreate", "-ff", "-y", dev])
    run(["vgcreate", "-y", vg, *data, *cache])
    run(["lvcreate", "-y", "-n", "data", "-l", "100%PVS", vg, *data])
    if cache:
        attach(run, vg, cache, mode, cache_gb)
    return f"/dev/{vg}/data"


def attach(run: Run, vg: str, cache: list[str], mode: str, cache_gb: int | None) -> None:
    if mode not in CACHE_MODES:
        raise ValueError(f"cache mode is one of {', '.join(CACHE_MODES)}")
    size = ["-L", f"{cache_gb}G"] if cache_gb else ["-l", "100%PVS"]
    run(["lvcreate", "-y", "-n", "cache", *size, vg, *cache])
    run(["lvconvert", "-y", "--type", "cache", "--cachevol", "cache", "--cachemode", mode,
         "--chunksize", chunk_size(run, vg), f"{vg}/data"])  # fmt: skip


def chunk_size(run: Run, vg: str, max_chunks: int = 1_000_000) -> str:
    """dm-cache allows about a million chunks: big SSDs need chunks bigger than 64 KiB."""
    out = run(["lvs", "--noheadings", "--units", "b", "--nosuffix", "-o", "lv_size", f"{vg}/cache"])
    size = int(float((out or "0").strip() or 0))
    chunk = 64 * 1024
    while size // chunk > max_chunks:
        chunk *= 2
    return f"{chunk // 1024}k"


def add_cache(run: Run, vg: str, cache: list[str], mode: str, cache_gb: int | None) -> None:
    if stats(run, vg):
        raise InvalidState("this pool has a cache already; remove it first")
    for dev in cache:
        run(["pvcreate", "-ff", "-y", dev])
    run(["vgextend", "-y", vg, *cache])
    try:
        attach(run, vg, cache, mode, cache_gb)
    except Exception:
        _drop_empty_pvs(run, vg)
        raise


def remove_cache(run: Run, vg: str) -> None:
    """Flush dirty blocks to the HDDs, drop the cache and free the SSDs."""
    if not stats(run, vg):
        raise InvalidState("this pool has no cache")
    run(["lvconvert", "-y", "--uncache", f"{vg}/data"])
    _drop_empty_pvs(run, vg)


def set_mode(run: Run, vg: str, mode: str) -> None:
    if mode not in CACHE_MODES:
        raise ValueError(f"cache mode is one of {', '.join(CACHE_MODES)}")
    if not stats(run, vg):
        raise InvalidState("this pool has no cache")
    run(["lvchange", "-y", "--cachemode", mode, f"{vg}/data"])


def _drop_empty_pvs(run: Run, vg: str) -> None:
    out = run(["pvs", "--noheadings", "--units", "b", "--nosuffix", "-o", "pv_name,pv_used",
               "-S", f"vg_name={vg}"])  # fmt: skip
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "0":
            run(["vgreduce", vg, parts[0]])
            run(["pvremove", "-y", parts[0]])


def destroy(run: Run, vg: str) -> None:
    """The whole volume group and its disks' LVM labels (the pool is gone)."""
    out = run(["pvs", "--noheadings", "-o", "pv_name", "-S", f"vg_name={vg}"])
    run(["vgremove", "-f", "-y", vg])
    for dev in out.split():
        run(["pvremove", "-y", dev])


def stats(run: Run, vg: str) -> dict | None:
    """The cache: mode, size, use, dirty blocks and hit ratios; None without a cache."""
    try:
        out = run(["lvs", "--noheadings", "--units", "b", "--nosuffix", "--separator", ";",
                   "-o", ",".join(STAT_FIELDS), f"{vg}/data"])  # fmt: skip
    except RuntimeError:
        return None
    values = dict(zip(STAT_FIELDS, (v.strip() for v in out.strip().split(";")), strict=False))
    if not values.get("cache_mode"):
        return None

    def n(key: str) -> int:
        try:
            return int(float(values.get(key) or 0))
        except ValueError:
            return 0

    chunk = n("chunk_size")
    reads = n("cache_read_hits") + n("cache_read_misses")
    writes = n("cache_write_hits") + n("cache_write_misses")
    hits = n("cache_read_hits") + n("cache_write_hits")

    def ratio(part: int, whole: int) -> float | None:
        return round(100 * part / whole, 1) if whole else None

    return {
        "mode": values["cache_mode"],
        "size_gb": round(n("cache_total_blocks") * chunk / GIB, 1),
        "used_pct": ratio(n("cache_used_blocks"), n("cache_total_blocks")) or 0.0,
        "dirty_gb": round(n("cache_dirty_blocks") * chunk / GIB, 2),
        "read_hit_pct": ratio(n("cache_read_hits"), reads),
        "write_hit_pct": ratio(n("cache_write_hits"), writes),
        "hit_pct": ratio(hits, reads + writes),
    }
