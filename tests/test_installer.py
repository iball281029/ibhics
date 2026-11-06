"""First-boot setup wizard and the installer files that go on the ISO."""

import json
import subprocess
from pathlib import Path

import pytest

from ibhics.installer import setup
from ibhics.manager.auth import Auth

INSTALLER = Path(__file__).parent.parent / "installer"
TOKEN = "t" * 24


def cfg(role="manager", **kw):
    base = dict(
        role=role,
        hostname="node1",
        interface="eno1",
        address="10.0.0.11/24",
        gateway="10.0.0.1",
        dns=["10.0.0.1", "1.1.1.1"],
        token=TOKEN,
    )
    return setup.Config(**{**base, **kw})


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, check=True, input=None, text=True, capture_output=False):
        self.calls.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("hostname", "Node_1", "lowercase"),
        ("address", "10.0.0.11", "prefix"),
        ("address", "10.0.0.300/24", "prefix"),
        ("gateway", "10.0.1.1", "inside the node's subnet"),
        ("dns", ["dns.example"], "not an IPv4"),
        ("token", "short", "16 characters"),
        ("role", "storage", "role must be"),
    ],
)
def test_config_validation(field, value, message):
    with pytest.raises(ValueError, match=message):
        cfg(**{field: value}).validate()


def test_vm_nodes_put_the_address_on_a_bridge():
    text = setup.render_interfaces(cfg())
    assert "iface eno1 inet manual" in text
    assert "auto br0\niface br0 inet static" in text
    assert "bridge_ports eno1" in text
    assert "address 10.0.0.11/24" in text and "gateway 10.0.0.1" in text
    assert "dns-nameservers 10.0.0.1 1.1.1.1" in text

    witness = setup.render_interfaces(cfg("witness", hostname="witness"))
    assert "br0" not in witness and "iface eno1 inet static" in witness


def test_env_per_role():
    manager = setup.render_env(cfg())
    assert "IBHICS_TLS_CERT=/etc/ibhics/tls/manager.crt" in manager
    assert "IBHICS_MIGRATION_HOST=10.0.0.11" in manager
    assert f"IBHICS_CLUSTER_TOKEN={TOKEN}" in manager

    agent = setup.render_env(cfg("compute", hostname="node2", address="10.0.0.12/24"))
    assert "IBHICS_ROLE=compute" in agent
    assert "IBHICS_AGENT_HOST=10.0.0.12" in agent and "IBHICS_AGENT_PORT=8007" in agent


def test_console_banner_says_where_to_go():
    assert "https://10.0.0.11:8888" in setup.render_issue(cfg())
    issue = setup.render_issue(cfg("compute", hostname="node2", address="10.0.0.12/24"))
    assert "Join node" in issue and "http://10.0.0.12:8007" in issue
    assert "beta version 1.0" in issue


def test_apply_manager(tmp_path):
    run = Recorder()
    setup.apply(cfg(), "root-pw-123", "admin-pw-123", root=tmp_path, run=run)

    assert (tmp_path / "etc/hostname").read_text() == "node1\n"
    assert "10.0.0.11\tnode1" in (tmp_path / "etc/hosts").read_text()
    # DNS works without resolvconf: apt can reach the Debian mirror after setup.
    resolv = (tmp_path / "etc/resolv.conf").read_text()
    assert "nameserver 10.0.0.1\nnameserver 1.1.1.1\n" in resolv
    env = tmp_path / "etc/ibhics/ibhics.env"
    assert env.stat().st_mode & 0o077 == 0  # the cluster token is secret
    assert (tmp_path / "etc/ibhics/configured").read_text() == "manager\n"
    assert "forward mode='bridge'" in (tmp_path / "etc/ibhics/default-network.xml").read_text()

    cmds = [c[0] for c in run.calls]
    assert (["chpasswd"], "root:root-pw-123\n") in run.calls
    assert ["timedatectl", "set-timezone", "Asia/Bangkok"] in cmds
    assert ["virsh", "net-define", "/etc/ibhics/default-network.xml"] in cmds
    assert any(c[:2] == ["virsh", "pool-define-as"] for c in cmds)
    openssl = next(c for c in cmds if c[0] == "openssl")
    assert "subjectAltName=DNS:node1,IP:10.0.0.11" in openssl
    assert ["systemctl", "enable", "--now", "ibhics-manager.service"] in cmds
    # Root can sign in over SSH with the password from setup (Debian allows keys only).
    sshd = (tmp_path / "etc/ssh/sshd_config.d/10-ibhics.conf").read_text()
    assert "PermitRootLogin yes" in sshd
    assert not (tmp_path / "root/.ssh/authorized_keys").exists()

    users = json.loads((tmp_path / "var/lib/ibhics/users.json").read_text())["users"]
    assert users[0]["name"] == "admin" and users[0]["must_change_password"] is False
    auth = Auth(state_path=tmp_path / "var/lib/ibhics/users.json")
    auth.load()
    assert auth.login("admin", "admin-pw-123").user == "admin"


