"""JSON form of VM specs, shared by the agent API and backup manifests."""

from __future__ import annotations

from .base import DiskSpec, Firmware, NicSpec, VMInfo, VMSpec


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
    }
