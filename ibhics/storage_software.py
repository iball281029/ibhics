"""Storage software preparation and explicit readiness for the creation wizard."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path

from .storage import readiness
from .storage_create import RF2_CREATION_POLICY


def software_status(role: str, controller: bool = False) -> dict:
    probe = readiness()
    missing = []
    if not probe["module_available"] or probe["error"]:
        missing.append(probe["error"] or "DRBD 9 is unavailable")
    services = ["linstor-satellite", "dbus"]
    if role == "compute":
        services.append("drbd-reactor")
        for binary in ("iptables", "arping", "mdadm", "lvs", "mkfs.xfs", "exportfs"):
            if not shutil.which(binary):
                missing.append(binary + " is not installed")
    if controller:
        services.extend(["linstor-controller", "ibhics-storage-gateway"])
        for binary in ("linstor", "linstor-gateway"):
            if not shutil.which(binary):
                missing.append(binary + " is not installed")
    for service in services:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", service],
                capture_output=True,
                timeout=3,
                check=False,
            )
            if result.returncode:
                missing.append(service + " is not running")
        except (OSError, subprocess.TimeoutExpired):
            missing.append(service + " status is unavailable")
    try:
        conf = tomllib.loads(Path("/etc/linstor/linstor_satellite.toml").read_text())
        permitted = conf.get("files", {}).get("allowExtFiles", [])
        if not {"/etc/systemd/system", "/etc/drbd-reactor.d"} <= set(permitted):
            missing.append("LINSTOR service configuration distribution is not enabled")
    except (OSError, tomllib.TOMLDecodeError):
        missing.append("LINSTOR satellite configuration is missing or invalid")
    return {
        **probe,
        "software_ready": not missing,
        "missing_components": missing,
        "creation_policy": RF2_CREATION_POLICY,
    }


def prepare_software(role: str, controller: bool, root: Path, report) -> dict:
    if role not in ("compute", "witness") or (controller and role != "compute"):
        raise ValueError("Invalid storage software role")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = root / "software.log"
    report("Installing replication and storage services; no disks are being changed")
    script = files("ibhics").joinpath("storage_assets/prepare.sh")
    with log.open("w") as stream:
        log.chmod(0o600)
        done = subprocess.run(
            ["bash", str(script), role, "yes" if controller else "no"],
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=2700,
            check=False,
        )
    if done.returncode:
        tail = log.read_text(errors="replace")[-1500:]
        raise RuntimeError("Storage software preparation failed: " + tail)
    result = software_status(role, controller)
    if not result["software_ready"]:
        raise RuntimeError("; ".join(result["missing_components"]))
    return result
