"""Coordinate node backup work with the application's installer."""

import fcntl
import sys
import tempfile
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .store import RepositoryBusy

GUARD_PATH = (
    Path("/run/lock/ibhics-replication.lock")
    if sys.platform == "linux"
    else Path(tempfile.gettempdir()) / "ibhics-replication.lock"
)


@contextmanager
def upgrade_guard(path=None):
    path = path or GUARD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RepositoryBusy("Application upgrade is running; retry later") from None
        try:
            yield
        finally:
            fcntl.flock(guard, fcntl.LOCK_UN)


def protected(operation):
    @wraps(operation)
    def run(*args, **kwargs):
        with upgrade_guard():
            return operation(*args, **kwargs)

    return run
