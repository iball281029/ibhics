"""Reject a tracker attached to hidden local state after an HA bind mount."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

path = Path(__file__).parents[1] / "ibhics/storage_assets/nfs-tracking.py"
spec = importlib.util.spec_from_file_location("nfs_tracking", path)
tracking = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracking)


@pytest.mark.parametrize(
    "source,fstype,ok",
    [
        ("/dev/drbd1000[/nfs]", "ext4", True),
        ("/dev/sda1[/nfs]", "ext4", False),
        ("172.16.12.106:/nfs", "nfs4", False),
    ],
)
def test_state_must_live_on_the_replica(source, fstype, ok):
    result = SimpleNamespace(
        stdout=json.dumps(
            {"filesystems": [{"target": "/var/lib/nfs", "source": source, "fstype": fstype}]}
        )
    )
    if ok:
        tracking.shared_state_mount(lambda *a, **k: result)
    else:
        with pytest.raises(RuntimeError, match="non-HA"):
            tracking.shared_state_mount(lambda *a, **k: result)


def test_reject_hidden_database_after_state_mount_changes(tmp_path):
    state = tmp_path / "state"
    db = state / "nfsdcld/main.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"old local database")
    proc = tmp_path / "proc/123/fd"
    proc.mkdir(parents=True)
    # Opening before replacement simulates a bind mount hiding the old database.
    with db.open("rb"):
        fd = proc / "6"
        tracking.os.link(db, fd)
        db.rename(state / "hidden.sqlite")
        db.write_bytes(b"new shared database")
        real_readlink = tracking.os.readlink
        from unittest.mock import patch

        with patch.object(
            tracking.os, "readlink", side_effect=lambda p: str(db) if p == fd else real_readlink(p)
        ):
            with pytest.raises(RuntimeError, match="stale database"):
                tracking.tracking_files_match(
                    tmp_path / "proc",
                    state,
                    lambda *a, **k: SimpleNamespace(stdout="123"),
                )
    fd.unlink()
    fd.symlink_to(db)
    tracking.tracking_files_match(
        tmp_path / "proc", state, lambda *a, **k: SimpleNamespace(stdout="123")
    )