def test_apply_reports_failures_but_not_expected_ones(tmp_path):
    class Failing(Recorder):
        def __call__(self, cmd, **kwargs):
            super().__call__(cmd, **kwargs)
            fails = cmd[:2] in (["virsh", "net-destroy"], ["virsh", "net-start"])
            return subprocess.CompletedProcess(cmd, int(fails), "", "network is not active")

    # net-destroy fails on every fresh node and is ignored; net-start failing is real.
    with pytest.raises(RuntimeError, match="virsh net-start default failed: network is not"):
        setup.apply(cfg(), "root-pw-123", "admin-pw-123", root=tmp_path, run=Failing())


def test_apply_witness_skips_libvirt(tmp_path):
    run = Recorder()
    setup.apply(cfg("witness", hostname="witness"), "root-pw-123", None, root=tmp_path, run=run)
    cmds = [c[0] for c in run.calls]
    assert not any(c[0] in ("virsh", "openssl") for c in cmds)
    assert ["systemctl", "enable", "--now", "ibhics-agent.service"] in cmds
    assert not (tmp_path / "var/lib/ibhics/users.json").exists()


def test_questions_retry_until_valid(capsys):
    answers = iter(["", "Bad Name", "node7"])
    assert setup.ask("Node name", check=setup.check_hostname, reader=lambda _p: next(answers)) == (
        "node7"
    )
    out = capsys.readouterr().out
    assert "A value is needed" in out and "lowercase" in out

    picks = iter(["9", "2"])
    role = setup.choose("Role?", list(setup.ROLES.items()), reader=lambda _p: next(picks))
    assert role == "compute"

    secrets = iter(["short", "long-enough", "different", "long-enough", "long-enough"])
    assert setup.ask_password("pw", reader=lambda _p: next(secrets)) == "long-enough"


def test_detect_interfaces(tmp_path):
    for name, physical in [("lo", False), ("eno1", True), ("br0", False), ("ens2f0", True)]:
        (tmp_path / name).mkdir()
        if physical:
            (tmp_path / name / "device").mkdir()
    assert setup.detect_interfaces(tmp_path) == ["eno1", "ens2f0"]
    (tmp_path / "ens2f0" / "carrier").write_text("1\n")
    assert setup.detect_interfaces(tmp_path) == ["ens2f0", "eno1"]  # the cabled port first


def test_installer_files_agree():
    preseed = (INSTALLER / "preseed.cfg").read_text()
    for menu in ("boot/isolinux.cfg", "boot/grub.cfg"):
        assert "preseed/file=/cdrom/ibhics/preseed.cfg" in (INSTALLER / menu).read_text()
    assert "/cdrom/ibhics /target/opt/ibhics-installer" in preseed
    # The install needs no network: packages come from the repository on the ISO.
    assert "d-i netcfg/enable boolean false" in preseed
    assert "d-i apt-setup/use_mirror boolean false" in preseed
    assert "sh /opt/ibhics-installer/install-ibhics.sh" in preseed
    # "!" makes the installer stop and ask for a root password, so a real hash is given.
    assert "root-password-crypted password $6$" in preseed and "passwd -l root" in preseed
    packages = (INSTALLER / "packages.txt").read_text().split()
    for pkg in ("libvirt-daemon-system", "python3-libvirt", "open-iscsi", "openssh-server"):
        assert pkg in packages
    install = (INSTALLER / "install-ibhics.sh").read_text()
    assert "file:$SRC/repo" in install and "packages.txt" in install
    for unit in (INSTALLER / "systemd").glob("*.service"):
        assert "/opt/ibhics/venv/bin/" in unit.read_text()
    assert "systemctl enable ibhics-firstboot.service" in install
    for script in ("install-ibhics.sh", "build-iso.sh", "build-repo.sh", "test-install.sh"):
        shell = "sh" if script == "install-ibhics.sh" else "bash"
        subprocess.run([shell, "-n", str(INSTALLER / script)], check=True)


