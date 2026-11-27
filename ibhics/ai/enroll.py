"""Operator-only enrollment. Writes secrets to private files, never stdout."""

import argparse
import json
import os
import secrets
from pathlib import Path

from ..manager.settings import cluster_key, private_json


def enroll(profiles_path, token_path, cluster_id, nodes, daily_requests):
    tenant = cluster_key(cluster_id)
    if not tenant or not 1 <= nodes <= 1024 or not 1 <= daily_requests <= 1000:
        raise ValueError("Specify cluster identity, node count and a daily allowance (1-1000)")
    profiles = json.loads(profiles_path.read_text()) if profiles_path.exists() else {}
    if tenant in profiles:
        raise ValueError("Installation already enrolled; explicitly review credential rotation")
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(32)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(token)
        import hashlib

        profiles[tenant] = {
            "enabled": True,
            "nodes": nodes,
            "daily_requests": daily_requests,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        }
        private_json(profiles_path, profiles)
    except BaseException:
        token_path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--nodes", type=int, required=True)
    parser.add_argument("--daily-requests", type=int, default=50)
    args = parser.parse_args()
    enroll(args.profiles, args.token_file, args.cluster_id, args.nodes, args.daily_requests)
    print("Installation enrolled. Private enrollment token written to", args.token_file)


if __name__ == "__main__":
    main()
