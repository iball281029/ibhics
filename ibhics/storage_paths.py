"""Reject bulk-data paths that would spill onto the system filesystem."""

from __future__ import annotations

import shutil
from os import fstat
from pathlib import Path

from .hypervisor import InvalidState


def require_data_storage(
    directory: Path, *, required_bytes: int = 0, file_fd: int | None = None
) -> Path:
    """Validate before mkdir; an absent mount must never become a root directory."""
    directory = Path(directory).absolute()
    existing = directory
    while not existing.exists():
        if existing.is_symlink() or existing.parent == existing:
            raise InvalidState("Migration/replication storage is unavailable")
        existing = existing.parent
    root_device = Path("/").stat().st_dev
    if not existing.is_dir() or existing.stat().st_dev == root_device:
        raise InvalidState("Migration/replication data storage is unavailable or on root")
    if file_fd is not None and fstat(file_fd).st_dev == root_device:
        raise InvalidState("Refusing to write migration data on the root filesystem")
    if required_bytes and shutil.disk_usage(existing).free < required_bytes:
        raise InvalidState("Not enough free space on migration/replication storage")
    return directory
