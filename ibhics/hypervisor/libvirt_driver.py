"""Driver for KVM/QEMU nodes through libvirt."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import quoteattr

from .base import (
    DiskSpec,
    Firmware,
    HostInfo,
    HypervisorDriver,
    InvalidState,
    NicSpec,
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
from .domain_xml import build_domain_xml, build_volume_xml
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
                disk.backend = "file" if _pool_kind(pool) in ("dir", "netfs") else "block"
                created.append(pool.createXML(build_volume_xml(spec.name, index, disk), 0))
            dom = self._conn.defineXML(build_domain_xml(spec))
        except Exception:
            for vol in created:
                vol.delete(0)
            raise
        return self._info(dom)

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
        for source in root.findall("./devices/disk[@device='disk']/source[@volume]"):
            pool = self._conn.storagePoolLookupByName(source.get("pool"))
            pool.storageVolLookupByName(source.get("volume")).delete(0)
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
            try:
                # dir/netfs: create the directory. logical: pvcreate + vgcreate (erases the device).
                pool.build(0)
                self._start(pool)
            except Exception:
                _undefine(pool)
                raise
        except Exception:
            if session is not None:
                _undefine(session)
            raise
        return next(p for p in self.list_pools() if p.name == spec.name)

    def delete_pool(self, name: str) -> None:
        pool = self._pool(name)
        for dom in self._conn.listAllDomains():
            root = ET.fromstring(dom.XMLDesc(0))
            if root.find(f"./devices/disk/source[@pool='{name}']") is not None:
                raise InvalidState(f"VM {dom.name()!r} still has disks in pool {name!r}")
        _undefine(pool)
        try:
            _undefine(self._conn.storagePoolLookupByName(iscsi_session_name(name)))
        except libvirt.libvirtError:
            pass

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
        if source is None or source.get("volume") is None:
            raise InvalidState("only disks in ibHiCS storage pools can be backed up")
        return self._pool(source.get("pool")).storageVolLookupByName(source.get("volume"))

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
                disk.size_gb = -(-self._volume(el).info()[1] // GIB)
            except (InvalidState, PoolNotFound, libvirt.libvirtError):
                pass
        return VMInfo(uuid=dom.UUIDString(), spec=spec, state=_state_from_libvirt(state))


def _pool_kind(pool) -> str:
    return ET.fromstring(pool.XMLDesc(0)).get("type", "")


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


def _spec_from_xml(xml: str) -> VMSpec:
    root = ET.fromstring(xml)
    memory = root.find("memory")
    amount = int(memory.text)
    # libvirt reports KiB; XML we built ourselves (and haven't round-tripped) says MiB.
    memory_mb = amount if memory.get("unit") == "MiB" else amount // 1024
    disks = []
    for disk in root.findall("./devices/disk[@device='disk']"):
        source = disk.find("source")
        driver = disk.find("driver")
        backend = "block" if driver is not None and driver.get("type") == "raw" else "file"
        disks.append(
            DiskSpec(
                size_gb=0,  # capacity lives on the volume, not in the domain XML
                backend=backend,
                pool=source.get("pool", "") if source is not None else "",
            )
        )
    nics = [
        NicSpec(
            network=iface.find("source").get("network", ""),
            model=iface.find("model").get("type", "virtio"),
            mac=iface.find("mac").get("address") if iface.find("mac") is not None else None,
        )
        for iface in root.findall("./devices/interface[@type='network']")
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
    )
