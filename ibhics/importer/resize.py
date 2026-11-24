"""Offline raw image resize. Only disposable working copies are modified by guestfish."""

from __future__ import annotations

import csv
import io
import math
import re
import subprocess
from pathlib import Path

from .convert import V2VConverter
from .sources import GIB


class ResizeError(ValueError):
    pass


def minimum_gb(used: int) -> int:
    return math.ceil((used + max(math.ceil(used / 10), GIB)) / GIB)


def resize_image(image: Path, size_gb: int, runner=None) -> None:
    """Grow capacity only; shrink supported filesystems on a copy before virt-resize.

    LVM is deliberately limited to a single PV and LV on this disk. PV extents that
    cannot fit are rejected by pvresize, never forcibly truncated or discarded.
    """
    runner = runner or subprocess.run
    environment = V2VConverter._guestfs_env(image.parent)

    def run(args, *, script=None):
        try:
            result = runner(
                args, input=script, text=True, capture_output=True, check=True, env=environment
            )
        except FileNotFoundError as exc:
            raise ResizeError(
                f"Disk resize requires {exc.filename}; install libguestfs tools on this node"
            ) from None
        except subprocess.CalledProcessError:
            # Do not copy command output into job logs (guest labels may contain secrets).
            raise ResizeError(
                f"{args[0]} could not safely resize this disk; original retained"
            ) from None
        return result.stdout

    target = size_gb * GIB
    current = image.stat().st_size  # converters emit raw images, not qcow2
    if target == current:
        return
    if target > current:
        run(["qemu-img", "resize", "-f", "raw", str(image), str(target)])
        return

    try:
        listing = run(
            [
                "virt-filesystems",
                "--format=raw",
                "-a",
                str(image),
                "--long",
                "--all",
                "--blocksize",
                "--csv",
            ]
        )
        rows = list(csv.DictReader(io.StringIO(listing)))
        filesystems = [r for r in rows if r["Type"] == "filesystem"]
        supported = {"ntfs", "ext2", "ext3", "ext4"}
        # EFI and swap may be copied at their original size; never shrink them.
        for r in filesystems:
            if r["VFS"].lower() not in supported | {"vfat", "swap"}:
                raise ResizeError(
                    f"Cannot shrink {r['VFS']} filesystem; "
                    "XFS/btrfs/ReFS, unknown filesystems and BitLocker are unsupported. "
                    "Disable BitLocker first"
                )
        parts = {r["Name"]: r for r in rows if r["Type"] == "partition"}
        pvs = [r for r in rows if r["Type"] == "pv"]
        lvs = [r for r in rows if r["Type"] == "lv"]
        candidates = [r for r in filesystems if r["VFS"].lower() in supported]
        if not candidates:
            raise ResizeError("No supported filesystem found; disable BitLocker before shrinking")
        fs = max(candidates, key=lambda r: int(r["Size"]))
        device = fs["Name"]
        is_lv = any(r["Name"] == device for r in lvs)
        if is_lv:
            if len(pvs) != 1 or len(lvs) != 1 or pvs[0]["Name"] not in parts:
                raise ResizeError("LVM shrink requires one PV and one LV on a single disk")
            partition = pvs[0]["Name"]
        else:
            partition = device
        if partition not in parts:
            raise ResizeError("Shrink requires a supported partitioned disk layout")
        # Any unrecognized partition may contain encrypted or unsupported data.
        # Small raw partitions (Windows MSR, BIOS boot) are copied as they are.
        known = {r["Name"] for r in filesystems} | {r["Name"] for r in pvs}
        if any(int(parts[n]["Size"]) > 256 * 1024**2 for n in set(parts) - known):
            raise ResizeError(
                "Unknown or encrypted partition; disable BitLocker and use a supported disk layout"
            )
        usage = list(
            csv.reader(io.StringIO(run(["virt-df", "--format=raw", "-a", str(image), "--csv"])))
        )
        used = {r[1]: int(r[3]) * 1024 for r in usage[1:] if len(r) >= 4}
        if any(r["Name"] not in used for r in candidates):
            raise ResizeError("Cannot determine filesystem usage safely")
        minimum = minimum_gb(sum(used[r["Name"]] for r in candidates))
        if size_gb < minimum:
            raise ResizeError(f"Target is below minimum {minimum} GiB (used +10%, at least +1 GiB)")
        # Reserve alignment/partition-table space and retain all other partitions.
        deficit = current - target + 64 * 1024**2
        content_size = int(fs["Size"]) - deficit
        if is_lv:
            vg_info = run(
                ["guestfish", "--ro", "--format=raw", "-a", str(image)], script="run\nvgs-full\n"
            )
            extents = re.findall(r"vg_extent_size:\s*(\d+)", vg_info)
            if len(extents) != 1 or int(extents[0]) <= 0:
                raise ResizeError("Cannot determine LVM extent size safely")
            # lvresize takes MiB and LVM rounds to extents. Never reduce an LV
            # to less than the filesystem we just prepared inside it.
            alignment = math.lcm(int(extents[0]), 1024**2)
            content_size = content_size // alignment * alignment
        if content_size < used[device] + max(math.ceil(used[device] / 10), GIB):
            raise ResizeError(
                "Target leaves insufficient room for filesystem usage, "
                "other partitions and alignment"
            )
        working = image.with_name(image.name + ".shrink-work")
        output = image.with_name(image.name + ".resized")
        run(["qemu-img", "convert", "-f", "raw", "-O", "raw", str(image), str(working)])
        commands = []
        if fs["VFS"].lower() == "ntfs":
            commands.append(f"ntfsresize-size {device} {content_size}")
        else:
            commands += [f"e2fsck-f {device}", f"resize2fs-size {device} {content_size}"]
        if is_lv:
            # Leave metadata headroom; lvreduce and pvresize both refuse unsafe layouts.
            pv_size = int(parts[partition]["Size"]) - deficit + 16 * 1024**2
            commands += [
                f"lvresize {device} {math.ceil(content_size / 1024**2)}",
                f"pvresize-size {partition} {pv_size}",
            ]
        # Device names originate from the guest. Reject script metacharacters.
        if not all(re.fullmatch(r"/dev/[A-Za-z0-9_+./-]+", d) for d in (device, partition)):
            raise ResizeError("Unsupported device name in disk layout")
        run(
            ["guestfish", "--rw", "--format=raw", "-a", str(working)],
            script="run\n" + "\n".join(commands) + "\nsync\n",
        )
        run(["qemu-img", "create", "-f", "raw", str(output), str(target)])
        run(
            [
                "virt-resize",
                "--format",
                "raw",
                "--output-format",
                "raw",
                "--shrink",
                partition,
                str(working),
                str(output),
            ]
        )
        if output.stat().st_size != target:
            raise ResizeError("Resized image has unexpected size")
        output.replace(image)  # only after every operation succeeds
    except (KeyError, ValueError, IndexError) as exc:
        if isinstance(exc, ResizeError):
            raise
        raise ResizeError("Cannot safely interpret disk layout or filesystem usage") from None
