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

    def __call__(self, cmd, check=True, input=None, text=True):
        self.calls.append((cmd, input))


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
    assert "https://10.0.0.11:8006" in setup.render_issue(cfg())
    issue = setup.render_issue(cfg("compute", hostname="node2", address="10.0.0.12/24"))
    assert "Join node" in issue and "http://10.0.0.12:8007" in issue
    assert "beta version 0.1" in issue


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

    users = json.loads((tmp_path / "var/lib/ibhics/users.json").read_text())["users"]
    assert users[0]["name"] == "admin" and users[0]["must_change_password"] is False
    auth = Auth(state_path=tmp_path / "var/lib/ibhics/users.json")
    auth.load()
    assert auth.login("admin", "admin-pw-123").user == "admin"


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


def test_installer_files_agree():
    preseed = (INSTALLER / "preseed.cfg").read_text()
    for menu in ("boot/isolinux.cfg", "boot/grub.cfg"):
        assert "preseed/file=/cdrom/ibhics/preseed.cfg" in (INSTALLER / menu).read_text()
    assert "/cdrom/ibhics /target/opt/ibhics-installer" in preseed
    assert "sh /opt/ibhics-installer/install-ibhics.sh" in preseed
    for pkg in ("libvirt-daemon-system", "python3-libvirt", "open-iscsi", "bridge-utils"):
        assert pkg in preseed
    install = (INSTALLER / "install-ibhics.sh").read_text()
    for unit in (INSTALLER / "systemd").glob("*.service"):
        assert "/opt/ibhics/venv/bin/" in unit.read_text()
    assert "systemctl enable ibhics-firstboot.service" in install
    for script in ("install-ibhics.sh", "build-iso.sh"):
        shell = "bash" if script.endswith("iso.sh") else "sh"
        subprocess.run([shell, "-n", str(INSTALLER / script)], check=True)
