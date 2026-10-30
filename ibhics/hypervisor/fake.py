"""In-memory driver for development and tests, so the manager runs without KVM."""

from __future__ import annotations

import copy
import shutil
import socket
import tempfile
import threading
import uuid as uuidlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from .base import (
    GuestInfo,
    GuestInterface,
    HostInfo,
    HypervisorDriver,
    InvalidState,
    MigrationFailed,
    PoolInfo,
    PoolNotFound,
    PoolSpec,
    PoolType,
    VMExists,
    VMInfo,
    VMNotFound,
    VMSpec,
    VMState,
)

# Fake disks hold real bytes so backup and restore can be tested; keep them small.
FAKE_BYTES_PER_GB = 64 * 1024


class FakeDriver(HypervisorDriver):
    def __init__(self, cpus: int = 32, memory_mb: int = 131072, demo_console: bool = True) -> None:
        self._cpus = cpus
        self._memory_mb = memory_mb
        self._vms: dict[str, VMInfo] = {}
        self._pools: dict[str, tuple[PoolSpec, float]] = {
            "default": (PoolSpec(name="default", type=PoolType.DIR), 1000.0)
        }
        self._disks: dict[str, list[bytearray]] = {}
        # Where each VM's "screen" is. By default a running VM gets a small built-in VNC
        # server with a test picture; tests can map a uuid to any (host, port) instead.
        self.consoles: dict[str, tuple[str, int]] = {}
        self.demo_console = demo_console
        # Other fake nodes this one can migrate VMs to, keyed by the host in a libvirt URI.
        self.peers: dict[str, FakeDriver] = {}
        self._lock = threading.Lock()

    def host_info(self) -> HostInfo:
        return HostInfo(
            hostname=socket.gethostname(),
            cpus=self._cpus,
            memory_mb=self._memory_mb,
            hypervisor="fake",
        )

    def list_vms(self) -> list[VMInfo]:
        with self._lock:
            return [copy.deepcopy(vm) for vm in self._vms.values()]

    def get_vm(self, uuid: str) -> VMInfo:
        with self._lock:
            return copy.deepcopy(self._get(uuid))

    def create_vm(self, spec: VMSpec) -> VMInfo:
        spec.validate()
        with self._lock:
            if any(vm.spec.name == spec.name for vm in self._vms.values()):
                raise VMExists(f"VM named {spec.name!r} already exists")
            spec = copy.deepcopy(spec)
            for disk in spec.disks:
                if disk.pool not in self._pools:
                    raise PoolNotFound(disk.pool)
                disk.backend = self._pools[disk.pool][0].disk_backend
            vm = VMInfo(uuid=str(uuidlib.uuid4()), spec=spec, state=VMState.STOPPED)
            for index, nic in enumerate(spec.nics):  # libvirt picks a MAC like this
                a, b = uuidlib.UUID(vm.uuid).bytes[:2]
                nic.mac = nic.mac or f"52:54:00:{a:02x}:{b:02x}:{index:02x}"
            self._vms[vm.uuid] = vm
            self._disks[vm.uuid] = [bytearray(d.size_gb * FAKE_BYTES_PER_GB) for d in spec.disks]
            return copy.deepcopy(vm)

    def insert_cdrom(self, uuid: str, iso: str | None) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            vm.spec.iso = iso
            return copy.deepcopy(vm)

    def start_vm(self, uuid: str) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            if vm.state == VMState.RUNNING:
                raise InvalidState("VM is already running")
            vm.state = VMState.RUNNING
            return copy.deepcopy(vm)

    def stop_vm(self, uuid: str, force: bool = False) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            if vm.state == VMState.STOPPED:
                raise InvalidState("VM is already stopped")
            vm.state = VMState.STOPPED
            return copy.deepcopy(vm)

    def delete_vm(self, uuid: str) -> None:
        with self._lock:
            vm = self._get(uuid)
            if vm.state == VMState.RUNNING:
                raise InvalidState("stop the VM before deleting it")
            del self._vms[uuid]
            del self._disks[uuid]

    def list_pools(self) -> list[PoolInfo]:
        with self._lock:
            return [self._pool_info(name) for name in self._pools]

    def create_pool(self, spec: PoolSpec) -> PoolInfo:
        spec.validate()
        with self._lock:
            if spec.name in self._pools:
                raise InvalidState(f"a pool named {spec.name!r} already exists")
            self._pools[spec.name] = (copy.deepcopy(spec), 500.0)
            return self._pool_info(spec.name)

    def delete_pool(self, name: str) -> None:
        with self._lock:
            if name not in self._pools:
                raise PoolNotFound(name)
            for vm in self._vms.values():
                if any(d.pool == name for d in vm.spec.disks):
                    raise InvalidState(f"VM {vm.spec.name!r} still has disks in pool {name!r}")
            del self._pools[name]

    def migrate_vm(
        self,
        uuid: str,
        dest_uri: str,
        copy_storage: bool = False,
        bandwidth_mib: int | None = None,
    ) -> None:
        dest = self.peers.get(urlparse(dest_uri).hostname or "")
        if dest is None:
            raise MigrationFailed(f"cannot connect to {dest_uri}")
        with self._lock:
            vm = self._get(uuid)
            if vm.state != VMState.RUNNING and copy_storage:
                raise InvalidState("only a running VM can have its disks copied")
            with dest._lock:
                for disk in vm.spec.disks:
                    if disk.pool not in dest._pools:
                        raise PoolNotFound(disk.pool)
                if any(v.spec.name == vm.spec.name for v in dest._vms.values()):
                    raise VMExists(f"VM named {vm.spec.name!r} already exists on the destination")
                dest._vms[uuid] = self._vms.pop(uuid)
                dest._disks[uuid] = self._disks.pop(uuid)
            self.consoles.pop(uuid, None)

    def list_local_disks(self):
        from .disks import LocalDisk

        return [
            LocalDisk("/dev/sdb", 12509.0, "RAID 930-8i", "6000d31000f0", "sas"),
            LocalDisk("/dev/sdc", 894.3, "MZ7L3960", "S6KN", "sata", False, "3 partitions (vmfs)"),
        ]

    def guest_info(self, uuid: str) -> GuestInfo:
        """A pretend guest agent: running VMs answer, as if qemu-guest-agent were installed."""
        with self._lock:
            vm = self._get(uuid)
            if vm.state != VMState.RUNNING:
                return GuestInfo()
            spec = vm.spec
        windows = "windows" in spec.os_type.lower() or spec.name.lower().startswith("win")
        seed = sum(spec.name.encode())
        return GuestInfo(
            agent=True,
            os_name=spec.os_type or ("Windows Server 2022" if windows else "Ubuntu 24.04 LTS"),
            os_version="10.0.20348" if windows else "24.04",
            kernel="" if windows else "6.8.0-45-generic",
            hostname=spec.name.upper() if windows else spec.name,
            interfaces=[
                GuestInterface(
                    name=f"Ethernet{i}" if windows else f"ens{3 + i}",
                    mac=nic.mac or "",
                    ips=[f"10.0.{10 + i}.{20 + seed % 200}/24"],
                )
                for i, nic in enumerate(spec.nics)
            ],
        )

    def console_address(self, uuid: str) -> tuple[str, int]:
        with self._lock:
            vm = self._get(uuid)
            if vm.state != VMState.RUNNING:
                raise InvalidState("start the VM to open its console")
            if uuid not in self.consoles:
                if not self.demo_console:
                    raise InvalidState("the VM has no VNC display")
                from .fake_vnc import FakeVNCServer

                seed = sum(vm.spec.name.encode())
                self.consoles[uuid] = FakeVNCServer(vm.spec.name, seed).address
            return self.consoles[uuid]

    @contextmanager
    def export_disks(self, uuid: str) -> Iterator[list[Path]]:
        with self._lock:
            self._get(uuid)
            contents = [bytes(d) for d in self._disks[uuid]]
        scratch = Path(tempfile.mkdtemp(prefix="ibhics-fake-"))
        try:
            paths = []
            for index, data in enumerate(contents):
                path = scratch / f"disk{index}.raw"
                path.write_bytes(data)
                paths.append(path)
            yield paths
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def import_disk(self, uuid: str, index: int, raw_image: Path) -> None:
        data = Path(raw_image).read_bytes()
        with self._lock:
            vm = self._get(uuid)
            if vm.state != VMState.STOPPED:
                raise InvalidState("stop the VM before overwriting its disks")
            disk = self._disks[uuid][index]
            if len(data) > len(disk):
                raise ValueError(f"image is larger than disk {index}")
            disk[: len(data)] = data

    # Test helpers: what a guest would do to its disks.

    def write_disk(self, uuid: str, index: int, offset: int, data: bytes) -> None:
        with self._lock:
            self._disks[uuid][index][offset : offset + len(data)] = data

    def read_disk(self, uuid: str, index: int) -> bytes:
        with self._lock:
            return bytes(self._disks[uuid][index])

    def _pool_info(self, name: str) -> PoolInfo:
        spec, capacity = self._pools[name]
        used = sum(d.size_gb for vm in self._vms.values() for d in vm.spec.disks if d.pool == name)
        source = {
            PoolType.DIR: spec.path or f"/var/lib/ibhics/pools/{name}",
            PoolType.NFS: f"{spec.host}:{spec.export}",
            PoolType.ISCSI: f"{spec.host}:{spec.port} {spec.iqn} lun {spec.lun}",
            PoolType.LVM: spec.device or "",
        }[spec.type]
        return PoolInfo(
            name=name,
            type=spec.type,
            active=True,
            capacity_gb=capacity,
            allocation_gb=float(used),
            available_gb=capacity - used,
            source=source,
        )

    def _get(self, uuid: str) -> VMInfo:
        try:
            return self._vms[uuid]
        except KeyError:
            raise VMNotFound(uuid) from None
