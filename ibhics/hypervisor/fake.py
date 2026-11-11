"""In-memory driver for development and tests, so the manager runs without KVM."""

from __future__ import annotations

import copy
import math
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid as uuidlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from . import customize
from .base import (
    VM_NAME_RE,
    CloneSpec,
    DiskSpec,
    GuestInfo,
    GuestInterface,
    HostInfo,
    HypervisorDriver,
    InvalidState,
    MigrationFailed,
    NetworkInfo,
    NetworkSpec,
    NicSpec,
    PoolInfo,
    PoolNotFound,
    PoolSpec,
    PoolType,
    SnapshotInfo,
    SnapshotNotFound,
    VMEdit,
    VMExists,
    VMInfo,
    VMNotFound,
    VMSpec,
    VMState,
    VMStats,
    check_snapshot_name,
    clone_spec,
    snapshot_allowed,
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
        self._caches: dict[str, dict | None] = {}  # hybrid pools: their cache, or None
        # Set to a folder to give file-based fake pools real files (datastore browser).
        self.files_root: Path | None = None
        # Where each VM's "screen" is. By default a running VM gets a small built-in VNC
        # server with a test picture; tests can map a uuid to any (host, port) instead.
        self.consoles: dict[str, tuple[str, int]] = {}
        self.demo_console = demo_console
        # Other fake nodes this one can migrate VMs to, keyed by the host in a libvirt URI.
        self.peers: dict[str, FakeDriver] = {}
        # The files each deployed VM's customization CD would hold, keyed by VM name.
        self.seeds: dict[str, dict[str, str]] = {}
        # Per VM: (info, spec, disk contents, state) for each snapshot, oldest first.
        self._snaps: dict[str, list[tuple[SnapshotInfo, VMSpec, list[bytearray], VMState]]] = {}
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
            spec.linked_to = ""
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

    def update_vm(self, uuid: str, edit: VMEdit) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            new = edit.apply_to(vm.spec)
            running = vm.state == VMState.RUNNING
            # Like libvirt: CPUs and memory can grow while the VM runs (hot-add).
            grows = vm.spec.vcpus <= new.vcpus <= 32 and vm.spec.memory_mb <= new.memory_mb
            key = (lambda sp: (sp.boot, sp.video)) if grows else _restart_key
            if running and key(new) != key(vm.spec):
                vm.restart_needed = True
            if edit.disks is not None:
                disks, data = [], []
                for d in edit.disks:
                    if d.index is not None:
                        disk = copy.deepcopy(vm.spec.disks[d.index])
                        blob = self._disks[uuid][d.index]
                        blob.extend(bytes((d.size_gb - disk.size_gb) * FAKE_BYTES_PER_GB))
                        disk.size_gb = d.size_gb
                    else:
                        if d.pool not in self._pools:
                            raise PoolNotFound(d.pool)
                        backend = self._pools[d.pool][0].disk_backend
                        disk = DiskSpec(size_gb=d.size_gb, pool=d.pool, backend=backend)
                        blob = bytearray(d.size_gb * FAKE_BYTES_PER_GB)
                    disks.append(disk)
                    data.append(blob)
                new.disks, self._disks[uuid] = disks, data
            if edit.nics is not None:
                new.nics = [
                    NicSpec(network=n.network, model=n.model, mac=n.mac or _random_mac())
                    for n in edit.nics
                ]
            vm.spec = new
            return copy.deepcopy(vm)

    def vm_definition(self, uuid: str) -> str:
        import json

        from .serialize import spec_to_json

        with self._lock:
            vm = self._get(uuid)
            return json.dumps({**spec_to_json(vm.spec), "uuid": vm.uuid, "state": "stopped"})

    def define_vm(self, definition: str) -> VMInfo:
        import json

        from .serialize import info_from_json

        vm = info_from_json(json.loads(definition))
        with self._lock:
            if vm.uuid in self._vms or any(v.spec.name == vm.spec.name for v in self._vms.values()):
                raise VMExists(f"VM named {vm.spec.name!r} already exists")
            for disk in vm.spec.disks:
                if disk.pool not in self._pools:
                    raise PoolNotFound(disk.pool)
            vm.state = VMState.STOPPED
            self._vms[vm.uuid] = vm
            self._disks[vm.uuid] = [bytearray(d.size_gb * FAKE_BYTES_PER_GB) for d in vm.spec.disks]
            return copy.deepcopy(vm)

    def forget_vm(self, uuid: str) -> None:
        with self._lock:
            self._get(uuid)
            del self._vms[uuid]
            self._disks.pop(uuid, None)
            self.consoles.pop(uuid, None)

    def start_vm(self, uuid: str) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            if vm.state == VMState.RUNNING:
                raise InvalidState("VM is already running")
            if vm.spec.template:
                raise InvalidState(
                    "this is a template, which does not run: deploy a VM from it, or convert "
                    "it back to a VM first"
                )
            if vm.spec.recycled:
                raise InvalidState("this VM is in the recycle bin: restore it first")
            vm.state = VMState.RUNNING
            return copy.deepcopy(vm)

    def stop_vm(self, uuid: str, force: bool = False) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            if vm.state == VMState.STOPPED:
                raise InvalidState("VM is already stopped")
            vm.state = VMState.STOPPED
            vm.restart_needed = False
            return copy.deepcopy(vm)

    def restart_vm(self, uuid: str, force: bool = False) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            if vm.state != VMState.RUNNING:
                raise InvalidState("VM is not running")
            vm.restart_needed = False  # pending changes: power off and on, so they apply
            return copy.deepcopy(vm)

    def delete_vm(self, uuid: str) -> None:
        with self._lock:
            vm = self._get(uuid)
            if vm.state == VMState.RUNNING:
                raise InvalidState("stop the VM before deleting it")
            self._refuse_if_linked(uuid, "deleting the template")
            del self._vms[uuid]
            del self._disks[uuid]
            self._snaps.pop(uuid, None)
            self.seeds.pop(vm.spec.name, None)

    # Snapshots

    def list_snapshots(self, uuid: str) -> list[SnapshotInfo]:
        with self._lock:
            self._get(uuid)
            return [copy.deepcopy(s[0]) for s in self._snaps.get(uuid, [])]

    def create_snapshot(
        self, uuid: str, name: str, description: str = "", restart: bool = False
    ) -> SnapshotInfo:
        check_snapshot_name(name)
        with self._lock:
            vm = self._get(uuid)
            snapshot_allowed(vm.spec, [self._pools[d.pool][0].disk_backend for d in vm.spec.disks])
            snaps = self._snaps.setdefault(uuid, [])
            if any(s[0].name == name for s in snaps):
                raise InvalidState(f"{vm.spec.name} already has a snapshot named {name!r}")
            parent = next((s[0].name for s in snaps if s[0].current), "")
            for s in snaps:
                s[0].current = False
            info = SnapshotInfo(
                name=name,
                created=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                vm_state=str(vm.state),
                description=description,
                parent=parent,
                current=True,
            )
            disks = [bytearray(d) for d in self._disks[uuid]]
            snaps.append((info, copy.deepcopy(vm.spec), disks, vm.state))
            return copy.deepcopy(info)

    def move_disks(self, uuid: str, moves: dict[int, str]) -> VMInfo:
        self._refuse_if_linked(uuid, "moving its storage")
        with self._lock:
            vm = self._get(uuid)
            for i, pool in moves.items():
                if not 0 <= i < len(vm.spec.disks):
                    raise ValueError(f"this VM has no disk {i + 1}")
                if pool not in self._pools:
                    raise PoolNotFound(pool)
            for i, pool in moves.items():
                disk = vm.spec.disks[i]
                disk.pool = pool
                disk.backend = self._pools[pool][0].disk_backend
            return copy.deepcopy(vm)

    def switch_to_virtio(self, uuid: str) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            if not any(d.bus == "sata" for d in vm.spec.disks):
                raise InvalidState("this VM's disks are VirtIO already")
            for d in vm.spec.disks:
                d.bus = "virtio"
            return copy.deepcopy(vm)

    def sysprep_vm(self, uuid: str) -> None:
        with self._lock:
            vm = self._get(uuid)
            if vm.state != VMState.RUNNING:
                raise InvalidState("start the VM (with its guest tools) to run sysprep")
            if "windows" not in vm.spec.os_type.lower():
                raise InvalidState("sysprep is for Windows VMs")
            vm.state = VMState.STOPPED

    def rename_vm(self, uuid: str, name: str) -> VMInfo:
        if not VM_NAME_RE.match(name):
            raise ValueError("VM names use letters (Thai too), digits and '_.-'")
        with self._lock:
            vm = self._get(uuid)
            if vm.spec.name != name:
                if any(v.spec.name == name for v in self._vms.values()):
                    raise VMExists(f"VM named {name!r} already exists")
                vm.spec.name = name
            return copy.deepcopy(vm)

    def revert_snapshot(self, uuid: str, name: str) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            if vm.spec.template:
                raise InvalidState("a template's snapshots cannot be reverted to")
            snaps = self._snaps.get(uuid, [])
            match = next((s for s in snaps if s[0].name == name), None)
            if match is None:
                raise SnapshotNotFound(name)
            info, spec, disks, state = match
            vm.spec = copy.deepcopy(spec)
            self._disks[uuid] = [bytearray(d) for d in disks]
            vm.state = state
            vm.restart_needed = False
            for s in snaps:
                s[0].current = s[0].name == name
            return copy.deepcopy(vm)

    def delete_snapshot(self, uuid: str, name: str) -> None:
        with self._lock:
            self._get(uuid)
            snaps = self._snaps.get(uuid, [])
            match = next((s for s in snaps if s[0].name == name), None)
            if match is None:
                raise SnapshotNotFound(name)
            snaps.remove(match)
            for s in snaps:  # children now follow the deleted one's parent
                if s[0].parent == name:
                    s[0].parent = match[0].parent
            if match[0].current and match[0].parent:
                for s in snaps:
                    if s[0].name == match[0].parent:
                        s[0].current = True

    def set_template(self, uuid: str, template: bool) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            if template and vm.state != VMState.STOPPED:
                raise InvalidState("shut the VM down before converting it to a template")
            if not template:
                self._refuse_if_linked(uuid, "converting the template back to a VM")
            vm.spec.template = template
            if template:
                vm.spec.autostart = False
            return copy.deepcopy(vm)

    def set_recycled(self, uuid: str, at: str) -> VMInfo:
        with self._lock:
            vm = self._get(uuid)
            if at and vm.state != VMState.STOPPED:
                raise InvalidState("shut the VM down before deleting it")
            vm.spec.recycled = at
            if at:
                vm.spec.autostart = False
            return copy.deepcopy(vm)

    def linked_clones(self, uuid: str) -> list[str]:
        return sorted(vm.spec.name for vm in self._vms.values() if vm.spec.linked_to == uuid)

    def _refuse_if_linked(self, uuid: str, what: str) -> None:
        clones = self.linked_clones(uuid)
        if clones:
            raise InvalidState(
                f"linked clones are based on this template ({', '.join(clones)}); delete them "
                f"before {what}"
            )

    def clone_vm(self, uuid: str, clone: CloneSpec) -> VMInfo:
        clone.validate()
        with self._lock:
            if any(vm.spec.name == clone.name for vm in self._vms.values()):
                raise VMExists(f"VM named {clone.name!r} already exists")
            source = self._get(uuid)
            if source.state != VMState.STOPPED:
                raise InvalidState(
                    "shut the VM down before cloning it, so its disks are consistent"
                )
            if clone.linked and not source.spec.template:
                raise InvalidState("linked clones are made from templates: convert the VM first")
            spec = clone_spec(source.spec, clone, uuid)
            for disk in spec.disks:
                if disk.pool not in self._pools:
                    raise PoolNotFound(disk.pool)
                disk.backend = self._pools[disk.pool][0].disk_backend
                if clone.linked and disk.backend != "file":
                    raise InvalidState(
                        "linked clones need the template's disks in qcow2 (a folder, thin "
                        "disk or NFS pool); deploy a full clone instead"
                    )
            vm = VMInfo(uuid=str(uuidlib.uuid4()), spec=spec, state=VMState.STOPPED)
            for nic in spec.nics:
                nic.mac = _random_mac()
            if clone.customization:
                self.seeds[spec.name] = customize.seed_files(
                    clone.customization, spec.name, vm.uuid
                )
                spec.iso = f"/var/lib/ibhics/seeds/{spec.name}-seed.iso"
            self._vms[vm.uuid] = vm
            self._disks[vm.uuid] = [bytearray(d) for d in self._disks[uuid]]
            return copy.deepcopy(vm)

    def list_pools(self) -> list[PoolInfo]:
        with self._lock:
            return [self._pool_info(name) for name in self._pools]

    def create_pool(self, spec: PoolSpec) -> PoolInfo:
        spec.validate()
        with self._lock:
            if spec.name in self._pools:
                raise InvalidState(f"a pool named {spec.name!r} already exists")
            self._pools[spec.name] = (copy.deepcopy(spec), 500.0)
            if spec.cache_devices:
                self._caches[spec.name] = _fake_cache(spec.cache_mode, spec.cache_gb or 100)
            return self._pool_info(spec.name)

    def pool_cache(
        self,
        name: str,
        action: str,
        devices: list[str] | None = None,
        mode: str = "writethrough",
        size_gb: int | None = None,
    ) -> PoolInfo:
        with self._lock:
            if name not in self._pools:
                raise PoolNotFound(name)
            if name not in self._caches:
                raise InvalidState(f"{name} is not a hybrid pool")
            have = self._caches[name]
            if mode not in ("writethrough", "writeback"):
                raise ValueError("cache mode is writethrough or writeback")
            if action == "add":
                if have:
                    raise InvalidState("this pool has a cache already; remove it first")
                if not devices:
                    raise ValueError("pick the SSDs for the cache")
                self._caches[name] = _fake_cache(mode, size_gb or 100)
            elif action in ("remove", "mode"):
                if not have:
                    raise InvalidState("this pool has no cache")
                self._caches[name] = None if action == "remove" else {**have, "mode": mode}
            else:
                raise ValueError("action is add, remove or mode")
            return self._pool_info(name)

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
        data_address: str = "",
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

    def list_networks(self) -> list[NetworkInfo]:
        with self._lock:
            nets = getattr(self, "_networks", None)
            if nets is None:
                nets = self._networks = {"default": NetworkInfo("default", "br0")}
            return sorted(copy.deepcopy(list(nets.values())), key=lambda n: (n.vlan or 0, n.name))

    def create_network(self, spec: NetworkSpec) -> NetworkInfo:
        spec.validate()
        for net in self.list_networks():
            if net.name == spec.name:
                raise InvalidState(f"a network named {spec.name!r} already exists")
            if net.vlan == spec.vlan:
                raise InvalidState(f"VLAN {spec.vlan} already has network {net.name!r}")
        with self._lock:
            info = NetworkInfo(spec.name, f"ibr{spec.vlan}", vlan=spec.vlan)
            self._networks[spec.name] = info
            return copy.deepcopy(info)

    def apply_edge(self, config: dict, router: bool) -> None:
        from .edge import EdgeConfig

        cfg = EdgeConfig.from_dict(config)
        names = {n.name for n in self.list_networks()}
        cfg.validate(names)
        self.edge = (cfg.to_dict(), router)

    def run_command(self, line: str) -> tuple[int, str]:
        from .terminal import run_line

        return run_line(line, lambda cmd: (0, f"(fake node) {' '.join(cmd)}"))

    def net_test(self, kind: str, target: str, network: str = "") -> tuple[bool, str]:
        from .edge import net_test

        def run(cmd: list[str]) -> tuple[int, str]:
            return 0, f"(fake) {' '.join(cmd)}\n4 packets transmitted, 4 received, 0% packet loss"

        source = ""
        if network:
            source = next((n.bridge for n in self.list_networks() if n.name == network), "")
        return net_test(kind, target, run, source)

    def delete_network(self, name: str) -> None:
        if name == "default":
            raise InvalidState("the default network is the LAN itself and stays")
        self.list_networks()
        with self._lock:
            for vm in self._vms.values():
                if any(n.network == name for n in vm.spec.nics):
                    raise InvalidState(f"VM {vm.spec.name!r} is still on network {name!r}")
            if self._networks.pop(name, None) is None:
                raise InvalidState(f"no network named {name!r}")

    def containers(self):
        from .containers import FakeContainers

        if getattr(self, "_containers", None) is None:
            self._containers = FakeContainers(
                lambda: {n.name for n in self.list_networks()}, lambda: set(self._pools)
            )
        return self._containers

    def host_system(self):
        from .host import FakeHost

        if getattr(self, "_host", None) is None:
            self._host = FakeHost()
        return self._host

    def pool_path(self, name: str) -> str | None:
        with self._lock:
            if name not in self._pools:
                raise PoolNotFound(name)
            spec = self._pools[name][0]
        if self.files_root is None or spec.type in (PoolType.LVM, PoolType.ISCSI):
            return None
        path = Path(self.files_root) / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def list_local_disks(self):
        from .disks import LocalDisk

        return [
            LocalDisk("/dev/sdb", 12509.0, "RAID 930-8i", "6000d31000f0", "sas", media="hdd"),
            LocalDisk(
                "/dev/sdc", 894.3, "MZ7L3960", "S6KN", "sata", False, "3 partitions (vmfs)", "ssd"
            ),
            LocalDisk(
                "/dev/sdd", 894.3, "MZ7L3960", "S6KP", "sata", False, "Ceph OSD (LVM)", "ssd"
            ),
            LocalDisk("/dev/sde", 3726.0, "ST4000NM0035", "ZC1A", "sata", media="hdd"),
            LocalDisk("/dev/sdf", 3726.0, "ST4000NM0035", "ZC1B", "sata", media="hdd"),
        ]

    def list_vm_stats(self) -> dict[str, VMStats]:
        """Pretend load that drifts over time, steady enough to read."""
        t = time.time() / 30
        result = {}
        with self._lock:
            vms = [vm for vm in self._vms.values() if vm.state == VMState.RUNNING]
        for vm in vms:
            seed = sum(vm.spec.name.encode())
            wave = (math.sin(t + seed) + 1) / 2
            cpu = 3 + (seed % 7) * 9 + wave * 18
            mem = vm.spec.memory_mb * (0.3 + (seed % 5) * 0.12 + wave * 0.08)
            result[vm.uuid] = VMStats(
                cpu_pct=round(min(cpu, 100), 1),
                mem_used_mb=int(min(mem, vm.spec.memory_mb)),
                disk_read_bps=wave * (seed % 9) * 400_000,
                disk_write_bps=wave * (seed % 4) * 250_000,
                net_rx_bps=wave * (seed % 6) * 90_000,
                net_tx_bps=wave * (seed % 3) * 60_000,
            )
        return result

    def guest_info(self, uuid: str) -> GuestInfo:
        """A pretend guest agent: running VMs answer, as if qemu-guest-agent were installed."""
        with self._lock:
            vm = self._get(uuid)
            if vm.state != VMState.RUNNING:
                return GuestInfo()
            spec = vm.spec
        windows = "windows" in spec.os_type.lower() or spec.name.lower().startswith("win")
        seed = sum(spec.name.encode())
        total = sum(d.size_gb for d in spec.disks)
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
            fs_used_gb=round(total * (0.05 + seed % 60 / 100), 1),
            fs_total_gb=total,
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
            PoolType.LVM: ", ".join(spec.all_devices()),
            PoolType.DISK: spec.device or "",
        }[spec.type]
        return PoolInfo(
            name=name,
            type=spec.type,
            active=True,
            capacity_gb=capacity,
            allocation_gb=float(used),
            available_gb=capacity - used,
            source=source,
            hybrid=bool(spec.cache_devices) or name in self._caches,
            cache=self._caches.get(name),
        )

    def _get(self, uuid: str) -> VMInfo:
        try:
            return self._vms[uuid]
        except KeyError:
            raise VMNotFound(uuid) from None


def _random_mac() -> str:
    return "52:54:00:" + ":".join(f"{b:02x}" for b in os.urandom(3))


def _restart_key(spec: VMSpec) -> tuple:
    return (spec.vcpus, spec.memory_mb, spec.boot, spec.video)


def _fake_cache(mode: str, size_gb: int) -> dict:
    return {"mode": mode, "size_gb": float(size_gb), "used_pct": 42.0, "dirty_gb": 0.0,
            "read_hit_pct": 87.5, "write_hit_pct": 61.0, "hit_pct": 80.2}  # fmt: skip
