"""Proxmox VE and KVM/libvirt hosts as Migrate sources."""

import subprocess
from pathlib import Path

from ibhics.hypervisor import Firmware
from ibhics.importer import Source, SourceType, SourceVM
from ibhics.importer.convert import HostSSH, V2VConverter
from ibhics.importer.kvm import libvirt_xml, list_kvm_vms, parse_libvirt_domain, parse_qm_config

QM_CONFIG = """boot: order=scsi0;ide2;net0
cores: 2
ide2: local:iso/ubuntu-24.04.iso,media=cdrom,size=2700M
memory: 8192
name: zabbix-pve
net0: virtio=BC:24:11:00:00:01,bridge=vmbr0
net1: virtio=BC:24:11:00:00:02,bridge=vmbr1
ostype: l26
scsi0: local-lvm:vm-101-disk-0,iothread=1,size=64G
scsi1: nfs:101/vm-101-disk-1.qcow2,size=512G
sockets: 2
bios: ovmf
efidisk0: local-lvm:vm-101-disk-2,efitype=4m,size=4M
"""

DOMAIN = """<domain type='kvm'><name>web 01</name><memory unit='KiB'>4194304</memory>
<vcpu>2</vcpu><os><type>hvm</type></os><devices>
<disk type='file' device='disk'><source file='/var/lib/libvirt/images/web.qcow2'/></disk>
<disk type='block' device='disk'><source dev='/dev/vg0/web-data'/></disk>
<disk type='file' device='cdrom'><source file='/iso/x.iso'/></disk>
<interface type='bridge'/></devices></domain>"""

PVE = Source("pve1", SourceType.KVM, host="10.0.0.70", username="root", password="pw")


def test_proxmox_config():
    vm, volumes = parse_qm_config("101", QM_CONFIG)
    assert (vm.ref, vm.name, vm.vcpus, vm.memory_mb, vm.nics) == ("101", "zabbix-pve", 4, 8192, 2)
    assert vm.firmware == Firmware.UEFI and vm.guest_os == "Linux"
    assert volumes == ["local-lvm:vm-101-disk-0", "nfs:101/vm-101-disk-1.qcow2"]  # no CD/EFI
    assert vm.disks_gb == [64, 512]


def test_libvirt_domain():
    vm, paths = parse_libvirt_domain(DOMAIN)
    assert (vm.name, vm.vcpus, vm.memory_mb, vm.firmware) == ("web-01", 2, 4096, Firmware.BIOS)
    assert paths == ["/var/lib/libvirt/images/web.qcow2", "/dev/vg0/web-data"]


class Host:
    """ssh stand-in for a Proxmox host whose key is already authorised."""

    def __init__(self, proxmox=True):
        self.proxmox, self.cmds = proxmox, []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        remote = cmd[-1]
        out = ""
        if remote == "true":
            out = ""
        elif remote.startswith("command -v qm"):
            out = "/usr/sbin/qm\n" if self.proxmox else ""
        elif remote == "qm list":
            out = (
                "      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID\n"
                "       101 zabbix-pve           stopped    8192              64.00 0\n"
                "       102 dc01                 running    4096              80.00 1234\n"
            )
        elif remote == "qm config 101":
            out = QM_CONFIG
        elif remote == "qm config 102":
            out = (
                "name: dc01\nmemory: 4096\nostype: win11\nsata0: local-lvm:vm-102-disk-0,size=80G\n"
            )
        elif remote.startswith("pvesm path"):
            vol = remote.split(" ", 2)[2].strip("'")
            out = (
                f"/dev/pve/{vol.split(':')[1]}\n"
                if vol.startswith("local-lvm")
                else f"/mnt/pve/{vol}\n"
            )
        elif remote.startswith("dd if="):
            kw["stdout"].write(b"QFI\xfb" + b"\0" * 508 if "qcow2" in remote else b"\0" * 512)
            return subprocess.CompletedProcess(cmd, 0, None, b"")
        return subprocess.CompletedProcess(cmd, 0, out, "")


def test_proxmox_listing(tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("k")
    vms = list_kvm_vms(PVE, HostSSH(Host(), key=key))
    assert [(v.name, v.state, v.guest_os) for v in vms] == [
        ("dc01", "running", "Windows 11 / Server 2022+"),
        ("zabbix-pve", "stopped", "Linux"),
    ]
    assert vms[1].disk_paths == ["/dev/pve/vm-101-disk-0", "/mnt/pve/nfs:101/vm-101-disk-1.qcow2"]


def test_proxmox_import_pulls_disks_then_converts(tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("k")
    host = Host()
    v2v = []

    def run(cmd, **kw):
        if cmd[0] == "virt-v2v":
            v2v.append(cmd)
            xml = Path(cmd[cmd.index("libvirtxml") + 1]).read_text()
            assert 'type="qcow2"' in xml and 'type="raw"' in xml and 'firmware="efi"' in xml
            out, name = Path(cmd[cmd.index("-os") + 1]), cmd[cmd.index("-on") + 1]
            (out / f"{name}-sda").write_bytes(b"boot")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return host(cmd, **kw)

    vm, _ = parse_qm_config("101", QM_CONFIG)
    vm.disk_paths = ["/dev/pve/vm-101-disk-0", "/mnt/pve/nfs/vm-101-disk-1.qcow2"]
    V2VConverter(run, log_dir=tmp_path, host_ssh=HostSSH(run, key=key)).convert(
        PVE, vm, "zabbix", tmp_path / "out"
    )
    assert v2v and v2v[0][:3] == ["virt-v2v", "-i", "libvirtxml"]


def test_libvirt_xml_for_v2v():
    vm = SourceVM(ref="1", name="a", vcpus=2, memory_mb=1024)
    xml = libvirt_xml(vm, [("/x/disk0", "raw")])
    assert '<source file="/x/disk0" />' in xml.replace("'", '"') or 'file="/x/disk0"' in xml
