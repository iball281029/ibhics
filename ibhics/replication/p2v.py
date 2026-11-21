"""Push delta staging. Only ReplicaStore publishes a completed round."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
from pathlib import Path

from .providers import ChangeProvider, Disk, Point
from .store import CHUNK, ReplicaStore


def session_store(folder: str | Path, machine: dict) -> ReplicaStore:
    root = Path(folder) / "replica"
    return ReplicaStore(root / "metadata", [root / str(i) for i in range(len(machine["disks"]))])


class PushRound(ChangeProvider):
    tracking = "hash-delta"

    def __init__(self, folder, machine, hashes, previous):
        self.machine = machine
        disks = machine["disks"]
        if not isinstance(hashes, list) or len(hashes) != len(disks):
            raise ValueError("hashes must contain one list per disk")
        self.hashes = []
        self.needed = []
        self.received = set()
        self.baseline = previous.get("generation")
        zero_hashes = {}
        for i, disk in enumerate(disks):
            values = hashes[i]
            count = (disk["size_bytes"] + CHUNK - 1) // CHUNK
            if (
                not isinstance(values, list)
                or len(values) != count
                or any(
                    not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in values
                )
            ):
                raise ValueError("invalid SHA256 chunk list")
            old = previous.get("disks", [])
            prior = old[i] if i < len(old) else {}
            compatible = (
                previous.get("chunk") == CHUNK
                and prior.get("key") == disk["name"]
                and prior.get("size") == disk["size_bytes"]
            )
            old_hashes = prior.get("hashes", []) if compatible else []
            for length in (CHUNK, disk["size_bytes"] % CHUNK):
                if length and length not in zero_hashes:
                    zero_hashes[length] = hashlib.sha256(bytes(length)).hexdigest()
            self.hashes.append(list(values))
            self.needed.append(
                [
                    n
                    for n, digest in enumerate(values)
                    if (n >= len(old_hashes) or old_hashes[n] != digest)
                    and digest != zero_hashes[min(CHUNK, disk["size_bytes"] - n * CHUNK)]
                ]
            )
        self.id = secrets.token_hex(16)
        self._needed_sets = [set(chunks) for chunks in self.needed]
        self.folder = Path(folder) / ("round-" + self.id)
        self.folder.mkdir(mode=0o700)

    def upload(self, index: int, chunk: int, data: bytes):
        if not 0 <= index < len(self.needed) or chunk not in self._needed_sets[index]:
            raise ValueError("chunk was not requested")
        size = min(CHUNK, self.machine["disks"][index]["size_bytes"] - chunk * CHUNK)
        if len(data) != size or hashlib.sha256(data).hexdigest() != self.hashes[index][chunk]:
            raise ValueError("chunk length or SHA256 mismatch; source may have changed")
        path = self.folder / str(index)
        with path.open("r+b" if path.exists() else "w+b") as out:
            path.chmod(0o600)
            out.seek(chunk * CHUNK)
            out.write(data)
        self.received.add((index, chunk))

    def begin(self):
        if any((i, n) not in self.received for i, chunks in enumerate(self.needed) for n in chunks):
            raise ValueError("round has missing chunks")
        return Point(
            json.dumps(self.machine, sort_keys=True),
            tuple(Disk(d["name"], d["size_bytes"]) for d in self.machine["disks"]),
        )

    def prepare(self, prev_token):
        # The session lock prevents another push round replacing the baseline.
        return None

    def hash(self, disk, offset, length):
        i = next(i for i, d in enumerate(self.machine["disks"]) if d["name"] == disk.key)
        return self.hashes[i][offset // CHUNK]

    def read(self, disk, offset, length):
        i = next(i for i, d in enumerate(self.machine["disks"]) if d["name"] == disk.key)
        with (self.folder / str(i)).open("rb") as source:
            source.seek(offset)
            return source.read(length)

    def end(self):
        # Keep staging after an unsuccessful commit so missing chunks can be retried.
        pass

    def discard(self):
        shutil.rmtree(self.folder)
