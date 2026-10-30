"""ibHiCS Migrate on a compute node: convert a source VM and create it here."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ..hypervisor import DiskSpec, HypervisorDriver, NicSpec, VMInfo, VMSpec
from .convert import FakeConverter, V2VConverter
from .sources import GIB, Source, SourceVM

SCRATCH_DIR = "/var/lib/ibhics/scratch"


class NodeImport:
    def __init__(self, driver: HypervisorDriver, converter=None, scratch_dir: str | None = None):
        from ..hypervisor.fake import FakeDriver

        self._driver = driver
        self._converter = converter or (
            FakeConverter() if isinstance(driver, FakeDriver) else V2VConverter()
        )
        self._scratch = scratch_dir or (
            tempfile.gettempdir() if isinstance(driver, FakeDriver) else SCRATCH_DIR
        )

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
    ) -> VMInfo:
        """Convert first, then create the VM, so nothing half-made exists during the copy."""
        Path(self._scratch).mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"import-{name}-", dir=self._scratch))
        try:
            converted = self._converter.convert(source, vm, name, work)
            spec = VMSpec(
                name=name,
                vcpus=vcpus or vm.vcpus,
                memory_mb=memory_mb or vm.memory_mb,
                firmware=converted.firmware,
                disks=[
                    DiskSpec(size_gb=max(1, -(-image.stat().st_size // GIB)), pool=pool)
                    for image in converted.images
                ],
                nics=[NicSpec(network=network) for _ in range(max(vm.nics, 1))],
                description=f"Imported from {source.name} ({vm.ref})",
                os_type=vm.guest_os[:64],
            )
            created = self._driver.create_vm(spec)
            try:
                for index, image in enumerate(converted.images):
                    self._driver.import_disk(created.uuid, index, image)
            except Exception:
                self._driver.delete_vm(created.uuid)
                raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if start:
            self._driver.start_vm(created.uuid)
        return self._driver.get_vm(created.uuid)
