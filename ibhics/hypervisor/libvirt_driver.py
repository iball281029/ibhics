"""Driver for KVM/QEMU nodes through libvirt."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid as uuidlib
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .base import (
    DiskSpec,
    Firmware,
    GuestInfo,
    GuestInterface,
    HostInfo,
    HypervisorDriver,
    InvalidState,
    MigrationFailed,
    NicSpec,
    PoolInfo,
    PoolNotFound,
    PoolSpec,
    PoolType,
    VMEdit,
    VMExists,
    VMInfo,
    VMNotFound,
    VMSpec,
    VMState,
)
from .domain_xml import (
    DISK_DEVS,
    META_NS,
    build_domain_xml,
    build_volume_xml,
    cdrom_element,
    disk_element,
    nic_element,
    volume_name,
)
from .pool_xml import (
    build_iscsi_session_xml,
    build_lvm_pool_xml,
    build_pool_xml,
    describe_pool_xml,
    iscsi_session_name,
)

GIB = 1024**3
SCRATCH_DIR = "/var/lib/ibhics/scratch"

try:
    import libvirt
except ImportError:  # pragma: no cover - depends on the host
    libvirt = None


def _state_from_libvirt(code: int) -> VMState:
    if code == libvirt.VIR_DOMAIN_RUNNING:
        return VMState.RUNNING
    if code == libvirt.VIR_DOMAIN_PAUSED:
        return VMState.PAUSED
    stopped = (libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_DOMAIN_SHUTDOWN, libvirt.VIR_DOMAIN_CRASHED)
    if code in stopped:
        return VMState.STOPPED
    return VMState.UNKNOWN


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {result.stderr.strip() or result.returncode}")


class LibvirtDriver(HypervisorDriver):
    # Backups copy disks here before they are chunked; it needs room for the largest VM
    # (raw images are sparse, so only the data actually written counts).
    scratch_dir = SCRATCH_DIR
    run = staticmethod(_run)

    def __init__(self, uri: str = "qemu:///system") -> None:
        if libvirt is None:
            raise RuntimeError("libvirt-python is not installed: pip install 'ibhics[libvirt]'")
        self._conn = libvirt.open(uri)

    def host_info(self) -> HostInfo:
        _model, memory_mb, cpus, *_ = self._conn.getInfo()
        return HostInfo(
            hostname=self._conn.getHostname(),
            cpus=cpus,
            memory_mb=memory_mb,
            hypervisor=f"kvm (libvirt {self._conn.getLibVersion()})",
        )

    def list_vms(self) -> list[VMInfo]:
        return [self._info(dom) for dom in self._conn.listAllDomains()]

    def get_vm(self, uuid: str) -> VMInfo:
        return self._info(self._lookup(uuid))

    def create_vm(self, spec: VMSpec) -> VMInfo:
        spec.validate()
        try:
            self._conn.lookupByName(spec.name)
        except libvirt.libvirtError:
            pass
        else:
            raise VMExists(f"VM named {spec.name!r} already exists")

        created = []
        try:
            for index, disk in enumerate(spec.disks):
                pool = self._pool(disk.pool)
                disk.backend = _backend(pool)
                created.append(pool.createXML(build_volume_xml(spec.name, index, disk), 0))
            dom = self._conn.defineXML(build_domain_xml(spec))
        except Exception:
            for vol in created:
                vol.delete(0)
            raise
        return self._info(dom)

    def insert_cdrom(self, uuid: str, iso: str | None) -> VMInfo:
        if iso and not Path(iso).is_file():
            raise ValueError(f"{iso} is not on node")
        dom = self._lookup(uuid)
        root = ET.fromstring(dom.XMLDesc(0))
        drive = root.find("./devices/disk[@device='cdrom']")
        new = cdrom_element(iso)
        if drive is not None:
            new.find("target").attrib.update(drive.find("target").attrib)
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
            if dom.isActive():
                flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
            dom.updateDeviceFlags(ET.tostring(new, encoding="unicode"), flags)
        elif dom.isActive():
            raise InvalidState("this VM has no CD drive; shut it down once to add one")
        else:  # VMs from before every VM had a drive
            root.find("devices").append(new)
            self._conn.defineXML(ET.tostring(root, encoding="unicode"))
        return self._info(self._lookup(uuid))

    def update_vm(self, uuid: str, edit: VMEdit) -> VMInfo:
        dom = self._lookup(uuid)
        current = self._info(dom)
        new = edit.apply_to(current.spec)
        running = dom.isActive()
        root = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
        name = root.findtext("name")

        description = root.find("description")
        if new.description:
            if description is None:
                description = ET.Element("description")
                root.insert(1, description)
            description.text = new.description
        elif description is not None:
            root.remove(description)
        meta = root.find(f"metadata/{{{META_NS}}}vm")
        if meta is None:
            metadata = root.find("metadata")
            if metadata is None:
                metadata = ET.SubElement(root, "metadata")
            meta = ET.SubElement(metadata, f"{{{META_NS}}}vm")
        os_label = meta.find(f"{{{META_NS}}}os")
        if os_label is None:
            os_label = ET.SubElement(meta, f"{{{META_NS}}}os")
        os_label.text = new.os_type
        root.find("vcpu").text = str(new.vcpus)
        for tag in ("memory", "currentMemory"):
            el = root.find(tag)
            if el is not None:
                el.set("unit", "KiB")
                el.text = str(new.memory_mb * 1024)
        if edit.boot is not None:
            os_el = root.find("os")
            for boot in os_el.findall("boot"):
                os_el.remove(boot)
            for dev in new.boot:
                ET.SubElement(os_el, "boot", dev=dev)

        devices = root.find("devices")
        attach, detach, update, resize, removed, created = [], [], [], [], [], []
        try:
            if edit.disks is not None:
                self._edit_disks(root, meta, name, current, edit, attach, detach, resize, created)
                removed = [(el, pool, vol) for el, pool, vol in detach if edit.delete_removed_disks]
                detach = [el for el, _pool, _vol in detach]
            if edit.nics is not None:
                self._edit_nics(devices, edit, attach, detach, update)
            self._conn.defineXML(ET.tostring(root, encoding="unicode"))
        except Exception:
            for vol in created:
                vol.delete(0)
            raise

        for vol, dev, size in resize:
            if running:
                dom.blockResize(dev, size, libvirt.VIR_DOMAIN_BLOCK_RESIZE_BYTES)
            else:
                vol.resize(size, 0)
        busy = set()
        if running:
            live = libvirt.VIR_DOMAIN_AFFECT_LIVE
            for el in detach:
                try:
                    dom.detachDeviceFlags(ET.tostring(el, encoding="unicode"), live)
                except libvirt.libvirtError:
                    busy.add(id(el))  # the guest holds it; gone after the next restart
            for el in attach:
                dom.attachDeviceFlags(ET.tostring(el, encoding="unicode"), live)
            for el in update:
                dom.updateDeviceFlags(ET.tostring(el, encoding="unicode"), live)
        for el, pool, volume in removed:
            if volume and id(el) not in busy:
                self._pool(pool).storageVolLookupByName(volume).delete(0)
        if edit.autostart is not None:
            dom.setAutostart(int(edit.autostart))
        return self._info(self._lookup(uuid))

    def _edit_disks(self, root, meta, name, current, edit, attach, detach, resize, created):
        devices = root.find("devices")
        disks = devices.findall("disk[@device='disk']")
        keep = {d.index for d in edit.disks if d.index is not None}
        for index, el in enumerate(disks):
            if index in keep:
                continue
            detach.append((el, *disk_source(root, el)))  # before its metadata goes
            devices.remove(el)
        for d in edit.disks:
            if d.index is not None and d.size_gb > current.spec.disks[d.index].size_gb:
                el = disks[d.index]
                resize.append((self._volume(el), el.find("target").get("dev"), d.size_gb * GIB))
        # Metadata for removed disks goes; that for kept ones stays.
        gone = {el.find("target").get("dev") for el, _pool, _vol in detach}
        for entry in meta.findall(f"{{{META_NS}}}disk"):
            if entry.get("dev") in gone:
                meta.remove(entry)
        used = {t.get("dev") for t in devices.findall("disk/target")}
        for d in (d for d in edit.disks if d.index is None):
            pool = self._pool(d.pool)
            disk = DiskSpec(size_gb=d.size_gb, pool=d.pool, backend=_backend(pool))
            n = 0
            while True:  # a volume name nobody uses yet, also not a detached disk's
                try:
                    pool.storageVolLookupByName(volume_name(name, n, disk))
                except libvirt.libvirtError:
                    break
                n += 1
            dev = next(f"vd{c}" for c in DISK_DEVS if f"vd{c}" not in used)
            used.add(dev)
            created.append(pool.createXML(build_volume_xml(name, n, disk), 0))
            el = disk_element(disk, volume_name(name, n, disk), dev)
            devices.append(el)
            ET.SubElement(
                meta, f"{{{META_NS}}}disk", dev=dev, pool=d.pool, volume=volume_name(name, n, disk)
            )
            attach.append(el)

    @staticmethod
    def _edit_nics(devices, edit, attach, detach, update):
        wanted = {n.mac: n for n in edit.nics if n.mac}
        for el in devices.findall("interface"):
            mac = el.find("mac").get("address") if el.find("mac") is not None else None
            nic = wanted.get(mac)
            if nic is None:
                devices.remove(el)
                detach.append(el)
                continue
            source = el.find("source")
            if el.get("type") != "network" or source.get("network") != nic.network:
                el.set("type", "network")
                source.attrib.clear()
                source.set("network", nic.network)
                update.append(el)
        for nic in (n for n in edit.nics if not n.mac):
            # Our own MAC, so the saved and the live NIC are the same one.
            el = nic_element(NicSpec(network=nic.network, model=nic.model, mac=_random_mac()))
            devices.append(el)
            attach.append(el)

    def start_vm(self, uuid: str) -> VMInfo:
        dom = self._lookup(uuid)
        if dom.isActive():
            raise InvalidState("VM is already running")
        dom.create()
        return self._info(dom)

    def stop_vm(self, uuid: str, force: bool = False) -> VMInfo:
        dom = self._lookup(uuid)
        if not dom.isActive():
            raise InvalidState("VM is already stopped")
        if force:
            dom.destroy()
        else:
            # ACPI shutdown; the guest powers off on its own, so state may still read running.
            dom.shutdown()
        return self._info(dom)

    def delete_vm(self, uuid: str) -> None:
        dom = self._lookup(uuid)
        if dom.isActive():
            raise InvalidState("stop the VM before deleting it")
        root = ET.fromstring(dom.XMLDesc(0))
        for disk in root.findall("./devices/disk[@device='disk']"):
            pool, volume = disk_source(root, disk)
            if volume:
                self._conn.storagePoolLookupByName(pool).storageVolLookupByName(volume).delete(0)
        dom.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)

    def list_pools(self) -> list[PoolInfo]:
        pools = self._conn.listAllStoragePools()
        sessions = {}
        for pool in pools:
            if _pool_kind(pool) == "iscsi":
                root = ET.fromstring(pool.XMLDesc(0))
                host = root.find("source/host")
                device = root.find("source/device")
                sessions[pool.name()] = (
                    f"{host.get('name')}:{host.get('port', '3260')} {device.get('path')}"
                )
        result = []
        for pool in pools:
            described = describe_pool_xml(pool.XMLDesc(0), sessions)
            if described is None:
                continue
            kind, source = described
            _state, capacity, allocation, available = pool.info()
            result.append(
                PoolInfo(
                    name=pool.name(),
                    type=kind,
                    active=bool(pool.isActive()),
                    capacity_gb=round(capacity / GIB, 1),
                    allocation_gb=round(allocation / GIB, 1),
                    available_gb=round(available / GIB, 1),
                    source=source,
                )
            )
        return result

    def create_pool(self, spec: PoolSpec) -> PoolInfo:
        spec.validate()
        if spec.name in self._pool_names() or iscsi_session_name(spec.name) in self._pool_names():
            raise InvalidState(f"a pool named {spec.name!r} already exists")

        if spec.type == PoolType.LVM and spec.device.startswith("/dev/disk/"):
            # libvirt finds the volume group by the device name pvs reports (/dev/sda), so a
            # /dev/disk/by-id link would make the pool fail to start after vgcreate.
            # (/dev/mapper names are what pvs reports for multipath, so they stay.)
            spec = replace(spec, device=os.path.realpath(spec.device))
        if spec.type == PoolType.DISK:
            # The pool mounts its disk on every start, so it must not follow /dev/sdX letters.
            spec = replace(spec, device=stable_device(spec.device))
        session = None
        try:
            if spec.type == PoolType.ISCSI:
                session = self._start(
                    self._conn.storagePoolDefineXML(build_iscsi_session_xml(spec))
                )
                device = _lun_path(session, spec.lun)
                xml = build_lvm_pool_xml(spec.name, device)
            else:
                xml = build_pool_xml(spec)
            pool = self._conn.storagePoolDefineXML(xml)
            built = False
            try:
                # dir/netfs: create the directory. logical: pvcreate + vgcreate (erases the device).
                # fs: mkfs.xfs over whatever is on the disk, which the caller confirmed.
                flags = (
                    libvirt.VIR_STORAGE_POOL_BUILD_OVERWRITE if spec.type == PoolType.DISK else 0
                )
                pool.build(flags)
                built = True
                self._start(pool)
            except Exception:
                root = ET.fromstring(pool.XMLDesc(0))
                _undefine(pool)
                if built and root.get("type") == "logical":
                    # Do not leave a volume group behind that blocks the next try.
                    vg = root.findtext("source/name")
                    device = root.find("source/device").get("path")
                    for cmd in (["vgremove", "-f", vg], ["pvremove", "-ff", "-y", device]):
                        try:
                            self.run(cmd)
                        except RuntimeError:
                            pass
                raise
        except Exception:
            if session is not None:
                _undefine(session)
            raise
        return next(p for p in self.list_pools() if p.name == spec.name)

    def list_local_disks(self):
        from .disks import list_local_disks

        return list_local_disks()

    @contextmanager
    def scratch_space(self, pool: str, size_gb: int) -> Iterator[Path]:
        try:
            target = self._conn.storagePoolLookupByName(pool)
        except libvirt.libvirtError:
            raise PoolNotFound(pool) from None
        root = ET.fromstring(target.XMLDesc(0))
        if root.get("type") in ("dir", "netfs", "fs"):
            # Raw images are sparse, so this takes only the data actually copied.
            base = Path(root.findtext("target/path")) / ".ibhics-scratch"
            base.mkdir(parents=True, exist_ok=True)
            path = Path(tempfile.mkdtemp(dir=base))
            try:
                yield path
            finally:
                shutil.rmtree(path, ignore_errors=True)
            return
        if root.get("type") != "logical" or size_gb <= 0:
            with super().scratch_space(pool, size_gb) as path:
                yield path
            return
        # LVM pool: a temporary logical volume with a filesystem, removed afterwards.
        name = f"ibhics-scratch-{uuidlib.uuid4().hex[:8]}"
        vol = target.createXML(
            f"<volume><name>{name}</name><capacity unit='GiB'>{size_gb + 1}</capacity></volume>", 0
        )
        Path(self.scratch_dir).mkdir(parents=True, exist_ok=True)
        mount = Path(tempfile.mkdtemp(prefix=f"{pool}-", dir=self.scratch_dir))
        try:
            self.run(["mkfs.xfs", "-q", vol.path()])
            self.run(["mount", vol.path(), str(mount)])
            try:
                yield mount
            finally:
                self.run(["umount", str(mount)])
        finally:
            mount.rmdir()
            vol.delete(0)

    def delete_pool(self, name: str) -> None:
        pool = self._pool(name)
        for dom in self._conn.listAllDomains():
            root = ET.fromstring(dom.XMLDesc(0))
            disks = root.findall("./devices/disk[@device='disk']")
            if any(disk_source(root, d)[0] == name for d in disks):
                raise InvalidState(f"VM {dom.name()!r} still has disks in pool {name!r}")
        _undefine(pool)
        try:
            _undefine(self._conn.storagePoolLookupByName(iscsi_session_name(name)))
        except libvirt.libvirtError:
            pass

    def migrate_vm(
        self,
        uuid: str,
        dest_uri: str,
        copy_storage: bool = False,
        bandwidth_mib: int | None = None,
    ) -> None:
        dom = self._lookup(uuid)
        # Peer-to-peer: this node's libvirtd connects to the destination's itself, over TLS
        # or SSH as set up when the node joined. The destination keeps the definition and
        # this node forgets it, so the VM is never defined on two nodes at once.
        flags = (
            libvirt.VIR_MIGRATE_PEER2PEER
            | libvirt.VIR_MIGRATE_PERSIST_DEST
            | libvirt.VIR_MIGRATE_UNDEFINE_SOURCE
        )
        if dom.isActive():
            flags |= libvirt.VIR_MIGRATE_LIVE | libvirt.VIR_MIGRATE_AUTO_CONVERGE
            if copy_storage:
                # libvirt creates same-named volumes in the same-named pools on the other side.
                flags |= libvirt.VIR_MIGRATE_NON_SHARED_DISK
        elif copy_storage:
            raise InvalidState("only a running VM can have its disks copied; start it first")
        else:
            flags |= libvirt.VIR_MIGRATE_OFFLINE
        root = ET.fromstring(dom.XMLDesc(0))
        disks = root.findall("./devices/disk[@device='disk']")
        volumes = [self._volume(d) for d in disks] if copy_storage else []
        params = {}
        if bandwidth_mib:
            params[libvirt.VIR_MIGRATE_PARAM_BANDWIDTH] = bandwidth_mib
        precreated = []
        try:
            if copy_storage:
                precreated = self._precreate_block_disks(dest_uri, root, disks, volumes)
            dom.migrateToURI3(dest_uri, params, flags)
        except libvirt.libvirtError as exc:
            for vol in precreated:
                try:
                    vol.delete(0)
                except libvirt.libvirtError:
                    pass
            raise MigrationFailed(str(exc)) from None
        for vol in volumes:  # the copies now live on the destination
            vol.delete(0)

    def _precreate_block_disks(self, dest_uri, root, disks, volumes) -> list:
        """libvirt creates the destination copy of a volume disk itself, but not of a
        block disk, so make those LVs on the destination first."""
        block = [(d, v) for d, v in zip(disks, volumes, strict=True) if d.get("type") == "block"]
        if not block:
            return []
        dest = libvirt.open(dest_uri)
        created = []
        try:
            for disk, vol in block:
                pool, name = disk_source(root, disk)
                capacity = vol.info()[1]
                xml = f"<volume><name>{escape(name)}</name><capacity>{capacity}</capacity></volume>"
                created.append(dest.storagePoolLookupByName(pool).createXML(xml, 0))
        except libvirt.libvirtError:
            for vol in created:
                vol.delete(0)
            raise
        return created

    def guest_info(self, uuid: str) -> GuestInfo:
        dom = self._lookup(uuid)
        if not dom.isActive():
            return GuestInfo()
        info = GuestInfo()
        try:
            data = dom.guestInfo(
                libvirt.VIR_DOMAIN_GUEST_INFO_OS | libvirt.VIR_DOMAIN_GUEST_INFO_HOSTNAME, 0
            )
            info.agent = True
            info.os_name = data.get("os.pretty-name") or data.get("os.name", "")
            info.os_version = data.get("os.version", "")
            info.kernel = data.get("os.kernel-release", "")
            info.hostname = data.get("hostname", "")
        except libvirt.libvirtError:
            pass  # no agent in the guest, or it isn't running yet
        if info.agent:
            try:
                fs = dom.guestInfo(libvirt.VIR_DOMAIN_GUEST_INFO_FILESYSTEM, 0)
                info.fs_used_gb, info.fs_total_gb = filesystem_usage(fs)
            except libvirt.libvirtError:
                pass
        sources = [libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT] if info.agent else []
        sources.append(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE)
        for source in sources:
            try:
                ifaces = dom.interfaceAddresses(source, 0)
            except libvirt.libvirtError:
                continue
            info.interfaces = [
                GuestInterface(
                    name=name,
                    mac=data.get("hwaddr") or "",
                    ips=[f"{a['addr']}/{a['prefix']}" for a in data.get("addrs") or []],
                )
                for name, data in ifaces.items()
                if name != "lo"
            ]
            if info.interfaces:
                break
        return info

    def console_address(self, uuid: str) -> tuple[str, int]:
        dom = self._lookup(uuid)
        if not dom.isActive():
            raise InvalidState("start the VM to open its console")
        graphics = ET.fromstring(dom.XMLDesc(0)).find("./devices/graphics[@type='vnc']")
        port = int(graphics.get("port", "-1")) if graphics is not None else -1
        if port <= 0:
            raise InvalidState("the VM has no VNC display")
        return "127.0.0.1", port

    @contextmanager
    def export_disks(self, uuid: str) -> Iterator[list[Path]]:
        dom = self._lookup(uuid)
        disks = ET.fromstring(dom.XMLDesc(0)).findall("./devices/disk[@device='disk']")
        Path(self.scratch_dir).mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix=f"backup-{uuid}-", dir=self.scratch_dir))
        try:
            images = [scratch / f"disk{i}.raw" for i in range(len(disks))]
            if dom.isActive():
                # Push-mode backup: QEMU copies a point-in-time view while the guest runs.
                self._push_backup(dom, disks, images)
            else:
                for disk, image in zip(disks, images, strict=True):
                    self.run(
                        ["qemu-img", "convert", "-O", "raw", self._disk_path(disk), str(image)]
                    )
            yield images
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def import_disk(self, uuid: str, index: int, raw_image: Path) -> None:
        dom = self._lookup(uuid)
        if dom.isActive():
            raise InvalidState("stop the VM before overwriting its disks")
        disks = ET.fromstring(dom.XMLDesc(0)).findall("./devices/disk[@device='disk']")
        vol = self._volume(disks[index])
        if Path(raw_image).stat().st_size > vol.info()[1]:
            raise ValueError(f"image is larger than disk {index}")
        driver = disks[index].find("driver")
        fmt = driver.get("type", "qcow2") if driver is not None else "qcow2"
        self.run(["qemu-img", "convert", "-n", "-f", "raw", "-O", fmt, str(raw_image), vol.path()])

    def _push_backup(self, dom, disks, images: list[Path], poll: float = 1.0) -> None:
        entries = "".join(
            f"<disk name={quoteattr(d.find('target').get('dev'))} backup='yes' type='file'>"
            f"<target file={quoteattr(str(image))}/><driver type='raw'/></disk>"
            for d, image in zip(disks, images, strict=True)
        )
        dom.backupBegin(f"<domainbackup mode='push'><disks>{entries}</disks></domainbackup>", None)
        while (
            dom.jobStats(0).get("type", libvirt.VIR_DOMAIN_JOB_NONE) != libvirt.VIR_DOMAIN_JOB_NONE
        ):
            time.sleep(poll)
        done = dom.jobStats(libvirt.VIR_DOMAIN_JOB_STATS_COMPLETED)
        if done.get("type") != libvirt.VIR_DOMAIN_JOB_COMPLETED:
            raise RuntimeError(f"disk backup of {dom.name()} did not complete: {done}")

    def _volume(self, disk):
        source = disk.find("source")
        if source is not None and source.get("volume"):
            return self._pool(source.get("pool")).storageVolLookupByName(source.get("volume"))
        if source is not None and source.get("dev"):
            try:
                return self._conn.storageVolLookupByPath(source.get("dev"))
            except libvirt.libvirtError:
                pass
        raise InvalidState("only disks in ibHiCS storage pools can be backed up")

    def _disk_path(self, disk) -> str:
        return self._volume(disk).path()

    def _pool(self, name: str):
        try:
            return self._conn.storagePoolLookupByName(name)
        except libvirt.libvirtError:
            raise PoolNotFound(name) from None

    def _pool_names(self) -> set[str]:
        return {p.name() for p in self._conn.listAllStoragePools()}

    @staticmethod
    def _start(pool):
        if not pool.isActive():
            pool.create(0)
        pool.setAutostart(1)
        return pool

    def _lookup(self, uuid: str):
        try:
            return self._conn.lookupByUUIDString(uuid)
        except libvirt.libvirtError:
            raise VMNotFound(uuid) from None

    def _info(self, dom) -> VMInfo:
        state, _reason = dom.state()
        xml = dom.XMLDesc(0)
        spec = _spec_from_xml(xml)
        # The domain XML doesn't carry disk sizes; read them from the volumes.
        for disk, el in zip(
            spec.disks, ET.fromstring(xml).findall("./devices/disk[@device='disk']"), strict=True
        ):
            try:
                _type, capacity, allocation = self._volume(el).info()
            except (InvalidState, PoolNotFound, libvirt.libvirtError):
                # Not in a pool we know (a hand-written disk): ask QEMU/libvirt directly.
                source = el.find("source")
                path = source.get("dev") or source.get("file") if source is not None else None
                try:
                    capacity, allocation, _physical = dom.blockInfo(path)
                except (TypeError, libvirt.libvirtError):
                    continue
            disk.size_gb = -(-capacity // GIB)
            disk.used_gb = round(allocation / GIB, 1)
        info = VMInfo(uuid=dom.UUIDString(), spec=spec, state=_state_from_libvirt(state))
        try:
            spec.autostart = bool(dom.autostart())
            if info.state == VMState.RUNNING:
                saved = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
                info.restart_needed = _restart_key(saved) != _restart_key(ET.fromstring(xml))
        except libvirt.libvirtError:
            pass
        return info


def stable_device(device: str, by_id: str = "/dev/disk/by-id") -> str:
    """A /dev/disk/by-id name for device, preferring the WWN; device itself if none."""
    if device.startswith("/dev/disk/"):
        return device
    real = os.path.realpath(device)
    try:
        names = sorted(os.listdir(by_id), key=lambda n: (not n.startswith("wwn-"), n))
    except OSError:
        return device
    for name in names:
        link = os.path.join(by_id, name)
        if "-part" not in name and os.path.realpath(link) == real:
            return link
    return device


def filesystem_usage(data: dict) -> tuple[float | None, float | None]:
    """Used and total GB over the guest's filesystems, from guestInfo's fs.N.* keys. A
    filesystem mounted twice (bind mounts, btrfs subvolumes) counts once."""
    seen, used, total = set(), 0, 0
    for i in range(int(data.get("fs.count", 0))):
        size = data.get(f"fs.{i}.total-bytes")
        if size is None:
            continue
        key = (data.get(f"fs.{i}.name"), size)
        if key in seen:
            continue
        seen.add(key)
        total += int(size)
        used += int(data.get(f"fs.{i}.used-bytes", 0))
    if not total:
        return None, None
    return round(used / GIB, 1), round(total / GIB, 1)


def _pool_kind(pool) -> str:
    return ET.fromstring(pool.XMLDesc(0)).get("type", "")


def _backend(pool) -> str:
    return "file" if _pool_kind(pool) in ("dir", "netfs", "fs") else "block"


def _random_mac() -> str:
    return "52:54:00:" + ":".join(f"{b:02x}" for b in os.urandom(3))


def _restart_key(root) -> tuple:
    memory = root.find("memory")
    return (
        root.findtext("vcpu"),
        memory.text if memory is not None else None,
        [b.get("dev") for b in root.findall("os/boot")],
    )


def _undefine(pool) -> None:
    if pool.isActive():
        pool.destroy()
    pool.undefine()


def _lun_path(session, lun: int) -> str:
    session.refresh(0)
    for vol in session.listAllVolumes():
        path = vol.path()
        if path.endswith(f"-lun-{lun}"):
            return path
    raise ValueError(f"LUN {lun} not found on the iSCSI target")


def disk_source(root, disk) -> tuple[str, str]:
    """(pool, volume) of a domain disk: from <source pool volume>, or for a block disk
    from the ibHiCS metadata kept beside it. Empty strings for disks outside any pool."""
    source = disk.find("source")
    if source is not None and source.get("volume"):
        return source.get("pool", ""), source.get("volume")
    target = disk.find("target")
    dev = target.get("dev") if target is not None else None
    for meta in root.findall(f"metadata/{{{META_NS}}}vm/{{{META_NS}}}disk"):
        if meta.get("dev") == dev:
            return meta.get("pool", ""), meta.get("volume", "")
    return "", ""


def _spec_from_xml(xml: str) -> VMSpec:
    root = ET.fromstring(xml)
    memory = root.find("memory")
    amount = int(memory.text)
    # libvirt reports KiB; XML we built ourselves (and haven't round-tripped) says MiB.
    memory_mb = amount if memory.get("unit") == "MiB" else amount // 1024
    disks = []
    for disk in root.findall("./devices/disk[@device='disk']"):
        driver = disk.find("driver")
        backend = "block" if driver is not None and driver.get("type") == "raw" else "file"
        disks.append(
            DiskSpec(
                size_gb=0,  # capacity lives on the volume, not in the domain XML
                backend=backend,
                pool=disk_source(root, disk)[0],
            )
        )
    nics = [
        NicSpec(
            # A hand-made <interface type='bridge'> reports its bridge, e.g. br0.
            network=iface.find("source").get("network") or iface.find("source").get("bridge", ""),
            model=iface.find("model").get("type", "virtio")
            if iface.find("model") is not None
            else "",
            mac=iface.find("mac").get("address") if iface.find("mac") is not None else None,
        )
        for iface in root.findall("./devices/interface")
        if iface.get("type") in ("network", "bridge") and iface.find("source") is not None
    ]
    cdrom = root.find("./devices/disk[@device='cdrom']/source")
    os_el = root.find("os")
    firmware = (
        Firmware.UEFI if os_el is not None and os_el.get("firmware") == "efi" else Firmware.BIOS
    )
    return VMSpec(
        name=root.findtext("name", ""),
        vcpus=int(root.findtext("vcpu", "1")),
        memory_mb=memory_mb,
        firmware=firmware,
        disks=disks,
        nics=nics,
        iso=cdrom.get("file") if cdrom is not None else None,
        description=root.findtext("description", ""),
        os_type=root.findtext(f"metadata/{{{META_NS}}}vm/{{{META_NS}}}os", ""),
        boot=[b.get("dev") for b in root.findall("os/boot")] or None,
    )
