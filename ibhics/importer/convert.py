"""Turn a source VM into raw disk images that boot on KVM.

virt-v2v (from libguestfs) does the hard part: it copies the disks and, inside the
guest, installs virtio drivers, removes VMware Tools or Hyper-V integration services
and fixes the boot loader. Windows guests also need the virtio-win drivers installed
on the node (/usr/share/virtio-win).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

from ..hypervisor.base import Firmware
from .sources import Source, SourceType, SourceVM


@dataclass
class Converted:
    images: list[Path]  # raw, in disk order; the first one boots
    firmware: Firmware
    # The source's MAC addresses, in NIC order. Keeping them keeps DHCP reservations and
    # guest network configs that match on MAC (netplan, NetworkManager) working.
    macs: list[str] = field(default_factory=list)


class ConversionFailed(RuntimeError):
    pass


MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
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
WINDOWS_HIBERNATED = (
    "Windows on this VM was hibernated or shut down with Fast Startup, so its disk cannot be "
    "changed safely and the virtio drivers could not be added. In the VM on the source, run "
    "'powercfg /h off' as administrator, then shut it down fully (Start > Power > Shut down, "
    "not Restart), and import it again."
)
CLEAR_HIBERNATION_SOURCES = (
    "clearing Windows hibernation works on copies of the disks, which ibHiCS makes from "
    "VMware ESXi over SSH and from Proxmox/KVM hosts; for this source, turn Fast Startup off "
    "in the VM instead ('powercfg /h off', then shut it down fully)"
)
VMX_DISK = re.compile(
    r'^(?P<dev>(?P<bus>scsi|sata|nvme|ide)(?P<ctl>\d+):(?P<unit>\d+))\.(?P<key>\w+)\s*=\s*"(?P<val>[^"]*)"',
    re.IGNORECASE | re.MULTILINE,
)
VMDK_EXTENT = re.compile(r'^(?:RW|RDONLY)\s+\d+\s+(?P<type>\w+)\s+"(?P<file>[^"]+)"', re.MULTILINE)
NO_SRVANY = (
    "virt-v2v needs rhsrvany.exe and pnp_wait.exe in /usr/share/virt-tools to convert Windows "
    "guests, and this node has none. Upgrade the node from the ibHiCS ISO, which puts them there."
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


# virt-v2v's stage lines ("[  12.3] Copying disk 1/2") and how far along each one is.
STAGE_LINE = re.compile(r"^\[\s*[\d.]+\]\s+(?P<stage>.+)$")
STAGES = [
    ("Setting up the source", 2),
    ("Opening the source", 5),
    ("Inspecting the source", 10),
    ("Checking for sufficient free disk space", 15),
    ("Converting", 20),
    ("Mapping filesystem data", 25),
    ("Closing the overlay", 28),
    ("Assigning disks", 29),
    ("Checking if the guest needs BIOS or UEFI", 30),
    ("Setting up the destination", 30),
    ("Copying disk", 30),  # 30-95, split by disk
    ("Creating output metadata", 96),
    ("Finishing off", 98),
]
COPY_DISK = re.compile(r"Copying disk (\d+)/(\d+)")
Progress = Callable[[str, int | None], None]


def stage_progress(line: str) -> tuple[str, int] | None:
    """(step, percent) from one line of virt-v2v output, or None for other lines."""
    m = STAGE_LINE.match(line.strip())
    if not m:
        return None
    stage = m["stage"].strip()
    copy = COPY_DISK.search(stage)
    if copy:
        index, total = int(copy[1]), max(int(copy[2]), 1)
        return stage, 30 + int(65 * (index - 1) / total)
    for prefix, pct in STAGES:
        if stage.startswith(prefix):
            return stage, pct
    return None


def _image_format(path: Path, run) -> str:
    """qcow2 or raw: Proxmox keeps disks as either (LVM and ZFS volumes are raw)."""
    with open(path, "rb") as f:
        return "qcow2" if f.read(4) == b"QFI\xfb" else "raw"


def vmx_ssh_url(source: Source, vm: SourceVM) -> str:
    m = DATASTORE_PATH.match(vm.vmx)
    if not m:
        raise ConversionFailed(f"the location of {vm.name}'s .vmx file is not known")
    return f"ssh://root@{source.host}/vmfs/volumes/{quote(m['ds'])}/{quote(m['path'])}"


def vmx_disk_files(vmx: str) -> list[str]:
    """The VM's virtual disks (their .vmdk descriptors) from a .vmx, in boot order."""
    devices: dict[str, dict[str, str]] = {}
    order: dict[str, tuple] = {}
    buses = {"scsi": 0, "sata": 1, "nvme": 2, "ide": 3}
    for m in VMX_DISK.finditer(vmx):
        dev = m["dev"].lower()
        devices.setdefault(dev, {})[m["key"].lower()] = m["val"]
        order[dev] = (buses[m["bus"].lower()], int(m["ctl"]), int(m["unit"]))
    found = []
    for dev, keys in devices.items():
        name = keys.get("filename", "")
        if (
            name.lower().endswith(".vmdk")
            and keys.get("present", "true").lower() == "true"
            and "cdrom" not in keys.get("devicetype", "").lower()
        ):
            found.append((order[dev], name))
    return [name for _, name in sorted(found)]


