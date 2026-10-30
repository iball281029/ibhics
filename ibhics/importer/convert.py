"""Turn a source VM into raw disk images that boot on KVM.

virt-v2v (from libguestfs) does the hard part: it copies the disks and, inside the
guest, installs virtio drivers, removes VMware Tools or Hyper-V integration services
and fixes the boot loader. Windows guests also need the virtio-win drivers installed
on the node (/usr/share/virtio-win).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

from ..hypervisor.base import Firmware
from .sources import Source, SourceType, SourceVM


@dataclass
class Converted:
    images: list[Path]  # raw, in disk order; the first one boots
    firmware: Firmware


class ConversionFailed(RuntimeError):
    pass


LOG_DIR = Path("/var/log/ibhics/imports")
# virt-v2v and nbdkit use root's default key. RSA: ESXi's sshd runs in FIPS mode and
# refuses ed25519 keys.
SSH_KEY = Path.home() / ".ssh" / "id_rsa"
DATASTORE_PATH = re.compile(r"^\[(?P<ds>[^\]]+)\]\s*(?P<path>.+)$")
NO_RANGES = (
    "This ESXi host does not serve disks in the way the HTTPS transfer needs (no byte "
    "ranges). Remove the source and add it again with Transfer: SSH, after turning on SSH "
    "on the ESXi host."
)
SSH_OFF = (
    "Cannot reach SSH on {host}. Check that a firewall lets this node reach port 22 of "
    "the ESXi host, or turn on SSH there (Host > Actions > Services > Enable Secure Shell)."
)


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


def vmx_ssh_url(source: Source, vm: SourceVM) -> str:
    m = DATASTORE_PATH.match(vm.vmx)
    if not m:
        raise ConversionFailed(f"the location of {vm.name}'s .vmx file is not known")
    return f"ssh://root@{source.host}/vmfs/volumes/{quote(m['ds'])}/{quote(m['path'])}"


def _ssh_closed(stderr: str | None) -> bool:
    err = stderr or ""
    return "Connection refused" in err or "timed out" in err or "No route" in err


class EsxiSSH:
    """Lets this node's root key into the ESXi host, using the source's password once, so
    virt-v2v can copy the disks over SSH without asking."""

    def __init__(self, run=subprocess.run, key: Path = SSH_KEY, api=None) -> None:
        self.run = run
        self.key = key
        self.api = api  # makes an EsxiApi for a source; tests pass a fake

    def _start_ssh_service(self, source: Source) -> None:
        from .esxi_api import EsxiApi, EsxiApiError

        try:
            (self.api or EsxiApi)(source).start_ssh()
        except (EsxiApiError, OSError, ET.ParseError) as exc:
            raise ConversionFailed(
                SSH_OFF.format(host=source.host) + f" (turning it on through the API failed: {exc})"
            ) from None

    def ensure_access(self, source: Source) -> None:
        if source.path:
            raise ConversionFailed(
                "SSH transfer needs the ESXi host itself as the source, not a vCenter"
            )
        if not self.key.exists():
            self.key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check(
                ["ssh-keygen", "-q", "-t", "rsa", "-b", "4096", "-N", "", "-f", str(self.key)]
            )
        probe = self._ssh(source, ["-o", "BatchMode=yes"], "true")
        if probe.returncode == 0:
            return  # the key is already authorised
        if _ssh_closed(probe.stderr):
            # SSH is off on most ESXi hosts; turn it on with the same credentials.
            self._start_ssh_service(source)
        public = self.key.with_suffix(".pub").read_text().strip()
        add = (
            f"grep -qxF '{public}' /etc/ssh/keys-root/authorized_keys 2>/dev/null || "
            f"echo '{public}' >> /etc/ssh/keys-root/authorized_keys"
        )
        with tempfile.TemporaryDirectory() as tmp:
            # ssh reads the password only from a terminal or SSH_ASKPASS.
            secret = Path(tmp) / "password"
            secret.write_text(source.password or "")
            askpass = Path(tmp) / "askpass"
            askpass.write_text(f"#!/bin/sh\ncat '{secret}'\n")
            askpass.chmod(0o700)
            env = {**os.environ, "SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force"}
            result = self._ssh(
                source,
                ["-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1"],
                add,
                env=env,
            )
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            if _ssh_closed(err):
                raise ConversionFailed(SSH_OFF.format(host=source.host))
            raise ConversionFailed(f"could not set up SSH access to {source.host}: {err}")
        if self._ssh(source, ["-o", "BatchMode=yes"], "true").returncode != 0:
            raise ConversionFailed(f"{source.host} still asks for a password over SSH")

    def find_vmx(self, source: Source, url: str) -> str:
        """The .vmx is usually named after its folder; when it is not (a renamed VM), use
        the one .vmx file in that folder."""
        prefix = f"ssh://root@{source.host}"
        folder = unquote(url.removeprefix(prefix)).rsplit("/", 1)[0]
        result = self._ssh(source, ["-o", "BatchMode=yes"], f"ls -1 '{folder}'/*.vmx")
        found = (result.stdout or "").split("\n") if result.returncode == 0 else []
        found = [f.strip() for f in found if f.strip().endswith(".vmx")]
        if len(found) == 1:
            return prefix + quote(found[0])
        return url

    def _ssh(self, source: Source, options: list[str], command: str, env=None):
        cmd = ["ssh", "-i", str(self.key), "-o", "StrictHostKeyChecking=accept-new"]
        cmd += ["-o", "ConnectTimeout=10", *options, f"root@{source.host}", command]
        return self.run(cmd, capture_output=True, text=True, env=env)

    def _check(self, cmd: list[str]) -> None:
        result = self.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise ConversionFailed(f"{cmd[0]} failed: {(result.stderr or '').strip()}")


class V2VConverter:
    def __init__(self, run=subprocess.run, log_dir: Path = LOG_DIR, ssh: EsxiSSH | None = None):
        self.run = run
        self.log_dir = log_dir
        self.ssh = ssh or EsxiSSH(run)

    def convert(self, source: Source, vm: SourceVM, name: str, out_dir: Path) -> Converted:
        out_dir.mkdir(parents=True, exist_ok=True)
        output = ["-o", "local", "-os", str(out_dir), "-of", "raw", "-on", name]
        password_file = None
        try:
            if source.type == SourceType.VMWARE and source.transport == "ssh":
                self.ssh.ensure_access(source)
                url = self.ssh.find_vmx(source, vmx_ssh_url(source, vm))
                cmd = ["virt-v2v", "-i", "vmx", "-it", "ssh", url]
            elif source.type == SourceType.VMWARE:
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
            output_text = result.stderr or result.stdout or ""
            message = v2v_error(output_text, result.returncode)
            if "does not support 'range'" in output_text:
                message = NO_RANGES
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
