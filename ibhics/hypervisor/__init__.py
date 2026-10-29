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


def load_driver(name: str, libvirt_uri: str = "qemu:///system") -> HypervisorDriver:
    if name == "fake":
        from .fake import FakeDriver

        return FakeDriver()
    if name == "libvirt":
        from .libvirt_driver import LibvirtDriver

        return LibvirtDriver(libvirt_uri)
    raise ValueError(f"unknown driver {name!r}; use 'fake' or 'libvirt'")


__all__ = [
    "DiskSpec",
    "Firmware",
    "HostInfo",
    "HypervisorDriver",
    "InvalidState",
    "NicSpec",
    "PoolInfo",
    "PoolNotFound",
    "PoolSpec",
    "PoolType",
    "VMExists",
    "VMInfo",
    "VMNotFound",
    "VMSpec",
    "VMState",
    "load_driver",
]
