"""One active manager per cluster; the others stand by and take over if it fails.

The active manager holds a POSIX lock on a file on the shared Virtual Storage
(NFSv4: the server keeps the lock while the holder renews its lease, about 90 s).
It keeps its state files on its own disk as before and copies them to the shared
folder every few seconds. A standby manager serves only a "standby" page and
keeps trying the lock; when it gets it, it copies the shared state onto its own
disk and becomes the active manager. Without the shared folder the manager runs
on its own state, so storage can still be repaired from the console.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

LOCK = "leader.lock"
INFO = "leader.json"
FD_ENV = "IBHICS_LEADER_FD"


class Leadership:
    def __init__(
        self,
        shared: Path,
        node: str,
        address: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.shared = Path(shared)
        self.node = node
        self.address = address
        self._clock = clock
        self.fd: int | None = None
        inherited = os.environ.get(FD_ENV)
        if inherited and inherited.isdigit():
            self.fd = int(inherited)  # re-exec'd after winning: the lock came along

    @property
    def available(self) -> bool:
        try:
            if not self.shared.is_dir() and self.shared.parent.is_dir():
                self.shared.mkdir(mode=0o700)  # first use, on a mounted share
            return self.shared.is_dir() and os.access(self.shared, os.W_OK)
        except OSError:
            return False

    def try_acquire(self) -> bool:
        if self.fd is not None:
            return True
        self.shared.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.shared / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        self.announce()
        return True

    def announce(self) -> None:
        data = {"node": self.node, "address": self.address, "at": self._clock()}
        tmp = self.shared / (INFO + f".{self.node}.tmp")
        tmp.write_text(json.dumps(data))
        tmp.chmod(0o600)
        tmp.replace(self.shared / INFO)

    def current(self) -> dict:
        try:
            return json.loads((self.shared / INFO).read_text())
        except (OSError, ValueError):
            return {}

    def still_leader(self) -> bool:
        """False once another node has announced itself: this one must stop."""
        info = self.current()
        return not info or info.get("node") == self.node

    def inheritable(self) -> dict:
        """Environment for exec'ing the active manager while keeping the lock."""
        os.set_inheritable(self.fd, True)
        return {**os.environ, FD_ENV: str(self.fd)}


def replicate(files: list[Path], shared: Path, seen: dict[str, tuple]) -> int:
    """Copy changed state files into the shared folder. SQLite through its backup API,
    so the copy is consistent while the manager writes."""
    copied = 0
    for path in files:
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        key = (st.st_mtime_ns, st.st_size)
        if seen.get(path.name) == key:
            continue
        target = shared / "state" / path.name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = target.with_name(target.name + ".tmp")
        if path.suffix == ".sqlite3":
            src = sqlite3.connect(str(path))
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        else:
            shutil.copyfile(path, tmp)
        os.chmod(tmp, 0o600)
        tmp.replace(target)
        seen[path.name] = key
        copied += 1
    return copied


def restore(files: list[Path], shared: Path, backup_root: Path) -> list[str]:
    """Before taking over: keep this node's old state aside, then use the shared copy."""
    source = shared / "state"
    if not source.is_dir():
        return []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_root / f"manager-state-before-takeover-{stamp}"
    restored = []
    for path in files:
        copy = source / path.name
        if not copy.exists():
            continue
        if path.exists():
            backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, backup / path.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".takeover")
        shutil.copyfile(copy, tmp)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        restored.append(path.name)
    return restored


class Replicator(threading.Thread):
    """While active: copy state out, renew the announcement, step down if replaced."""

    def __init__(
        self,
        leader: Leadership,
        files: list[Path],
        every: float = 10.0,
        on_replaced: Callable[[], None] | None = None,
        vip=None,
    ) -> None:
        super().__init__(daemon=True, name="manager-replicate")
        self.leader, self.files, self.every, self.vip = leader, files, every, vip

        def step_down() -> None:
            if self.vip is not None:
                self.vip.release()
            os._exit(75)  # systemd restarts us as a standby

        self.on_replaced = on_replaced or step_down
        self._seen: dict[str, tuple] = {}
        self.stop = threading.Event()

    def step(self) -> None:
        if not self.leader.available:
            return  # shared storage away: keep running on local state
        if not self.leader.still_leader():
            self.on_replaced()
            return
        self.leader.announce()
        if self.vip is not None:
            self.vip.claim()  # put it back if something removed it
        replicate(self.files, self.leader.shared, self._seen)

    def run(self) -> None:
        while not self.stop.wait(self.every):
            try:
                self.step()
            except Exception:
                pass  # an NFS hiccup must not stop the manager


