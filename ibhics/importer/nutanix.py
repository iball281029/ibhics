"""Nutanix AHV as a Migrate source, through the Prism v3 REST API.

Listing reads the VMs. Importing copies each disk out of the cluster: Prism makes a
temporary image from the VM disk, the node downloads that image, and the image is
deleted again, also when the copy fails. Nothing else on the cluster changes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from ..hypervisor.base import Firmware
from .sources import GIB, Source, SourceVM

API = "/api/nutanix/v3"
TEMP_PREFIX = "ibhics-export-"


class NutanixError(RuntimeError):
    pass


class Prism:
    def __init__(self, source: Source, http=None, sleep: Callable[[float], None] = time.sleep):
        import httpx

        self._http = http or httpx.Client(
            base_url=f"https://{source.host}:9440",
            auth=(source.username or "", source.password or ""),
            verify=source.verify_tls,
            timeout=httpx.Timeout(30.0, read=600.0),
        )
        self._sleep = sleep

    def _call(self, method: str, path: str, **kwargs):
        resp = self._http.request(method, API + path, **kwargs)
        if resp.status_code == 401:
            raise NutanixError("Prism refused the username or password")
        if resp.status_code >= 400:
            raise NutanixError(f"Prism answered {resp.status_code} to {method} {path}")
        return resp.json() if resp.content else {}

    def vms(self) -> list[dict]:
        found, offset = [], 0
        while True:
            page = self._call(
                "POST", "/vms/list", json={"kind": "vm", "length": 250, "offset": offset}
            )
            entities = page.get("entities", [])
            found.extend(entities)
            total = page.get("metadata", {}).get("total_matches", len(found))
            offset += len(entities)
            if not entities or offset >= total:
                return found

    def wait(self, task_uuid: str, timeout_s: float = 3600) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            task = self._call("GET", f"/tasks/{task_uuid}")
            status = task.get("status")
            if status == "SUCCEEDED":
                return task
            if status in ("FAILED", "ABORTED"):
                raise NutanixError(task.get("error_detail") or f"Prism task {status.lower()}")
            if time.monotonic() > deadline:
                raise NutanixError("Prism did not finish the disk export in time")
            self._sleep(3)

    def export_disk(self, disk_uuid: str, target: Path, progress=None) -> Path:
        """Download one VM disk to `target` through a temporary image."""
        body = {
            "metadata": {"kind": "image"},
            "spec": {
                "name": TEMP_PREFIX + disk_uuid[:13],
                "description": "Temporary copy for an ibHiCS import; deleted afterwards",
                "resources": {
                    "image_type": "DISK_IMAGE",
                    "data_source_reference": {"kind": "vm_disk", "uuid": disk_uuid},
                },
            },
        }
        created = self._call("POST", "/images", json=body)
        image = created.get("metadata", {}).get("uuid", "")
        task = created.get("status", {}).get("execution_context", {}).get("task_uuid")
        if not image:
            raise NutanixError("Prism did not return the temporary image")
        try:
            if task:
                self.wait(task)
            with self._http.stream("GET", f"{API}/images/{image}/file") as resp:
                if resp.status_code >= 400:
                    raise NutanixError(f"Prism refused the image download ({resp.status_code})")
                total = int(resp.headers.get("content-length") or 0)
                done = 0
                with open(target, "wb") as out:
                    for chunk in resp.iter_bytes(4 << 20):
                        out.write(chunk)
                        done += len(chunk)
                        if progress and total:
                            progress(done / total)
            return target
        finally:
            try:
                self._call("DELETE", f"/images/{image}")
            except Exception:
                pass  # left as ibhics-export-…; the operator can delete it in Prism


def parse_vm(entity: dict) -> SourceVM:
    spec = entity.get("spec", {})
    res = spec.get("resources", {})
    status = entity.get("status", {}).get("resources", {}) or res
    disks = [
        d
        for d in status.get("disk_list", res.get("disk_list", []))
        if d.get("device_properties", {}).get("device_type", "DISK") == "DISK"
    ]
    nics = status.get("nic_list", res.get("nic_list", []))
    boot = (res.get("boot_config") or {}).get("boot_type", "LEGACY")
    return SourceVM(
        ref=entity.get("metadata", {}).get("uuid", ""),
        name=spec.get("name", ""),
        vcpus=max(1, int(res.get("num_sockets", 1)) * int(res.get("num_vcpus_per_socket", 1))),
        memory_mb=int(res.get("memory_size_mib", 1024)),
        firmware=Firmware.UEFI if boot in ("UEFI", "SECURE_BOOT") else Firmware.BIOS,
        disks_gb=[-(-int(d.get("disk_size_bytes", 0)) // GIB) for d in disks],
        nics=len(nics),
        networks=[(n.get("subnet_reference") or {}).get("name", "") for n in nics],
        state="running" if res.get("power_state") == "ON" else "stopped",
        guest_os=(res.get("guest_tools") or {})
        .get("nutanix_guest_tools", {})
        .get("guest_os_version", ""),
        notes=spec.get("description", ""),
        disk_paths=[d.get("uuid", "") for d in disks],
    )


def list_nutanix_vms(source: Source, prism: Prism | None = None) -> list[SourceVM]:
    prism = prism or Prism(source)
    vms = [parse_vm(e) for e in prism.vms()]
    return sorted((v for v in vms if v.ref), key=lambda v: v.name.lower())


def pull_disks(source: Source, vm: SourceVM, out_dir: Path, progress, prism=None, probe=None):
    """Copy the VM's disks out of Prism: [(path, format)] for virt-v2v -i libvirtxml."""
    prism = prism or Prism(source)
    probe = probe or _probe_format
    if not vm.disk_paths:
        raise NutanixError(f"{vm.name} has no disks to copy")
    disks = []
    count = len(vm.disk_paths)
    for index, disk in enumerate(vm.disk_paths):
        target = out_dir / f"nutanix-disk{index}"

        def report(fraction, index=index):
            progress(
                f"Copying disk {index + 1}/{count} from Prism",
                2 + int(40 * (index + fraction) / count),
            )

        report(0.0)
        prism.export_disk(disk, target, report)
        disks.append((str(target), probe(target)))
    return disks


def _probe_format(path: Path) -> str:
    with open(path, "rb") as f:
        return "qcow2" if f.read(4) == b"QFI\xfb" else "raw"
