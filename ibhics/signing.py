"""Shared offline envelope signing; requires the OpenSSL CLI."""

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path


def sign_payload(key, payload):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    with tempfile.TemporaryDirectory(prefix="ibhics-sign-") as directory:
        data, sig = Path(directory) / "data", Path(directory) / "sig"
        for path in (data, sig):
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        data.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(key),
                "-rawin",
                "-in",
                str(data),
                "-out",
                str(sig),
            ],
            check=True,
            timeout=10,
        )
        return {"payload": payload, "signature": base64.b64encode(sig.read_bytes()).decode()}
