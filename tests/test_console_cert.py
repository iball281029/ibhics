"""The console certificate gains the VIP and node IP, keeping its key."""

import shutil
import subprocess

import pytest

from ibhics.manager import console_cert

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


def make(tmp_path, san="DNS:n1,IP:10.0.0.1"):
    key, cert = tmp_path / "m.key", tmp_path / "m.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "30",
            "-keyout",
            key,
            "-out",
            cert,
            "-subj",
            "/CN=n1",
            "-addext",
            f"subjectAltName={san}",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def pubkey(cert):
    return subprocess.run(
        ["openssl", "x509", "-in", cert, "-noout", "-pubkey"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_adds_missing_names_and_keeps_the_key(tmp_path):
    cert, key = make(tmp_path)
    before = pubkey(cert)
    assert console_cert.ensure(cert, key, ["n1"], ["10.0.0.1", "10.0.0.11"])
    _, names = console_cert.names_of(cert)
    assert {"n1", "10.0.0.1", "10.0.0.11"} <= names
    assert pubkey(cert) == before  # P2V pins stay valid
    assert list(tmp_path.glob("m.crt.before-*"))
    assert not console_cert.ensure(cert, key, ["n1"], ["10.0.0.1", "10.0.0.11"])


def test_ca_issued_certificate_is_left_alone(tmp_path):
    cert, key = make(tmp_path)
    fake = lambda args: "subject=CN=n1\nissuer=CN=Corp CA\n"  # noqa: E731
    assert not console_cert.ensure(cert, key, ["n1"], ["10.0.0.11"], run=fake)
