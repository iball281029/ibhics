"""Real libvirt/QEMU smoke test, using only a new disposable paused domain.

Run with the ibHiCS virtualenv Python on Linux. No installed application files or
production domains are modified. This tests block consistency, not guest VSS.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import sys
import traceback
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape

MIB = 1024 * 1024
SIZE = 64 * MIB


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def qemu_io_targets(blocks):
    """Select only the sole inserted test disk, including modern unnamed backends."""
    disks = [block for block in blocks if block.get("inserted")]
    need(len(disks) == 1, "Disposable test must have exactly one block device")
    disk = disks[0]
    targets = []
    device = disk.get("device")
    if isinstance(device, str) and re.fullmatch(r"[a-zA-Z0-9_.-]+", device):
        targets.append(device)
    qdev = disk.get("qdev")
    if (
        isinstance(qdev, str)
        and qdev.startswith("/machine/")
        and re.fullmatch(r"/[a-zA-Z0-9_./-]+", qdev)
    ):
        targets.append("-d " + qdev)
    node = disk["inserted"].get("node-name")
    if isinstance(node, str) and re.fullmatch(r"[a-zA-Z0-9_.-]+", node):
        targets.append(node)
    need(targets, "No supported test disk identifier in QEMU query-block")
    return targets


def write_qemu_pattern(qmp, targets, index, value, size):
    """HMP may return no text; the subsequent restore verifies the actual bytes."""
    failures = []
    for target in targets:
        command = f'qemu-io {target} "write -P {value} {index * size} {size}"'
        text = qmp("human-monitor-command", {"command-line": command})
        if text.strip() and not re.search(rf"wrote\s+{size}/{size}", text):
            failures.append(target + ": " + text.strip())
            continue
        flushed = qmp("human-monitor-command", {"command-line": f'qemu-io {target} "flush"'})
        need(not flushed.strip(), "Test QEMU flush failed: " + flushed)
        return target
    raise RuntimeError("Test QEMU write failed: " + "; ".join(failures))


def qemu_test_directory(path):
    """Only a newly created test directory is made traversable by QEMU."""
    path.mkdir(mode=0o755)
    path.chmod(0o755)  # mkdir's mode is otherwise reduced by the installer's umask.
    return path


def main():
    need(sys.platform == "linux" and os.geteuid() == 0, "Run as root on a Linux libvirt node")
    missing = [
        name
        for name in ("libvirt", "libvirt_qemu", "nbd", "zstandard")
        if importlib.util.find_spec(name) is None
    ]
    need(
        not missing,
        "Missing Python modules: "
        + ", ".join(missing)
        + "; install python3-libvirt python3-libnbd python3-zstandard for this interpreter",
    )
    need(shutil.which("qemu-img"), "qemu-img is required")
    import libvirt
    import libvirt_qemu

    from ibhics.backup import DirStore, Repository
    from ibhics.backup.compression import codec
    from ibhics.hypervisor.backup_cbt import capture
    from ibhics.hypervisor.libvirt_driver import LibvirtDriver

    run_id = uuid.uuid4().hex
    name = "ibhics-backup-smoke-" + run_id[:12]
    domain_uuid = str(uuid.UUID(run_id))
    evidence = Path(os.environ.get("IBHICS_BACKUP_EVIDENCE", "/var/tmp/ibhics-backup-tests")) / name
    evidence.mkdir(parents=True, mode=0o700)
    os.chmod(evidence, 0o700)
    workspace = Path("/var/lib/ibhics/pools") / (".backup-smoke-" + run_id)
    qemu_test_directory(workspace)
    conn = pool = domain = volume = None
    report = {
        "status": "running",
        "name": name,
        "domain_uuid": domain_uuid,
        "started": datetime.now(UTC).isoformat(),
        "checks": [],
        "operations": [],
    }
    error = None
    cleanup = []

    def check(label, condition=True):
        need(condition, label)
        report["checks"].append(label)
        print("PASS:", label, flush=True)

    def timeout(signum, frame):
        raise TimeoutError("Disposable backup smoke test exceeded 180 seconds")

    old_alarm = signal.signal(signal.SIGALRM, timeout)
    signal.alarm(180)
    try:
        conn = libvirt.open("qemu:///system")
        need(conn is not None, "Cannot connect to system libvirt")
        need(conn.getFreeMemory() >= 256 * MIB, "Need at least 256 MiB free host memory")
        need(shutil.disk_usage(workspace).free >= 512 * MIB, "Need 512 MiB free test disk space")
        pool_xml = (
            f"<pool type='dir'><name>{name}</name><target>"
            f"<path>{escape(str(workspace))}</path></target></pool>"
        )
        pool = conn.storagePoolCreateXML(pool_xml, 0)
        volume = pool.createXML(
            "<volume><name>test.qcow2</name><capacity unit='bytes'>"
            + str(SIZE)
            + "</capacity><target><format type='qcow2'/></target></volume>",
            0,
        )
        xml = f"""<domain type='qemu'><name>{name}</name><uuid>{domain_uuid}</uuid>
