"""Node-local storage labels shared by its manager and agent."""

from __future__ import annotations

import contextlib
import fcntl
import json
import threading
from pathlib import Path


class PoolLabels:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._values = {}
        self._lock = threading.RLock()

    def get(self, name: str) -> str:
        with self._lock:
            values = self._values
            if self.path and self.path.exists():
                values = json.loads(self.path.read_text())
            return values.get(name, "")

    def set(self, name: str, label: str) -> str:
        label = label.strip()
        if len(label) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise ValueError("storage display name must be at most 80 characters without controls")
        with self._lock, contextlib.ExitStack() as stack:
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                lock = stack.enter_context(self.path.with_suffix(".lock").open("a"))
                fcntl.flock(lock, fcntl.LOCK_EX)
                values = json.loads(self.path.read_text()) if self.path.exists() else {}
            else:
                values = self._values.copy()
            if label:
                values[name] = label
            else:
                values.pop(name, None)
            if self.path:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(values, ensure_ascii=False) + "\n")
                tmp.chmod(0o600)
                tmp.replace(self.path)
            self._values = values
        return label
