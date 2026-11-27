"""Key/value object stores that hold a backup repository: a directory, or an S3 bucket."""

from __future__ import annotations

import abc
import array
import errno
import fcntl
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .targets import LOCAL_ROOT, BackupTarget, TargetType

MOUNT_ROOT = "/mnt/ibhics-backup"


class TargetUnavailable(RuntimeError):
    """The NAS, USB disk or bucket could not be reached or mounted."""


class RepositoryBusy(RuntimeError):
    pass


class RepositoryLock:
    """Node/manager admission: two backups or one maintenance operation."""

    def __init__(self):
        self._condition = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0
        self.backup = _BackupLock(self)

    def acquire(self, blocking=True, timeout=-1, *, shared=False):
        deadline = time.monotonic() + timeout if timeout >= 0 else None
        with self._condition:
            if not shared:
                self._waiting_writers += 1
            try:
                while self._writer or (
                    self._readers >= 2 or self._waiting_writers
                    if shared else self._readers
                ):
                    if not blocking:
                        return False
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._condition.wait(remaining)
                if shared:
                    self._readers += 1
                else:
                    self._writer = True
                return True
            finally:
                if not shared:
                    self._waiting_writers -= 1
                    self._condition.notify_all()

    def release(self, *, shared=False):
        with self._condition:
            if shared:
                self._readers -= 1
            else:
                self._writer = False
            self._condition.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


class _BackupLock:
    def __init__(self, lock):
        self._lock = lock

    def acquire(self, blocking=True, timeout=-1):
        return self._lock.acquire(blocking, timeout, shared=True)

    def release(self):
        self._lock.release(shared=True)


