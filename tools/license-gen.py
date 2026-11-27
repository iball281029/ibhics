#!/usr/bin/env python3
"""Issue cluster-bound AI passes using a vendor's logged-in Codex accounts."""

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ibhics.ai.license_auth import ACCOUNT, MAX_AUTH, seal, validate_auth  # noqa: E402
from ibhics.signing import sign_payload  # noqa: E402


def account_path(root, name):
    if not ACCOUNT.fullmatch(name):
        raise ValueError("Account name must contain only a-z, 0-9 and hyphens")
    return root / name


def logged_in(path):
    result = subprocess.run(
        ["codex", "login", "status"],
        env={**os.environ, "CODEX_HOME": str(path)},
        capture_output=True,
        timeout=20,
        check=False,
    )
    return result.returncode == 0 and (path / "auth.json").is_file()


def names(root):
    return (
        sorted(p.name for p in root.iterdir() if p.is_dir() and ACCOUNT.fullmatch(p.name))
        if root.exists()
        else []
    )


def records(root):
    path = root.parent / "issued.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if path.exists()
        else []
    )


def generate(args, root):
    import fcntl

    if not args.support_until and not args.update_until:
        raise ValueError("Specify --support-until and/or --update-until")
    for value in (args.support_until, args.update_until):
        if value:
            date.fromisoformat(value)
    if args.nodes < 1 or not args.customer.strip() or not args.cluster_id.strip():
        raise ValueError("Customer, cluster ID and positive node count are required")
    if not 1 <= args.ai_daily_limit <= 50:
        raise ValueError("AI daily limit must be between 1 and 50")
    name = args.ai_account
    if not name:
        options = names(root)
        if not options or not sys.stdin.isatty():
            raise ValueError("Use add-account first, then specify --ai-account")
        for index, item in enumerate(options, 1):
            print(f"{index}. {item}")
        try:
            selected = int(input("Select GPT account: "))
            if not 1 <= selected <= len(options):
                raise ValueError
            name = options[selected - 1]
        except (ValueError, EOFError):
            raise ValueError("Invalid account selection") from None
    account = account_path(root, name)
    if not logged_in(account):
        raise ValueError("Account is not logged in; run add-account for this account")
    with (account / "auth.json").open("rb") as source:
        auth = source.read(MAX_AUTH + 1)
    if len(auth) > MAX_AUTH:
        raise ValueError("AI auth exceeds 64 KiB")
    validate_auth(auth)
    # Serialize count, confirmation and issuance so simultaneous issuers see the same limit.
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root.parent / "issued.jsonl", os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a+") as journal:
        os.fchmod(journal.fileno(), 0o600)
        fcntl.flock(journal, fcntl.LOCK_EX)
        count = sum(row.get("account") == name for row in records(root))
        if count >= 5:
            print(f"Warning: {name} already has {count} issued licenses.", file=sys.stderr)
            if not args.force and (
                not sys.stdin.isatty() or input("Issue another? [y/N] ").lower() != "y"
            ):
                raise ValueError("Confirmation required; use --force to issue another license")
        issued = date.today().isoformat()
        payload = {
            "customer": args.customer,
            "cluster_id": args.cluster_id,
            "nodes": args.nodes,
            "ai": {
                "account": name,
                "daily_limit": args.ai_daily_limit,
                "auth": seal(auth, args.cluster_id),
                "issued": issued,
            },
        }
        for key in ("support_until", "update_until"):
            if getattr(args, key):
                payload[key] = getattr(args, key)
        envelope = sign_payload(args.key, payload)
        out_fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(out_fd, "w") as output:
            json.dump(envelope, output, indent=2)
        journal.write(
            json.dumps(
                {
                    "customer": args.customer,
                    "cluster_id": args.cluster_id,
                    "account": name,
                    "date": issued,
                }
            )
            + "\n"
        )
        journal.flush()
        os.fsync(journal.fileno())
    print(f"License written to {args.output} (account {name})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accounts-dir", type=Path, default=Path.home() / ".ibhics-license" / "accounts"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("accounts")
    add = commands.add_parser("add-account")
    add.add_argument("name")
    gen = commands.add_parser("gen")
    for flag in ("key", "customer", "cluster-id"):
        gen.add_argument("--" + flag, required=True)
    gen.add_argument("--nodes", type=int, required=True)
    gen.add_argument("--support-until")
    gen.add_argument("--update-until")
    gen.add_argument("--ai-account")
    gen.add_argument("--ai-daily-limit", type=int, default=20)
    gen.add_argument("--force", action="store_true")
    gen.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.accounts_dir.expanduser().resolve()
    try:
        if args.command == "accounts":
            issued = records(root)
            for name in names(root):
                count = sum(row.get("account") == name for row in issued)
                state = "logged in" if logged_in(root / name) else "not logged in"
                print(f"{name}: {state}; {count} licenses")
        elif args.command == "add-account":
            path = account_path(root, args.name)
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
            subprocess.run(
                ["codex", "login"], check=True, env={**os.environ, "CODEX_HOME": str(path)}
            )
        else:
            generate(args, root)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # Never print subprocess output or auth contents.
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
