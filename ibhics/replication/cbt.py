"""CBT hints over the existing snapshot/SSH transport, with safe hash fallback."""

import json
import posixpath
import re
from dataclasses import replace

from .ssh import SnapshotSSHProvider
from .vsphere import VSphereCBT


class ESXiCBTProvider(SnapshotSSHProvider):
    def __init__(self, source, vm, ssh=None, cbt_factory=VSphereCBT):
        super().__init__(source, vm, ssh)
        self.cbt_factory = cbt_factory
        self.cbt = None
        self.tracking = "hash-delta"
        self.fallback_reason = None
        self.snapshot_mor = None
        self.disk_tokens = {}

    def begin(self):
        # Discovery uses VM names; vim-cmd and direct ESXi SOAP require VM ids.
        # Resolve only an exact, unique datastore VMX path, never a display name.
        if not self.vm.ref.isdecimal():
            ids = []
            if not self.vm.vmx:
                raise ValueError("ESXi replication requires a known datastore VMX path")
            for line in self.command("vim-cmd", "vmsvc/getallvms").splitlines():
                match = re.match(r"^\s*(\d+)\s+", line)
                if match and re.search(r"\s" + re.escape(self.vm.vmx) + r"(?:\s|$)", line):
                    ids.append(match[1])
            if len(ids) != 1:
                raise ValueError("ESXi VMX path did not resolve to a unique VM id")
            self.vm = replace(self.vm, ref=ids[0])
        return super().begin()

    def _prepare_tracking(self):
        try:
            self.cbt = self.cbt_factory(self.source)
            self.cbt.connect(self.source)
            self.cbt.enable(self.vm.ref)
        except Exception:
            # Never persist SDK/HTTP exceptions: responses may echo credentials.
            self.fallback_reason = "CBT API unavailable, unsupported or permission denied"

    def _tracking_token(self, disks):
        if self.fallback_reason:
            return self.name
        try:
            self.snapshot_mor, devices = self.cbt.snapshot_disks(self.vm.ref, self.name)
            by_path = {}
            for filename, device in devices.items():
                match = re.fullmatch(r"\[([^\]]+)\]\s+(.+)", filename)
                if not match:
                    raise ValueError("not a datastore path")
                path = posixpath.normpath(f"/vmfs/volumes/{match[1]}/{match[2]}")
                by_path[path] = device
            for disk in disks:
                device = by_path[self.descriptors[disk.key]]
                if device["size"] != disk.size:
                    raise ValueError("disk size mismatch")
                self.disk_tokens[disk.key] = device
            if len(self.disk_tokens) != len(devices):
                raise ValueError("disk set mismatch")
            self.fallback_reason = "CBT baseline requires a full hash scan"
            return json.dumps(
                {
                    "format": "esxi-cbt-v1",
                    "host": self.source.host,
                    "vm": self.vm.ref,
                    "vmx": self.vm.vmx,
                    "point": self.name,
                    "disks": self.disk_tokens,
                },
                sort_keys=True,
            )
        except Exception:
            self.disk_tokens = {}
            self.fallback_reason = "CBT snapshot changeIds unavailable or disk backing unsupported"
            return self.name

    def list_changed(self, prev_token):
        if not self.disk_tokens:
            return None
        try:
            previous = json.loads(prev_token)
            if any(
                previous.get(key) != value
                for key, value in {
                    "format": "esxi-cbt-v1",
                    "host": self.source.host,
                    "vm": self.vm.ref,
                    "vmx": self.vm.vmx,
                }.items()
            ):
                raise ValueError("different CBT source")
            old = previous["disks"]
            if old.keys() != self.disk_tokens.keys():
                raise ValueError("different disk set")
            ranges = {}
            for path, current in self.disk_tokens.items():
                prior = old[path]
                if (
                    prior["key"] != current["key"]
                    or prior["size"] != current["size"]
                    or not isinstance(prior["change_id"], str)
                    or not prior["change_id"]
                    or prior["change_id"] == "*"
                ):
                    raise ValueError("invalid CBT baseline")
                ranges[path] = self.cbt.changed(
                    self.vm.ref,
                    self.snapshot_mor,
                    current["key"],
                    current["size"],
                    prior["change_id"],
                )
            self.tracking, self.fallback_reason = "cbt", None
            return ranges
        except Exception:
            self.tracking = "hash-delta"
            self.fallback_reason = (
                "CBT token invalid/reset or changed-area query failed; full hash scan"
            )
            return None

    def end(self):
        try:
            super().end()
        finally:
            if self.cbt:
                try:
                    self.cbt.close()
                except Exception:
                    pass  # logout failure must not hide snapshot cleanup errors
