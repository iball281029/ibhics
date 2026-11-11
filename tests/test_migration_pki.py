"""Migration certificates with real OpenSSL in a scratch folder (no libvirt, no root)."""

import shutil
import subprocess

import pytest

from ibhics.cluster.migration_pki import MigrationPKI, PKIError

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


def node(tmp_path, name):
    pki = MigrationPKI(tmp_path / name, hostname=lambda: name, libvirt_gid=lambda: 0, chown=False)
    (tmp_path / name / "etc/ibhics").mkdir(parents=True)
    (tmp_path / name / "etc/ibhics/ibhics.env").write_text("IBHICS_NODE_NAME=x\n")
    return pki


def enroll(ca, pki, name, ip, peers):
    csrs = pki.prepare(name)
    certs = {p: ca.sign(csr, name, ip, p) for p, csr in csrs.items()}
    return pki.install(name, ip, ca.ca_cert(), certs, peers)


def test_enroll_two_nodes_and_keys_never_leave(tmp_path):
    ca = node(tmp_path, "n1")
    ca.create_ca()
    n2 = node(tmp_path, "n2")
    enroll(ca, ca, "n1", "10.0.0.1", ["n1", "n2"])
    enroll(ca, n2, "n2", "10.0.0.2", ["n1", "n2"])
    base = tmp_path / "n2"
    assert not (base / "etc/ibhics/migration-pki/ca.key").exists()  # CA key stays on n1
    assert (base / "etc/pki/libvirt/private/serverkey.pem").stat().st_mode & 0o777 == 0o600
    conf = (base / "etc/libvirt/libvirtd.conf").read_text()
    assert 'tls_allowed_dn_list = ["*CN=n1-control*", "*CN=n2-control*"]' in conf
    assert "tls_no_verify_certificate = 0" in conf
    qemu = (base / "etc/libvirt/qemu.conf").read_text()
    assert "migrate_tls_force = 1" in qemu and "migrate_tls_x509_verify = 1" in qemu
    env = (base / "etc/ibhics/ibhics.env").read_text()
    assert "IBHICS_MIGRATION_HOST=10.0.0.2" in env and "IBHICS_NODE_NAME=x" in env
    assert (
        "10.0.0.2:16514"
        in (base / "etc/systemd/system/libvirtd-tls.socket.d/ibhics.conf").read_text()
    )
    cert = subprocess.run(
        [
            "openssl",
            "x509",
            "-in",
            str(base / "etc/pki/libvirt/servercert.pem"),
            "-noout",
            "-subject",
            "-ext",
            "subjectAltName",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "n2-control" in cert and "10.0.0.2" in cert
    status = n2.status()
    assert status["control"]["renew"] is False and status["ca"]["not_after"]


def test_signing_uses_the_cluster_record_not_the_request(tmp_path):
    ca = node(tmp_path, "n1")
    ca.create_ca()
    evil = node(tmp_path, "evil")
    csrs = evil.prepare("evil")
    # a request claiming to be another node still gets the name the manager chose
    cert = ca.sign(csrs["control"], "n2", "10.0.0.2", "control")
    out = subprocess.run(
        ["openssl", "x509", "-noout", "-subject"],
        input=cert,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "n2-control" in out and "evil" not in out


def test_install_refuses_foreign_cert_ip_mismatch_and_other_ca(tmp_path):
    ca = node(tmp_path, "n1")
    ca.create_ca()
    n2, n3 = node(tmp_path, "n2"), node(tmp_path, "n3")
    csrs2, csrs3 = n2.prepare("n2"), n3.prepare("n3")
    certs3 = {p: ca.sign(c, "n2", "10.0.0.2", p) for p, c in csrs3.items()}
    with pytest.raises(PKIError, match="does not match"):
        n2.install("n2", "10.0.0.2", ca.ca_cert(), certs3, ["n1", "n2"])
    certs2 = {p: ca.sign(c, "n2", "10.0.0.2", p) for p, c in csrs2.items()}
    with pytest.raises(PKIError):
        n2.install("n2", "10.0.0.9", ca.ca_cert(), certs2, ["n1", "n2"])
    other = node(tmp_path, "other")
    other.create_ca()
    (tmp_path / "n2/etc/pki/CA").mkdir(parents=True)
    (tmp_path / "n2/etc/pki/CA/cacert.pem").write_text(other.ca_cert())
    with pytest.raises(PKIError, match="different libvirt CA"):
        n2.install("n2", "10.0.0.2", ca.ca_cert(), certs2, ["n1", "n2"])


def test_prepare_refuses_wrong_host_and_names(tmp_path):
    n2 = node(tmp_path, "n2")
    with pytest.raises(PKIError):
        n2.prepare("n3")
    with pytest.raises(PKIError):
        n2.set_peers(["ok", "bad name"])
    with pytest.raises(PKIError):
        n2.set_peers([])


def test_set_peers_replaces_only_its_lines(tmp_path):
    n2 = node(tmp_path, "n2")
    conf = tmp_path / "n2/etc/libvirt/libvirtd.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text('log_level = 3\ntls_allowed_dn_list = ["old"]\n')
    n2.set_peers(["n1", "n2"])
    n2.set_peers(["n1"])
    text = conf.read_text()
    assert "log_level = 3" in text and "old" not in text and "n2-control" not in text
    assert text.count("# ibHiCS verified migration TLS") == 1
