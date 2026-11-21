"""SSH hash delta from explicit snapshots. No SSH key installation or service changes.

KVM/Proxmox currently require file-backed qcow2 disks with internal snapshots.
Snapshot content is flattened on the source into an owned temporary raw export; only
changed chunks cross SSH. ESXi reads the frozen base flat disks after a snapshot and
refuses pre-existing chains. Production hardware validation is still required.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import subprocess
import uuid
from urllib.parse import unquote

from ..importer.convert import SSH_KEY, VMX_DISK, vmdk_flat_file, vmx_disk_files, vmx_ssh_url
from ..importer.kvm import parse_libvirt_domain, parse_qm_config
from ..importer.sources import Source, SourceType, SourceVM
from .providers import ChangeProvider, Disk, Point


class SSH:
    """Existing key only; remote diagnostics can contain secrets, so never echo them."""

    def __init__(self, source: Source, run=subprocess.run):
        source.validate()
        self.source, self.run = source, run

    def command(self, command: str, binary: bool = False):
        argv = [
            "ssh",
            "-i",
            str(SSH_KEY),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            f"root@{self.source.host}",
            command,
        ]
        try:
            result = self.run(argv, capture_output=True, timeout=3600)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("Replication SSH command could not complete") from None
        if result.returncode:
            raise RuntimeError("Replication SSH command failed; check source snapshot support")
        return result.stdout if binary else result.stdout.decode("utf-8", errors="strict")


class SnapshotSSHProvider(ChangeProvider):
    def __init__(self, source: Source, vm: SourceVM, ssh=None):
        self.source, self.vm = source, vm
        self.ssh = ssh or SSH(source)
        self.name = "ibhics-repl-" + uuid.uuid4().hex
        self.paths: dict[str, str] = {}
        self.descriptors: dict[str, str] = {}
        self.exports: list[str] = []
        self.export_dir: str | None = None
        self.kind = ""
        self.snapshot_attempted = False
        self.esxi_snapshot_id: str | None = None

    def command(self, *args: str) -> str:
        return self.ssh.command(shlex.join(args))

    def begin(self) -> Point:
        if self.source.type == SourceType.KVM:
            return self._kvm()
        if self.source.type == SourceType.VMWARE:
            return self._esxi()
        raise ValueError("source has no SSH snapshot replication provider")

    def _kvm(self) -> Point:
        proxmox = bool(self.ssh.command("command -v qm || true").strip())
        if proxmox:
            if not self.vm.ref.isdecimal():
                raise ValueError("Proxmox VM reference must be numeric")
            _, volumes = parse_qm_config(self.vm.ref, self.command("qm", "config", self.vm.ref))
            paths = [self.command("pvesm", "path", volume).strip() for volume in volumes]
            self.kind = "proxmox"
        else:
            _, paths = parse_libvirt_domain(self.command("virsh", "dumpxml", self.vm.ref))
            self.kind = "libvirt"
            import xml.etree.ElementTree as ET

            domain = ET.fromstring(self.command("virsh", "dumpxml", self.vm.ref))
            if any(
                d.find("source") is None or not d.find("source").get("file")
                for d in domain.findall("./devices/disk[@device='disk']")
            ):
                raise ValueError(
                    "NBD pull unavailable; snapshot fallback requires file-backed qcow2 disks"
                )
        if not paths:
            raise ValueError("VM has no snapshot-capable disks")
        needed = 0
        for path in paths:
            # Do not guess snapshot paths for LVM, ZFS, RBD or raw disks.
            if not path.startswith("/") or path.startswith("/dev/"):
                raise ValueError(
                    "NBD pull unavailable; snapshot fallback requires file-backed qcow2 disks"
                )
            info = json.loads(self.command("qemu-img", "info", "-U", "--output=json", path))
            if info.get("format") != "qcow2":
                raise ValueError(
                    "NBD pull unavailable; snapshot fallback requires file-backed qcow2 disks"
                )
            needed += int(info.get("virtual-size") or 0)
        # The snapshot is exported to /var/tmp on the source host. Never fill the
        # source's filesystem: keep 10% (at least 2 GiB) free after the export.
        avail = int(self.command("df", "-B1", "--output=avail", "/var/tmp").split()[-1])
        if avail - needed < max(needed // 10, 2 * 1024**3):
            raise ValueError(
                "Not enough free space in /var/tmp on the source host for the snapshot export"
            )
        self.snapshot_attempted = True
        if proxmox:
            self.command("qm", "snapshot", self.vm.ref, self.name, "--vmstate", "0")
        else:
            # Default libvirt system checkpoint includes RAM; --disk-only defaults to
            # external snapshots. An explicit XML requests internal disk snapshots only.
            import xml.etree.ElementTree as ET

            domain = ET.fromstring(self.command("virsh", "dumpxml", self.vm.ref))
            root = ET.Element("domainsnapshot")
            ET.SubElement(root, "name").text = self.name
            ET.SubElement(root, "memory", snapshot="no")
            disks = ET.SubElement(root, "disks")
            for disk in domain.findall("./devices/disk"):
                target = disk.find("target")
                if target is not None:
                    ET.SubElement(
                        disks,
                        "disk",
                        name=target.get("dev"),
                        snapshot="internal" if disk.get("device") == "disk" else "no",
                    )
            xml = ET.tostring(root, encoding="unicode")
            # /dev/stdin is read by remote virsh. Every variable is independently quoted.
            self.ssh.command(
                f"printf %s {shlex.quote(xml)} | "
                + shlex.join(["virsh", "snapshot-create", self.vm.ref, "/dev/stdin", "--atomic"])
            )
        export_dir = f"/var/tmp/{self.name}"
        self.command("mkdir", "-m", "700", "--", export_dir)
        self.export_dir = export_dir
        result = []
        for index, path in enumerate(paths):
            export = f"{export_dir}/{index}.raw"
            self.exports.append(export)
            self.command(
                "qemu-img",
                "convert",
                "-U",
                "-l",
                f"snapshot.name={self.name}",
                "-O",
                "raw",
                path,
                export,
            )
            size = int(self.command("stat", "-c", "%s", export).strip())
            key = path
            self.paths[key] = export
            result.append(Disk(key, size))
        return Point(self.name, tuple(result))

    def _esxi(self) -> Point:
        if not self.vm.ref.isdecimal():
            raise ValueError("ESXi SSH replication requires a numeric VM id")
        self.kind = "esxi"
        snapshots = self.command("vim-cmd", "vmsvc/snapshot.get", self.vm.ref)
        if re.search(r"Snapshot (?:Id|Name)\s*:", snapshots, re.I):
            raise ValueError("Consolidate existing ESXi snapshots before replication")
        path = unquote(vmx_ssh_url(self.source, self.vm).split(self.source.host, 1)[1])
        config = self.command("cat", path)
        for entry in VMX_DISK.finditer(config):
            if (entry["key"].lower() == "mode" and "independent" in entry["val"].lower()) or (
                entry["key"].lower() == "sharing" and entry["val"].lower() == "multi-writer"
            ):
                raise ValueError("Independent or shared-write ESXi disks cannot be snapshotted")
        folder = posixpath.dirname(path)
        disks = []
        for descriptor in vmx_disk_files(config):
            descpath = posixpath.normpath(posixpath.join(folder, descriptor))
            flat = vmdk_flat_file(self.command("cat", descpath), descriptor)
            remote = posixpath.normpath(posixpath.join(posixpath.dirname(descpath), flat))
            size = int(self.command("stat", "-c", "%s", remote).strip())
            self.paths[remote] = remote
            self.descriptors[remote] = descpath
            disks.append(Disk(remote, size))
        if not disks:
            raise ValueError("ESXi VM has no flat disks")
        self._prepare_tracking()
        self.snapshot_attempted = True
        self.command(
            "vim-cmd",
            "vmsvc/snapshot.create",
            self.vm.ref,
            self.name,
            "ibHiCS replication point",
            "0",
            "0",
        )
        self.esxi_snapshot_id = self._snapshot_id()
        if not self.esxi_snapshot_id:
            raise RuntimeError("ESXi snapshot ownership could not be verified")
        return Point(self._tracking_token(disks), tuple(disks))

    def _prepare_tracking(self):
        pass

    def _tracking_token(self, disks):
        return self.name

    def _snapshot_id(self) -> str | None:
        text = self.command("vim-cmd", "vmsvc/snapshot.get", self.vm.ref)
        # Match name and id within one snapshot entry; never remove all snapshots.
        current = None
        for line in text.splitlines():
            match = re.search(r"Snapshot Name\s*:\s*(.*)", line)
            if match:
                current = match[1].strip()
            match = re.search(r"Snapshot Id\s*:\s*(\d+)", line)
            if match and current == self.name:
                return match[1]
        return None

    def hash(self, disk: Disk, offset: int, length: int) -> str | None:
        # BusyBox dd is supported on ESXi. Offsets are chunk aligned; final count is
        # allowed to exceed EOF. sha256sum absence falls back to a binary read.
        command = self._dd(disk, offset, length)
        try:
            output = self.ssh.command(f"{command} | sha256sum")
        except RuntimeError:
            return None
        digest = output.split()[0] if output.split() else ""
        return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None

    def _dd(self, disk: Disk, offset: int, length: int) -> str:
        from .store import CHUNK

        if offset < 0 or length < 1 or offset % CHUNK or length > CHUNK:
            raise ValueError("invalid replication read range")
        return (
            shlex.join(
                [
                    "dd",
                    f"if={self.paths[disk.key]}",
                    f"bs={CHUNK}",
                    f"skip={offset // CHUNK}",
                    "count=1",
                ]
            )
            + " 2>/dev/null"
        )

    def read(self, disk: Disk, offset: int, length: int) -> bytes:
        data = self.ssh.command(self._dd(disk, offset, length), binary=True)
        if len(data) != length:
            raise ValueError("short source snapshot read")
        return data

    def end(self) -> None:
        errors = []
        for export in self.exports:
            try:
                self.command("rm", "-f", "--", export)
            except Exception:
                errors.append("temporary snapshot export cleanup failed")
        if self.export_dir:
            try:
                self.command("rmdir", "--", self.export_dir)
            except Exception:
                errors.append("temporary snapshot directory cleanup failed")
        if self.snapshot_attempted:
            try:
                if self.kind == "esxi":
                    snapshot = self.esxi_snapshot_id or self._snapshot_id()
                    if snapshot:
                        self.command("vim-cmd", "vmsvc/snapshot.remove", self.vm.ref, snapshot, "0")
                elif self.kind == "proxmox":
                    config = self.command("qm", "listsnapshot", self.vm.ref)
                    if self.name in config:
                        self.command("qm", "delsnapshot", self.vm.ref, self.name)
                else:
                    names = self.command(
                        "virsh", "snapshot-list", self.vm.ref, "--name"
                    ).splitlines()
                    if self.name in names:
                        self.command("virsh", "snapshot-delete", self.vm.ref, self.name)
            except Exception:
                errors.append(f"snapshot cleanup failed: {self.name}; inspect source manually")
        if errors:
            raise RuntimeError("; ".join(errors))
