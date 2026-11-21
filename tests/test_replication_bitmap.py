"""Offline NBD pull, bitmap interpretation, fallback and checkpoint lifecycle."""

import json
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ibhics.replication.bitmap import KVMPullProvider, dirty_extents
from ibhics.replication.store import CHUNK, ReplicaStore
from tests.test_replication_ssh import SOURCE, VM, Commands


class PullCommands:
    def __init__(self, backing="file"):
        source = {
            "file": "<source file='/pool/disk.raw'/>",
            "lvm": "<source dev='/dev/vg/volume'/>",
            "zfs": "<source dev='/dev/zvol/pool/volume'/>",
            "rbd": "<source protocol='rbd' name='pool/disk'><host name='ceph'/></source>",
        }[backing]
        self.xml = (
            "<domain><name>vm</name><memory unit='MiB'>2048</memory>"
            "<vcpu>2</vcpu><devices><disk device='disk'>"
            + source
            + "<target dev='vda'/></disk></devices>"
            "<seclabel model='dac'><label>+107:+107</label></seclabel></domain>"
        )
        self.commands, self.reads, self.deleted = [], [], []
        self.files, self.checkpoints = {}, set()
        self.active = None
        self.data = b"a" * CHUNK + b"b" * CHUNK
        self.fail = None
        self.avail = 100 * 1024**3

    def command(self, command, binary=False):
        self.commands.append(command)
        words = shlex.split(command)
        if command == "command -v qm || true":
            return ""
        if words[0] == "umask":
            self.files[words[-1]] = ET.fromstring(words[-3])
            return ""
        if words[:2] == ["virsh", "dumpxml"]:
            return self.xml
        if words[:2] == ["virsh", "domblkinfo"]:
            return f"Capacity: {len(self.data)}\nAllocation: 1024\nPhysical: 1024\n"
        if words[:2] == ["virsh", "backup-dumpxml"]:
            if self.active is None:
                raise RuntimeError("no backup job")
            return ET.tostring(self.active, encoding="unicode")
        if words[:2] == ["virsh", "backup-begin"]:
            assert self.active is None
            backup = self.files[words[3]]
            checkpoint = self.files[words[5]].findtext("name") if len(words) > 4 else None
            if checkpoint:
                self.checkpoints.add(checkpoint)
            if self.fail == "checkpoint" and checkpoint:
                raise RuntimeError("checkpoint unsupported TOP-SECRET")
            if self.fail == "incremental" and backup.find("incremental") is not None:
                raise RuntimeError("bitmap reset TOP-SECRET")
            self.active = backup
            if self.fail == "timeout":
                raise RuntimeError("backup-begin timed out TOP-SECRET")
            return ""
        if words[:2] == ["virsh", "domjobabort"]:
            assert self.active is not None
            assert "ibhics-repl-" in self.active.find("server").get("socket")
            self.active = None
            return ""
        if words[:2] == ["virsh", "checkpoint-list"]:
            return "\n".join(sorted(self.checkpoints))
        if words[:2] == ["virsh", "checkpoint-delete"]:
            self.checkpoints.remove(words[3])
            self.deleted.append(words[3])
            return ""
        if words[:2] == ["qemu-img", "map"]:
            options = json.loads(words[-1][5:])
            assert options["x-dirty-bitmap"] == "qemu:dirty-bitmap:ibhics"
            assert self.active.find("incremental") is not None
            if self.fail == "map":
                return "[]"
            return json.dumps(
                [
                    {"start": 0, "length": CHUNK, "data": True, "zero": False},
                    {"start": CHUNK, "length": CHUNK, "data": False, "zero": False},
                ]
            )
        if words[:2] == ["qemu-img", "info"]:
            return json.dumps({"virtual-size": len(self.data)})
        if words[:2] == ["qemu-img", "dd"]:
            assert self.active is not None
            opts = dict(w.split("=", 1) for w in words[2:])
            image = json.loads(opts["if"][5:])
            assert image["driver"] == "raw" and image["file"]["driver"] == "nbd"
            assert "skip" not in opts
            offset = image["offset"]
            self.reads.append(offset)
            if self.fail == "read":
                raise RuntimeError("NBD read failed")
            length = min(image["size"], int(opts["count"]) * int(opts["bs"]))
            self.files[opts["of"]] = self.data[offset : offset + length]
            return ""
        if words[0] == "cat":
            assert binary
            return self.files[words[-1]]
        if words[0] == "df":
            return f"Avail\n{self.avail}\n"
        if words[0] == "rm":
            self.files.pop(words[-1], None)
            return ""
        if words[0] == "rmdir":
            assert self.active is None
            assert not any(path.startswith(words[-1]) for path in self.files)
            return ""
        if words[0] == "mkdir":
            assert words[1:3] == ["-m", "700"]
            return ""
        if words[0] == "chown":
            assert words[1] == "107:107"
            assert words[-1].startswith("/var/tmp/ibhics-repl-")
            return ""
        raise AssertionError(command)