class Store(abc.ABC):
    """Flat keys such as 'chunks/ab/abcd...' map to bytes."""

    @contextmanager
    def _gate(self):
        """Atomic admission gate; all repository clients must use this lock protocol."""
        with self._lease_mutex:
            if getattr(self._lease_state, "depth", 0):
                self._lease_state.depth += 1
                try:
                    yield
                finally:
                    self._lease_state.depth -= 1
                return
            token = uuid4().hex.encode()
            self._acquire_lease(token)
            self._lease_state.depth = 1
            try:
                yield
            finally:
                self._lease_state.depth = 0
                # An operator must never clear an active lease. Do not delete a replacement.
                if self.get(".repository-lease") == token:
                    self.delete(".repository-lease")

    @contextmanager
    def exclusive(self):
        """Keep all backups out for the whole maintenance operation."""
        if getattr(self._lease_state, "shared_depth", 0):
            raise RepositoryBusy("Cannot upgrade an active backup lease to exclusive")
        with self._gate():
            if any(self.exists(f".repository-backup-{slot}") for slot in range(2)):
                raise RepositoryBusy("Repository has active or uncleared backup leases")
            yield

    @contextmanager
    def shared(self):
        """Two durable backup slots, allocated under the cross-node atomic gate.

        No expiry: a crashed holder requires operator review, just like exclusive.
        Exclusive holders may nest backup work without allocating a separate slot.
        """
        state = self._lease_state
        if getattr(state, "depth", 0) or getattr(state, "shared_depth", 0):
            state.shared_depth = getattr(state, "shared_depth", 0) + 1
            try:
                yield
            finally:
                state.shared_depth -= 1
            return
        token = uuid4().hex.encode()
        with self._gate():
            key = next(
                (f".repository-backup-{slot}" for slot in range(2)
                 if not self.exists(f".repository-backup-{slot}")), None
            )
            if key is None:
                raise RepositoryBusy("Both repository backup slots are occupied")
            self.put(key, token)
        state.shared_depth = 1
        try:
            yield
        finally:
            state.shared_depth = 0
            # Slot ownership alone permits deletion; admission always uses the gate.
            if self.get(key) == token:
                self.delete(key)

    @contextmanager
    def waiting(self, shared=False, check_cancel=lambda: None):
        """Wait for cross-node admission; never infer stale ownership from time."""
        while True:
            check_cancel()
            context = (self.shared if shared else self.exclusive)()
            try:
                context.__enter__()
                break
            except RepositoryBusy:
                time.sleep(0.25)
        try:
            check_cancel()
            yield
        finally:
            context.__exit__(None, None, None)

    def protect(self, key, until) -> bool:
        """Best effort physical protection; False means application soft lock only."""
        return False

    def release(self, key) -> None:
        """Called only after retention expires and no restore point references the key."""
        return None

    def put_chunk(self, key: str, data: bytes) -> bool:
        """Publish a content-addressed object without replacing peer retention."""
        self.put(key, data)
        return True

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
        self.root = Path(root).resolve()
        self._lease_mutex = threading.RLock()
        self._immutable_unsupported_devices: set[int] = set()
        self._lease_state = threading.local()

    def _acquire_lease(self, token):
        path = self._path(".repository-lease")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise RepositoryBusy("Repository is busy or has an uncleared lease") from None
        with os.fdopen(fd, "wb") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"bad key {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        self._put(key, data, only_absent=False)

    def put_chunk(self, key: str, data: bytes) -> bool:
        return self._put(key, data, only_absent=True)

    def _put(self, key, data, only_absent):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if only_absent:
                # Concurrent backups may discover the same missing digest. Publish
                # once, preserving any inode/retention already protected by a peer.
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    return False
                finally:
                    Path(tmp).unlink(missing_ok=True)
            else:
                os.replace(tmp, path)
            return True
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

    def _immutable(self, key, enabled):
        # Linux FS_IOC_*FLAGS: preserve unrelated flags and avoid a chattr process
        # per deduplicated chunk. NFS rejects this ioctl; cache only that explicit
        # capability failure, per filesystem device (not permission errors).
        device = None
        try:
            with self._path(key).open("rb") as stream:
                device = os.fstat(stream.fileno()).st_dev
                if device in self._immutable_unsupported_devices:
                    return False
                flags = array.array("L", [0])
                fcntl.ioctl(stream.fileno(), 0x80086601, flags, True)
                old = flags[0]
                flags[0] = old | 0x10 if enabled else old & ~0x10
                if flags[0] != old:
                    fcntl.ioctl(stream.fileno(), 0x40086602, flags, True)
                return True
        except OSError as exc:
            if device is not None and exc.errno in (errno.ENOTTY, errno.EOPNOTSUPP, errno.ENOSYS):
                self._immutable_unsupported_devices.add(device)
            return False

    def protect(self, key, until) -> bool:
        return self._immutable(key, True)

    def release(self, key) -> None:
        self._immutable(key, False)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Store(Store):
    def __init__(self, client, bucket: str, prefix: str = "", storage_class="STANDARD") -> None:
        self._s3 = client
        self._bucket = bucket
        self._prefix = f"{prefix.strip('/')}/" if prefix.strip("/") else ""
        self._storage_class = storage_class
        self._lease_mutex = threading.RLock()
        self._lease_state = threading.local()

    def _acquire_lease(self, token):
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=self._prefix + ".repository-lease",
                Body=token,
                IfNoneMatch="*",
            )
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in (409, 412):
                raise RepositoryBusy("Repository is busy or has an uncleared lease") from exc
            raise TargetUnavailable(
                "Repository locking requires conditional S3 PutObject: " + str(exc)
            ) from exc

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
        return cls(client, target.bucket, target.prefix, target.storage_class)

    def put(self, key: str, data: bytes) -> None:
        self._put(key, data, only_absent=False)

    def put_chunk(self, key: str, data: bytes) -> bool:
        return self._put(key, data, only_absent=True)

    def _put(self, key, data, only_absent):
        # Keep manifests/locks hot. Only chunk payloads use the archive storage class.
        options = (
            {"StorageClass": self._storage_class}
            if (self._storage_class != "STANDARD" and key.startswith(("chunks/", "fulls/")))
            else {}
        )
        if only_absent:
            options["IfNoneMatch"] = "*"
        try:
            self._s3.put_object(Bucket=self._bucket, Key=self._prefix + key, Body=data, **options)
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if only_absent and status == 412:
                return False
            raise
        return True

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

    def protect(self, key, until) -> bool:
        try:
            config = self._s3.get_object_lock_configuration(Bucket=self._bucket)
            if config.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") != "Enabled":
                return False
            head = self._s3.head_object(Bucket=self._bucket, Key=self._prefix + key)
            # Never shorten a shared chunk's retention, or retain a delete marker.
            until = max(until, head.get("ObjectLockRetainUntilDate", until))
            self._s3.put_object_retention(
                Bucket=self._bucket,
                Key=self._prefix + key,
                VersionId=head["VersionId"],
                Retention={"Mode": "COMPLIANCE", "RetainUntilDate": until},
            )
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=self._prefix + key)


