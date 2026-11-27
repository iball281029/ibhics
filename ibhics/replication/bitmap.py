"""Libvirt point-in-time NBD pull over SSH; never open a live backing disk.

Only the committed checkpoint is retained between cycles. Backup scratch files and
the owned NBD job are released before publication; superseded checkpoint metadata
is released after publication. Proxmox without libvirt keeps the qcow2 fallback.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import xml.etree.ElementTree as ET

from .providers import ChangeProvider, Disk, Point
from .ssh import SSH, SnapshotSSHProvider
from .store import CHUNK

CHECKPOINT = re.compile(r"ibhics-repl-[0-9a-f]{32}")


def dirty_extents(entries, size):
    """Validate complete bitmap coverage, including clean areas and changed zeroes.

    QEMU's NBD driver maps bit 0 (dirty) to NBD_STATE_HOLE, hence data=False
    denotes DIRTY for x-dirty-bitmap, the inverse of normal allocation maps.
    Missing/ambiguous coverage must never mean an unchanged disk.
    """
    if not isinstance(entries, list):
        raise ValueError("invalid dirty bitmap map")
    cursor, changed = 0, []
    for item in entries:
        start, length = item["start"], item["length"]
        if (
            type(start) is not int
            or type(length) is not int
            or start != cursor
            or length <= 0
            or start + length > size
            or type(item.get("data")) is not bool
            or item.get("zero") is not False
        ):
            raise ValueError("invalid dirty bitmap coverage")
        if not item["data"]:
            changed.append((start, length))
        cursor += length
    if cursor != size:
        raise ValueError("incomplete dirty bitmap coverage")
    return changed


class KVMPullProvider(ChangeProvider):
    def __init__(self, source, vm, ssh=None):
        self.source, self.vm = source, vm
        self.ssh = ssh or SSH(source)
        self.legacy = SnapshotSSHProvider(source, vm, self.ssh)
        self.name = self.legacy.name
        self.directory = ""
        self.socket = ""
        self.files = []
        self.directory_created = False
        self.attempted = False
        self.new_checkpoint = False
        self.old_checkpoint = None
        self.previous = None
        self.fallback = False
        self.ranges = None
        self.tracking = "hash-delta"
        self.fallback_reason = "Dirty bitmap baseline requires a full hash scan"

    def command(self, *args):
        return self.ssh.command(shlex.join(args))

    def prepare(self, prev_token):
        try:
            value = json.loads(prev_token)
            if (
                value["format"] == "libvirt-pull-v1"
                and value["host"] == self.source.host
                and value["vm"] == self.vm.ref
                and CHECKPOINT.fullmatch(value["checkpoint"])
                and isinstance(value["disks"], dict)
            ):
                self.previous = value
                self.old_checkpoint = value["checkpoint"]
            else:
                raise ValueError("different baseline")
        except (ValueError, TypeError, KeyError):
            if prev_token:
                self.fallback_reason = (
                    "Dirty bitmap token invalid or source changed; full hash scan"
                )

    def _xml_file(self, basename, element):
        path = f"{self.directory}/{basename}.xml"
        if path not in self.files:
            self.files.append(path)
        xml = ET.tostring(element, encoding="unicode")
        self.ssh.command(f"umask 077; printf %s {shlex.quote(xml)} > {shlex.quote(path)}")
        return path

    def _options(self, disk, bitmap=False, *, offset=None, length=None):
        # JSON avoids QEMU option-string comma injection through a target name.
        opts = {
            "driver": "nbd",
            "server": {"type": "unix", "path": self.socket},
            "export": disk.key,
        }
        if bitmap:
            opts["x-dirty-bitmap"] = "qemu:dirty-bitmap:ibhics"
        if offset is not None:
            # A bounded raw view avoids qemu-img dd's count/skip interaction:
            # some versions apply count before subtracting the skipped blocks.
            opts = {"driver": "raw", "file": opts, "offset": offset, "size": length}
        return "json:" + json.dumps(opts, separators=(",", ":"))

    def _active(self):
        try:
            return ET.fromstring(self.command("virsh", "backup-dumpxml", self.vm.ref))
        except RuntimeError:
            return None

    def _stop_owned(self):
        active = self._active()
        if active is not None:
            server = active.find("server")
            if server is None or server.get("socket") != self.socket:
                raise RuntimeError("Refusing to abort an unowned libvirt backup job")
            self.command("virsh", "domjobabort", self.vm.ref)
        self.attempted = False

    def _delete_checkpoint(self, name):
        if not CHECKPOINT.fullmatch(name):
            raise ValueError("invalid owned checkpoint name")
        names = self.command("virsh", "checkpoint-list", self.vm.ref, "--name").splitlines()
        if name in names:
            # Delete this bitmap/metadata only, never descendants or disk contents.
            self.command("virsh", "checkpoint-delete", self.vm.ref, name)

    def _start(self, disks, incremental, checkpoint):
        backup = ET.Element("domainbackup", mode="pull")
        if incremental:
            ET.SubElement(backup, "incremental").text = self.old_checkpoint
        ET.SubElement(backup, "server", transport="unix", socket=self.socket)
        spec = ET.SubElement(backup, "disks")
        for i, disk in enumerate(disks):
            attrs = {"name": disk.key, "type": "file", "exportname": disk.key}
            if incremental:
                attrs["exportbitmap"] = "ibhics"
            item = ET.SubElement(spec, "disk", attrs)
            ET.SubElement(item, "scratch", file=f"{self.directory}/{i}.scratch")
        args = ["virsh", "backup-begin", self.vm.ref, self._xml_file("backup", backup)]
        if checkpoint:
            root = ET.Element("domaincheckpoint")
            ET.SubElement(root, "name").text = self.name
            targets = ET.SubElement(root, "disks")
            for disk in disks:
                ET.SubElement(targets, "disk", name=disk.key, checkpoint="bitmap")
            args += ["--checkpointxml", self._xml_file("checkpoint", root)]
        # Even a timeout may leave an active backup/checkpoint. Cleanup verifies ownership.
        self.attempted = True
        self.new_checkpoint = checkpoint
        self.command(*args)

    def _reset_attempt(self):
        if self.attempted:
            self._stop_owned()
        if self.new_checkpoint:
            self._delete_checkpoint(self.name)
            self.new_checkpoint = False

    def begin(self):
        if self.ssh.command("command -v qm || true").strip():
            self.fallback = True
            self.fallback_reason = "Proxmox has no libvirt pull adapter; qcow2 snapshot hash-delta"
            return self.legacy.begin()
        domain = ET.fromstring(self.command("virsh", "dumpxml", self.vm.ref))
        disks, identities = [], {}
        for item in domain.findall("./devices/disk[@device='disk']"):
            target, source = item.find("target"), item.find("source")
            if target is None or source is None or not target.get("dev"):
                raise ValueError("Libvirt disk identity is missing")
            key = target.get("dev")
            info = self.command("virsh", "domblkinfo", self.vm.ref, key)
            match = re.search(r"^Capacity:\s*(\d+)\s*$", info, re.M)
            if not match or int(match[1]) < 1:
                raise ValueError("Libvirt disk capacity is unknown")
            disks.append(Disk(key, int(match[1])))
            identities[key] = {
                "size": int(match[1]),
                "source": hashlib.sha256(ET.tostring(source)).hexdigest(),
            }
        if not disks or len(identities) != len(disks):
            raise ValueError("Libvirt VM has no uniquely identified disks")
        if self._active() is not None:
            raise ValueError("An existing libvirt backup job must finish before replication")
        needed = sum(d.size for d in disks)
        scratch = self.legacy.scratch_root(needed + CHUNK)
        self.directory = f"{scratch}/{self.name}"
        self.socket = f"{self.directory}/nbd.sock"
        if len(self.socket.encode()) > 107:
            raise ValueError("Replication source scratch path is too long for the NBD socket")
        self.command("mkdir", "-m", "700", "--", self.directory)
        self.directory_created = True
        # QEMU creates the UNIX socket and accesses scratch files as its DAC user.
        # Never make a backup directory world-writable or change host configuration.
        label = domain.findtext("./seclabel[@model='dac']/label", "")
        if re.fullmatch(r"\+?\d+:\+?\d+", label):
            self.command("chown", label.replace("+", ""), "--", self.directory)
        incremental = bool(self.previous and self.previous["disks"] == identities)
        if self.previous and not incremental:
            self.fallback_reason = "Dirty bitmap disk identity/size changed; full hash scan"
        try:
            try:
                self._start(disks, incremental, True)
                if incremental:
                    self.ranges = {
                        disk.key: dirty_extents(
                            json.loads(
                                self.command(
                                    "qemu-img", "map", "--output=json", self._options(disk, True)
                                )
                            ),
                            disk.size,
                        )
                        for disk in disks
                    }
                    self.tracking, self.fallback_reason = "dirty-bitmap", None
            except (RuntimeError, ValueError, KeyError, TypeError):
                self._reset_attempt()
                self.ranges = None
                self.fallback_reason = (
                    "Dirty bitmap unavailable/reset or invalid map; full NBD hash scan"
                )
                try:
                    self._start(disks, False, True)
                except RuntimeError:
                    self._reset_attempt()
                    self._start(disks, False, False)
            # Verify every full export before returning a point or attempting fallback.
            if self.ranges is None:
                for disk in disks:
                    info = json.loads(
                        self.command("qemu-img", "info", "--output=json", self._options(disk))
                    )
                    if info.get("virtual-size") != disk.size:
                        raise ValueError("NBD export size mismatch")
        except (RuntimeError, ValueError):
            self._reset_attempt()
            # The legacy provider uses the same owned name/directory. Release pull
            # resources before it creates its snapshot export workspace.
            self.end()
            self.fallback = True
            self.fallback_reason = (
                "Libvirt NBD pull unavailable; file-backed qcow2 snapshot hash-delta"
            )
            # This provider refuses raw/block/network backings explicitly.
            return self.legacy.begin()
        return Point(
            json.dumps(
                {
                    "format": "libvirt-pull-v1",
                    "host": self.source.host,
                    "vm": self.vm.ref,
                    "checkpoint": self.name if self.new_checkpoint else None,
                    "disks": identities,
                },
                sort_keys=True,
            ),
            tuple(disks),
        )

    def list_changed(self, prev_token):
        return self.ranges

    def hash(self, disk, offset, length):
        return self.legacy.hash(disk, offset, length) if self.fallback else None

    def read(self, disk, offset, length):
        if self.fallback:
            return self.legacy.read(disk, offset, length)
        if offset < 0 or offset % CHUNK or length != min(CHUNK, disk.size - offset):
            raise ValueError("invalid NBD read range")
        path = f"{self.directory}/chunk.raw"
        if path not in self.files:
            self.files.append(path)
        try:
            self.command(
                "qemu-img",
                "dd",
                f"if={self._options(disk, offset=offset, length=length)}",
                f"of={path}",
                f"bs={CHUNK}",
                "count=1",
            )
            data = self.ssh.command(shlex.join(["cat", "--", path]), binary=True)
            if len(data) != length:
                raise ValueError("short NBD snapshot read")
            return data
        finally:
            self.command("rm", "-f", "--", path)

    def end(self):
        errors = []
        try:
            if self.fallback:
                self.legacy.end()
        except Exception:
            errors.append("qcow2 snapshot cleanup failed")
        try:
            if self.attempted:
                self._stop_owned()
        except Exception:
            errors.append("owned NBD backup cleanup failed; inspect source")
        if self.directory_created:
            try:
                for path in self.files:
                    self.command("rm", "-f", "--", path)
                # Scratch/socket files are owned and removed by libvirt, never unlink
                # them while a backup job may still be using them.
                self.command("rmdir", "--", self.directory)
                self.directory_created = False
                self.files.clear()
            except Exception:
                errors.append("NBD backup temporary directory cleanup failed")
        if errors:
            raise RuntimeError("; ".join(errors))

    def finalize(self, committed):
        if not committed and self.new_checkpoint:
            self._delete_checkpoint(self.name)
        elif committed and self.old_checkpoint:
            self._delete_checkpoint(self.old_checkpoint)
