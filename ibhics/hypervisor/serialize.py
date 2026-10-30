"""JSON form of VM specs, shared by the agent API and backup manifests."""

from __future__ import annotations

from .base import DiskSpec, Firmware, GuestInfo, GuestInterface, NicSpec, VMInfo, VMSpec


def spec_from_json(data: dict) -> VMSpec:
    return VMSpec(
        name=data["name"],
        vcpus=data["vcpus"],
        memory_mb=data["memory_mb"],
        firmware=Firmware(data["firmware"]),
        disks=[DiskSpec(**d) for d in data["disks"]],
        nics=[NicSpec(**n) for n in data["nics"]],
        iso=data.get("iso"),
        description=data.get("description", ""),
        os_type=data.get("os_type", ""),
    )


def info_from_json(data: dict) -> VMInfo:
    return VMInfo(uuid=data["uuid"], spec=spec_from_json(data), state=data["state"])


def spec_to_json(spec: VMSpec) -> dict:
    return {
        "name": spec.name,
        "vcpus": spec.vcpus,
        "memory_mb": spec.memory_mb,
        "firmware": str(spec.firmware),
        "disks": [{"size_gb": d.size_gb, "backend": d.backend, "pool": d.pool} for d in spec.disks],
        "nics": [{"network": n.network, "model": n.model, "mac": n.mac} for n in spec.nics],
        "iso": spec.iso,
        "description": spec.description,
        "os_type": spec.os_type,
    }


def guest_to_json(guest: GuestInfo) -> dict:
    return {
        "agent": guest.agent,
        "os_name": guest.os_name,
        "os_version": guest.os_version,
        "kernel": guest.kernel,
        "hostname": guest.hostname,
        "interfaces": [{"name": i.name, "mac": i.mac, "ips": i.ips} for i in guest.interfaces],
        "ips": guest.ips,
    }


def guest_from_json(data: dict) -> GuestInfo:
    return GuestInfo(
        agent=data["agent"],
        os_name=data.get("os_name", ""),
        os_version=data.get("os_version", ""),
        kernel=data.get("kernel", ""),
        hostname=data.get("hostname", ""),
        interfaces=[GuestInterface(**i) for i in data.get("interfaces", [])],
    )
