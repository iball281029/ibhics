"""ibHiCS Migrate on a compute node: convert a source VM and create it here."""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import nullcontext
from pathlib import Path

from ..hypervisor import DiskSpec, HypervisorDriver, NicSpec, PoolNotFound, VMInfo, VMSpec
from .convert import FakeConverter, V2VConverter
from .resize import ResizeError, resize_image
from .sources import GIB, Source, SourceVM

log = logging.getLogger(__name__)


class NodeImport:
    def __init__(self, driver: HypervisorDriver, converter=None, scratch_dir: str | None = None):
        from ..hypervisor.fake import FakeDriver

        self._driver = driver
        self._converter = converter or (
            FakeConverter() if isinstance(driver, FakeDriver) else V2VConverter()
        )
        self._scratch = scratch_dir  # None: on the target pool (driver.scratch_space)
        self.progress: dict[str, tuple[str, int | None]] = {}  # name -> (step, percent)

    def run(
        self,
        source: Source,
        vm: SourceVM,
        name: str,
        pool: str = "default",
        network: str | list[str] = "default",
        vcpus: int | None = None,
        memory_mb: int | None = None,
        start: bool = False,
        progress=None,
        clear_hibernation: bool = False,
        disks: list[dict] | None = None,
    ) -> VMInfo:
        """Convert first, then create the VM, so nothing half-made exists during the copy.
        The converted images wait on the target pool, not on the system disk."""
        if disks is not None:
            if len(disks) != len(vm.disks_gb):
                raise ValueError("disks must have one entry per source disk")
            available = {p.name for p in self._driver.list_pools() if p.active}
            for d in disks:
                if (d.get("pool") or pool) not in available:
                    raise PoolNotFound(d.get("pool") or pool)
        if self._scratch:
            Path(self._scratch).mkdir(parents=True, exist_ok=True)
            scratch = nullcontext(Path(self._scratch))
        else:
            scratch_gb = sum(vm.disks_gb)
            if disks is not None:
                scratch_gb = sum(
                    max(d.get("size_gb") or g, g) for d, g in zip(disks, vm.disks_gb, strict=True)
                )
                if any(
                    d.get("size_gb") and d["size_gb"] < g
                    for d, g in zip(disks, vm.disks_gb, strict=True)
                ):
                    scratch_gb *= 3
            scratch = self._driver.scratch_space(pool, scratch_gb)
        report = progress or (lambda step, pct: None)
        self.progress[name] = ("Preparing", 0)

        def track(step: str, pct: int | None) -> None:
            self.progress[name] = (step, pct)
            report(step, pct)

        try:
            with scratch as base:
                created = self._convert_and_create(
                    source,
                    vm,
                    name,
                    pool,
                    network,
                    vcpus,
                    memory_mb,
                    base,
                    track,
                    clear_hibernation,
                    disks,
                )
        finally:
            self.progress.pop(name, None)
        if start:
            self._driver.start_vm(created.uuid)
        return self._driver.get_vm(created.uuid)

    def cancel(self, name: str) -> bool:
        """Stop a running conversion; the import then fails as cancelled."""
        cancel = getattr(self._converter, "cancel", None)
        return bool(cancel and cancel(name))

    def log(self, name: str, max_bytes: int | None = None) -> str:
        """virt-v2v's output for an import, while it runs and after."""
        log_dir = getattr(self._converter, "log_dir", None)
        path = Path(log_dir) / f"{name}.log" if log_dir else None
        if not path or not path.is_file():
            return ""
        with open(path, "rb") as f:
            if max_bytes is not None:
                f.seek(max(0, path.stat().st_size - max_bytes))
            return f.read().decode(errors="replace")

    def _convert_and_create(
        self,
        source,
        vm,
        name,
        pool,
        network,
        vcpus,
        memory_mb,
        base: Path,
        track,
        clear_hibernation: bool = False,
        disks: list[dict] | None = None,
    ) -> VMInfo:
        work = Path(tempfile.mkdtemp(prefix=f"import-{name}-", dir=base))
        try:
            extra = {"clear_hibernation": True} if clear_hibernation else {}
            converted = self._converter.convert(source, vm, name, work, track, **extra)
            if disks is not None:
                if len(disks) != len(converted.images):
                    raise ValueError("converted disk count differs from disk targets")
                for image, target in zip(converted.images, disks, strict=True):
                    if target.get("size_gb") is not None:
                        track("Resizing converted disks", 94)
                        try:
                            resize_image(image, target["size_gb"])
                        except ResizeError:
                            raise
                        except Exception:
                            raise ResizeError("Unexpected disk resize failure") from None
            track("Creating the VM", 96)
            spec = VMSpec(
                name=name,
                vcpus=vcpus or vm.vcpus,
                memory_mb=memory_mb or vm.memory_mb,
                firmware=converted.firmware,
                disks=[
                    DiskSpec(
                        size_gb=(disks[i].get("size_gb") if disks else None)
                        or max(1, -(-image.stat().st_size // GIB)),
                        pool=(disks[i].get("pool") or pool) if disks else pool,
                    )
                    for i, image in enumerate(converted.images)
                ],
                nics=[
                    NicSpec(network=_network_for(network, i), mac=mac)
                    for i, mac in enumerate(
                        _pad(
                            # Two live NICs with one MAC break the LAN, so a VM still running
                            # at the source gets new addresses.
                            [] if vm.state == "running" else converted.macs,
                            max(vm.nics, len(converted.macs), 1),
                        )
                    )
                ],
                description=f"Imported from {source.name} ({vm.ref})",
                os_type=vm.guest_os[:64],
            )
            created = self._driver.create_vm(spec)
            try:
                keep = getattr(self._converter, "keep_nic_names", None)
                if keep and "windows" not in vm.guest_os.lower():
                    track("Keeping the guest's network names", 97)
                    note = keep(converted.images, [nic.mac for nic in created.spec.nics])
                    log.info("import %s: %s", name, note)
                for index, image in enumerate(converted.images):
                    self._driver.import_disk(created.uuid, index, image)
            except Exception:
                self._driver.delete_vm(created.uuid)
                raise
        finally:
            # The source VM is untouched, so a failed resize can simply be retried.
            shutil.rmtree(work, ignore_errors=True)
        return created


def _pad(macs: list[str], count: int) -> list[str | None]:
    """One entry per NIC: the source's MAC where known, otherwise a new one."""
    return [*macs, *[None] * (count - len(macs))][:count]


def _network_for(network: str | list[str], index: int) -> str:
    """The network for NIC index: one for all, or one per source NIC (the last repeats)."""
    if isinstance(network, str):
        return network
    return (network[index] if index < len(network) else network[-1]) if network else "default"
