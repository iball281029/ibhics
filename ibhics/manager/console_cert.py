"""Keep the console's self-signed certificate valid for every address it answers on.

Re-signed with the same key, so anything pinned to that key (the P2V agent) keeps
working. A certificate the operator installed from a real CA is never touched.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path


def _openssl(args: list[str]) -> str:
    done = subprocess.run(["openssl", *args], capture_output=True, text=True, timeout=30)
    if done.returncode:
        raise RuntimeError(done.stderr.strip()[-300:])
    return done.stdout


def names_of(cert: Path, run=_openssl) -> tuple[bool, set[str]]:
    """(self-signed?, the DNS names and IPs it is valid for)."""
    text = run(
        ["x509", "-in", str(cert), "-noout", "-subject", "-issuer", "-ext", "subjectAltName"]
    )
    subject = re.search(r"^subject=\s*(.*)$", text, re.M)
    issuer = re.search(r"^issuer=\s*(.*)$", text, re.M)
    self_signed = bool(subject and issuer and subject.group(1) == issuer.group(1))
    names = set(re.findall(r"DNS:([^,\s]+)", text)) | set(re.findall(r"IP Address:([0-9.]+)", text))
    return self_signed, names


def ensure(cert: Path, key: Path, dns: list[str], ips: list[str], run=_openssl) -> bool:
    """Re-sign when a name is missing. Returns True when the certificate changed."""
    cert, key = Path(cert), Path(key)
    if not cert.exists() or not key.exists():
        return False
    self_signed, have = names_of(cert, run)
    want = [d for d in dns if d] + [i for i in ips if i]
    if not self_signed or all(w in have for w in want):
        return False
    names = sorted(have | set(want))
    san = ",".join(f"IP:{n}" if re.fullmatch(r"[0-9.]+", n) else f"DNS:{n}" for n in names)
    cn = next((d for d in dns if d), names[0])
    tmp = cert.with_name(cert.name + ".new")
    run(
        [
            "req",
            "-x509",
            "-new",
            "-key",
            str(key),
            "-sha256",
            "-days",
            "3650",
            "-subj",
            f"/CN={cn}",
            "-addext",
            f"subjectAltName={san}",
            "-out",
            str(tmp),
        ]
    )
    shutil.copy2(cert, cert.with_name(cert.name + time.strftime(".before-%Y%m%d-%H%M%S")))
    tmp.chmod(0o644)
    tmp.replace(cert)
    return True
