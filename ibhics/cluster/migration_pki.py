"""Certificates for encrypted live migration between compute nodes.

Each node makes its own private keys and certificate requests; only the manager node
holds the CA key and signs. Only public material (requests, certificates, the CA
certificate) ever leaves a node. libvirtd accepts TLS connections only from the
control certificates of current compute nodes, so removing a node from that list
revokes it without a CRL.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
PURPOSES = ("control", "migration")
CERT_DAYS = 825
RENEW_DAYS = 60  # status reports a certificate due for renewal this long before expiry


class PKIError(ValueError):
    pass


def _check_name(name: str) -> str:
    if not NAME.fullmatch(name or ""):
        raise PKIError(f"invalid node name {name!r}")
    return name


def _check_ip(address: str) -> str:
    try:
        return str(ipaddress.IPv4Address(address))
    except ValueError:
        raise PKIError(f"{address!r} is not an IPv4 address") from None


def _run(args: list[str], stdin: str | None = None) -> str:
    p = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=60, check=False)
    if p.returncode:
        raise PKIError(f"{args[0]} {args[1] if len(args) > 1 else ''} failed: {p.stderr[-500:]}")
    return p.stdout


class MigrationPKI:
    """Files of one node, all under `base` ("/" on a real node, a folder in tests)."""

    def __init__(
        self,
        base: Path | str = "/",
        run: Callable[..., str] = _run,
        hostname: Callable[[], str] = socket.gethostname,
        libvirt_gid: Callable[[], int] | None = None,
        chown: bool = True,
    ) -> None:
        self.base = Path(base)
        self.run = run
        self.hostname = hostname
        self._gid = libvirt_gid
        self._chown = chown
        self.root = self.base / "etc/ibhics/migration-pki"

    # Paths

    def _p(self, path: str) -> Path:
        return self.base / path.lstrip("/")

    def _secure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def _key(self, path: Path) -> None:
        if not path.exists():
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".key-")
            os.close(fd)
            try:
                self.run(
                    [
                        "openssl",
                        "genpkey",
                        "-algorithm",
                        "RSA",
                        "-pkeyopt",
                        "rsa_keygen_bits:3072",
                        "-out",
                        tmp,
                    ]
                )
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
        path.chmod(0o600)

    # CA (manager node only)

    def has_ca(self) -> bool:
        return (self.root / "ca.key").exists() and (self.root / "ca.crt").exists()

    def create_ca(self) -> str:
        self._secure_root()
        key, cert = self.root / "ca.key", self.root / "ca.crt"
        if key.exists() != cert.exists():
            raise PKIError("incomplete migration CA exists; inspect it before continuing")
        if not cert.exists():
            self._key(key)
            self.run(
                [
                    "openssl",
                    "req",
                    "-new",
                    "-x509",
                    "-sha256",
                    "-days",
                    "3650",
                    "-key",
                    str(key),
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
        return cert.read_text()

    def ca_cert(self) -> str:
        if not self.has_ca():
            raise PKIError("this node holds no migration CA")
        return (self.root / "ca.crt").read_text()

    def sign(self, csr_pem: str, name: str, address: str, purpose: str) -> str:
        """Sign a node's public request; the subject and names come from the cluster's
        record of the node, not from the request."""
        _check_name(name)
        address = _check_ip(address)
        if purpose not in PURPOSES:
            raise PKIError(f"unknown certificate purpose {purpose!r}")
        if not self.has_ca():
            raise PKIError("this node holds no migration CA")
        with tempfile.TemporaryDirectory(dir=self.root) as work:
            csr, ext, out = (Path(work) / n for n in ("req.csr", "ext.cnf", "out.crt"))
            csr.write_text(csr_pem)
            ext.write_text(
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth,clientAuth\n"
                f"subjectAltName=DNS:{name},IP:{address}\n"
            )
            self.run(["openssl", "req", "-in", str(csr), "-verify", "-noout"])
            self.run(
                [
                    "openssl",
                    "x509",
                    "-req",
                    "-in",
                    str(csr),
                    "-CA",
                    str(self.root / "ca.crt"),
                    "-CAkey",
                    str(self.root / "ca.key"),
                    "-CAcreateserial",
                    "-days",
                    str(CERT_DAYS),
                    "-sha256",
                    "-subj",
                    f"/O=ibHiCS/CN={name}-{purpose}",
                    "-extfile",
                    str(ext),
                    "-out",
                    str(out),
                ]
            )
            return out.read_text()

    # Every compute node

    def prepare(self, name: str) -> dict[str, str]:
        """Keys (kept, or made once) and fresh requests for this node. Public only."""
        _check_name(name)
        if self.hostname() != name:
            raise PKIError(f"this node is {self.hostname()!r}, not {name!r}")
        self._secure_root()
        csrs = {}
        for purpose in PURPOSES:
            key = self.root / f"{purpose}.key"
            self._key(key)
            csr = self.root / f"{purpose}.csr"
            self.run(
                [
                    "openssl",
                    "req",
                    "-new",
                    "-key",
                    str(key),
                    "-out",
                    str(csr),
                    "-subj",
                    f"/O=ibHiCS/CN={name}-{purpose}",
                ]
            )
            csrs[purpose] = csr.read_text()
        return csrs

    def _verify(self, ca: Path, cert: Path, address: str, purpose: str) -> None:
        for usage in ("sslserver", "sslclient"):
            self.run(
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
        public = self.run(["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"])
        local = self.run(["openssl", "pkey", "-in", str(self.root / f"{purpose}.key"), "-pubout"])
        if public.strip() != local.strip():
            raise PKIError(f"the {purpose} certificate does not match this node's key")

    def install(
        self, name: str, address: str, ca_pem: str, certs: dict[str, str], peers: list[str]
    ) -> dict:
        """Check the signed certificates against this node's keys and the CA, then put
        them where libvirt and QEMU read them. Takes effect at `activate`."""
        _check_name(name)
        address = _check_ip(address)
        if self.hostname() != name:
            raise PKIError(f"this node is {self.hostname()!r}, not {name!r}")
        if set(certs) != set(PURPOSES):
            raise PKIError("need a control and a migration certificate")
        self._secure_root()
        existing = self._p("/etc/pki/CA/cacert.pem")
        if existing.exists() and existing.read_text().strip() != ca_pem.strip():
            raise PKIError("a different libvirt CA is installed; not replacing that trust")
        with tempfile.TemporaryDirectory(dir=self.root) as work:
            ca = Path(work) / "ca.crt"
            ca.write_text(ca_pem)
            for purpose in PURPOSES:
                cert = Path(work) / f"{purpose}.crt"
                cert.write_text(certs[purpose])
                self._verify(ca, cert, address, purpose)
            backup = self._backup()
            pki = self._p("/etc/pki")
            pki.mkdir(parents=True, exist_ok=True)
            pki.chmod(0o755)  # public parent; private folders keep their own modes
            self._copy(ca, existing)
            existing.parent.chmod(0o755)
            private = self._p("/etc/pki/libvirt/private")
            private.mkdir(parents=True, exist_ok=True, mode=0o700)
            private.chmod(0o700)
            control_crt = Path(work) / "control.crt"
            for role in ("server", "client"):
                self._copy(self.root / "control.key", private / f"{role}key.pem", 0o600)
                self._copy(control_crt, self._p(f"/etc/pki/libvirt/{role}cert.pem"))
            gid = self._libvirt_gid()
            qemu = self._p("/etc/pki/qemu")
            qemu.mkdir(parents=True, exist_ok=True, mode=0o750)
            if self._chown:
                os.chown(qemu, 0, gid)
            qemu.chmod(0o750)
            self._copy(ca, qemu / "ca-cert.pem")
            for role in ("server", "client"):
                self._copy(self.root / "migration.key", qemu / f"{role}-key.pem", 0o640, gid)
                self._copy(Path(work) / "migration.crt", qemu / f"{role}-cert.pem")
        self._settings(
            "/etc/libvirt/qemu.conf",
            {
                "migrate_tls_x509_cert_dir": '"/etc/pki/qemu"',
                "migrate_tls_x509_verify": "1",
                "migrate_tls_force": "1",
            },
        )
        self.set_peers(peers, backup=False)
        env = self._p("/etc/ibhics/ibhics.env")
        lines = env.read_text().splitlines() if env.exists() else []
        lines = [
            s
            for s in lines
            if not s.startswith(("IBHICS_MIGRATION_HOST=", "IBHICS_MIGRATION_TRANSPORT="))
        ]
        lines += [f"IBHICS_MIGRATION_HOST={address}", "IBHICS_MIGRATION_TRANSPORT=tls"]
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text("\n".join(lines) + "\n")
        env.chmod(0o600)
        override = self._p("/etc/systemd/system/libvirtd-tls.socket.d/ibhics.conf")
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(f"[Socket]\nListenStream=\nListenStream={address}:16514\n")
        return {"node": name, "backup": str(backup), "peers": sorted(set(peers))}

    def set_peers(self, peers: list[str], backup: bool = True) -> list[str]:
        """Only these compute nodes' control certificates may connect to libvirtd."""
        names = sorted({_check_name(p) for p in peers})
        if not names:
            raise PKIError("at least one compute node must stay allowed")
        if backup:
            self._backup()
        self._settings(
            "/etc/libvirt/libvirtd.conf",
            {
                "listen_tls": "1",
                "listen_tcp": "0",
                "auth_tls": '"none"',
                "tls_no_verify_certificate": "0",
                # GnuTLS orders DN parts its own way: match the CN wherever it sits.
                "tls_allowed_dn_list": json.dumps([f"*CN={n}-control*" for n in names]),
            },
        )
        return names

    def status(self) -> dict:
        """When this node's certificates expire; nothing secret."""
        out = {}
        for purpose, path in (
            ("ca", "/etc/pki/CA/cacert.pem"),
            ("control", "/etc/pki/libvirt/servercert.pem"),
            ("migration", "/etc/pki/qemu/server-cert.pem"),
        ):
            p = self._p(path)
            if not p.exists():
                out[purpose] = None
                continue
            end = self.run(["openssl", "x509", "-in", str(p), "-enddate", "-noout"])
            soon = True
            try:
                self.run(
                    [
                        "openssl",
                        "x509",
                        "-in",
                        str(p),
                        "-checkend",
                        str(RENEW_DAYS * 86400),
                        "-noout",
                    ]
                )
                soon = False
            except PKIError:
                pass
            out[purpose] = {"not_after": end.strip().removeprefix("notAfter="), "renew": soon}
        return out

    # Helpers

    def _libvirt_gid(self) -> int:
        if self._gid:
            return self._gid()
        import grp

        return grp.getgrnam("libvirt-qemu").gr_gid

    def _copy(self, source: Path, target: Path, mode: int = 0o644, gid: int = 0) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".pki-")
        os.close(fd)
        try:
            shutil.copyfile(source, tmp)
            if self._chown:
                os.chown(tmp, 0, gid)
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _backup(self) -> Path:
        backup = self._p("/root") / (
            "ibhics-migration-tls-backup-" + time.strftime("%Y%m%d-%H%M%S")
        )
        backup.mkdir(parents=True, mode=0o700, exist_ok=True)
        for p in ("/etc/libvirt/libvirtd.conf", "/etc/libvirt/qemu.conf", "/etc/ibhics/ibhics.env"):
            if self._p(p).exists():
                shutil.copy2(self._p(p), backup / Path(p).name)
        return backup

    def _settings(self, path: str, values: dict[str, str]) -> None:
        p = self._p(path)
        old = p.read_text() if p.exists() else ""
        keep = [
            line
            for line in old.splitlines()
            if line.strip() != "# ibHiCS verified migration TLS"
            and not any(re.match(r"^\s*" + re.escape(k) + r"\s*=", line) for k in values)
        ]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "\n".join(keep)
            + "\n# ibHiCS verified migration TLS\n"
            + "\n".join(f"{k} = {v}" for k, v in values.items())
            + "\n"
        )


