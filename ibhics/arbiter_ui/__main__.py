"""Run the Arbiter web console on HTTPS 8888, making its certificate on first start."""

import ipaddress
import os
import socket
import subprocess
from pathlib import Path

import uvicorn

from . import create_app

TLS = Path("/etc/ibhics/tls")
CERT, KEY = TLS / "arbiter.crt", TLS / "arbiter.key"


def management_ip(env: Path = Path("/etc/ibhics/ibhics.env")) -> str:
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("IBHICS_AGENT_HOST="):
            value = line.split("=", 1)[1].strip().strip("\"'")
            try:
                return str(ipaddress.IPv4Address(value))
            except ValueError:
                pass
    return ""


def ensure_certificate(cert: Path = CERT, key: Path = KEY, run=subprocess.run) -> None:
    """A self-signed certificate for this appliance; the private key never leaves it."""
    if cert.exists() and key.exists():
        return
    cert.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = socket.gethostname()
    san = f"DNS:{name}" + (f",IP:{ip}" if (ip := management_ip()) else "")
    old = os.umask(0o077)
    try:
        run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:3072",
                "-sha256",
                "-days",
                "3650",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-subj",
                f"/O=ibHiCS/CN={name}",
                "-addext",
                f"subjectAltName={san}",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    finally:
        os.umask(old)
    key.chmod(0o600)
    cert.chmod(0o644)


def main() -> None:
    ensure_certificate()
    uvicorn.run(
        create_app(), host="0.0.0.0", port=8888, ssl_certfile=str(CERT), ssl_keyfile=str(KEY)
    )


if __name__ == "__main__":
    main()
