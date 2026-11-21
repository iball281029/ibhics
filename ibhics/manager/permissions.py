"""Central category policy for every authenticated HTTP route and VM console."""

CATEGORIES = {
    "vms": "VMs",
    "storage": "Storage",
    "network": "Network / Firewall",
    "backup": "Backup",
    "replication": "Replication / Migrate",
    "cluster": "Cluster",
    "updates": "Updates",
    "users": "Users",
    "subscription": "Subscription / Support",
}
BUILTIN = {
    "admin": set(CATEGORIES),
    "operator": {"vms", "network", "backup", "replication"},
    "viewer": set(),
}


def category(route: str) -> str:
    path = route.removeprefix("/api/v1/")
    if path.startswith("settings/subscription") or path.startswith("settings/support"):
        return "subscription"
    if path.startswith("settings/updates"):
        return "updates"
    if path.startswith(("users", "roles", "audit", "security", "approvals")):
        return "users"
    if path.startswith(("backup",)) or "/backup" in path or "/restore" in path:
        return "backup"
    if path.startswith(("migrate", "migrations", "deploys", "p2v")) or "/migrate" in path:
        return "replication"
    if path == "ha" or path.endswith("/ha"):
        return "cluster"
    if path.startswith(("edge", "networks")):
        return "network"
    if path.startswith(("pools", "virtual-storage", "isos")) or "/pools" in path:
        return "storage"
    if path.startswith(("vms", "containers", "vm-groups", "recycle-bin", "snapshots")):
        return "vms"
    return "cluster"  # unknown mutations require cluster privileges, never operator by default
