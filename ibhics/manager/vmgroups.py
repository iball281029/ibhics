"""VM groups: folders that sort the VM list, nested like a directory tree.

Groups are cluster configuration kept by the manager, not by the VMs, so a VM keeps
its group when it moves to another node. A VM is in at most one group; one in none
shows under "Ungrouped"."""

from __future__ import annotations

import json
import os
import threading
import uuid as uuidlib
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_DEPTH = 4


class GroupNotFound(KeyError):
    pass


@dataclass
class Group:
    id: str
    name: str
    parent: str = ""  # "" for a top-level group
    description: str = ""


class VMGroups:
    def __init__(self, state_path: Path | None = None) -> None:
        self._state_path = state_path
        self._groups: dict[str, Group] = {}
        self._members: dict[str, str] = {}  # VM uuid -> group id
        self._lock = threading.RLock()

    def groups(self) -> list[Group]:
        with self._lock:
            return sorted(self._groups.values(), key=lambda g: g.name.lower())

    def members(self) -> dict[str, str]:
        with self._lock:
            return dict(self._members)

    def _get(self, group_id: str) -> Group:
        try:
            return self._groups[group_id]
        except KeyError:
            raise GroupNotFound(group_id) from None

    def _depth(self, group_id: str) -> int:
        depth = 0
        while group_id:
            depth += 1
            group_id = self._groups[group_id].parent
        return depth

    def _check_name(self, name: str, parent: str, own_id: str = "") -> str:
        name = name.strip()
        if not name or len(name) > 64:
            raise ValueError("a group name has 1 to 64 characters")
        if any(
            g.parent == parent and g.name.lower() == name.lower() and g.id != own_id
            for g in self._groups.values()
        ):
            raise ValueError(f"there is already a group named {name!r} here")
        return name

    def create(self, name: str, parent: str = "", description: str = "") -> Group:
        with self._lock:
            if parent:
                self._get(parent)
                if self._depth(parent) >= MAX_DEPTH:
                    raise ValueError(f"groups nest at most {MAX_DEPTH} deep")
            group = Group(
                uuidlib.uuid4().hex[:10], self._check_name(name, parent), parent, description
            )
            self._groups[group.id] = group
            self._save()
            return group

    def update(
        self,
        group_id: str,
        name: str | None = None,
        parent: str | None = None,
        description: str | None = None,
    ) -> Group:
        with self._lock:
            group = self._get(group_id)
            new_parent = group.parent if parent is None else parent
            if new_parent != group.parent:
                walk = new_parent
                while walk:
                    if walk == group_id:
                        raise ValueError("a group cannot go inside itself")
                    walk = self._get(walk).parent
                if new_parent and self._depth(new_parent) + self._height(group_id) > MAX_DEPTH:
                    raise ValueError(f"groups nest at most {MAX_DEPTH} deep")
            group.name = self._check_name(
                group.name if name is None else name, new_parent, group_id
            )
            group.parent = new_parent
            if description is not None:
                group.description = description
            self._save()
            return group

    def _height(self, group_id: str) -> int:
        kids = [g.id for g in self._groups.values() if g.parent == group_id]
        return 1 + max((self._height(k) for k in kids), default=0)

    def delete(self, group_id: str) -> None:
        """The group's VMs and subgroups move up to its parent; no VM is touched."""
        with self._lock:
            group = self._get(group_id)
            for g in self._groups.values():
                if g.parent == group_id:
                    g.parent = group.parent
                    g.name = self._unique(g.name, group.parent, g.id)
            for vm, gid in list(self._members.items()):
                if gid == group_id:
                    if group.parent:
                        self._members[vm] = group.parent
                    else:
                        del self._members[vm]
            del self._groups[group_id]
            self._save()

    def _unique(self, name: str, parent: str, own_id: str) -> str:
        taken = {
            g.name.lower() for g in self._groups.values() if g.parent == parent and g.id != own_id
        }
        candidate, n = name, 2
        while candidate.lower() in taken:
            candidate, n = f"{name} ({n})", n + 1
        return candidate

    def assign(self, vms: list[str], group_id: str) -> None:
        """Put the VMs in the group, or take them out of any with group_id ""."""
        with self._lock:
            if group_id:
                self._get(group_id)
            for vm in vms:
                if group_id:
                    self._members[vm] = group_id
                else:
                    self._members.pop(vm, None)
            self._save()

    def forget(self, vm: str) -> None:
        with self._lock:
            if self._members.pop(vm, None) is not None:
                self._save()

    # Persistence

    def load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        data = json.loads(self._state_path.read_text())
        with self._lock:
            self._groups = {g["id"]: Group(**g) for g in data.get("groups", [])}
            self._members = {
                vm: gid for vm, gid in data.get("members", {}).items() if gid in self._groups
            }

    def _save(self) -> None:
        if not self._state_path:
            return
        data = {"groups": [asdict(g) for g in self._groups.values()], "members": self._members}
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self._state_path)
