"""First boot of the dedicated diskless Arbiter appliance."""

from __future__ import annotations

import secrets

from . import setup as s


def main() -> int:
    print("\n  Arbiter — ibHiCS cluster and storage witness\n")
    print("  Arbiter votes in quorum; it stores no VM data.\n")
    hostname = s.ask("Appliance name", "arbiter-01", s.check_hostname)
    nics = s.detect_interfaces()
    if not nics:
        print("No network interface found.")
        return 1
    s.wake_ports(nics)
    interface = nics[0] if len(nics) == 1 else s.choose("Network port", [(n, n) for n in nics])
    address = s.ask("Management address with prefix", "172.16.12.105/24", s.check_cidr)
    gateway = s.ask("Default gateway", "172.16.12.254", s.check_ip)
    dns = s.ask(
        "DNS servers (space separated)",
        "172.16.10.1 8.8.8.8",
        lambda v: [s.check_ip(x) for x in v.split()],
    )
    timezone = s.ask("Time zone", "Asia/Bangkok")
    password = s.ask_password("Password for console user root")
    key = s.ask("SSH public key (optional)", check=s.check_ssh_key, optional=True)
    cfg = s.Config(
        "witness",
        hostname,
        interface,
        address,
        gateway,
        dns.split(),
        timezone,
        secrets.token_urlsafe(32),
        key,
    )
    cfg.validate()
    print(f"\n  Arbiter {hostname} at {address} on {interface}\n")
    if s.ask("Apply these settings? (yes/no)", "yes").lower() not in ("yes", "y"):
        return 1
    s.apply(cfg, password, None)
    print(
        f"\n  In Cluster management, Add node → witness:\n"
        f"  Name: {hostname}\n  Address: http://{cfg.ip}:8007\n"
        f"  Agent pairing key: {cfg.token}\n\n"
        "  Keep the pairing key private. Storage replication is configured separately.\n"
    )
    input("Press Enter to continue to login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