def test_answers_file():
    text = """
    # unattended install
    ROLE=manager
    HOSTNAME=node1
    ADDRESS=10.0.2.15/24
    GATEWAY=10.0.2.2
    DNS=10.0.2.3
    ROOT_PASSWORD=Root-pw-123
    ADMIN_PASSWORD=Admin-pw-123
    """
    cfg, root_pw, admin_pw = setup.read_answers(text, ["ens3"])
    assert (cfg.role, cfg.interface, cfg.address, cfg.dns) == (
        "manager",
        "ens3",
        "10.0.2.15/24",
        ["10.0.2.3"],
    )
    assert len(cfg.token) >= 24 and (root_pw, admin_pw) == ("Root-pw-123", "Admin-pw-123")
    cfg.validate()
    with pytest.raises(ValueError, match="TOKEN"):
        setup.read_answers(text.replace("manager", "compute"), ["ens3"])
    with pytest.raises(ValueError, match="KEY=value"):
        setup.read_answers("ROLE manager", ["ens3"])


def test_ssh_key_for_root(tmp_path):
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample admin@laptop"
    setup.apply(cfg(ssh_key=key), "root-pw-123", "admin-pw-123", root=tmp_path, run=Recorder())
    keys = tmp_path / "root/.ssh/authorized_keys"
    assert keys.read_text() == key + "\n" and keys.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="SSH public key"):
        cfg(ssh_key="not-a-key").validate()
    # The wizard question can be skipped with Enter.
    assert setup.ask("key", check=setup.check_ssh_key, reader=lambda _p: "", optional=True) == ""


