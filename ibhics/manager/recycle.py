"""Recycle bin: a deleted VM is kept, stopped and hidden, for a number of days, so a
mistaken delete can be undone. Its disks stay in their pool until it is emptied."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from ..cluster import Cluster, NodeUnavailable
from ..hypervisor import InvalidState, VMInfo, VMState

log = logging.getLogger(__name__)


class RecycleBin:
    def __init__(
        self,
        cluster: Cluster,
        days: int = 0,
        forget: Callable[[str], None] = lambda _uuid: None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._cluster = cluster
        self.days = max(0, days)  # 0: deleting is permanent
        self.forget = forget  # called once a VM is gone for good (groups, ...)
        self._clock = clock

    def delete(self, uuid: str, permanent: bool = False) -> VMInfo | None:
        """Move the VM into the bin, or delete it for good when the bin is off or asked to.
        Returns the recycled VM, or None when it was deleted."""
        self._cluster.require_quorum()
        member, vm = self._cluster.find_vm(uuid)
        if vm.state != VMState.STOPPED:
            raise InvalidState("shut the VM down before deleting it")
        clones = sorted(v.spec.name for _, v in self._cluster.all_vms() if v.spec.linked_to == uuid)
        if clones:
            raise InvalidState(
                f"linked clones are based on this template ({', '.join(clones)}); delete them first"
            )
        if permanent or not self.days or vm.spec.recycled:
            member.driver.delete_vm(uuid)
            self.forget(uuid)
            return None
        return member.driver.set_recycled(uuid, self._now().isoformat(timespec="seconds"))

    def restore(self, uuid: str) -> VMInfo:
        self._cluster.require_quorum()
        member, vm = self._cluster.find_vm(uuid)
        if not vm.spec.recycled:
            raise InvalidState(f"{vm.spec.name} is not in the recycle bin")
        return member.driver.set_recycled(uuid, "")

    def expires(self, vm: VMInfo) -> datetime | None:
        if not vm.spec.recycled or not self.days:
            return None
        return datetime.fromisoformat(vm.spec.recycled) + timedelta(days=self.days)

    def contents(self) -> list[tuple[str, VMInfo]]:
        """(node, VM) for everything in the bin, most recently deleted first."""
        found = [(m.name, vm) for m, vm in self._cluster.all_vms() if vm.spec.recycled]
        return sorted(found, key=lambda x: x[1].spec.recycled, reverse=True)

    def purge_expired(self) -> list[str]:
        """Delete for good what has been in the bin longer than `days`. Returns the names."""
        if not self.days:
            return []
        gone = []
        now = self._now()
        for member, vm in self._cluster.all_vms():
            expires = self.expires(vm)
            if expires is None or expires > now:
                continue
            try:
                member.driver.delete_vm(vm.uuid)
            except (InvalidState, NodeUnavailable) as exc:  # e.g. linked clones still use it
                log.warning("recycle bin: could not delete %s yet: %s", vm.spec.name, exc)
                continue
            self.forget(vm.uuid)
            gone.append(vm.spec.name)
        return gone

    def _now(self) -> datetime:
        return self._clock()
