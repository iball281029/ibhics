"""Validated disk selections and the destructive-operation review for Virtual Storage."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME = r"^[a-z][a-z0-9-]{0,30}$"
NODE = r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,62}$"
DEVICE = r"^/dev/(?:sd[a-z]+|vd[a-z]+|nvme[0-9]+n[0-9]+)$"
RF2_CREATION_POLICY = "rf2-aggregate-v1"


class NodeDisks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(pattern=NODE)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    data: list[str] = Field(min_length=1, max_length=8)
    cache: list[str] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def unique_devices(self):
        import re

        devices = self.data + self.cache
        if len(set(devices)) != len(devices):
            raise ValueError("a disk can be selected only once, as data or cache")
        if not all(re.fullmatch(DEVICE, d) for d in devices):
            raise ValueError("select whole local disks, not partitions or device mapper volumes")
        return self


class StorageCreateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replicas: Literal[2] = 2
    name: str = Field(default="hybrid01", pattern=NAME)
    nodes: list[NodeDisks] = Field(min_length=2, max_length=2)
    arbiter: str = Field(pattern=NODE)
    virtual_ip: str
    prefix: int = Field(default=24, ge=1, le=30)
    size_gib: int = Field(ge=1, le=1048576)

    @model_validator(mode="after")
    def distinct_nodes_and_valid_ip(self):
        import ipaddress

        addresses = [n.node for n in self.nodes] + [self.arbiter]
        if len(set(addresses)) != 3:
            raise ValueError("select two different data nodes and a separate Arbiter")
        ip = ipaddress.IPv4Address(self.virtual_ip)
        net = ipaddress.IPv4Network(f"{ip}/{self.prefix}", strict=False)
        if ip in (net.network_address, net.broadcast_address) or not ip.is_private:
            raise ValueError("storage VIP must be a private host IPv4 address")
        self.virtual_ip = str(ip)
        return self


class StorageCreateConfirm(BaseModel):
    token: str = Field(pattern=r"^[0-9a-f]{48}$")
    confirm_name: str = Field(pattern=NAME)
    confirm_wipe: bool = False


def thin_geometry(sizes: list[float]) -> tuple[int, int]:
    # RF2 protection is across nodes. Do not halve capacity again with local RAID1.
    gib = sum(sizes)
    metadata_mib = 128 if gib < 100 else 1024
    return int(gib * 1024 * 0.90) - 2 * metadata_mib - 256, metadata_mib


def replication_reserve_mib(thin_mib: int) -> int:
    # Two DRBD peer bitmap slots (32 KiB/GiB each), plus activity-log,
    # alignment and the gateway's private volume. Keep at least 1 GiB.
    return max(1024, math.ceil(thin_mib / 16384) + 256)


def review_node(selection: NodeDisks, inventory: dict, size_gib: int) -> dict:
    if not inventory or inventory.get("error"):
        raise ValueError(f"Cannot read disks on {selection.node}; refresh and try again")
    if inventory.get("fingerprint") != selection.fingerprint:
        raise ValueError(f"Disks or their usage changed on {selection.node}; refresh the selection")
    disks = {d["path"]: d for d in inventory["disks"]}
    selected = set(selection.data + selection.cache)
    for path in sorted(selected):
        disk = disks.get(path)
        if not disk or not disk["selectable"]:
            why = "; ".join(disk["reasons"]) if disk else "Disk not found"
            raise ValueError(f"{selection.node} {path}: {why}")
        missing = set(disk["required_disks"]) - selected
        if missing:
            raise ValueError(f"{selection.node} {path} also requires {', '.join(sorted(missing))}")
        if path in selection.cache and disk["media"] != "ssd":
            raise ValueError(f"{selection.node} {path}: cache requires an SSD or NVMe disk")
    data_sizes = [disks[d]["size_gib"] for d in selection.data]
    # Reserve LVM thin metadata, DRBD metadata, filesystem and gateway state.
    thin_mib, metadata_mib = thin_geometry(data_sizes)
    replication_mib = replication_reserve_mib(thin_mib)
    usable = max(0, (thin_mib - replication_mib) // 1024)
    if size_gib > usable:
        raise ValueError(f"{selection.node}: requested size exceeds {usable} GiB usable capacity")
    return {
        "node": selection.node,
        "raw_data_gib": sum(data_sizes),
        "headroom_gib": sum(data_sizes) * 0.10,
        "metadata_and_service_gib": (2 * metadata_mib + 256 + replication_mib) / 1024,
        "data": [disks[p] for p in selection.data],
        "cache": [disks[p] for p in selection.cache],
        "reclaim_pools": sorted({name for p in selected for name in disks[p]["reclaim_pools"]}),
        "usable_gib": usable,
    }


def review_disks(spec: StorageCreateSpec, inventories: dict[str, dict]) -> dict:
    """Recheck identities, mounts, references and every shared PV before approval."""
    reviewed = [review_node(n, inventories.get(n.node), spec.size_gib) for n in spec.nodes]
    raw = sum(n["raw_data_gib"] for n in reviewed)
    limiting = min(reviewed, key=lambda n: n["usable_gib"])
    return {
        "name": spec.name,
        "nodes": reviewed,
        "arbiter": spec.arbiter,
        "virtual_ip": spec.virtual_ip,
        "prefix": spec.prefix,
        "size_gib": spec.size_gib,
        "maximum_gib": min(n["usable_gib"] for n in reviewed),
        "copies": 2,
        "policy": "RF2",
        "capacity": {
            "raw_data_gib": raw,
            "after_rf2_gib": min(n["raw_data_gib"] for n in reviewed),
            "replication_factor": 2,
            "headroom_gib": limiting["headroom_gib"],
            "metadata_and_service_gib": limiting["metadata_and_service_gib"],
            "maximum_gib": limiting["usable_gib"],
            "cache_gib": sum(d["size_gib"] for n in reviewed for d in n["cache"]),
            "unmatched_gib": raw / 2 - min(n["raw_data_gib"] for n in reviewed),
        },
        "warning": "All existing data on the selected data and cache disks will be erased.",
    }