ACTIVATE_UNIT = "ibhics-migration-tls-activate"


def schedule_activate(run: Callable[..., str] = _run, delay_s: int = 3) -> None:
    """Restart libvirtd with the new TLS settings a moment from now, from outside this
    service: activation restarts ibhics-agent and ibhics-manager too."""
    run(
        [
            "systemd-run",
            f"--unit={ACTIVATE_UNIT}-{int(time.time())}",
            f"--on-active={delay_s}",
            "/opt/ibhics/venv/bin/python",
            "-m",
            "ibhics.cluster.migration_pki",
            "activate",
        ]
    )


def activate(run: Callable[..., str] = _run) -> dict:
    """Pick up new libvirt TLS settings. Only management daemons restart: libvirtd's
    KillMode=process keeps every running QEMU guest."""
    for cert in ("/etc/pki/CA/cacert.pem", "/etc/pki/libvirt/servercert.pem"):
        run(["openssl", "x509", "-in", cert, "-checkend", "86400", "-noout"])
    pids = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                if proc.joinpath("comm").read_text().startswith("qemu-system"):
                    pids.append(int(proc.name))
            except OSError:
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
                "libvirtd-tls.socket",
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
        run(["systemctl", "start", "ibhics-agent"])
        try:
            run(["systemctl", "start", "ibhics-manager"])
        except PKIError:
            pass  # a node without the manager
    lost = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except OSError:
            lost.append(pid)
    if lost:
        raise PKIError(f"QEMU processes ended during activation: {lost}")
    return {"qemu_processes_preserved": len(pids)}


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["activate"]:
        print(json.dumps(activate()))
    else:
        raise SystemExit("usage: python -m ibhics.cluster.migration_pki activate")
