"""Turn a source VM into raw disk images that boot on KVM.

virt-v2v (from libguestfs) does the hard part: it copies the disks and, inside the
guest, installs virtio drivers, removes VMware Tools or Hyper-V integration services
and fixes the boot loader. Windows guests also need the virtio-win drivers installed
on the node (/usr/share/virtio-win).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from ..hypervisor.base import Firmware
from .sources import Source, SourceType, SourceVM


@dataclass
class Converted:
    images: list[Path]  # raw, in disk order; the first one boots
    firmware: Firmware


class ConversionFailed(RuntimeError):
    pass


LOG_DIR = Path("/var/log/ibhics/imports")


def v2v_error(output: str, returncode: int) -> str:
    """The 'virt-v2v: error: ...' paragraph. The last line of output is only virt-v2v's
    advice to rerun with -v -x, which says nothing about what went wrong."""
    lines = output.strip().splitlines()
    for i, line in enumerate(lines):
        if "virt-v2v: error:" in line:
            message = [line.split("virt-v2v: error:", 1)[1].strip()]
            for more in lines[i + 1 :]:
                if not more.strip() or "virt-v2v -v -x" in more or more.startswith("If reporting"):
                    break
                message.append(more.strip())
            return " ".join(message)
    useful = [ln for ln in lines if ln.strip() and "virt-v2v -v -x" not in ln]
    return useful[-1] if useful else f"virt-v2v exited {returncode}"


class V2VConverter:
    def __init__(self, run=subprocess.run, log_dir: Path = LOG_DIR) -> None:
        self.run = run
        self.log_dir = log_dir

    def convert(self, source: Source, vm: SourceVM, name: str, out_dir: Path) -> Converted:
        out_dir.mkdir(parents=True, exist_ok=True)
        output = ["-o", "local", "-os", str(out_dir), "-of", "raw", "-on", name]
        password_file = None
        try:
            if source.type == SourceType.VMWARE:
                fd, password_file = tempfile.mkstemp(prefix="v2v-", dir=out_dir)
                with os.fdopen(fd, "w") as f:  # mkstemp makes it readable by us only
                    f.write(source.password or "")
                cmd = ["virt-v2v", "-ic", source.libvirt_uri, "-ip", password_file, vm.ref]
            elif source.type == SourceType.OVA:
                cmd = ["virt-v2v", "-i", "ova", vm.ref]
            else:
                cmd = ["virt-v2v", "-i", "disk", vm.ref]
            result = self.run(
                cmd + output,
                capture_output=True,
                text=True,
                env={**os.environ, "LIBGUESTFS_BACKEND": "direct"},
            )
        finally:
            if password_file:
                os.unlink(password_file)
        log = self._keep_log(name, result)
        if result.returncode != 0:
            message = v2v_error(result.stderr or result.stdout or "", result.returncode)
            raise ConversionFailed(message + (f" (full log: {log})" if log else ""))
        return read_output(out_dir, name)

    def _keep_log(self, name: str, result) -> Path | None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log = self.log_dir / f"{name}.log"
            log.write_text((result.stdout or "") + (result.stderr or ""))
            return log
        except OSError:
            return None


def read_output(out_dir: Path, name: str) -> Converted:
    """virt-v2v -o local writes NAME-sda, NAME-sdb, ... and NAME.xml."""
    images = sorted(p for p in out_dir.glob(f"{name}-sd*") if p.suffix == "")
    if not images:
        raise ConversionFailed("virt-v2v produced no disks")
    firmware = Firmware.BIOS
    xml = out_dir / f"{name}.xml"
    if xml.exists():
        os_el = ET.parse(xml).getroot().find("os")
        if os_el is not None and (
            os_el.get("firmware") == "efi" or os_el.find("loader") is not None
        ):
            firmware = Firmware.UEFI
    return Converted(images=images, firmware=firmware)


class FakeConverter:
    """For the fake driver: small images with a recognisable first sector."""

    def convert(self, source: Source, vm: SourceVM, name: str, out_dir: Path) -> Converted:
        out_dir.mkdir(parents=True, exist_ok=True)
        images = []
        for index in range(max(len(vm.disks_gb), 1)):
            path = out_dir / f"{name}-sd{chr(ord('a') + index)}"
            path.write_bytes(f"{source.type}:{vm.ref}:{index}".encode().ljust(512, b"\0"))
            images.append(path)
        return Converted(images=images, firmware=vm.firmware)
