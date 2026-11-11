#!/usr/bin/env python3
"""Root-only, explicit libvirt migration PKI provisioning; never prints private keys.

Prepare keys/CSRs on each node, sign ONLY public CSRs on the CA node, transfer only
public certificates, then apply. Daemon restarts are deliberately a separate step.
"""

import argparse
import grp
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path("/etc/ibhics/migration-pki")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")


def run(args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    if p.returncode:
        raise RuntimeError(f"{args[0]} failed: {p.stderr[-1000:]}")
    return p.stdout


def secure_root():
    if os.geteuid() != 0:
        raise ValueError("Run as root on the intended node")
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    os.umask(0o077)


def key(path):
    if not path.exists():
        run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:3072",
                "-out",
                str(path),
            ]
        )
    path.chmod(0o600)


def names(args):
    if not NAME.fullmatch(args.name):
        raise ValueError("Invalid node name")
    return str(ipaddress.IPv4Address(args.address))


def prepare(args):
    address = names(args)
    if socket.gethostname() != args.name:
        raise ValueError("Node name must match the actual hostname")
    for purpose in ("control", "migration"):
        private = ROOT / (purpose + ".key")
        key(private)
        run(
            [
                "openssl",
                "req",
                "-new",
                "-key",
                str(private),
                "-out",
                str(ROOT / (purpose + ".csr")),
                "-subj",
                f"/O=ibHiCS/CN={args.name}-{purpose}",
            ]
        )
    print(
        json.dumps(
            {
                "node": args.name,
                "address": address,
                "public_csrs": [str(ROOT / "control.csr"), str(ROOT / "migration.csr")],
            }
        )
    )


def create_ca(_args):
    private = ROOT / "ca.key"
    cert = ROOT / "ca.crt"
    if private.exists() != cert.exists():
        raise ValueError("Incomplete CA exists; inspect before continuing")
    if not cert.exists():
        key(private)
        run(
            [
                "openssl",
                "req",
                "-new",
                "-x509",
                "-sha256",
                "-days",
                "3650",
                "-key",
                str(private),
                "-out",
                str(cert),
                "-subj",
                "/O=ibHiCS/CN=ibHiCS migration CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            ]
        )
    print("CA ready; export ca.crt only, never ca.key")


def sign(args):
    address = names(args)
    with tempfile.NamedTemporaryFile(mode="w", dir=ROOT, suffix=".cnf") as ext:
        ext.write(
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth,clientAuth\n"
            f"subjectAltName=DNS:{args.name},IP:{address}\n"
        )
        ext.flush()
        run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(args.csr),
                "-CA",
                str(ROOT / "ca.crt"),
                "-CAkey",
                str(ROOT / "ca.key"),
                "-CAcreateserial",
                "-days",
                "825",
                "-sha256",
                "-subj",
                f"/O=ibHiCS/CN={args.name}-{args.purpose}",
                "-extfile",
                ext.name,
                "-out",
                str(args.out),
            ]
        )
    print("Signed public certificate:", args.out)


def copy(source, target, mode=0o644, gid=0):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    os.chown(target, 0, gid)
    target.chmod(mode)


def settings(path, values):
    path = Path(path)
    old = path.read_text() if path.exists() else ""
    keys = set(values)
    lines = [
        line
        for line in old.splitlines()
        if not any(re.match(r"^\s*" + re.escape(k) + r"\s*=", line) for k in keys)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(lines)
        + "\n# ibHiCS verified migration TLS\n"
        + "\n".join(k + " = " + v for k, v in values.items())
        + "\n"
    )


