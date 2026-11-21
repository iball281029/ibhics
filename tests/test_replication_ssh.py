"""Snapshot command plans and secret handling, with no SSH or real hypervisors."""

import hashlib
import shlex
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ibhics.hypervisor import Firmware
from ibhics.importer import Source, SourceType, SourceVM
from ibhics.replication.ssh import SSH, SnapshotSSHProvider
from ibhics.replication.store import ReplicaStore

SOURCE = Source("src", SourceType.KVM, host="hypervisor", password="TOP-SECRET")
VM = SourceVM("vm", "vm", 2, 2048, Firmware.BIOS, [1], 1, state="running")
XML = """<domain><name>vm</name><memory unit='MiB'>2048</memory><vcpu>2</vcpu>
<devices><disk device='disk'><source file='/pool/disk;quoted.qcow2'/>
<target dev='vda'/></disk></devices></domain>"""


class Commands:
    def __init__(self, kind="libvirt", fail_export=False, existing=False, avail=100 * 1024**3):
        self.kind, self.fail_export, self.existing = kind, fail_export, existing
        self.avail = avail
        self.commands = []
        self.snapshot = None
        self.data = b"aaaabbbb"

    def command(self, command, binary=False):
        self.commands.append(command)
        words = shlex.split(command)
        if command == "command -v qm || true":
            return "/usr/sbin/qm" if self.kind == "proxmox" else ""
        if "snapshot-create" in words:
            import xml.etree.ElementTree as ET

            self.snapshot = ET.fromstring(words[2]).findtext("name")
            return ""
        if words[:2] == ["virsh", "dumpxml"]:
            return XML
        if words[:2] == ["virsh", "snapshot-list"]:
            return (self.snapshot or "") + "\nuser-snapshot\n"
        if words[:2] == ["virsh", "snapshot-delete"]:
            assert words[-1] == self.snapshot
            self.snapshot = None
            return ""
        if words[:2] == ["qm", "config"]:
            return "name: vm\nscsi0: storage:disk.qcow2,size=1G\n"
        if words[:2] == ["pvesm", "path"]:
            return "/pool/disk.qcow2"
        if words[:2] == ["qm", "snapshot"]:
            self.snapshot = words[3]
            return ""
        if words[:2] == ["qm", "listsnapshot"]:
            return f"user-snapshot\n{self.snapshot}"
        if words[:2] == ["qm", "delsnapshot"]:
            assert words[-1] == self.snapshot
            self.snapshot = None
            return ""
        if words[:2] == ["qemu-img", "info"]:
            return '{"format":"qcow2","virtual-size":8}'
        if words[:2] == ["qemu-img", "convert"]:
            assert self.snapshot in words[4]
            if self.fail_export:
                raise RuntimeError("export failed")
            return ""
        if words[0] == "df":
            return f"Avail\n{self.avail}\n"
        if words[0] == "stat":
            return "8"
        if words[0] == "dd":
            assert words[1].startswith("if=/")
            return self.data if binary else hashlib.sha256(self.data).hexdigest() + "  -\n"
        if words[0] in ("mkdir", "rmdir"):
            assert words[-1].startswith("/var/tmp/ibhics-repl-")
            if words[0] == "mkdir":
                assert words[1:3] == ["-m", "700"]
            return ""
        if words[0] == "rm":
            assert words[-1].startswith("/var/tmp/ibhics-repl-")
            return ""
        if words[:2] == ["vim-cmd", "vmsvc/snapshot.get"]:
            if self.existing:
                return "Snapshot Name : user-snapshot\nSnapshot Id : 1\n"
            return f"Snapshot Name : {self.snapshot}\nSnapshot Id : 9\n" if self.snapshot else ""
        if words[:2] == ["vim-cmd", "vmsvc/snapshot.create"]:
            self.snapshot = words[3]
            return ""
        if words[:2] == ["vim-cmd", "vmsvc/snapshot.remove"]:
            assert words[-2:] == ["9", "0"]
            self.snapshot = None
            return ""
        if words[0] == "cat":
            if words[1].endswith(".vmx"):
                return 'scsi0:0.fileName = "disk.vmdk"'
            return 'RW 8 VMFS "disk-flat.vmdk"'
        raise AssertionError(command)


