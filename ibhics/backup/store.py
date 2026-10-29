"""Key/value object stores that hold a backup repository: a directory, or an S3 bucket."""

from __future__ import annotations

import abc
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .targets import LOCAL_ROOT, BackupTarget, TargetType

MOUNT_ROOT = "/mnt/ibhics-backup"


class TargetUnavailable(RuntimeError):
    """The NAS, USB disk or bucket could not be reached or mounted."""


class Store(abc.ABC):
    """Flat keys such as 'chunks/ab/abcd...' map to bytes."""

    @abc.abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abc.abstractmethod
    def get(self, key: str) -> bytes: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def list(self, prefix: str) -> list[str]: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...


class DirStore(Store):
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"bad key {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix) if prefix else self.root
        if not base.is_dir():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in base.rglob("*")
            if p.is_file() and not p.name.startswith(".tmp-")
        )

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Store(Store):
    def __init__(self, client, bucket: str, prefix: str = "") -> None:
        self._s3 = client
        self._bucket = bucket
        self._prefix = f"{prefix.strip('/')}/" if prefix.strip("/") else ""

    @classmethod
    def from_target(cls, target: BackupTarget) -> S3Store:
        try:
            import boto3
        except ImportError:
            raise TargetUnavailable("S3 targets need boto3: pip install 'ibhics[s3]'") from None
        client = boto3.client(
            "s3",
            endpoint_url=target.endpoint,
            region_name=target.region,
            aws_access_key_id=target.access_key,
            aws_secret_access_key=target.secret_key,
        )
        return cls(client, target.bucket, target.prefix)

    def put(self, key: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=self._prefix + key, Body=data)

    def get(self, key: str) -> bytes:
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=self._prefix + key)["Body"].read()
        except self._s3.exceptions.NoSuchKey:
            raise KeyError(key) from None

    def exists(self, key: str) -> bool:
        resp = self._s3.list_objects_v2(Bucket=self._bucket, Prefix=self._prefix + key, MaxKeys=1)
        return any(obj["Key"] == self._prefix + key for obj in resp.get("Contents", []))

    def list(self, prefix: str) -> list[str]:
        keys = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix + prefix):
            keys.extend(obj["Key"][len(self._prefix) :] for obj in page.get("Contents", []))
        return sorted(keys)

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=self._prefix + key)


Runner = Callable[[list[str]], None]


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise TargetUnavailable(f"{cmd[0]} failed: {result.stderr.strip() or result.returncode}")


class Mounter:
    """Mounts NFS, SMB and USB targets for the length of a job, shared by concurrent jobs."""

    def __init__(self, run: Runner = _run, root: str = MOUNT_ROOT) -> None:
        self._run = run
        self._root = Path(root)
        self._lock = threading.Lock()
        self._users: dict[str, int] = {}

    @contextmanager
    def mounted(self, target: BackupTarget) -> Iterator[Path]:
        point = self._root / target.name
        with self._lock:
            if not self._users.get(target.name):
                point.mkdir(parents=True, exist_ok=True)
                self._mount(target, point)
            self._users[target.name] = self._users.get(target.name, 0) + 1
        try:
            yield point
        finally:
            with self._lock:
                self._users[target.name] -= 1
                if not self._users[target.name]:
                    self._run(["umount", str(point)])

    def _mount(self, target: BackupTarget, point: Path) -> None:
        if target.type == TargetType.NFS:
            self._run(["mount", "-t", "nfs", f"{target.host}:{target.export}", str(point)])
        elif target.type == TargetType.USB:
            self._run(["mount", target.device, str(point)])
        elif target.type == TargetType.SMB:
            # Credentials go in a root-only file so they never show up in the process list.
            fd, creds = tempfile.mkstemp(prefix="ibhics-smb-")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(f"username={target.username or 'guest'}\n")
                    f.write(f"password={target.password or ''}\n")
                    if target.domain:
                        f.write(f"domain={target.domain}\n")
                options = f"credentials={creds},vers=3.0"
                share = f"//{target.host}/{target.share}"
                self._run(["mount", "-t", "cifs", share, str(point), "-o", options])
            finally:
                os.unlink(creds)
        else:
            raise ValueError(f"{target.type} targets are not mounted")


@contextmanager
def open_store(
    target: BackupTarget, mounter: Mounter, s3_factory=S3Store.from_target
) -> Iterator[Store]:
    if target.type == TargetType.LOCAL:
        root = Path(target.path or f"{LOCAL_ROOT}/{target.name}")
        root.mkdir(parents=True, exist_ok=True)
        yield DirStore(root)
    elif target.type == TargetType.S3:
        yield s3_factory(target)
    else:
        with mounter.mounted(target) as point:
            root = point / target.prefix if target.prefix else point
            root.mkdir(parents=True, exist_ok=True)
            yield DirStore(root)
