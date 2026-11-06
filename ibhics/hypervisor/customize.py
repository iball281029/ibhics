"""Guest customization for VMs deployed from templates: the files of the seed CD.

Linux guests read a cloud-init NoCloud seed (a CD labelled "cidata" with meta-data and
user-data). Windows guests generalized with sysprep read an unattend answer file from the
root of removable media when they first boot. One CD carries whichever the guest needs.
Free of the libvirt bindings so it can be unit tested anywhere.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from .base import Customization, InvalidState

SEED_LABEL = "cidata"  # what cloud-init's NoCloud source looks for


def hostname_for(c: Customization, vm_name: str) -> str:
    """The guest's host name: the one asked for, else the VM name made into a valid one."""
    if c.hostname:
        return c.hostname
    name = (
        "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in vm_name).strip("-") or "vm"
    )
    return name[: 15 if c.os == "windows" else 63].strip("-")


def cloud_init_files(c: Customization, vm_name: str, instance_id: str) -> dict[str, str]:
    host = hostname_for(c, vm_name)
    # A new instance-id makes cloud-init run its first-boot modules again, even though the
    # template's own image already booted once.
    meta = f"instance-id: {instance_id}\nlocal-hostname: {host}\n"
    if c.user_data.strip():
        return {"meta-data": meta, "user-data": c.user_data}
    config: dict = {"hostname": host, "preserve_hostname": False}
    if c.timezone:
        config["timezone"] = c.timezone
    if c.username:
        user: dict = {
            "name": c.username,
            "groups": ["sudo"],
            "shell": "/bin/bash",
            "sudo": "ALL=(ALL) NOPASSWD:ALL",
            "lock_passwd": not c.password,
        }
        if c.ssh_keys:
            user["ssh_authorized_keys"] = c.ssh_keys
        config["users"] = ["default", user]
    elif c.ssh_keys:
        config["ssh_authorized_keys"] = c.ssh_keys
    if c.password:
        config["chpasswd"] = {
            "expire": False,
            "users": [{"name": c.username or "root", "password": c.password, "type": "text"}],
        }
        if not c.username:
            config["password"] = c.password  # the image's default user
        config["ssh_pwauth"] = True
    # JSON is valid YAML, so no YAML library is needed to write cloud-config.
    return {"meta-data": meta, "user-data": "#cloud-config\n" + json.dumps(config, indent=2)}


COMPONENT = (
    'processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" '
    'versionScope="nonSxS"'
)
WCM = "http://schemas.microsoft.com/WMIConfig/2002/State"
UNATTEND = """<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="{wcm}">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" {attrs}>
      <ComputerName>{host}</ComputerName>{tz}
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" {attrs}>
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" {attrs}>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>{password}{tz}
    </component>
  </settings>
</unattend>
"""


def unattend_xml(c: Customization, vm_name: str) -> str:
    tz = f"\n      <TimeZone>{escape(c.timezone)}</TimeZone>" if c.timezone else ""
    accounts = ""
    if c.password:
        secret = f"<Value>{escape(c.password)}</Value><PlainText>true</PlainText>"
        accounts = f"<AdministratorPassword>{secret}</AdministratorPassword>"
        if c.username:  # Windows client editions keep Administrator disabled
            accounts += (
                '<LocalAccounts><LocalAccount wcm:action="add">'
                f"<Name>{escape(c.username)}</Name><Group>Administrators</Group>"
                f"<Password>{secret}</Password></LocalAccount></LocalAccounts>"
            )
    password = f"\n      <UserAccounts>{accounts}</UserAccounts>" if accounts else ""
    return UNATTEND.format(
        host=escape(hostname_for(c, vm_name)), tz=tz, password=password, attrs=COMPONENT, wcm=WCM
    )


def seed_files(c: Customization, vm_name: str, instance_id: str) -> dict[str, str]:
    if c.os == "windows":
        xml = unattend_xml(c, vm_name)
        # Windows looks for either name at the root of removable media.
        return {"Autounattend.xml": xml, "Unattend.xml": xml}
    return cloud_init_files(c, vm_name, instance_id)


def build_iso(
    files: dict[str, str],
    out: Path,
    label: str = SEED_LABEL,
    run: Callable[[list[str]], object] | None = None,
) -> Path:
    """Write files to an ISO 9660 image with Rock Ridge and Joliet names (so "user-data"
    keeps its name), using xorriso or genisoimage on the node."""
    tool = shutil.which("xorriso") or shutil.which("genisoimage")
    if not tool:
        raise InvalidState("guest customization needs xorriso on the node (apt install xorriso)")
    run = run or (lambda cmd: subprocess.run(cmd, check=True, capture_output=True))
    # The CD may hold a password: only root (and QEMU, which libvirt hands it to) reads it.
    out.parent.mkdir(mode=0o711, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ibhics-seed-") as tmp:
        for name, text in files.items():
            (Path(tmp) / name).write_text(text)
        args = ["-quiet", "-J", "-R", "-V", label, "-o", str(out), tmp]
        run([tool, "-as", "mkisofs", *args] if tool.endswith("xorriso") else [tool, *args])
    out.chmod(0o600)
    return out
