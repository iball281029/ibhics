"""Node-local pool storage and conversion on independent replica copies."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..hypervisor import DiskSpec, InvalidState, NicSpec, VMSpec
from ..hypervisor.fake import FakeDriver
from ..importer.convert import V2VConverter
from ..importer.resize import resize_image
from ..importer.sources import SourceType
from .bitmap import KVMPullProvider
from .cbt import ESXiCBTProvider
from .nutanix import NutanixSnapshotProvider
from .store import ReplicaStore, durable_json, operation_lock, replica_path, replica_size


class ReplicaConverter(V2VConverter):
    """Use the existing virt-v2v conversion path with a multi-disk libvirt XML input."""

    def __init__(self, images: list[Path], **kwargs):
        super().__init__(**kwargs)
        self.images = images

    def _pull_kvm_disks(self, source, vm, out_dir, progress):
        return [(str(path), "raw") for path in self.images]


class LocalRuntime:
    def __init__(self, cluster, root: Path):
        self.cluster, self.root = cluster, Path(root)

    def store(self, job) -> ReplicaStore:
        member = self.cluster.member(job.node)
        if not member.local:
            raise ValueError("Remote replication execution is not yet supported on this node")
        roots = []
        for disk in job.destination["disks"]:
            base = member.driver.pool_path(disk.get("pool") or job.destination["pool"])
            if not base:
                raise ValueError("Replication requires a mounted directory/file pool")
            if not isinstance(member.driver, FakeDriver):
                directory = Path(base).resolve()
                if not directory.is_dir() or directory.stat().st_dev == Path("/").stat().st_dev:
                    raise InvalidState(
                        "Replication destination storage is unavailable or on the root filesystem"
                    )
                replica_root = directory / ".ibhics-replicas"
                if replica_root.is_symlink():
                    raise InvalidState("Replication storage directory must not be a symlink")
            roots.append(Path(base) / ".ibhics-replicas" / job.id / str(len(roots)))
        return ReplicaStore(roots[0].parent / "metadata", roots)

    def sync(self, job, source, vm, progress):
        provider = {
            SourceType.VMWARE: ESXiCBTProvider,
            SourceType.KVM: KVMPullProvider,
            SourceType.NUTANIX: NutanixSnapshotProvider,
        }.get(source.type)
        if provider is None:
            raise ValueError("No replication provider for this source")
        store = self.store(job)
        if source.type == SourceType.VMWARE:
            from .cow import CowReplicaStore

            store = CowReplicaStore(store.root, store.disk_roots, store.chunk)
        return store.sync(provider(source, vm), progress)

    def ownership(self, job):
        path = self.store(job).root / "ownership.json"
        owned = json.loads(path.read_text()) if path.exists() else {}
        # Close the VM-create/ownership-write crash window using our own description.
        for vm in self.cluster.member(job.node).driver.list_vms():
            if vm.spec.description == f"Replication test {job.id}":
                owned["test_vm"] = vm.uuid
            elif vm.spec.description == f"Replication cutover {job.id}":
                owned["result"] = vm.uuid
        return owned

    def _own(self, job):
        durable_json(
            self.store(job).root / "ownership.json",
            {
                "test_vm": job.test_vm,
                "result": job.result,
            },
        )

    def adopt_manifest(self, job, manifest):
        import re

        if not re.fullmatch(r"[0-9a-f]{32}", manifest.get("generation", "")):
            raise ValueError("Invalid replica generation")
        store = self.store(job)
        if len(manifest["disks"]) != len(store.disk_roots):
            raise ValueError("Replica disk count differs from target")
        for disk, parent in zip(manifest["disks"], store.disk_roots, strict=True):
            path = replica_path(parent, manifest, disk)
            if not path.is_file() or replica_size(path, disk) != disk["size"]:
                raise ValueError("Replica generation is not accessible on target pool")
            disk["path"] = str(path)
        with operation_lock(store.disk_roots[0].parent / "replication.lock"):
            current = store.current()
            if current:
                if current["generation"] != manifest["generation"]:
                    raise ValueError("Refusing a stale replica generation")
                return
            durable_json(store.root / "current.json", manifest)

    def materialize(self, job, source, vm, *, test: bool, networks: list[str] | None):
        store = self.store(job)
        with operation_lock(store.disk_roots[0].parent / "replication.lock"):
            return self._materialize(job, source, vm, test=test, networks=networks)

    def _materialize(self, job, source, vm, *, test, networks):
        from dataclasses import replace

        store = self.store(job)
        ownership = self.ownership(job)
        if ownership.get("test_vm" if test else "result"):
            raise ValueError("A VM from an earlier operation exists; inspect it before retrying")
        folder = store.disk_roots[0].parent / ("test" if test else "cutover")
        if folder.exists():
            raise ValueError("Previous conversion workspace exists; inspect before retrying")
        images = store._clone(folder)
        member = self.cluster.member(job.node)
        created = None
        try:
            # This source descriptor is local and contains no original credentials.
            local_source = replace(source, type="kvm", password=None, host=None)
            converter = ReplicaConverter(images)
            converted = converter.convert(
                local_source,
                vm,
                job.name,
                folder / "converted",
                clear_hibernation=job.destination.get("clear_hibernation", False),
            )
            targets = job.destination["disks"]
            for image, target in zip(converted.images, targets, strict=True):
                if target.get("size_gb"):
                    resize_image(image, target["size_gb"])
            network = networks if test else job.destination["network"]
            if isinstance(network, str):
                network = [network] * vm.nics
            if network and len(network) != vm.nics:
                raise ValueError(
                    "Choose one destination network per NIC, or none for isolated test"
                )
            spec = VMSpec(
                name=f"{job.name[:48]}-test-{job.id[:6]}" if test else job.name,
                vcpus=job.destination.get("vcpus") or vm.vcpus,
                memory_mb=job.destination.get("memory_mb") or vm.memory_mb,
                firmware=converted.firmware,
                disks=[
                    DiskSpec(
                        size_gb=target.get("size_gb")
                        or max(1, -(-image.stat().st_size // 1024**3)),
                        pool=target.get("pool") or job.destination["pool"],
                    )
                    for image, target in zip(converted.images, targets, strict=True)
                ],
                nics=[
                    # A test VM runs next to the live source, so it always gets new MACs.
                    NicSpec(network=name, mac=None if test else _target_mac(job, converted, i))
                    for i, name in enumerate(network or [])
                ],
                cpu_sockets=job.destination.get("cpu_sockets"),
                cpu_cores=job.destination.get("cpu_cores"),
                description=f"Replication {'test' if test else 'cutover'} {job.id}",
            )
            created = member.driver.create_vm(spec)
            # Persist ownership before disk import or boot. A later failure retains the
            # test id so cleanup can recover it instead of losing a VM in the cluster.
            if test:
                job.test_vm = created.uuid
            else:
                job.result = created.uuid
            self._own(job)
            for i, image in enumerate(converted.images):
                member.driver.import_disk(created.uuid, i, image)
            if test or job.destination.get("start"):
                member.driver.start_vm(created.uuid)
            return created.uuid
        finally:
            shutil.rmtree(folder)

    def cleanup_test(self, job):
        store = self.store(job)
        with operation_lock(store.disk_roots[0].parent / "replication.lock"):
            self._cleanup_test(job)

    def _cleanup_test(self, job):
        job.test_vm = job.test_vm or self.ownership(job).get("test_vm")
        if job.test_vm:
            driver = self.cluster.member(job.node).driver
            from ..hypervisor import VMNotFound, VMState

            try:
                vm = driver.get_vm(job.test_vm)
                if vm.spec.description != f"Replication test {job.id}":
                    raise ValueError("Refusing to remove a VM not owned by this replication test")
                if vm.state == VMState.RUNNING:
                    driver.stop_vm(job.test_vm, force=True)
                driver.delete_vm(job.test_vm)
            except VMNotFound:
                pass
            job.test_vm = None
            self._own(job)

    def delete(self, job):
        store = self.store(job)
        with operation_lock(store.disk_roots[0].parent / "replication.lock"):
            if self.ownership(job).get("test_vm"):
                raise ValueError("Clean up the test VM before deleting the replica")
            for parent in store.disk_roots:
                if parent.exists():
                    shutil.rmtree(parent)
            if store.root.exists():
                shutil.rmtree(store.root)


class ClusterRuntime(LocalRuntime):
    """Execute sync on the reachable node, convert on the chosen destination node.

    Relays require shared pools, checked by ImportManager._relay. Manifests transferred
    between nodes remap relative generation paths to the target's own pool mounts.
    """

    def _call(self, member, action, job, **body):
        from dataclasses import asdict

        from ..hypervisor import InvalidState

        try:
            response = member.client.request(
                "POST",
                f"/replications/{job.id}/{action}",
                json={"job": asdict(job), **body},
                timeout=None,
            )
        except Exception:
            raise InvalidState("Replication agent request failed; inspect node operation") from None
        if response.status_code == 404:
            raise ValueError("This compute agent does not support replication; upgrade it first")
        if response.status_code == 409:
            from .cow import CBTRequired, ReplicaMaintenanceRequired

            detail = response.json().get("detail")
            if detail == "SOURCE_SCRATCH_UNAVAILABLE":
                from .ssh import SourceScratchUnavailable

                raise SourceScratchUnavailable(
                    "Replication source storage is unavailable, on root, or lacks free space. "
                    "Check IBHICS_REPLICATION_SOURCE_SCRATCH on the execution node."
                )
            if detail == "CBT_REQUIRED":
                raise CBTRequired
            if detail == "REPLICA_MAINTENANCE_REQUIRED":
                raise ReplicaMaintenanceRequired
        if response.status_code != 200:
            raise InvalidState("Replication agent operation failed; inspect the execution node")
        return response.json()

    def store(self, job):
        member = self.cluster.member(job.node)
        if not member.local:
            self._call(member, "validate", job)
            return None
        from dataclasses import replace

        return super().store(replace(job, node=member.name))

    def sync(self, job, source, vm, progress):
        from dataclasses import replace

        member = self.cluster.member(job.via or job.node)
        if member.local:
            local = replace(job, node=member.name)
            return super().sync(local, source, vm, progress)
        return self._remote_sync(member, job, source, progress)

    def _remote_sync(self, member, job, source, progress, every=3.0):
        """Run the sync on the node and relay its progress; a stop raised by the
        progress callback (operator Pause) is passed on to the node."""
        import threading

        outcome = {}

        def run():
            try:
                outcome["result"] = self._call(member, "sync", job, source=source.to_dict())
            except Exception as exc:  # re-raised below in the caller's thread
                outcome["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        stopped = None
        while worker.is_alive():
            worker.join(every)
            if not worker.is_alive() or stopped:
                continue
            try:
                state = self._call(member, "progress", job)
                progress(state.get("done", 0), state.get("total", 0))
            except Exception as exc:
                if type(exc).__name__ == "SyncStopped":
                    stopped = exc
                    try:
                        self._call(member, "stop", job)
                    except Exception:
                        pass
        if stopped:
            raise stopped
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    def ownership(self, job):
        member = self.cluster.member(job.node)
        if member.local:
            return super().ownership(job)
        return self._call(member, "ownership", job)

    def _manifest(self, job):
        member = self.cluster.member(job.via or job.node)
        if member.local:
            from dataclasses import replace

            return LocalRuntime.store(self, replace(job, node=member.name)).current()
        return self._call(member, "manifest", job)

    def materialize(self, job, source, vm, *, test, networks):
        target = self.cluster.member(job.node)
        manifest = self._manifest(job) if job.via else None
        if target.local:
            if manifest:
                # LocalRuntime.store deliberately uses the destination, not via.
                LocalRuntime.adopt_manifest(self, job, manifest)
            return super().materialize(job, source, vm, test=test, networks=networks)
        try:
            result = self._call(
                target,
                "materialize",
                job,
                test=test,
                networks=networks,
                manifest=manifest,
                source_powered_off=not test,
            )
            return result["uuid"]
        finally:
            # Recover ownership even if boot/import failed, avoiding orphaned test VMs.
            ownership = self._call(target, "ownership", job)
            job.test_vm = ownership.get("test_vm") or job.test_vm
            job.result = ownership.get("result") or job.result

    def cleanup_test(self, job):
        target = self.cluster.member(job.node)
        if target.local:
            super().cleanup_test(job)
        else:
            self._call(target, "test-cleanup", job)

    def delete(self, job):
        member = self.cluster.member(job.via or job.node)
        if member.local:
            from dataclasses import replace

            super().delete(replace(job, node=member.name))
        else:
            self._call(member, "delete", job)
        # The replica files live on via's shared pools. The destination manifest is
        # metadata only and can be safely superseded if a new job is ever created.


def _target_mac(job, converted, index: int) -> str | None:
    """The MAC chosen in the wizard for one NIC: keep, custom, or None for a new one."""
    nics = job.destination.get("nics") or []
    if index >= len(nics):
        return None
    nic = nics[index]
    if nic.get("mac_mode") == "custom":
        return nic.get("mac")
    if nic.get("mac_mode") == "keep" and index < len(converted.macs):
        return converted.macs[index]
    return None
