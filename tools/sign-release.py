#!/usr/bin/env python3
"""Sign a license or release manifest: sign-release.py PRIVATE.pem payload.json output.json.

For bundles the payload must include filename, version, released and sha256 of the wheel.
Generate offline keys with: openssl genpkey -algorithm ED25519 -out private.pem
Export the lab key with: openssl pkey -in private.pem -pubout -out release-public.pem
Keep the private key offline; install only the public key in the node state directory.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    key, source, target = map(Path, sys.argv[1:])
    payload = json.loads(source.read_text())
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
        envelope = {"payload": payload, "signature": base64.b64encode(sig.read_bytes()).decode()}
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(envelope, out, indent=2)


if __name__ == "__main__":
    main()
