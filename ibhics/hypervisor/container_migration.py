"""Fenced, stopped container handover on demonstrably shared storage.

The private marker is deliberately durable: interrupted handovers fail closed.
Only the source may abort before ownership changes; only the destination may finish.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from .base import InvalidState
from .containers import CT_NAME_RE, ContainerSpec

MARKER = ".ibhics-migration.json"
CHECKPOINT = ".ibhics-checkpoint"


def checkpoint(backend, base, name, restore=False):
    """Use files rather than pipes: restored processes must not inherit RPC pipes."""
    directory = base / name / CHECKPOINT
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    error = ""
    with tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(
                ["lxc-checkpoint", "-P", str(base), "-n", name,
                 "-D", str(directory), "-r" if restore else "-s"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=stderr, timeout=120, check=False,
            )
            if result.returncode == 0:
                return None
            error = f"lxc-checkpoint exited {result.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = str(exc)
        stderr.seek(0, os.SEEK_END)
        stderr.seek(max(0, stderr.tell() - 1000))
        tail = stderr.read().decode(errors="replace").strip()
    log = directory / ("restore.log" if restore else "dump.log")
    try:
        with log.open(errors="replace") as stream:
            errors = []
            for line in stream:
                if "error" in line.lower():
                    errors.append(line.strip())
                    if sum(map(len, errors)) >= 300:
                        break
        if errors:
            return "; ".join(errors)[:300]
    except OSError:
        pass
    return (tail or error)[-300:]


def resume(backend, base, spec, marker):
    """Keep the fence through restore/start, including failed recovery attempts."""
    method = "offline"
    started = False
    if marker["was_running"]:
        method = "restart"
        if backend._info(base, spec).state != "running":
            if marker.get("checkpoint"):
                # Restore must hold the lock even if LXC skips pre-start hooks.
                backend._runlock_acquire(base / spec.name)
                error = (checkpoint(backend, base, spec.name, restore=True)
                         if shutil.which("criu") else
                         f"criu is not installed on {backend.node_name}")
                if not error and backend._info(base, spec).state == "running":
                    method = "live"
                else:
                    marker["live_error"] = error or "Restore did not reach running state"
                    atomic(base / spec.name / MARKER, marker)
            if method != "live":
                if backend._info(base, spec).state != "stopped":
                    backend._lxc("stop", base, spec.name, "-k")
                if backend._info(base, spec).state != "stopped":
                    raise InvalidState("Container must be stopped before fallback start")
                # Internal equivalent of action(start); public actions remain fenced.
                backend._lxc("start", base, spec.name)
                backend._wait_running(base, spec.name)
                started = True
        else:
            method = marker.get("method", "restart")
        if backend._info(base, spec).state != "running":
            raise InvalidState("Container did not start; migration remains fenced")
    marker["method"] = method
    marker["downtime_seconds"] = max(0, time.time() - marker.get("stop_started", time.time()))
    atomic(base / spec.name / MARKER, marker)
    directory = base / spec.name / CHECKPOINT
    if directory.exists():
        shutil.rmtree(directory)
    (base / spec.name / MARKER).unlink()
    if started:
        backend._apply_network(spec)
    result = backend.get(spec.name).to_dict()
    result.update(method=method, downtime_seconds=marker["downtime_seconds"])
    if marker.get("live_error"):
        result["live_error"] = marker["live_error"]
    return result


def atomic(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".migration-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def operate(backend, action, name, body):
    if not CT_NAME_RE.fullmatch(name):
        raise ValueError("Invalid container name")
    if action == "prepare":
        destination = body.get("node", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", destination):
            raise ValueError("Invalid destination node")
        if destination == backend.node_name:
            raise InvalidState("Container is already on this node")
        base, spec = backend._find(name)
        with backend._creation_lock(base, name):
            ct = backend.get(name)
            running = ct.state == "running"
            mode = body.get("mode", "offline")
            if mode not in ("live", "offline"):
                raise ValueError("Invalid migration mode")
            if running and mode == "offline" and body.get("allow_stop") is not True:
                raise InvalidState("Allow a temporary stop before migrating a running container")
            home = base / name
            if (home / MARKER).exists():
                raise InvalidState("Container migration already in progress")
            marker = dict(
                source=backend.node_name,
                destination=destination,
                proof=uuid4().hex,
                pool=spec.pool,
                path=str(home),
                was_running=running,
                mode=mode,
                live=mode == "live" and running,
            )
            # Fence normal actions before stopping; resume requires explicit completion.
            atomic(home / MARKER, marker)
            if running:
                marker["stop_started"] = time.time()
                atomic(home / MARKER, marker)
                if marker["live"]:
                    error = (checkpoint(backend, base, name) if shutil.which("criu") else
                             f"criu is not installed on {backend.node_name}")
                    marker["checkpoint"] = not error
                    if error:
                        marker["live_error"] = error
                    atomic(home / MARKER, marker)
                if not marker.get("checkpoint") and backend._info(base, spec).state != "stopped":
                    backend._lxc("stop", base, name, "-t", "60")
            if backend._info(base, spec).state != "stopped":
                raise InvalidState("Container did not stop; migration remains fenced")
            backend._runlock_release(home)
            return marker

    if action == "recover":
        base, spec = backend._find(name, require_owner=False)
        marker = json.loads((base / name / MARKER).read_text())
        step = "finish" if marker["destination"] == backend.node_name else "abort"
        return operate(backend, step, name, marker)

    pool, proof = body.get("pool", ""), body.get("proof", "")
    if pool not in backend._pools() or not re.fullmatch(r"[a-f0-9]{32}", proof):
        raise InvalidState("Invalid migration receipt")
    base = backend._dir(pool)
    home = base / name
    try:
        marker = json.loads((home / MARKER).read_text())
    except (FileNotFoundError, ValueError) as exc:
        raise InvalidState(
            "Shared storage migration receipt is unavailable; no ownership changed"
        ) from exc
    if marker.get("proof") != proof or marker.get("path") != str(home):
        raise InvalidState("Destination does not see the same shared container storage")
    spec = ContainerSpec(**json.loads((home / "ibhics.json").read_text()))
    owner = backend._owner(home)
    with backend._creation_lock(base, name):
        if action == "probe":
            if marker["destination"] != backend.node_name or owner != marker["source"]:
                raise InvalidState("Migration ownership changed")
            if backend._info(base, spec).state != "stopped":
                raise InvalidState("Container is already active on destination")
            if not (home / "rootfs.img").is_file():
                raise InvalidState("Shared root filesystem is unavailable")
            for card in spec.interfaces():
                backend._bridge_of(card["network"])
            for line in (home / "config").read_text().splitlines():
                if line.strip().startswith("lxc.include"):
                    if not Path(line.partition("=")[2].strip()).is_file():
                        raise InvalidState("Destination lacks a required LXC configuration include")
                if line.strip().startswith("lxc.mount.entry") and "/dev/fuse " not in line:
                    raise InvalidState("Custom mounts require a separate migration plan")
            if marker.get("checkpoint") and not shutil.which("criu"):
                marker["live_error"] = f"criu is not installed on {backend.node_name}"
                atomic(home / MARKER, marker)
            return {"ready": True, "node": backend.node_name}
        if action == "commit":
            if marker["source"] != backend.node_name or owner != backend.node_name:
                raise InvalidState("Only the current source can hand over ownership")
            if backend._info(base, spec).state != "stopped":
                raise InvalidState("Source must remain stopped")
            # A disagreement between these two files is intentionally rejected by _owner.
            # If writing the second fails, preserve the fence for operator recovery.
            data = json.loads((home / "ibhics.json").read_text())
            data["owner_node"] = marker["destination"]
            atomic(home / "ibhics.json", data)
            atomic(home / ".ibhics-owner.json", {"node": marker["destination"]})
            return {"committed": True}
        if action == "finish":
            if owner != backend.node_name or marker["destination"] != backend.node_name:
                raise InvalidState("Only the destination owner can finish migration")
            backend._write_config(base, replace(spec, owner_node=owner))
            return resume(backend, base, replace(spec, owner_node=owner), marker)
        if action == "abort":
            if owner != backend.node_name or marker["source"] != backend.node_name:
                raise InvalidState("Ownership changed; source restart is forbidden")
            data = json.loads((home / "ibhics.json").read_text())
            data["owner_node"] = backend.node_name
            atomic(home / "ibhics.json", data)
            return resume(backend, base, replace(spec, owner_node=owner), marker)
        raise ValueError("Unknown migration operation")
