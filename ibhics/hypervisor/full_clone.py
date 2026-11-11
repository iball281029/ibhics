"""Independent qcow2 copies accelerated by filesystem/NFS copy-on-write cloning."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

FICLONE = 0x40049409
UNSUPPORTED = {errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL, errno.ENOSYS}


def image_info(path):
    result = subprocess.run(
        ["qemu-img", "info", "--output=json", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        return None
    return json.loads(result.stdout)


def independent_image(info, capacity):
    return bool(
        info
        and info.get("format") == "qcow2"
        and info.get("virtual-size") == capacity
        and not info.get("backing-filename")
        and not info.get("full-backing-filename")
        and not info.get("snapshots")
    )


def try_reflink(source, destination, capacity, *, inspect=image_info, ioctl=fcntl.ioctl):
    """True on a durable independent copy; False permits the normal conversion path.

    A temporary sibling keeps the libvirt-created target untouched on unsupported
    filesystems. Never copy a backing-file dependency or internal snapshot history.
    """
    source, destination = Path(source), Path(destination)
    if not source.is_file() or not destination.is_file():
        return False
    if not independent_image(inspect(source), capacity):
        return False
    original = destination.stat()
    if source.samefile(destination):
        raise ValueError("Clone source and destination must be different files")
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=".ibhics-full-clone-", dir=destination.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as target, source.open("rb") as src:
            before = os.fstat(src.fileno())
            try:
                ioctl(target.fileno(), FICLONE, src.fileno())
            except OSError as exc:
                if exc.errno in UNSUPPORTED:
                    return False
                raise
            os.ftruncate(target.fileno(), before.st_size)
            current = os.fstat(target.fileno())
            if (current.st_uid, current.st_gid) != (original.st_uid, original.st_gid):
                os.fchown(target.fileno(), original.st_uid, original.st_gid)
            os.fchmod(target.fileno(), stat.S_IMODE(original.st_mode))
            os.fsync(target.fileno())
            after = os.fstat(src.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError("Source image changed during cloning")
        if not independent_image(inspect(temporary), capacity):
            raise RuntimeError("Reflink image verification failed")
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
