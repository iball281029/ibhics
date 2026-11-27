"""Cluster-bound obfuscation of a signed license's Codex credentials.

PEPPER is shipped in customer software: this makes extraction harder, not secret.
The Ed25519 envelope, rather than this encryption, establishes vendor authenticity.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess

from ..license_identity import cluster_key

PEPPER = b"ibHiCS/customer-ai-pass/2026/v1"
MAX_AUTH = 64 * 1024
ACCOUNT = re.compile(r"[a-z0-9-]+\Z")


def _keys(cluster_id):
    cluster = cluster_key(cluster_id).encode()
    if not cluster:
        raise ValueError("AI pass requires a cluster ID")
    key = hashlib.sha256(b"ibhics-ai-auth-v1" + cluster + PEPPER).digest()
    mac_key = hashlib.sha256(b"ibhics-ai-auth-mac-v1" + cluster + PEPPER).digest()
    return key, mac_key


def _crypt(data, key, iv):
    try:
        return subprocess.run(
            ["openssl", "enc", "-aes-256-ctr", "-K", key.hex(), "-iv", iv.hex(), "-nosalt"],
            input=data,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Unable to process AI pass with OpenSSL") from None


def _parts(blob):
    if not isinstance(blob, str) or len(blob) > 4 * ((MAX_AUTH + 2) // 3) + 100:
        raise ValueError("Invalid AI pass size")
    try:
        version, iv, ct, mac = blob.split(".")
        iv, ct, mac = (base64.b64decode(v, validate=True) for v in (iv, ct, mac))
    except (ValueError, TypeError):
        raise ValueError("Invalid AI pass format") from None
    if version != "v1" or len(iv) != 16 or not 0 < len(ct) <= MAX_AUTH or len(mac) != 32:
        raise ValueError("Invalid AI pass format")
    return iv, ct, mac


def validate_ai(ai):
    if not isinstance(ai, dict):
        raise ValueError("Invalid AI entitlement")
    if not isinstance(ai.get("account"), str) or not ACCOUNT.fullmatch(ai["account"]):
        raise ValueError("Invalid AI account")
    if type(ai.get("daily_limit")) is not int or not 1 <= ai["daily_limit"] <= 50:
        raise ValueError("AI daily limit must be between 1 and 50")
    _parts(ai.get("auth"))


def validate_auth(data):
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise ValueError("AI pass does not contain valid auth.json") from None
    if (
        not isinstance(value, dict)
        or value.get("OPENAI_API_KEY")
        or not isinstance(value.get("tokens"), dict)
        or not isinstance(value["tokens"].get("access_token"), str)
        or not value["tokens"]["access_token"]
    ):
        raise ValueError("AI pass does not contain valid auth.json")


def seal(auth_json_bytes: bytes, cluster_id: str) -> str:
    if not isinstance(auth_json_bytes, bytes) or not 0 < len(auth_json_bytes) <= MAX_AUTH:
        raise ValueError("AI auth exceeds 64 KiB or is empty")
    key, mac_key = _keys(cluster_id)
    iv = os.urandom(16)
    ct = _crypt(auth_json_bytes, key, iv)
    mac = hmac.digest(mac_key, b"v1" + iv + ct, "sha256")
    return "v1." + ".".join(base64.b64encode(v).decode() for v in (iv, ct, mac))


def open_sealed(blob: str, cluster_id: str) -> bytes:
    iv, ct, mac = _parts(blob)
    key, mac_key = _keys(cluster_id)
    if not hmac.compare_digest(mac, hmac.digest(mac_key, b"v1" + iv + ct, "sha256")):
        raise ValueError("AI pass authentication failed for this cluster")
    return _crypt(ct, key, iv)
