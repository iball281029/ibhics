"""ibHiCS Migrate on a compute node: convert a source VM and create it here."""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import nullcontext
from pathlib import Path

from ..hypervisor import DiskSpec, HypervisorDriver, NicSpec, VMInfo, VMSpec
from .convert import FakeConverter, V2VConverter
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
        network: str = "default",
        vcpus: int | None = None,
        memory_mb: int | None = None,
        start: bool = False,
        progress=None,
    ) -> VMInfo:
        """Convert first, then create the VM, so nothing half-made exists during the copy.
        The converted images wait on the target pool, not on the system disk."""
        if self._scratch:
            Path(self._scratch).mkdir(parents=True, exist_ok=True)
            scratch = nullcontext(Path(self._scratch))
        else:
            scratch = self._driver.scratch_space(pool, sum(vm.disks_gb))
        report = progress or (lambda step, pct: None)
        self.progress[name] = ("Preparing", 0)

        def track(step: str, pct: int | None) -> None:
            self.progress[name] = (step, pct)
            report(step, pct)

        try:
            with scratch as base:
                created = self._convert_and_create(
                    source, vm, name, pool, network, vcpus, memory_mb, base, track
                )
        finally:
            self.progress.pop(name, None)
        if start:
            self._driver.start_vm(created.uuid)
        return self._driver.get_vm(created.uuid)

    def log(self, name: str, max_bytes: int = 65536) -> str:
        """The end of virt-v2v's output for an import, while it runs and after."""
        log_dir = getattr(self._converter, "log_dir", None)
        path = Path(log_dir) / f"{name}.log" if log_dir else None
        if not path or not path.is_file():
            return ""
        with open(path, "rb") as f:
            f.seek(max(0, path.stat().st_size - max_bytes))
            return f.read().decode(errors="replace")

    def _convert_and_create(
        self, source, vm, name, pool, network, vcpus, memory_mb, base: Path, track
    ) -> VMInfo:
        work = Path(tempfile.mkdtemp(prefix=f"import-{name}-", dir=base))
        try:
            converted = self._converter.convert(source, vm, name, work, track)
            track("Creating the VM", 96)
            spec = VMSpec(
                name=name,
                vcpus=vcpus or vm.vcpus,
                memory_mb=memory_mb or vm.memory_mb,
                firmware=converted.firmware,
                disks=[
                    DiskSpec(size_gb=max(1, -(-image.stat().st_size // GIB)), pool=pool)
                    for image in converted.images
                ],
                nics=[
                    NicSpec(network=network, mac=mac)
                    for mac in _pad(
                        # Two live NICs with one MAC break the LAN, so a VM still running
                        # at the source gets new addresses.
                        [] if vm.state == "running" else converted.macs,
                        max(vm.nics, len(converted.macs), 1),
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
            shutil.rmtree(work, ignore_errors=True)
        return created


def _pad(macs: list[str], count: int) -> list[str | None]:
    """One entry per NIC: the source's MAC where known, otherwise a new one."""
    return [*macs, *[None] * (count - len(macs))][:count]
