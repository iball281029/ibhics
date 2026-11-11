"""Inspect and change only an ibHiCS Virtual Storage thin-data cache."""

from __future__ import annotations

import fcntl
import json
import subprocess
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .storage_create import NAME


class CacheModeIn(BaseModel):
    name: str = Field(pattern=NAME)
    mode: Literal["writethrough", "writeback"]


def command(args):
    done = subprocess.run(
        args, capture_output=True, text=True, timeout=20 if args[0] == "lvs" else 600, check=False
    )
    if done.returncode:
        raise RuntimeError((done.stderr or done.stdout)[-1500:])
    return done.stdout


def cache_status(name, run=command):
    CacheModeIn(name=name, mode="writethrough")
    raw = run(
        [
            "lvs",
            "-a",
            "--reportformat",
            "json",
            "--units",
            "b",
            "--nosuffix",
            "-o",
            "lv_name,segtype,cache_mode,cache_policy,cache_dirty_blocks,chunk_size",
            "ibhv-" + name,
        ]
    )
    matches = [
        r
        for report in json.loads(raw)["report"]
        for r in report["lv"]
        if r["lv_name"].strip("[]") == "thin_tdata" and r["segtype"] == "cache"
    ]
    if len(matches) != 1:
        raise ValueError("This Virtual Storage has no SSD cache on this node")
    row = matches[0]
    return {
        "mode": row["cache_mode"],
        "policy": row["cache_policy"],
        "dirty_bytes": int(row["cache_dirty_blocks"]) * int(row["chunk_size"]),
    }


def set_cache_mode(
    body: CacheModeIn,
    *,
    root: Path,
    run=command,
    report=lambda _: None,
    clock=time.monotonic,
    sleep=time.sleep,
):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "provision.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = cache_status(body.name, run)
        if before["mode"] == body.mode:
            return before
        lv = "ibhv-" + body.name + "/thin_tdata"
        cleaning = False
        try:
            if body.mode == "writethrough" and before["dirty_bytes"]:
                report("Flushing SSD cache to data disks before changing to Writethrough")
                run(["lvchange", "-y", "--cachepolicy", "cleaner", lv])
                cleaning = True
                until = clock() + 600
                while cache_status(body.name, run)["dirty_bytes"]:
                    if clock() >= until:
                        raise RuntimeError("Cache is still flushing; mode was not changed")
                    sleep(2)
            report("Changing SSD cache to " + body.mode)
            run(["lvchange", "-y", "--cachemode", body.mode, lv])
        finally:
            if cleaning:
                run(["lvchange", "-y", "--cachepolicy", before["policy"], lv])
        after = cache_status(body.name, run)
        if after["mode"] != body.mode:
            raise RuntimeError("Cache mode verification failed; inspect this node")
        return after