def standby_app(leader: Leadership):
    """What a standby manager serves: who is active, nothing else."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def info() -> dict:
        current = leader.current()
        return {
            "status": "standby",
            "node": leader.node,
            "active_node": current.get("node"),
            "active_url": (
                f"https://{os.environ['IBHICS_MANAGER_VIP'].split('/')[0]}:8888"
                if os.environ.get("IBHICS_MANAGER_VIP")
                else f"https://{current['address']}:8888"
                if current.get("address")
                else None
            ),
        }

    @app.get("/api/v1/health")
    def health():
        return JSONResponse(info(), status_code=503)

    @app.get("/{path:path}")
    def page(path: str = ""):
        data = info()
        link = (
            f'<a href="{data["active_url"]}">{data["active_url"]}</a>'
            if data["active_url"]
            else "the active node"
        )
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>ibHiCS standby</title>"
            "<body style='font:16px system-ui;margin:3em'>"
            f"<h2>{socket.gethostname()} is the standby manager</h2>"
            f"<p>Manage the cluster on {link} (active: {data['active_node'] or 'unknown'}).</p>"
            "<p>This node takes over by itself if the active manager stops.</p>",
            status_code=503,
        )

    return app


class ManagerVIP:
    """The console address that follows the active manager (`ip addr` + gratuitous ARP)."""

    def __init__(self, cidr: str, device: str, run=None) -> None:
        import ipaddress
        import subprocess

        self.iface = ipaddress.IPv4Interface(cidr)
        self.device = device
        self._run = run or (
            lambda args: subprocess.run(args, capture_output=True, text=True, timeout=15)
        )

    @property
    def ip(self) -> str:
        return str(self.iface.ip)

    def held(self) -> bool:
        out = self._run(["ip", "-o", "-4", "addr", "show", "dev", self.device]).stdout or ""
        return f" {self.ip}/" in out

    def claim(self) -> bool:
        if self.held():
            return True
        # Duplicate address detection: never take an address another host answers for.
        if self._run(
            ["arping", "-D", "-q", "-c", "2", "-w", "3", "-I", self.device, self.ip]
        ).returncode:
            print(f"Manager VIP {self.ip} is in use by another host; not taking it", flush=True)
            return False
        added = self._run(["ip", "addr", "add", str(self.iface), "dev", self.device])
        if added.returncode:
            return False
        self._run(["arping", "-U", "-q", "-c", "3", "-I", self.device, self.ip])
        print(f"Manager VIP {self.ip} on {self.device}", flush=True)
        return True

    def release(self) -> None:
        if self.held():
            self._run(["ip", "addr", "del", str(self.iface), "dev", self.device])


def manager_vip(address: str):
    """IBHICS_MANAGER_VIP=a.b.c.d/nn, on the interface that holds `address` unless
    IBHICS_MANAGER_VIP_DEV says otherwise."""
    import subprocess

    cidr = os.environ.get("IBHICS_MANAGER_VIP")
    if not cidr:
        return None
    device = os.environ.get("IBHICS_MANAGER_VIP_DEV", "")
    if not device and address:
        out = subprocess.run(["ip", "-o", "-4", "addr"], capture_output=True, text=True).stdout
        device = next((ln.split()[1] for ln in out.splitlines() if f" {address}/" in ln), "")
    return ManagerVIP(cidr, device) if device else None


if __name__ == "__main__":
    # ExecStopPost of ibhics-manager: a stopped or crashed manager gives the console
    # address back, so the standby can take it (uvicorn ends on the signal itself).
    import sys

    if sys.argv[1:] == ["release-vip"]:
        vip = manager_vip(os.environ.get("IBHICS_MIGRATION_HOST", ""))
        if vip is not None:
            vip.release()
