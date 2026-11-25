"""Fenced, stopped container handover on demonstrably shared storage.

The private marker is deliberately durable: interrupted handovers fail closed.
Only the source may abort before ownership changes; only the destination may finish.
"""

import json
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from .base import InvalidState
from .containers import CT_NAME_RE, ContainerSpec

MARKER = ".ibhics-migration.json"


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
            if running and body.get("allow_stop") is not True:
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
            )
            # Fence normal actions before stopping; resume requires explicit completion.
            atomic(home / MARKER, marker)
            try:
                if running:
                    backend._lxc("stop", base, name, "-t", "60")
                if backend._info(base, spec).state != "stopped":
                    raise InvalidState("Container did not stop")
            except BaseException:
                (home / MARKER).unlink()
                raise
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
            (home / MARKER).unlink()
            if marker["was_running"]:
                backend.action(name, "start")
            return backend.get(name).to_dict()
        if action == "abort":
            if owner != backend.node_name or marker["source"] != backend.node_name:
                raise InvalidState("Ownership changed; source restart is forbidden")
            data = json.loads((home / "ibhics.json").read_text())
            data["owner_node"] = backend.node_name
            atomic(home / "ibhics.json", data)
            (home / MARKER).unlink()
            if marker["was_running"]:
                backend.action(name, "start")
            return backend.get(name).to_dict()
        raise ValueError("Unknown migration operation")
