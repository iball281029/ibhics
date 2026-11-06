"""Browse the files of a storage pool, like a datastore browser: folders such as "iso"
for install media, uploads into them, and the VM disk images the pool holds.

Only pools kept as files (a folder, a thin XFS disk, NFS) have a file tree; an LVM pool
holds VM disks as logical volumes and nothing else. Paths are always relative to the
pool's own folder and never leave it.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

NAME_RE = re.compile(r"^[\w.+\- ()]+$")
ISO_DEPTH = 3  # how deep the ISO library looks for .iso files in a pool


@dataclass
class Entry:
    name: str
    path: str  # relative to the pool
    dir: bool
    size_bytes: int
    modified: float


def clean(rel: str) -> PurePosixPath:
    """A safe relative path: no "..", no hidden parts, only plain names."""
    parts = [p for p in PurePosixPath(rel or "").parts if p not in ("", ".", "/")]
    for p in parts:
        if p == ".." or p.startswith(".") or not NAME_RE.match(p):
            raise ValueError(f"invalid name {p!r}: use letters, digits, spaces, . _ - + ( )")
    return PurePosixPath(*parts) if parts else PurePosixPath()


def resolve(root: str | Path, rel: str) -> Path:
    """`rel` under the pool folder `root`, refusing anything that ends up outside it
    (a symlink pointing elsewhere, for instance)."""
    base = Path(root).resolve()
    target = (base / clean(rel)).resolve()
    if target != base and base not in target.parents:
        raise ValueError("that path leaves the storage pool")
    return target


def listing(root: str | Path, rel: str = "") -> list[Entry]:
    folder = resolve(root, rel)
    if not folder.is_dir():
        raise LookupError(f"no folder {rel!r} in this pool")
    entries = []
    base = Path(root).resolve()
    for child in folder.iterdir():
        if child.name.startswith(".") or child.name == "lost+found":
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        is_dir = child.is_dir()
        entries.append(
            Entry(
                name=child.name,
                path=str(child.relative_to(base)),
                dir=is_dir,
                size_bytes=0 if is_dir else st.st_size,
                modified=st.st_mtime,
            )
        )
    return sorted(entries, key=lambda e: (not e.dir, e.name.lower()))


def make_folder(root: str | Path, rel: str) -> Path:
    if not clean(rel).parts:
        raise ValueError("name the folder")
    target = resolve(root, rel)
    if target.exists():
        raise ValueError(f"{rel} already exists")
    target.mkdir(parents=True)
    return target


def remove(root: str | Path, rel: str, in_use: set[str]) -> None:
    """Delete a file, or a folder with what is in it. Files in the pool's top folder are
    VM disks that the VM's settings manage, so they are refused here, as is anything a
    VM is using (an ISO in its CD/DVD drive)."""
    if not clean(rel).parts:
        raise ValueError("the pool itself cannot be deleted here")
    target = resolve(root, rel)
    if not target.exists():
        raise LookupError(f"no {rel!r} in this pool")
    if target.is_file() and target.parent == Path(root).resolve():
        raise ValueError("VM disks are removed from the VM's settings, not here")
    used = [p for p in in_use if Path(p) == target or target in Path(p).parents]
    if used:
        raise ValueError(f"in use by a VM: {', '.join(sorted(used))}; eject it first")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def upload_target(root: str | Path, rel: str) -> Path:
    """Where an upload to `rel` goes; its folder must exist and the file must not."""
    path = clean(rel)
    if not path.parts:
        raise ValueError("name the file")
    target = resolve(root, str(path))
    if target.parent == Path(root).resolve():
        raise ValueError("upload into a folder (for example iso), not next to the VM disks")
    if not target.parent.is_dir():
        raise LookupError(f"no folder {str(path.parent)!r} in this pool")
    if target.exists():
        raise ValueError(f"{path.name} already exists; delete it first")
    return target


def find_isos(root: str | Path) -> list[Path]:
    """.iso files in the pool's folders, a few levels deep."""
    base = Path(root)
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        depth = len(Path(dirpath).relative_to(base).parts)
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and depth < ISO_DEPTH]
        found += [
            Path(dirpath) / f
            for f in filenames
            if f.lower().endswith(".iso") and not f.startswith(".")
        ]
    return sorted(found)
