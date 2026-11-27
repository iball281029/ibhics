#!/usr/bin/env python3
"""Sign a license or release manifest: sign-release.py PRIVATE.pem payload.json output.json.

For bundles the payload must include filename, version, released and sha256 of the wheel.
Generate offline keys with: openssl genpkey -algorithm ED25519 -out private.pem
Export the lab key with: openssl pkey -in private.pem -pubout -out release-public.pem
Keep the private key offline; install only the public key in the node state directory.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ibhics.signing import sign_payload  # noqa: E402


def main():
    key, source, target = map(Path, sys.argv[1:])
    envelope = sign_payload(key, json.loads(source.read_text()))
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(envelope, out, indent=2)


if __name__ == "__main__":
    main()