def test_pick_disk_prefers_small_internal_boot_drive(tmp_path):
    """A server with a big RAID volume first and an M.2 boot drive: install on the M.2,
    never on the USB stick the installer runs from or a small/removable disk."""
    gib = 1024 * 1024 * 2  # sectors
    disks = {
        "sda": (12_000 * gib, "0", "/devices/pci0000:00/raid/host0"),  # RAID 5
        "sdb": (16 * gib, "0", "/devices/pci0000:00/usb1/1-1"),  # the installer stick
        "sdc": (64 * gib, "0", "/devices/pci0000:00/usb2/2-1"),  # another USB disk
        "sdd": (8 * gib, "0", "/devices/pci0000:00/ata1"),  # SATADOM too small
        "sde": (100 * gib, "1", "/devices/pci0000:00/sd"),  # removable
        "nvme0n1": (240 * gib, "0", "/devices/pci0000:00/nvme0"),  # M.2 boot drive
    }
    sys_block = tmp_path / "block"
    for name, (size, removable, device) in disks.items():
        d = sys_block / name
        d.mkdir(parents=True)
        (d / "size").write_text(f"{size}\n")
        (d / "removable").write_text(removable + "\n")
        target = tmp_path / device.lstrip("/")
        target.mkdir(parents=True, exist_ok=True)
        (d / "device").symlink_to(target)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    listing = "\n".join(f"/dev/{n}" for n in disks)
    (bin_dir / "list-devices").write_text(f"#!/bin/sh\nprintf '%s\\n' '{listing}'\n")
    (bin_dir / "list-devices").chmod(0o755)
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sdb1 /cdrom iso9660 ro 0 0\n")

    def pick(**extra):
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "SYS": str(sys_block), "MOUNTS": str(mounts)}
        out = subprocess.run(
            ["sh", str(INSTALLER / "pick-disk.sh")],
            env={**env, **extra},
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()

    assert pick() == "/dev/nvme0n1"
    (sys_block / "nvme0n1" / "size").write_text("0\n")
    assert pick() == "/dev/sda"  # no small drive: the RAID volume, never the USB stick


def test_wake_ports_brings_ports_up_and_waits_for_a_link(tmp_path):
    # Fresh boot: the ports are down, so none reports a carrier until it is brought up.
    for name in ("eno1", "eno2", "ens1f0"):
        (tmp_path / name / "device").mkdir(parents=True)
    commands = []
    naps = []

    def run(cmd, **_kw):
        commands.append(cmd)

    def sleep(seconds):
        naps.append(seconds)
        if len(naps) == 3:  # the cable on eno2 negotiates after a moment
            (tmp_path / "eno2" / "carrier").write_text("1\n")

    nics = setup.detect_interfaces(tmp_path)
    assert nics == ["eno1", "eno2", "ens1f0"]  # nothing looks cabled yet
    setup.wake_ports(nics, run=run, sys_net=tmp_path, sleep=sleep)
    assert commands == [["ip", "link", "set", n, "up"] for n in nics]
    assert len(naps) == 3
    assert setup.detect_interfaces(tmp_path)[0] == "eno2"  # the cabled port is the default


def test_wake_ports_gives_up_when_nothing_is_cabled(tmp_path):
    (tmp_path / "eno1" / "device").mkdir(parents=True)
    naps = []
    setup.wake_ports(
        ["eno1"], run=lambda *a, **k: None, sys_net=tmp_path, wait=2, sleep=naps.append
    )
    assert sum(naps) == 2


def test_find_ibhics_spots_an_existing_install(tmp_path):
    sys_block, dev = tmp_path / "block", tmp_path / "dev"
    dev.mkdir()
    for disk, parts in {"sda": ["sda1", "sda2", "sda3"], "nvme0n1": ["nvme0n1p1"]}.items():
        for p in parts:
            (sys_block / disk / p).mkdir(parents=True)
            (dev / p).write_bytes(b"\0" * 8192)
    # sda3 is an LVM PV of the "ibhics" volume group: label, then text metadata.
    (dev / "sda3").write_bytes(
        b"\0" * 512 + b"LABELONE" + b"\0" * 4088 + b'ibhics {\nid = "abc"\nseqno = 3\n'
    )
    (dev / "nvme0n1p1").write_bytes(b"\0" * 4096 + b'pve {\nid = "def"\n')  # someone else's VG

    def found(disk):
        env = {"PATH": "/usr/bin:/bin", "SYS": str(sys_block), "DEV": str(dev)}
        script = str(INSTALLER / "find-ibhics.sh")
        return subprocess.run(["sh", script, disk], env=env).returncode == 0

    assert found("/dev/sda")
    assert not found("/dev/nvme0n1")
    assert not found("/dev/sdz")


def test_default_entry_guards_an_existing_install():
    preseed = (INSTALLER / "preseed.cfg").read_text()
    assert "find-ibhics.sh" in preseed and "already-installed.sh" in preseed
    assert "priority=critical" in (INSTALLER / "boot/isolinux.cfg").read_text()
    templates = (INSTALLER / "ibhics.templates").read_text()
    assert "Template: ibhics/already-installed" in templates
    build = (INSTALLER / "build-iso.sh").read_text()
    for name in ("find-ibhics.sh", "already-installed.sh", "ibhics.templates"):
        assert f'"$HERE/{name}"' in build  # shipped on the ISO under /cdrom/ibhics


def test_boot_menu_title_names_the_release():
    menu = (INSTALLER / "boot/isolinux.cfg").read_text()
    assert "menu title ibHiCS @VERSION@ beta installer" in menu
    assert "s/@VERSION@/$SHORT/g" in (INSTALLER / "build-iso.sh").read_text()


def test_apparmor_lets_qemu_open_pool_volumes():
    rules = (INSTALLER / "apparmor-libvirt-qemu").read_text()
    assert "/var/lib/ibhics/pools/** rwk," in rules
    assert "/mnt/ibhics/** rwk," in rules  # NFS pools
    target = "/etc/apparmor.d/local/abstractions/libvirt-qemu"
    for script in ("install-ibhics.sh", "upgrade.sh"):
        text = (INSTALLER / script).read_text()
        assert '"$SRC/apparmor-libvirt-qemu"' in text and target in text
    assert '"$HERE/apparmor-libvirt-qemu"' in (INSTALLER / "build-iso.sh").read_text()


def test_custom_folder_pool_added_to_apparmor(tmp_path):
    from ibhics.hypervisor.libvirt_driver import allow_qemu_path

    rules = tmp_path / "apparmor.d/local/abstractions/libvirt-qemu"
    rules.parent.mkdir(parents=True)
    rules.write_text("/var/lib/ibhics/pools/** rwk,\n")
    assert allow_qemu_path("/data/vms/", rules)
    assert not allow_qemu_path("/data/vms", rules)  # once only
    assert not allow_qemu_path("/var/lib/ibhics/pools/big", rules)  # already covered
    assert rules.read_text().splitlines()[-1] == "/data/vms/** rwk,"