# run(cmd, ok_codes) returns stdout; any other exit code raises TargetUnavailable.
Runner = Callable[..., str]


def _run(cmd: list[str], ok_codes: tuple[int, ...] = (0,)) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in ok_codes:
        raise TargetUnavailable(f"{cmd[0]} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def iscsi_device(target: BackupTarget) -> str:
    return f"/dev/disk/by-path/ip-{target.host}:{target.port}-iscsi-{target.iqn}-lun-{target.lun}"


class Mounter:
    """Attaches NAS, SAN and USB targets for the length of a job and detaches them after,
    so between jobs nothing on the node (ransomware included) can reach the backups.
    Concurrent jobs on one target share the attachment."""

    def __init__(self, run: Runner = _run, root: str = MOUNT_ROOT) -> None:
        self._run = run
        self._root = Path(root)
        self._sessions = self._root / ".iscsi-sessions"
        self._lock = threading.Lock()
        self._users: dict[str, int] = {}

    def cleanup(self, mounts_file: str = "/proc/mounts") -> list[str]:
        """Detach whatever a crash or power loss left attached. Run when the node starts."""
        detached = []
        root = str(self._root.resolve()) + "/"
        with self._lock:
            try:
                lines = Path(mounts_file).read_text().splitlines()
            except OSError:
                lines = []
            for line in lines:
                point = line.split()[1] if len(line.split()) > 1 else ""
                point = point.replace("\\040", " ")
                if point.startswith(root):
                    self._run(["umount", "-l", point])
                    detached.append(point)
            if self._sessions.is_dir():
                for record in self._sessions.iterdir():
                    iqn, portal = record.read_text().split()
                    self._run(
                        ["iscsiadm", "-m", "node", "-T", iqn, "-p", portal, "--logout"],
                        ok_codes=(0, 21),
                    )
                    record.unlink()
                    detached.append(iqn)
        return detached

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
                    try:
                        self._run(["umount", str(point)])
                    finally:
                        if target.type == TargetType.ISCSI:
                            self._iscsi_logout(target)

    def _mount(self, target: BackupTarget, point: Path) -> None:
        if target.type == TargetType.NFS:
            self._run(["mount", "-t", "nfs", f"{target.host}:{target.export}", str(point)])
        elif target.type == TargetType.USB:
            self._run(["mount", target.device, str(point)])
        elif target.type == TargetType.FC:
            self._mount_block(target, target.device, point)
        elif target.type == TargetType.ISCSI:
            portal = f"{target.host}:{target.port}"
            self._run(["iscsiadm", "-m", "discovery", "-t", "sendtargets", "-p", portal])
            # Note the session first, so cleanup() can log out if the node dies mid-job.
            self._sessions.mkdir(parents=True, exist_ok=True)
            (self._sessions / target.name).write_text(f"{target.iqn} {portal}\n")
            # 15: already logged in (another job, or a session left from before)
            try:
                self._run(
                    ["iscsiadm", "-m", "node", "-T", target.iqn, "-p", portal, "--login"],
                    ok_codes=(0, 15),
                )
            except BaseException:
                (self._sessions / target.name).unlink(missing_ok=True)
                raise
            try:
                self._run(["udevadm", "settle"])
                self._mount_block(target, iscsi_device(target), point)
            except BaseException:
                self._iscsi_logout(target)
                raise
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

    def _mount_block(self, target: BackupTarget, device: str, point: Path) -> None:
        # blkid exits 2 when it finds no filesystem.
        fstype = self._run(["blkid", "-p", "-o", "value", "-s", "TYPE", device], ok_codes=(0, 2))
        if not fstype.strip():
            if not target.confirm_wipe:
                raise TargetUnavailable(
                    f"{device} has no filesystem; set confirm_wipe to format it for backups"
                )
            self._run(["mkfs.xfs", "-q", device])
        self._run(["mount", device, str(point)])

    def _iscsi_logout(self, target: BackupTarget) -> None:
        portal = f"{target.host}:{target.port}"
        # 21: no session to log out of
        self._run(
            ["iscsiadm", "-m", "node", "-T", target.iqn, "-p", portal, "--logout"],
            ok_codes=(0, 21),
        )
        (self._sessions / target.name).unlink(missing_ok=True)


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