def vmdk_flat_file(descriptor: str, name: str) -> str:
    """The -flat.vmdk that holds a VMware disk's data, from its descriptor."""
    m = VMDK_EXTENT.search(descriptor)
    if "parentFileNameHint" in descriptor or (m and m["type"].upper() != "VMFS"):
        raise ConversionFailed(
            f"{name} has snapshots: delete or consolidate them on the ESXi host first"
        )
    if not m:
        raise ConversionFailed(f"{name}: cannot tell where its data is from the descriptor")
    return m["file"]


def _ssh_closed(stderr: str | None) -> bool:
    err = stderr or ""
    return "Connection refused" in err or "timed out" in err or "No route" in err


class EsxiSSH:
    """Lets this node's root key into the ESXi host, using the source's password once, so
    virt-v2v can copy the disks over SSH without asking."""

    authorized_keys = "/etc/ssh/keys-root/authorized_keys"

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
        if source.path and source.type == SourceType.VMWARE:
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
        keys = self.authorized_keys
        add = (
            f"mkdir -p \"$(dirname {keys})\"; grep -qxF '{public}' {keys} 2>/dev/null || "
            f"echo '{public}' >> {keys}"
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

    def ssh_command(self, source: Source, command: str) -> list[str]:
        cmd = ["ssh", "-i", str(self.key), "-o", "StrictHostKeyChecking=accept-new"]
        return cmd + ["-o", "BatchMode=yes", f"root@{source.host}", command]

    def command(self, source: Source, command: str) -> str:
        """Run a command on the host with the key; its output, or ConversionFailed."""
        result = self._ssh(source, ["-o", "BatchMode=yes"], command)
        if result.returncode != 0:
            raise ConversionFailed(
                f"{source.host}: {command.split()[0]} failed: {(result.stderr or '').strip()}"
            )
        return result.stdout or ""

    def _check(self, cmd: list[str]) -> None:
        result = self.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise ConversionFailed(f"{cmd[0]} failed: {(result.stderr or '').strip()}")


class HostSSH(EsxiSSH):
    """The same for a Linux host (Proxmox VE, plain KVM): root's own authorized_keys."""

    authorized_keys = "~/.ssh/authorized_keys"

    def _start_ssh_service(self, source: Source) -> None:
        raise ConversionFailed(f"Cannot reach SSH on {source.host} (port 22).")


# Runs inside a converted Linux guest (virt-customize --run). The NIC's name changes on
# KVM (ens192 on VMware becomes enp1s0), and configs written for the old name stop
# working. This finds the names the guest's configs use and pins them to the new NICs'
# MAC addresses with systemd .link files, so the old configs keep working unchanged.
PIN_NIC_NAMES = r"""#!/bin/sh
macs="%(macs)s"
names=$( {
  grep -hoE '^[[:space:]]+(en[[:alnum:]]+|eth[0-9]+):' /etc/netplan/*.yaml 2>/dev/null \
    | tr -d ' :\t'
  grep -hoE '^(auto|allow-hotplug|iface)[[:space:]]+(en[[:alnum:]]+|eth[0-9]+)' \
    /etc/network/interfaces /etc/network/interfaces.d/* 2>/dev/null | awk '{print $2}'
  ls /etc/sysconfig/network-scripts 2>/dev/null \
    | sed -n 's/^ifcfg-\(en[[:alnum:]]*\|eth[0-9]*\)$/\1/p'
  grep -hoE '^interface-name=(en[[:alnum:]]+|eth[0-9]+)' \
    /etc/NetworkManager/system-connections/* 2>/dev/null | cut -d= -f2
} | sort -uV )  # VMware's ens160, ens192, ens224... sort in NIC order
[ -n "$names" ] || exit 0
mkdir -p /etc/systemd/network
for name in $names; do
  mac=${macs%%%% *}
  [ -n "$mac" ] || break
  link="/etc/systemd/network/10-ibhics-$name.link"
  printf '# Written by ibHiCS Migrate: keeps the name this NIC had before.\n' > "$link"
  printf '[Match]\nMACAddress=%%s\n\n[Link]\nName=%%s\n' "$mac" "$name" >> "$link"
  case "$macs" in *" "*) macs=${macs#* } ;; *) macs="" ;; esac
done
# The rename happens in early boot too, so rebuild the initramfs where that is cheap.
command -v update-initramfs >/dev/null && update-initramfs -u >/dev/null 2>&1
command -v dracut >/dev/null && dracut -f --regenerate-all >/dev/null 2>&1
exit 0
"""


class V2VConverter:
    def __init__(
        self,
        run=subprocess.run,
        log_dir: Path = LOG_DIR,
        ssh: EsxiSSH | None = None,
        host_ssh: HostSSH | None = None,
    ):
        self.run = run
        self.log_dir = log_dir
        self.ssh = ssh or EsxiSSH(run)
        self.host_ssh = host_ssh or HostSSH(run)

    def keep_nic_names(self, images: list[Path], macs: list[str]) -> str:
        """Pin the guest's old NIC names to macs. Best effort: returns a note for the log
        (Windows guests and guests without such configs are left as they are)."""
        macs = [m for m in macs if m and MAC_RE.match(m)]
        if not macs:
            return "no MAC addresses to pin NIC names to"
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as script:
            script.write(PIN_NIC_NAMES % {"macs": " ".join(macs)})
        try:
            cmd = ["virt-customize", "--no-network"]
            for image in images:
                cmd += ["-a", str(image), "--format", "raw"]
            done = self.run(
                [*cmd, "--run", script.name],
                capture_output=True,
                text=True,
                env={**os.environ, "LIBGUESTFS_BACKEND": "direct"},
            )
        finally:
            os.unlink(script.name)
        if done.returncode:
            return "could not keep NIC names: " + (done.stderr or done.stdout).strip()[-300:]
        return "kept the guest's NIC names"

    def convert(
        self,
        source: Source,
        vm: SourceVM,
        name: str,
        out_dir: Path,
        progress: Progress | None = None,
        clear_hibernation: bool = False,
    ) -> Converted:
        progress = progress or (lambda step, pct: None)
        out_dir.mkdir(parents=True, exist_ok=True)
        output = ["-o", "local", "-os", str(out_dir), "-of", "raw", "-on", name]
        password_file = None
        try:
            if clear_hibernation:
                # Copy the disks here, clear hibernation on the copies, convert those.
                if source.type == SourceType.VMWARE and source.transport == "ssh":
                    disks = self._pull_esxi_disks(source, vm, out_dir, progress)
                elif source.type == SourceType.KVM:
                    disks = self._pull_kvm_disks(source, vm, out_dir, progress)
                else:
                    raise ConversionFailed(CLEAR_HIBERNATION_SOURCES)
                progress("Clearing Windows hibernation on the copied disks", 4)
                self.clear_hibernation([Path(p) for p, fmt in disks if fmt == "raw"])
                from .kvm import libvirt_xml

                xml = out_dir / "source.xml"
                xml.write_text(libvirt_xml(vm, disks))
                cmd = ["virt-v2v", "-i", "libvirtxml", str(xml)]
            elif source.type == SourceType.VMWARE and source.transport == "ssh":
                progress("Setting up SSH access to the ESXi host", 1)
                self.ssh.ensure_access(source)
                url = self.ssh.find_vmx(source, vmx_ssh_url(source, vm))
                cmd = ["virt-v2v", "-i", "vmx", "-it", "ssh", url]
            elif source.type == SourceType.VMWARE:
                fd, password_file = tempfile.mkstemp(prefix="v2v-", dir=out_dir)
                with os.fdopen(fd, "w") as f:  # mkstemp makes it readable by us only
                    f.write(source.password or "")
                cmd = ["virt-v2v", "-ic", source.libvirt_uri, "-ip", password_file, vm.ref]
            elif source.type == SourceType.KVM:
                from .kvm import libvirt_xml

                xml = out_dir / "source.xml"
                xml.write_text(libvirt_xml(vm, self._pull_kvm_disks(source, vm, out_dir, progress)))
                cmd = ["virt-v2v", "-i", "libvirtxml", str(xml)]
            elif source.type == SourceType.OVA or vm.ref.lower().endswith(".ova"):
                cmd = ["virt-v2v", "-i", "ova", vm.ref]
            else:
                cmd = ["virt-v2v", "-i", "disk", vm.ref]
            env = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}
            if self.run is subprocess.run:
                result, log = self._run_streaming(cmd + output, env, name, progress)
            else:
                result = self.run(cmd + output, capture_output=True, text=True, env=env)
                log = self._keep_log(name, result)
        finally:
            if password_file:
                os.unlink(password_file)
        if result.returncode != 0:
            output_text = result.stderr or result.stdout or ""
            message = v2v_error(output_text, result.returncode)
            if "does not support 'range'" in output_text:
                message = NO_RANGES
            elif re.search(r"rhsrvany\.exe|pvvxsvc\.exe|pnp_wait\.exe", output_text):
                message = NO_SRVANY
            elif re.search(r"[Hh]ibernat|Fast Restart|Fast Startup", output_text):
                message = WINDOWS_HIBERNATED
            raise ConversionFailed(message + (f" (full log: {log})" if log else ""))
        return read_output(out_dir, name)

    def _pull_kvm_disks(
        self, source: Source, vm: SourceVM, out_dir: Path, progress
    ) -> list[tuple[str, str]]:
        """Copy the VM's disks from the Proxmox/KVM host into out_dir over SSH: (path, format)
        of each copy, for virt-v2v -i libvirtxml. The copies are removed with out_dir."""
        progress("Setting up SSH access to the host", 1)
        self.host_ssh.ensure_access(source)
        if not vm.disk_paths:
            raise ConversionFailed(f"{vm.name} has no disks to copy")
        pull = out_dir / "source"
        pull.mkdir(exist_ok=True)
        disks = []
        for index, remote in enumerate(vm.disk_paths):
            local = pull / f"disk{index}"
            progress(f"Copying disk {index + 1}/{len(vm.disk_paths)} from {source.host}", 2)
            with open(local, "wb") as f:
                cmd = self.host_ssh.ssh_command(source, f"dd if={shlex.quote(remote)} bs=4M")
                result = self.run(cmd, stdout=f, stderr=subprocess.PIPE)
            if result.returncode != 0:
                err = result.stderr.decode(errors="replace") if result.stderr else ""
                raise ConversionFailed(f"copying {remote} from {source.host} failed: {err.strip()}")
            disks.append((str(local), _image_format(local, self.run)))
        return disks

    def _pull_esxi_disks(
        self, source: Source, vm: SourceVM, out_dir: Path, progress
    ) -> list[tuple[str, str]]:
        """Copy the VM's -flat.vmdk files (raw disks) from the ESXi host into out_dir over
        SSH, skipping runs of zeros so thin disks stay small here."""
        progress("Setting up SSH access to the ESXi host", 1)
        self.ssh.ensure_access(source)
        vmx = unquote(self.ssh.find_vmx(source, vmx_ssh_url(source, vm)).split(source.host, 1)[1])
        folder = vmx.rsplit("/", 1)[0]
        files = vmx_disk_files(self.ssh.command(source, f"cat {shlex.quote(vmx)}"))
        if not files:
            raise ConversionFailed(f"{vm.name} has no disks in its .vmx")
        pull = out_dir / "source"
        pull.mkdir(exist_ok=True)
        disks = []
        for index, name in enumerate(files):
            desc = name if name.startswith("/") else f"{folder}/{name}"
            flat = vmdk_flat_file(self.ssh.command(source, f"cat {shlex.quote(desc)}"), name)
            remote = flat if flat.startswith("/") else f"{desc.rsplit('/', 1)[0]}/{flat}"
            local = pull / f"disk{index}"
            progress(f"Copying disk {index + 1}/{len(files)} from {source.host}", 2)
            ssh = shlex.join(
                self.ssh.ssh_command(source, f"dd if={shlex.quote(remote)} bs=1048576")
            )
            copy = f"set -o pipefail; {ssh} | dd of={shlex.quote(str(local))} bs=4M conv=sparse"
            result = self.run(["bash", "-c", copy], capture_output=True, text=True)
            if result.returncode != 0:
                raise ConversionFailed(
                    f"copying {remote} from {source.host} failed: {(result.stderr or '').strip()}"
                )
            disks.append((str(local), "raw"))
        return disks

    def clear_hibernation(self, images: list[Path]) -> list[str]:
        """Clear the hibernation file and the dirty flag of every NTFS partition on these raw
        disk copies, as Windows does when it starts fully. Returns the partitions done."""
        done = []
        for image in images:
            got = self.run(
                ["losetup", "--find", "--show", "--partscan", str(image)],
                capture_output=True,
                text=True,
            )
            loop = (got.stdout or "").strip()
            if got.returncode != 0 or not loop:
                raise ConversionFailed(f"cannot open {image.name}: {(got.stderr or '').strip()}")
            try:
                parts = self.run(["lsblk", "-nrpo", "NAME", loop], capture_output=True, text=True)
                for part in (parts.stdout or "").split()[1:]:
                    kind = self.run(
                        ["blkid", "-o", "value", "-s", "TYPE", part], capture_output=True, text=True
                    )
                    if (kind.stdout or "").strip() != "ntfs":
                        continue
                    self.run(["ntfsfix", "-d", part], capture_output=True, text=True)
                    mount = tempfile.mkdtemp(prefix="ibhics-ntfs-")
                    try:
                        mounted = self.run(
                            ["mount", "-t", "ntfs-3g", "-o", "remove_hiberfile", part, mount],
                            capture_output=True,
                            text=True,
                        )
                        if mounted.returncode == 0:
                            self.run(["umount", mount], capture_output=True, text=True)
                            done.append(part)
                    finally:
                        os.rmdir(mount)
            finally:
                self.run(["losetup", "-d", loop], capture_output=True, text=True)
        return done

    def _run_streaming(self, cmd, env, name: str, progress: Progress):
        """Run virt-v2v writing its output to the log as it comes, so the web console can
        show the current stage and the log while the copy runs."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log = self.log_dir / f"{name}.log"
            sink = open(log, "w")  # noqa: SIM115 - closed below
        except OSError:
            log, sink = None, None
        lines = []
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
        )
        for line in proc.stdout:
            lines.append(line)
            if sink:
                sink.write(line)
                sink.flush()
            found = stage_progress(line)
            if found:
                progress(*found)
        proc.wait()
        if sink:
            sink.close()
        out = "".join(lines)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, out), log

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
    firmware, macs = Firmware.BIOS, []
    xml = out_dir / f"{name}.xml"
    if xml.exists():
        root = ET.parse(xml).getroot()
        os_el = root.find("os")
        if os_el is not None and (
            os_el.get("firmware") == "efi" or os_el.find("loader") is not None
        ):
            firmware = Firmware.UEFI
        macs = [
            mac.get("address", "").lower()
            for mac in root.findall("devices/interface/mac")
            if MAC_RE.match(mac.get("address", ""))
        ]
    return Converted(images=images, firmware=firmware, macs=macs)


class FakeConverter:
    """For the fake driver: small images with a recognisable first sector."""

    def convert(
        self,
        source: Source,
        vm: SourceVM,
        name: str,
        out_dir: Path,
        progress=None,
        clear_hibernation: bool = False,
    ) -> Converted:
        if progress:
            progress("Copying disk 1/1", 30)
        out_dir.mkdir(parents=True, exist_ok=True)
        images = []
        for index in range(max(len(vm.disks_gb), 1)):
            path = out_dir / f"{name}-sd{chr(ord('a') + index)}"
            path.write_bytes(f"{source.type}:{vm.ref}:{index}".encode().ljust(512, b"\0"))
            images.append(path)
        return Converted(images=images, firmware=vm.firmware)