@pytest.mark.parametrize("backing", ["file", "lvm", "zfs", "rbd"])
def test_baseline_then_bitmap_only_reads_changed_chunk(tmp_path, backing):
    commands = PullCommands(backing)
    store = ReplicaStore(tmp_path / "meta", [tmp_path / "disk"])
    first = KVMPullProvider(SOURCE, VM, commands)
    initial = store.sync(first)
    assert commands.checkpoints == {first.name}
    assert commands.active is None and not commands.files
    assert initial["change_tracking"] == "hash-delta"
    commands.reads.clear()
    commands.data = b"a" * CHUNK + bytes(CHUNK)  # changed-to-zero is dirty too
    second = KVMPullProvider(SOURCE, VM, commands)
    result = store.sync(second)
    assert commands.reads == [CHUNK]
    assert result["change_tracking"] == "dirty-bitmap"
    assert result["fallback_reason"] is None
    assert result["bytes_transferred"] == CHUNK
    assert Path(result["disks"][0]["path"]).read_bytes() == commands.data
    assert commands.checkpoints == {second.name}
    assert first.name in commands.deleted
    assert commands.active is None and not commands.files
    assert "TOP-SECRET" not in json.dumps(result)
    assert "TOP-SECRET" not in " ".join(commands.commands)


@pytest.mark.parametrize("failure", ["checkpoint", "incremental", "map", "bad-token"])
def test_full_pull_fallback_resets_invalid_incremental_exports(tmp_path, failure):
    commands = PullCommands("lvm")
    store = ReplicaStore(tmp_path / "meta", [tmp_path / "disk"])
    first = store.sync(KVMPullProvider(SOURCE, VM, commands))
    commands.reads.clear()
    commands.data = b"c" * CHUNK + b"d" * CHUNK
    if failure == "bad-token":
        first["token"] = "old-token"
        (store.root / "current.json").write_text(json.dumps(first))
    else:
        commands.fail = failure
    result = store.sync(KVMPullProvider(SOURCE, VM, commands))
    assert commands.reads == [0, CHUNK]
    assert result["change_tracking"] == "hash-delta"
    assert result["fallback_reason"] and "TOP-SECRET" not in json.dumps(result)
    assert Path(result["disks"][0]["path"]).read_bytes() == commands.data
    assert commands.active is None and not commands.files


def test_failed_read_keeps_committed_checkpoint_and_manifest(tmp_path):
    commands = PullCommands()
    store = ReplicaStore(tmp_path / "meta", [tmp_path / "disk"])
    first = KVMPullProvider(SOURCE, VM, commands)
    initial = store.sync(first)
    commands.fail = "read"
    second = KVMPullProvider(SOURCE, VM, commands)
    with pytest.raises(RuntimeError, match="NBD read failed"):
        store.sync(second)
    assert store.current() == initial
    assert commands.checkpoints == {first.name}
    assert second.name in commands.deleted
    assert commands.active is None and not commands.files


def test_final_partial_chunk_uses_bounded_raw_nbd_view(tmp_path):
    commands = PullCommands()
    commands.data = b"a" * CHUNK + b"b" * 512
    result = ReplicaStore(tmp_path / "meta", [tmp_path / "disk"]).sync(
        KVMPullProvider(SOURCE, VM, commands)
    )
    assert commands.reads == [0, CHUNK]
    assert Path(result["disks"][0]["path"]).read_bytes() == commands.data


def test_manifest_failure_discards_uncommitted_checkpoint(tmp_path, monkeypatch):
    from ibhics.replication import store as module

    commands = PullCommands()
    store = ReplicaStore(tmp_path / "meta", [tmp_path / "disk"])
    first = KVMPullProvider(SOURCE, VM, commands)
    initial = store.sync(first)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(module, "durable_json", fail)
    with pytest.raises(OSError, match="disk full"):
        store.sync(KVMPullProvider(SOURCE, VM, commands))
    assert store.current() == initial
    assert commands.checkpoints == {first.name}