def apply(args):
    address = names(args)
    if socket.gethostname() != args.name or not all(NAME.fullmatch(n) for n in args.peers):
        raise ValueError("Host or peer identity does not match")
    incoming = args.certs
    ca = incoming / "ca.crt"
    for purpose in ("control", "migration"):
        cert = incoming / (args.name + "-" + purpose + ".crt")
        for usage in ("sslserver", "sslclient"):
            run(
                [
                    "openssl",
                    "verify",
                    "-CAfile",
                    str(ca),
                    "-purpose",
                    usage,
                    "-verify_ip",
                    address,
                    str(cert),
                ]
            )
        public = run(["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"])
        local = run(["openssl", "pkey", "-in", str(ROOT / (purpose + ".key")), "-pubout"])
        if public != local:
            raise ValueError("Certificate does not match this node private key")
    Path("/etc/pki").mkdir(exist_ok=True)
    Path("/etc/pki").chmod(0o755)  # Public PKI parent; private keys retain restricted modes.
    existing = Path("/etc/pki/CA/cacert.pem")
    if existing.exists() and existing.read_bytes() != ca.read_bytes():
        raise ValueError(
            "A different libvirt CA exists; do not overwrite an established trust domain"
        )
    backup = Path("/root/ibhics-migration-tls-backup-" + time.strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(mode=0o700)
    for p in ("/etc/libvirt/libvirtd.conf", "/etc/libvirt/qemu.conf", "/etc/ibhics/ibhics.env"):
        if Path(p).exists():
            shutil.copy2(p, backup / Path(p).name)
    copy(ca, existing)
    existing.parent.chmod(0o755)
    private = Path("/etc/pki/libvirt/private")
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.chmod(0o700)
    for role in ("server", "client"):
        copy(ROOT / "control.key", private / (role + "key.pem"), 0o600)
        copy(
            incoming / (args.name + "-control.crt"), Path("/etc/pki/libvirt") / (role + "cert.pem")
        )
    gid = grp.getgrnam("libvirt-qemu").gr_gid
    qemu = Path("/etc/pki/qemu")
    qemu.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chown(qemu, 0, gid)
    qemu.chmod(0o750)
    copy(ca, qemu / "ca-cert.pem")
    for role in ("server", "client"):
        copy(ROOT / "migration.key", qemu / (role + "-key.pem"), 0o640, gid)
        copy(incoming / (args.name + "-migration.crt"), qemu / (role + "-cert.pem"))
    settings(
        "/etc/libvirt/libvirtd.conf",
        {
            "listen_tls": "1",
            "listen_tcp": "0",
            "auth_tls": '"none"',
            "tls_no_verify_certificate": "0",
            "tls_allowed_dn_list": json.dumps(["*CN=" + n + "-control*" for n in args.peers]),
        },
    )
    settings(
        "/etc/libvirt/qemu.conf",
        {
            "migrate_tls_x509_cert_dir": '"/etc/pki/qemu"',
            "migrate_tls_x509_verify": "1",
            "migrate_tls_force": "1",
        },
    )
    env = Path("/etc/ibhics/ibhics.env")
    lines = env.read_text().splitlines()
    lines = [
        s
        for s in lines
        if not s.startswith(("IBHICS_MIGRATION_HOST=", "IBHICS_MIGRATION_TRANSPORT="))
    ]
    env.write_text(
        "\n".join(lines) + f"\nIBHICS_MIGRATION_HOST={address}\nIBHICS_MIGRATION_TRANSPORT=tls\n"
    )
    env.chmod(0o600)
    override = Path("/etc/systemd/system/libvirtd-tls.socket.d/ibhics.conf")
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(f"[Socket]\nListenStream=\nListenStream={address}:16514\n")
    print(
        json.dumps(
            {
                "node": args.name,
                "configured": True,
                "backup": str(backup),
                "next": "Enable TLS socket; restart libvirtd/apps only; preserve QEMU guests",
            }
        )
    )


def activate(_args):
    # Socket activation refuses to add a listener while its service is already up.
    # Stop only management daemons/sockets; libvirtd's KillMode=process preserves VMs.
    for cert in ("/etc/pki/CA/cacert.pem", "/etc/pki/libvirt/servercert.pem"):
        run(["openssl", "x509", "-in", cert, "-checkend", "86400", "-noout"])
    pids = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                if proc.joinpath("comm").read_text().startswith("qemu-system"):
                    pids.append(int(proc.name))
            except (FileNotFoundError, PermissionError):
                pass
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "libvirtd-tls.socket"])
    run(["systemctl", "stop", "ibhics-agent", "ibhics-manager"])
    try:
        run(
            [
                "systemctl",
                "stop",
                "libvirtd.service",
                "libvirtd.socket",
                "libvirtd-ro.socket",
                "libvirtd-admin.socket",
            ]
        )
        run(
            [
                "systemctl",
                "start",
                "libvirtd-tls.socket",
                "libvirtd-ro.socket",
                "libvirtd-admin.socket",
            ]
        )
        run(["systemctl", "start", "libvirtd.service"])
    finally:
        run(["systemctl", "start", "ibhics-agent", "ibhics-manager"])
    for pid in pids:
        os.kill(pid, 0)
    print(
        json.dumps(
            {
                "tls_socket_active": run(["systemctl", "is-active", "libvirtd-tls.socket"]).strip(),
                "existing_qemu_processes_preserved": len(pids),
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("create-ca").set_defaults(func=create_ca)
    sub.add_parser("activate").set_defaults(func=activate)
    for action, func in (("prepare", prepare), ("sign", sign), ("apply", apply)):
        p = sub.add_parser(action)
        p.add_argument("--name", required=True)
        p.add_argument("--address", required=True)
        p.set_defaults(func=func)
        if action == "sign":
            p.add_argument("--csr", type=Path, required=True)
            p.add_argument("--out", type=Path, required=True)
            p.add_argument("--purpose", choices=("control", "migration"), required=True)
        if action == "apply":
            p.add_argument("--certs", type=Path, required=True)
            p.add_argument("--peers", nargs="+", required=True)
    args = parser.parse_args()
    secure_root()
    args.func(args)


if __name__ == "__main__":
    main()
