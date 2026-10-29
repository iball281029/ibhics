"""Driver for KVM/QEMU nodes through libvirt."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .base import (
    DiskSpec,
    Firmware,
    HostInfo,
    HypervisorDriver,
    InvalidState,
    NicSpec,
    VMExists,
    VMInfo,
    VMNotFound,
    VMSpec,
    VMState,
)
from .domain_xml import build_domain_xml, build_volume_xml

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


class LibvirtDriver(HypervisorDriver):
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
                pool = self._conn.storagePoolLookupByName(disk.pool)
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

    def _lookup(self, uuid: str):
        try:
            return self._conn.lookupByUUIDString(uuid)
        except libvirt.libvirtError:
            raise VMNotFound(uuid) from None

    def _info(self, dom) -> VMInfo:
        state, _reason = dom.state()
        return VMInfo(
            uuid=dom.UUIDString(),
            spec=_spec_from_xml(dom.XMLDesc(0)),
            state=_state_from_libvirt(state),
        )


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
        backend = "rbd" if driver is not None and driver.get("type") == "raw" else "file"
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