def test_error_after_manifest_replace_preserves_published_checkpoint(tmp_path, monkeypatch):
    from ibhics.replication import store as module

    commands = PullCommands()
    store = ReplicaStore(tmp_path / "meta", [tmp_path / "disk"])
    first = KVMPullProvider(SOURCE, VM, commands)
    initial = store.sync(first)
    write = module.durable_json

    def published_then_failed(path, data):
        write(path, data)
        raise OSError("directory fsync failed after replace")

    monkeypatch.setattr(module, "durable_json", published_then_failed)
    second = KVMPullProvider(SOURCE, VM, commands)
    with pytest.raises(OSError, match="after replace"):
        store.sync(second)
    assert store.current()["generation"] != initial["generation"]
    assert commands.checkpoints == {second.name}
    assert Path(store.current()["disks"][0]["path"]).read_bytes() == commands.data


def test_block_backing_without_pull_is_rejected_with_reason(tmp_path):
    class NoPull(PullCommands):
        def command(self, command, binary=False):
            if shlex.split(command)[:2] == ["virsh", "backup-begin"]:
                raise RuntimeError("backup API unavailable")
            return super().command(command, binary)

    commands = NoPull("lvm")
    with pytest.raises(ValueError, match="NBD pull unavailable.*file-backed qcow2"):
        ReplicaStore(tmp_path / "meta", [tmp_path / "disk"]).sync(
            KVMPullProvider(SOURCE, VM, commands)
        )
    assert commands.active is None and not commands.checkpoints and not commands.files


def test_timeout_after_backup_creation_still_aborts_owned_job(tmp_path, monkeypatch):
    commands = PullCommands()
    commands.fail = "timeout"
    instance = KVMPullProvider(SOURCE, VM, commands)

    def unavailable():
        raise ValueError("legacy snapshot unavailable")

    monkeypatch.setattr(instance.legacy, "begin", unavailable)
    with pytest.raises(ValueError, match="legacy snapshot unavailable"):
        ReplicaStore(tmp_path / "meta", [tmp_path / "disk"]).sync(instance)
    assert commands.active is None and not commands.files and not commands.checkpoints
    assert sum("domjobabort" in c for c in commands.commands) == 3


def test_unavailable_pull_falls_back_to_owned_qcow2_snapshot(tmp_path):
    class OldLibvirt(Commands):
        def command(self, command, binary=False):
            words = shlex.split(command)
            if words[:2] == ["virsh", "domblkinfo"]:
                return "Capacity: 8\n"
            if words[:2] in (["virsh", "backup-dumpxml"], ["virsh", "backup-begin"]):
                raise RuntimeError("backup API unsupported")
            if words[:2] == ["virsh", "checkpoint-list"]:
                return ""
            if words[0] == "umask":
                return ""
            return super().command(command, binary)

    commands = OldLibvirt()
    result = ReplicaStore(tmp_path / "meta", [tmp_path / "disk"]).sync(
        KVMPullProvider(SOURCE, VM, commands)
    )
    assert result["change_tracking"] == "hash-delta"
    assert "NBD pull unavailable" in result["fallback_reason"]
    assert commands.snapshot is None


def test_refuses_existing_backup_without_aborting_it(tmp_path):
    commands = PullCommands()
    commands.active = ET.fromstring("<domainbackup><server socket='/user/backup'/></domainbackup>")
    with pytest.raises(ValueError, match="existing libvirt backup"):
        ReplicaStore(tmp_path / "meta", [tmp_path / "disk"]).sync(
            KVMPullProvider(SOURCE, VM, commands)
        )
    assert not any("domjobabort" in c or "backup-begin" in c for c in commands.commands)


def test_refuses_insufficient_scratch_space(tmp_path):
    commands = PullCommands()
    commands.avail = 1024**3
    with pytest.raises(ValueError, match="free space"):
        ReplicaStore(tmp_path / "meta", [tmp_path / "disk"]).sync(
            KVMPullProvider(SOURCE, VM, commands)
        )
    assert not any("backup-begin" in c for c in commands.commands)


def test_proxmox_legacy_fallback_has_per_cycle_reason(tmp_path):
    from dataclasses import replace

    commands = Commands("proxmox")
    result = ReplicaStore(tmp_path / "meta", [tmp_path / "disk"]).sync(
        KVMPullProvider(SOURCE, replace(VM, ref="101"), commands)
    )
    assert result["change_tracking"] == "hash-delta"
    assert "Proxmox" in result["fallback_reason"]
    assert commands.snapshot is None


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"start": 1, "length": 7, "data": False, "zero": False}],
        [{"start": 0, "length": 9, "data": False, "zero": False}],
        [{"start": 0, "length": 8, "data": False}],
        [{"start": 0, "length": 8, "data": False, "zero": True}],
        [{"start": 0, "length": 8, "data": 0, "zero": False}],
    ],
)
def test_ambiguous_or_incomplete_maps_are_never_unchanged(entries):
    with pytest.raises(ValueError):
        dirty_extents(entries, 8)
