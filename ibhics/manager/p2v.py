"""Physical-to-virtual: a small agent on a physical Linux server sends its disks here.

An operator opens a session for a storage pool on the manager's node and gets a
one-time command. The agent posts the machine's inventory, streams each disk (gzip
over HTTPS, the manager's key pinned) and then says it is done. The disks land as
sparse raw files in that pool; the session then shows up as a VM on the built-in
"physical" Migrate source, converted by virt-v2v like any other import.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

SESSION_TTL_S = 24 * 3600
MAX_DISKS = 16
CHUNK_ZERO = bytes(1 << 20)
HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
DEVICE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class P2VError(ValueError):
    pass


@dataclass
class Session:
    id: str
    pool: str
    folder: str
    token_hash: str
    created: float
    state: str = "waiting"  # waiting, uploading, ready, imported, failed
    machine: dict = field(default_factory=dict)
    received: dict = field(default_factory=dict)  # disk index -> bytes written so far
    error: str = ""

    def public(self) -> dict:
        data = {k: v for k, v in asdict(self).items() if k not in ("token_hash", "folder")}
        data["created"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.created))
        disks = self.machine.get("disks", [])
        total = sum(d["size_bytes"] for d in disks) or 0
        done = sum(int(v) for v in self.received.values())
        data["percent"] = round(100 * done / total) if total else 0
        return data


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class P2VSessions:
    def __init__(self, state_path: Path | None = None, clock=time.time) -> None:
        self._path = state_path
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        if state_path and state_path.exists():
            for data in json.loads(state_path.read_text()):
                self._sessions[data["id"]] = Session(**data)

    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(s) for s in self._sessions.values()]))
        tmp.chmod(0o600)
        tmp.replace(self._path)

    # Operator side

    def open(self, pool: str, pool_folder: Path) -> tuple[Session, str]:
        sid = secrets.token_hex(6)
        token = secrets.token_urlsafe(32)
        folder = Path(pool_folder) / ".ibhics-p2v" / sid
        folder.mkdir(parents=True, mode=0o700)
        session = Session(sid, pool, str(folder), _hash(token), self._clock())
        with self._lock:
            self._sessions[sid] = session
            self._save()
        return session, token

    def list(self) -> list[Session]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda s: -s.created)

    def get(self, sid: str) -> Session:
        with self._lock:
            session = self._sessions.get(sid)
        if session is None:
            raise KeyError(sid)
        return session

    def ready(self) -> list[Session]:
        return [s for s in self.list() if s.state == "ready"]

    def mark(self, sid: str, state: str) -> None:
        with self._lock:
            self._sessions[sid].state = state
            self._save()

    def delete(self, sid: str) -> None:
        with self._lock:
            session = self._sessions.pop(sid, None)
            self._save()
        if session:
            shutil.rmtree(session.folder, ignore_errors=True)

    # Agent side: every call carries the session's token

    def authorize(self, sid: str, token: str) -> Session:
        with self._lock:
            session = self._sessions.get(sid)
        if session is None or not hmac.compare_digest(session.token_hash, _hash(token or "")):
            raise PermissionError("unknown session or wrong token")
        if session.state not in ("waiting", "uploading"):
            raise PermissionError("this session no longer accepts uploads")
        if self._clock() - session.created > SESSION_TTL_S:
            raise PermissionError("this session has expired; open a new one")
        return session

    def inventory(self, session: Session, data: dict) -> dict:
        machine = validate_inventory(data)
        with self._lock:
            session.machine, session.state, session.received = machine, "uploading", {}
            self._save()
        return machine

    def disk_path(self, session: Session, index: int) -> Path:
        disks = session.machine.get("disks", [])
        if not 0 <= index < len(disks):
            raise P2VError("send the inventory first; no such disk")
        return Path(session.folder) / f"disk{index}.raw"

    def write_disk(self, session: Session, index: int, chunks, gzip: bool) -> int:
        """Write one disk from an iterator of byte chunks; zero blocks stay holes."""
        target = self.disk_path(session, index)
        size = session.machine["disks"][index]["size_bytes"]
        partial = target.with_suffix(".partial")
        inflate = zlib.decompressobj(wbits=31) if gzip else None
        written = 0
        with open(partial, "wb") as out:
            pending = b""

            def flush(block: bytes) -> None:
                nonlocal written
                if written + len(block) > size:
                    raise P2VError("the disk sent more data than its size")
                if block == CHUNK_ZERO[: len(block)]:
                    out.seek(len(block), os.SEEK_CUR)
                else:
                    out.write(block)
                written += len(block)
                session.received[str(index)] = written

            for chunk in chunks:
                data = inflate.decompress(chunk) if inflate else chunk
                pending += data
                while len(pending) >= len(CHUNK_ZERO):
                    flush(pending[: len(CHUNK_ZERO)])
                    pending = pending[len(CHUNK_ZERO) :]
            if inflate:
                pending += inflate.flush()
            if pending:
                flush(pending)
            out.truncate(written)
        if written != size:
            partial.unlink(missing_ok=True)
            raise P2VError(f"disk {index} stopped at {written} of {size} bytes; send it again")
        partial.replace(target)
        with self._lock:
            self._save()
        return written

    def finish(self, session: Session) -> Session:
        disks = session.machine.get("disks", [])
        missing = [i for i in range(len(disks)) if not self.disk_path(session, i).exists()]
        if not disks or missing:
            raise P2VError(f"disks still missing: {missing or 'all'}")
        (Path(session.folder) / "machine.json").write_text(json.dumps(session.machine))
        with self._lock:
            session.state = "ready"
            self._save()
        return session


def validate_inventory(data: dict) -> dict:
    if not isinstance(data, dict):
        raise P2VError("inventory must be an object")
    hostname = str(data.get("hostname", ""))
    if not HOST.fullmatch(hostname):
        raise P2VError("hostname is missing or invalid")
    try:
        cpus = int(data.get("cpus", 0))
        memory_mb = int(data.get("memory_mb", 0))
    except (TypeError, ValueError):
        raise P2VError("cpus and memory_mb must be numbers") from None
    if not 1 <= cpus <= 512 or not 128 <= memory_mb <= 16 * 1024 * 1024:
        raise P2VError("cpus or memory_mb out of range")
    firmware = str(data.get("firmware", "bios")).lower()
    if firmware not in ("bios", "uefi"):
        raise P2VError("firmware must be bios or uefi")
    disks = data.get("disks")
    if not isinstance(disks, list) or not 1 <= len(disks) <= MAX_DISKS:
        raise P2VError(f"send 1 to {MAX_DISKS} disks")
    clean = []
    for d in disks:
        name = str((d or {}).get("name", ""))
        size = int((d or {}).get("size_bytes", 0))
        if not DEVICE.fullmatch(name) or size < 1 << 20 or size > 64 << 40:
            raise P2VError(f"bad disk entry {d!r}")
        clean.append({"name": name, "size_bytes": size})
    macs = [str(m).lower() for m in data.get("macs", []) if MAC.fullmatch(str(m).lower())][:8]
    os_name = re.sub(r"[^\w .()/+-]", "", str(data.get("os", "")))[:80]
    return {
        "hostname": hostname,
        "cpus": cpus,
        "memory_mb": memory_mb,
        "firmware": firmware,
        "disks": clean,
        "macs": macs,
        "os": os_name,
    }


AGENT = Path(__file__).parent / "static" / "p2v-agent.sh"


def source_vm(session: Session):
    """The machine as a Migrate source VM (its disks are raw files in the pool)."""
    from ..hypervisor.base import Firmware
    from ..importer.sources import GIB, SourceVM

    m = session.machine
    return SourceVM(
        ref=session.id,
        name=m["hostname"],
        vcpus=m["cpus"],
        memory_mb=m["memory_mb"],
        firmware=Firmware.UEFI if m["firmware"] == "uefi" else Firmware.BIOS,
        disks_gb=[-(-d["size_bytes"] // GIB) for d in m["disks"]],
        nics=max(1, len(m["macs"])),
        state="running",  # the physical machine keeps its MACs: the VM gets new ones
        guest_os=m.get("os", ""),
        notes=f"P2V session {session.id}",
        disk_paths=[str(Path(session.folder) / f"disk{i}.raw") for i in range(len(m["disks"]))],
    )
