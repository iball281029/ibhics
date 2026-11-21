"""RFC 6238 TOTP using only Python's standard library."""

import base64
import hashlib
import hmac
import re
import struct


def totp(secret: str, timestamp: float, digits: int = 6, algorithm: str = "sha1") -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = struct.pack(">Q", int(timestamp) // 30)
    digest = hmac.new(key, counter, getattr(hashlib, algorithm)).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def verify_totp(secret: str, code: str, now: float, last_step: int = -1) -> int | None:
    if not re.fullmatch(r"[0-9]{6}", code):
        return None
    for step in (int(now) // 30, int(now) // 30 - 1, int(now) // 30 + 1):
        if step > last_step and step >= 0 and hmac.compare_digest(totp(secret, step * 30), code):
            return step
    return None
