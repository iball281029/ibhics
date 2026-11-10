"""Durable, single-operation storage jobs; a restart never replays disk erasure."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path


class StorageJobs:
    def __init__(self, root: Path):
        self.root = root
        self._mutex = threading.Lock()

    def _save(self, job: dict) -> None:
        path = self.root / "current.json"
        temporary = self.root / "current.tmp"
        with temporary.open("w") as stream:
            os.chmod(temporary, 0o600)
            json.dump(job, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def current(self) -> dict | None:
        path = self.root / "current.json"
        if not path.exists():
            return None
        job = json.loads(path.read_text())
        # A crashed process cannot leave the UI displaying "running" forever.
        if job["state"] in ("queued", "running") and job.get("pid") != os.getpid():
            with (self.root / "run.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return job
                job.update(
                    state="interrupted",
                    error="Service restarted; inspect the storage job "
                    "before retrying. No destructive operation has been replayed.",
                )
        return job

    def start(self, kind: str, payload: dict, work) -> dict:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (self.root / "run.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError("Another storage operation is running") from None
        now = datetime.now(UTC).isoformat()
        job = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "state": "queued",
            "step": "Queued",
            "created": now,
            "finished": None,
            "error": None,
            "result": None,
            "payload": payload,
            "pid": os.getpid(),
        }
        try:
            self._save(job)

            def update(step: str) -> None:
                with self._mutex:
                    job.update(step=step)
                    self._save(job)

            def perform() -> None:
                try:
                    job.update(state="running")
                    self._save(job)
                    result = work(update)
                    job.update(state="done", step="Completed", result=result)
                except Exception as exc:
                    job.update(state="failed", error=f"{type(exc).__name__}: {exc}")
                finally:
                    job["finished"] = datetime.now(UTC).isoformat()
                    try:
                        self._save(job)
                        archive = self.root / (job["id"] + ".json")
                        archive.write_text(json.dumps(job))
                        archive.chmod(0o600)
                    finally:
                        lock.close()

            threading.Thread(target=perform, daemon=True, name="ibhics-storage-job").start()
            return dict(job)
        except Exception:
            lock.close()
            raise