@pytest.mark.parametrize("kind", ["libvirt", "proxmox", "esxi"])
def test_snapshot_lifecycle_and_hash_delta(tmp_path, kind):
    commands = Commands(kind)
    source, vm = SOURCE, VM
    if kind == "proxmox":
        vm = replace(vm, ref="101")
    if kind == "esxi":
        source = replace(source, type=SourceType.VMWARE, username="root")
        vm = replace(vm, ref="42", vmx="[datastore] vm/vm.vmx")
    provider = SnapshotSSHProvider(source, vm, commands)
    replica = ReplicaStore(tmp_path / "metadata", [tmp_path / "pool"])
    result = replica.sync(provider)
    assert result["bytes_transferred"] == 8
    assert commands.snapshot is None
    assert provider.name.startswith("ibhics-repl-")
    assert "TOP-SECRET" not in " ".join(commands.commands)
    # Snapshot creation never issues shutdown, power-off, revert or broad snapshot removal.
    assert not any(
        "power.off" in c or "snapshot.removeall" in c or "revert" in c for c in commands.commands
    )
    if kind == "libvirt":
        command = next(c for c in commands.commands if c.startswith("qemu-img info"))
        assert shlex.split(command)[-1] == "/pool/disk;quoted.qcow2"


def test_failed_begin_still_cleans_snapshot_and_partial_exports(tmp_path):
    commands = Commands(fail_export=True)
    provider = SnapshotSSHProvider(SOURCE, VM, commands)
    with pytest.raises(RuntimeError, match="export failed"):
        ReplicaStore(tmp_path / "metadata", [tmp_path / "pool"]).sync(provider)
    assert commands.snapshot is None
    assert any(c.startswith("rm -f -- /var/tmp/ibhics-repl-") for c in commands.commands)


def test_esxi_refuses_existing_snapshots_without_mutation(tmp_path):
    commands = Commands("esxi", existing=True)
    source = replace(SOURCE, type=SourceType.VMWARE, username="root")
    vm = replace(VM, ref="42", vmx="[ds] vm/vm.vmx")
    with pytest.raises(ValueError, match="existing ESXi snapshots"):
        ReplicaStore(tmp_path / "metadata", [tmp_path / "pool"]).sync(
            SnapshotSSHProvider(source, vm, commands)
        )
    assert len(commands.commands) == 1


def test_ssh_errors_never_echo_stderr_or_credentials():
    requests = []

    def run(argv, **kwargs):
        requests.append(argv)
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"password=TOP-SECRET")

    with pytest.raises(RuntimeError) as exc:
        SSH(SOURCE, run=run).command("true")
    assert "TOP-SECRET" not in str(exc.value)
    assert "TOP-SECRET" not in repr(requests)
    assert requests[0][-2:] == ["root@hypervisor", "true"]
    assert "StrictHostKeyChecking=yes" in requests[0]


def test_esxi_refuses_independent_disks_before_snapshot(tmp_path):
    class Independent(Commands):
        def command(self, command, binary=False):
            result = super().command(command, binary=binary)
            if command.startswith("cat ") and command.endswith(".vmx"):
                return result + '\nscsi0:0.mode = "independent-persistent"'
            return result

    commands = Independent("esxi")
    source = replace(SOURCE, type=SourceType.VMWARE, username="root")
    vm = replace(VM, ref="42", vmx="[ds] vm/vm.vmx")
    with pytest.raises(ValueError, match="Independent"):
        ReplicaStore(tmp_path / "metadata", [tmp_path / "pool"]).sync(
            SnapshotSSHProvider(source, vm, commands)
        )
    assert not any("snapshot.create" in c for c in commands.commands)


def test_refuses_to_fill_the_source_host():
    commands = Commands(avail=1024**3)
    provider = SnapshotSSHProvider(SOURCE, VM, commands)
    with pytest.raises(ValueError, match="free space"):
        provider.begin()
    assert commands.snapshot is None
    assert not any(c.startswith("qemu-img convert") for c in commands.commands)
