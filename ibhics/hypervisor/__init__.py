from .base import (
    DiskEdit,
    DiskSpec,
    Firmware,
    GuestInfo,
    GuestInterface,
    HostInfo,
    HypervisorDriver,
    InvalidState,
    MigrationFailed,
    NicEdit,
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


def load_driver(name: str, libvirt_uri: str = "qemu:///system") -> HypervisorDriver:
    if name == "fake":
        from .fake import FakeDriver

        return FakeDriver()
    if name == "libvirt":
        from .libvirt_driver import LibvirtDriver

        return LibvirtDriver(libvirt_uri)
    raise ValueError(f"unknown driver {name!r}; use 'fake' or 'libvirt'")


__all__ = [
    "DiskEdit",
    "DiskSpec",
    "Firmware",
    "GuestInfo",
    "GuestInterface",
    "HostInfo",
    "HypervisorDriver",
    "InvalidState",
    "MigrationFailed",
    "NicEdit",
    "NicSpec",
    "PoolInfo",
    "PoolNotFound",
    "PoolSpec",
    "PoolType",
    "VMExists",
    "VMInfo",
    "VMNotFound",
    "VMEdit",
    "VMSpec",
    "VMState",
    "load_driver",
]
