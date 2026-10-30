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
    assert "beta version 0.2" in issue


def test_apply_manager(tmp_path):
    run = Recorder()
    setup.apply(cfg(), "root-pw-123", "admin-pw-123", root=tmp_path, run=run)

    assert (tmp_path / "etc/hostname").read_text() == "node1\n"
    assert "10.0.0.11\tnode1" in (tmp_path / "etc/hosts").read_text()
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
