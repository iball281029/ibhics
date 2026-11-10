"""DRBD diskless witness readiness and deterministic configuration planning.

Planning does not change block devices or advertise a pool as available.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator


class StorageNode(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,62}$")
    address: str
    device: str = Field(pattern=r"^/dev/[a-zA-Z0-9_./-]+$")

    @field_validator("address")
    @classmethod
    def ipv4(cls, value: str) -> str:
        return str(ipaddress.IPv4Address(value))

    @field_validator("device")
    @classmethod
    def device_path(cls, value: str) -> str:
        if ".." in value.split("/") or "//" in value:
            raise ValueError("use an absolute block device path")
        return value


class VirtualStoragePlan(BaseModel):
    name: str = Field(default="hybrid01", pattern=r"^[a-z][a-z0-9-]{0,30}$")
    nodes: list[StorageNode] = Field(min_length=2, max_length=2)
    arbiter_name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,62}$")
    arbiter_address: str
    virtual_ip: str
    prefix: int = Field(default=24, ge=1, le=30)
    size_gib: int = Field(ge=1, le=1048576)
    port: int = Field(default=7790, ge=1024, le=65535)

    @model_validator(mode="after")
    def unique_hosts(self):
        addresses = [n.address for n in self.nodes]
        addresses += [
            str(ipaddress.IPv4Address(self.arbiter_address)),
            str(ipaddress.IPv4Address(self.virtual_ip)),
        ]
        if len(set(addresses)) != 4:
            raise ValueError("compute, Arbiter and storage VIP must use different addresses")
        names = [n.name for n in self.nodes] + [self.arbiter_name]
        if len(set(names)) != 3:
            raise ValueError("compute nodes and Arbiter must have different stable node IDs")
        net = ipaddress.ip_network(f"{self.virtual_ip}/{self.prefix}", strict=False)
        if any(ipaddress.ip_address(a) not in net for a in addresses):
            raise ValueError("this deployment requires all addresses in the VIP subnet")
        if any(
            ipaddress.ip_address(a) in (net.network_address, net.broadcast_address)
            for a in addresses
        ):
            raise ValueError("network and broadcast addresses cannot be used")
        return self

    def drbd_config(self) -> str:
        # One primary filesystem only; no XFS concurrent mounts on replicated blocks.
        text = (
            f"resource {self.name} {{\n"
            "  protocol C;\n"
            "  options { quorum majority; on-no-quorum suspend-io; "
            "on-no-data-accessible suspend-io; }\n"
            "  net { allow-two-primaries no; }\n"
            "  disk { on-io-error detach; }\n"
        )
        for i, node in enumerate(self.nodes):
            text += (
                f"  on {node.name} {{\n    node-id {i};\n"
                f"    address {node.address}:{self.port};\n"
                f"    volume 0 {{ device /dev/drbd100; disk {node.device}; "
                f"meta-disk internal; disk {{ size {self.size_gib}G; }} }}\n  }}\n"
            )
        text += (
            f"  on {self.arbiter_name} {{\n    node-id 2;\n"
            f"    address {self.arbiter_address}:{self.port};\n"
            "    volume 0 { device /dev/drbd100; disk none; }\n  }\n"
            "  connection-mesh { hosts "
            + " ".join([n.name for n in self.nodes] + [self.arbiter_name])
            + "; }\n}\n"
        )
        return text


def readiness(run=subprocess.run) -> dict:
    """Read-only probe, also available on the diskless witness agent."""
    result = {"kernel_version": None, "module_available": False, "resources": [], "error": None}
    if not shutil.which("drbdadm") or not shutil.which("modinfo"):
        result["error"] = "DRBD 9 tools or kernel module are not installed"
        return result
    try:
        mod = run(
            ["modinfo", "-F", "version", "drbd"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        version = mod.stdout.strip()
        result["kernel_version"] = version or None
        result["module_available"] = bool(re.match(r"^9\.", version))
        if not result["module_available"]:
            result["error"] = "DRBD 9 kernel module is required"
            return result
        status = run(
            ["drbdsetup", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if status.returncode:
            result["error"] = "DRBD module is not loaded or status could not be read"
        else:
            result["resources"] = json.loads(status.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        result["error"] = str(exc)
    return result


def save_plan(plan: VirtualStoragePlan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(plan.model_dump_json(indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)
