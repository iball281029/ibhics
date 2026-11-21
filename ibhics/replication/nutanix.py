"""AHV full-diff from a stopped, isolated clone of a per-round v3 VM snapshot.

Schema: nutanix/Automation experimental/swagger-redoc-sandbox/
swagger-pe56rc4-3.1.0.yaml (vm_snapshot and vm_resources.parent_reference).
These v3 snapshot endpoints are hidden/version dependent: fail closed when absent.
CRT needs two extant snapshot files; per-round deletion precludes that baseline.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from ..importer.nutanix import NutanixError, Prism
from .providers import ChangeProvider, Disk, Point


def disk_map(entity):
    resources = entity.get("status", {}).get("resources") or entity.get("spec", {}).get(
        "resources", {}
    )
    result = {}
    for disk in resources.get("disk_list", []):
        kind = disk.get("device_properties", {}).get("device_type", "DISK")
        if kind == "CDROM":
            continue
        if kind != "DISK" or disk.get("volume_group_reference"):
            raise NutanixError("Replication requires ordinary AHV disks; volume groups unsupported")
        address = disk.get("device_properties", {}).get("disk_address", {})
        key = (address.get("adapter_type"), address.get("device_index"))
        if None in key or key in result or not disk.get("uuid"):
            raise NutanixError("Prism did not provide unique disk identities and addresses")
        size = disk.get("disk_size_bytes") or int(disk.get("disk_size_mib", 0)) * (1 << 20)
        if not isinstance(size, int) or size < 1:
            raise NutanixError("Prism did not provide a positive disk size")
        result[key] = (disk["uuid"], size)
    if not result:
        raise NutanixError("VM has no exportable AHV disks")
    return result


class NutanixSnapshotProvider(ChangeProvider):
    tracking = "full-diff"
    fallback_reason = "CRT requires a retained reference snapshot; per-round snapshots are removed"

    def __init__(self, source, vm, *, prism=None, run=subprocess.run, temp_root=None):
        self.vm = vm
        self._own_client = prism is None
        self.prism = prism or Prism(source)
        self.run, self.temp_root = run, temp_root
        self.name = "ibhics-repl-" + uuid.uuid4().hex
        self.snapshot = str(uuid.uuid4())
        self.clone = str(uuid.uuid4())
        self.snapshot_attempted = self.clone_attempted = False
        self.folder = None
        self.paths = {}

    def begin(self):
        try:
            return self._begin()
        except Exception:
            # Prism task details may contain credentials supplied by upstream code.
            raise NutanixError(
                "AHV snapshot export failed; check v3 snapshot/clone support, disk layout and space"
            ) from None

    def _begin(self):
        live = disk_map(self.prism._call("GET", f"/vms/{self.vm.ref}"))
        by_id = {identity: (address, size) for address, (identity, size) in live.items()}
        if set(by_id) != set(self.vm.disk_paths) or len(by_id) != len(self.vm.disk_paths):
            raise NutanixError(
                "AHV disk inventory changed; recreate replication with current disks"
            )
        self.folder = Path(tempfile.mkdtemp(prefix=self.name + "-", dir=self.temp_root))
        required = sum(size for _, size in live.values()) * 2 + (64 << 20)
        if shutil.disk_usage(self.folder).free < required:
            raise NutanixError(
                "Insufficient local space for AHV snapshot exports and raw conversion"
            )
        self.snapshot_attempted = True
        response = self.prism._call(
            "POST",
            "/vm_snapshots",
            json={
                "api_version": "3.1.0",
                "metadata": {"kind": "vm_snapshot", "uuid": self.snapshot},
                "spec": {
                    "name": self.name,
                    "snapshot_type": "CRASH_CONSISTENT",
                    "resources": {"entity_uuid": self.vm.ref},
                },
            },
        )
        self._created(response, self.snapshot)
        self.prism.wait_operation(response)
        self.prism.wait_entity("vm_snapshots", self.snapshot)
        self.clone_attempted = True
        response = self.prism._call(
            "POST",
            "/vms",
            json={
                "api_version": "3.1.0",
                "metadata": {"kind": "vm", "uuid": self.clone},
                "spec": {
                    "name": self.name + "-clone",
                    "resources": {
                        "parent_reference": {"kind": "vm_snapshot", "uuid": self.snapshot},
                        "power_state": "OFF",
                        "nic_list": [],
                    },
                },
            },
        )
        self._created(response, self.clone)
        self.prism.wait_operation(response)
        clone = self.prism.wait_entity("vms", self.clone)
        resources = clone.get("status", {}).get("resources", {})
        if resources.get("power_state") != "OFF" or resources.get("nic_list"):
            raise NutanixError("Snapshot export clone must be powered off and isolated")
        frozen = disk_map(clone)
        if frozen.keys() != live.keys():
            raise NutanixError("Snapshot clone disk addresses differ from source inventory")
        disks = []
        for index, identity in enumerate(self.vm.disk_paths):
            address, size = by_id[identity]
            clone_disk, clone_size = frozen[address]
            if clone_disk in by_id or clone_size != size:
                raise NutanixError("Refusing a live disk or mismatched snapshot clone disk")
            exported = self.folder / f"export-{index}"
            self.prism.export_disk(
                clone_disk, exported, prefix=self.name + "-", strict_cleanup=True
            )
            exported.chmod(0o600)
            target = self.folder / f"disk-{index}.raw"
            with exported.open("rb") as stream:
                qcow2 = stream.read(4) == b"QFI\xfb"
            if qcow2:
                result = self.run(
                    ["qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(exported), str(target)],
                    capture_output=True,
                    timeout=3600,
                )
                if result.returncode:
                    raise NutanixError("Could not convert snapshot image to raw")
                target.chmod(0o600)
                exported.unlink()
            else:
                exported.replace(target)
            if target.stat().st_size != size:
                raise NutanixError("Snapshot export size differs from AHV disk size")
            self.paths[identity] = target
            disks.append(Disk(identity, size))
        return Point(self.name, tuple(disks))

    @staticmethod
    def _created(response, expected):
        if response.get("metadata", {}).get("uuid") != expected:
            raise NutanixError("Prism did not honor the temporary entity UUID")

    def read(self, disk, offset, length):
        with self.paths[disk.key].open("rb") as stream:
            stream.seek(offset)
            return stream.read(length)

    def end(self):
        errors = []
        try:
            for attempted, kind, identity in (
                (self.clone_attempted, "vms", self.clone),
                (self.snapshot_attempted, "vm_snapshots", self.snapshot),
            ):
                if attempted:
                    try:
                        # A 404 after a failed create is already clean. Other failures
                        # block publication; never silently leave a temporary entity.
                        self.prism.delete_temporary(kind, identity)
                    except Exception:
                        errors.append(kind)
        finally:
            if self.folder:
                shutil.rmtree(self.folder)
                self.folder = None
            if self._own_client:
                self.prism._http.close()
        if errors:
            raise NutanixError("AHV temporary entity cleanup failed: " + ", ".join(errors))
