"""Dependency-free Ed25519 verification using OpenSSL (never a shell)."""

import os
import subprocess
import tempfile
from pathlib import Path

# The manufacturer's release public key. The private key is kept offline by the
# release owner and is never stored in this repository.
PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAwzr7VSk+ag0c5Soet4w7ZU+JUqQVixwp5sArI3OZhlM=
-----END PUBLIC KEY-----
"""


class Ed25519Verifier:
    def __init__(self, state: Path | None = None):
        self.key_file = state / "release-public.pem" if state else None

    def __call__(self, data: bytes, signature: bytes) -> bool:
        if len(signature) != 64:
            return False
        try:
            key = (
                self.key_file.read_bytes()
                if self.key_file and self.key_file.exists()
                else PUBLIC_KEY.encode()
            )
            # Enforce Ed25519 SPKI, not another algorithm accepted by pkeyutl.
            import base64

            der = base64.b64decode(b"".join(key.splitlines()[1:-1]), validate=True)
            if len(der) != 44 or der[:12] != bytes.fromhex("302a300506032b6570032100"):
                return False
            with tempfile.TemporaryDirectory(prefix="ibhics-verify-") as directory:
                paths = [Path(directory) / name for name in ("pub.pem", "data", "sig")]
                for path, content in zip(paths, (key, data, signature), strict=True):
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as out:
                        out.write(content)
                result = subprocess.run(
                    [
                        "openssl",
                        "pkeyutl",
                        "-verify",
                        "-pubin",
                        "-inkey",
                        str(paths[0]),
                        "-rawin",
                        "-in",
                        str(paths[1]),
                        "-sigfile",
                        str(paths[2]),
                    ],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                return result.returncode == 0
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