<memory unit='MiB'>128</memory><vcpu>1</vcpu>
<os><type arch='x86_64' machine='pc'>hvm</type><boot dev='hd'/></os>
<devices><disk type='file' device='disk'><driver name='qemu' type='qcow2'/>
<source file='{escape(volume.path())}'/><target dev='vda' bus='virtio'/></disk>
<memballoon model='none'/></devices></domain>"""
        domain = conn.defineXML(xml)
        domain.createWithFlags(
            libvirt.VIR_DOMAIN_START_PAUSED | libvirt.VIR_DOMAIN_START_AUTODESTROY
        )
        check(
            "Disposable domain is paused and has no network",
            domain.info()[0] == libvirt.VIR_DOMAIN_PAUSED
            and not ET.fromstring(domain.XMLDesc(0)).findall("./devices/interface"),
        )

        def qmp(command, arguments=None):
            need(
                domain.UUIDString() == domain_uuid and domain.name() == name,
                "Test domain identity changed",
            )
            command = {"execute": command}
            if arguments is not None:
                command["arguments"] = arguments
            result = json.loads(libvirt_qemu.qemuMonitorCommand(domain, json.dumps(command), 0))
            need("error" not in result, "Test QMP failed: " + str(result.get("error")))
            return result["return"]

        blocks = qmp("query-block")
        report["qemu_test_blocks"] = blocks
        targets = qemu_io_targets(blocks)
        expected = bytearray(SIZE)

        def write(index, value):
            report["qemu_io_target"] = write_qemu_pattern(qmp, targets, index, value, MIB)
            expected[index * MIB : (index + 1) * MIB] = bytes([value]) * MIB

        for index, value in ((0, 0x41), (1, 0x42), (2, 0x43), (4, 0x41)):
            write(index, value)
        driver = object.__new__(LibvirtDriver)
        driver._conn = conn
        driver.scratch_dir = str(workspace / "scratch")
        qemu_test_directory(Path(driver.scratch_dir))
        vm = SimpleNamespace(uuid=domain_uuid, spec=SimpleNamespace(name=name))
        repo = Repository(DirStore(evidence / "primary"), MIB, compression="maximum")

        def backup(parent=None):
            with capture(driver, domain_uuid, parent) as (readers, point):
                result = repo.backup_readers(vm, readers, {"test_only": True}, point, parent)
            report["operations"].append(asdict(result))
            return result

        raw = evidence / "restored.raw"

        def verify(repository, result):
            repository.restore_disk(repository.manifest(result.id), 0, raw)
            got = hashlib.sha256(raw.read_bytes()).hexdigest()
            want = hashlib.sha256(expected).hexdigest()
            need(
                raw.stat().st_size == SIZE and got == want,
                "Restored disk does not match expected bytes",
            )
            report["expected_sha256"] = want
            report["restored_sha256"] = got

        first = backup()
        check(
            "Real QEMU full capture establishes a checkpoint",
            first.kind == "full"
            and first.read_bytes == SIZE
            and bool(repo.manifest(first.id).get("checkpoint")),
        )
        verify(repo, first)
        check("Full restore verifies the initial QEMU writes byte-for-byte")
        write(1, 0)  # A dirty range becoming all-zero must overwrite the old block map.
        write(3, 0x44)
        inc = backup(repo.manifest(first.id))
        check(
            "Real dirty bitmap reads less than a full disk",
            inc.kind == "incremental" and 2 * MIB <= inc.read_bytes < SIZE,
        )
        verify(repo, inc)
        check("Incremental restores byte-for-byte, including changed-to-zero blocks")
        empty_inc = backup(repo.manifest(inc.id))
        check(
            "Unchanged disk reads no VM blocks",
            empty_inc.kind == "incremental" and empty_inc.read_bytes == 0,
        )
        full = repo.synthetic_full(empty_inc.id)
        verify(repo, full)
        check("Synthetic full restores correct bytes without reading the VM", full.read_bytes == 0)
        for point in list(repo.list()):
            if point.id != full.id:
                repo.delete(point.id)
        repo.gc()
        verify(repo, full)
        check(
            "Synthetic full survives removal of original shared data",
            not repo.store.list("chunks/"),
        )
        copy = Repository(DirStore(evidence / "copy"), MIB, compression="balanced")
        archive = Repository(DirStore(evidence / "archive"), MIB, compression="maximum")
        copied = repo.transfer(full.id, copy)
        archived = repo.transfer(full.id, archive, "archive", independent=True)
        repo.delete(full.id)
        repo.gc()
        verify(copy, copied)
        verify(archive, archived)
        check("Copy and archive restore after total primary data removal")
        check("Maximum profile uses installed Zstandard", codec("maximum") == "zstd-19")
        check(
            "Deduplication records repeated nonzero input",
            inc.dedup_saved_bytes >= 0 and first.dedup_saved_bytes >= MIB,
        )
        for profile in ("fast", "balanced", "maximum"):
            r = Repository(
                DirStore(evidence / ("compression-" + profile)), MIB, compression=profile
            )
            result = r.backup(vm, [raw], {"test_only": True})
            verify(r, result)
            report["operations"].append(asdict(result))
            check(
                "Compression and dedup restore correctly: " + profile,
                result.stored_bytes > 0 and result.dedup_saved_bytes >= MIB,
            )
        report["status"] = "passed"
    except BaseException as exc:
        error = exc
        report.update(status="failed", error=str(exc), traceback=traceback.format_exc())
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_alarm)
        # Only handles created in this run are eligible for cleanup. No inventory sweeps.
        if domain is not None:
            try:
                need(
                    domain.UUIDString() == domain_uuid and domain.name() == name,
                    "Refuse unknown domain cleanup",
                )
                if domain.isActive():
                    domain.destroy()
                domain.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA)
            except Exception as exc:
                cleanup.append("domain: " + str(exc))
        if not cleanup and volume is not None:
            try:
                volume.delete(0)
            except Exception as exc:
                cleanup.append("test volume: " + str(exc))
        if not cleanup and pool is not None:
            try:
                if pool.isActive():
                    pool.destroy()
            except Exception as exc:
                cleanup.append("test pool: " + str(exc))
        if conn is not None:
            conn.close()
        if not cleanup:
            shutil.rmtree(workspace)
        report["cleanup_errors"] = cleanup
        if cleanup:
            report["status"] = "failed"
        report["finished"] = datetime.now(UTC).isoformat()
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        print("REPORT:", evidence / "report.json", flush=True)
    if error is not None:
        raise error
    need(not cleanup, "Cleanup incomplete; review exact test handles in report.json")
    print(
        "BACKUP_LINUX_SMOKE_OK: real full/incremental/synthetic/copy/archive restores verified",
        flush=True,
    )


if __name__ == "__main__":
    main()
