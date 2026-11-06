"""The console's Terminal: run a diagnostic command on a node, like over SSH but only
from an allowlist (look, never change), with no shell, and a time limit."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable

ARG_RE = re.compile(r"^[A-Za-z0-9_.:/=@%+,-]{1,253}$")
# Words that would make `ip`, `bridge` or `nft` change something.
CHANGES = {"add", "del", "delete", "set", "flush", "change", "replace", "append", "insert",
           "create", "destroy", "reset", "monitor", "exec", "netns", "-b", "-batch",
           "-f", "-force", "restore"}  # fmt: skip
TIMEOUT = 60

# command: (what it does, the subcommands allowed as first word or None for any)
COMMANDS: dict[str, tuple[str, set[str] | None]] = {
    "ping": ("reachability (ICMP echo)", None),
    "tracepath": ("the route to a host", None),
    "traceroute": ("the route to a host (runs tracepath)", None),
    "arping": ("whether an IP answers on a LAN (ARP)", None),
    "ip": ("addresses, routes, links, neighbours", None),
    "bridge": ("bridge ports, VLANs and MAC table", {"link", "vlan", "fdb"}),
    "ss": ("listening and open sockets", None),
    "nft": ("firewall rules", {"list"}),
    "getent": ("look a host name up (DNS, /etc/hosts)", {"hosts", "ahosts", "ahostsv4"}),
    "nslookup": ("ask DNS about a name", None),
    "dig": ("ask DNS about a name", None),
    "ethtool": ("link speed and driver of a NIC", None),
    "df": ("free space of filesystems", None),
    "free": ("memory use", None),
    "uptime": ("uptime and load", None),
    "lsblk": ("disks and partitions", None),
    "hostname": ("the node's name", set()),
    "date": ("the node's clock", set()),
    "virsh": (
        "libvirt state",
        {
            "list",
            "net-list",
            "pool-list",
            "domblklist",
            "domiflist",
            "dominfo",
            "version",
            "nodeinfo",
            "vol-list",
        },
    ),  # fmt: skip
}
ETHTOOL_OK = {"-i", "-S", "-k", "-g", "-a", "-m", "--driver", "--statistics"}


def help_text() -> str:
    rows = [f"  {name:<11} {what}" for name, (what, _) in sorted(COMMANDS.items())]
    return "Commands you can run here:\n" + "\n".join(rows) + "\n\nNo pipes, redirects or shell."


def parse(line: str) -> list[str]:
    """Check a command line; returns argv ready to run, or raises ValueError."""
    try:
        argv = shlex.split(line)
    except ValueError as exc:
        raise ValueError(f"cannot read the command: {exc}") from None
    if not argv:
        raise ValueError("type a command, or 'help'")
    name, args = argv[0], argv[1:]
    if name not in COMMANDS:
        raise ValueError(f"{name!r} is not allowed here; type 'help' for the list")
    for arg in args:
        if not ARG_RE.match(arg):
            raise ValueError(f"argument {arg!r} has characters that are not allowed")
    allowed = COMMANDS[name][1]
    words = [a for a in args if not a.startswith("-")]
    if allowed is not None and (args if not allowed else not words or words[0] not in allowed):
        raise ValueError(f"{name} here only takes: {', '.join(sorted(allowed)) or 'nothing'}")
    if name in ("ip", "bridge", "nft", "virsh") and CHANGES & {a.lower() for a in args}:
        raise ValueError(f"{name} may only show things here, not change them")
    if name == "ethtool" and any(a.startswith("-") and a not in ETHTOOL_OK for a in args):
        raise ValueError(f"ethtool here only takes {', '.join(sorted(ETHTOOL_OK))}")
    if name == "ping":
        if "-f" in args or any(a.startswith("-i") for a in args):
            raise ValueError("flood ping and custom intervals are not allowed")
        if "-c" not in args:
            argv[1:1] = ["-c", "4"]
        count = argv[argv.index("-c") + 1]
        if not count.isdigit() or not 1 <= int(count) <= 50:
            raise ValueError("ping -c takes 1 to 50")
    if name == "ss" and {"-K", "--kill"} & set(args):
        raise ValueError("ss may only show sockets here")
    if name == "arping" and {"-U", "-A"} & set(args):
        raise ValueError("arping here only asks; it does not announce addresses")
    if name == "arping" and "-c" not in args:
        argv[1:1] = ["-c", "3"]
    if name == "traceroute":
        argv[0] = "tracepath"
        argv = [a for a in argv if a not in ("-n", "-I")]
        argv[1:1] = ["-n"]
    return argv


def run_line(line: str, run: Callable[[list[str]], tuple[int, str]]) -> tuple[int, str]:
    if line.strip() in ("help", "?"):
        return 0, help_text()
    return run(parse(line))
