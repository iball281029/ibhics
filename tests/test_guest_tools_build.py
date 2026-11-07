"""The guest tools CD: current virtio-win for 2016+/10/11, 0.1.229 for older Windows,
and $WinPEDriver$ so Windows Setup sees the disk without "Load driver"."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path("installer/prune-virtio-win.py")


def fake_iso(root: Path, versions: list[str]) -> Path:
    for drv in ("viostor", "NetKVM", "vioscsi", "Balloon"):
        for v in versions:
            d = root / drv / v / "amd64"
            d.mkdir(parents=True)
            (d / f"{drv}.inf").write_text("x")
    (root / "virtio-win-guest-tools.exe").write_bytes(b"exe")
    (root / "guest-agent").mkdir()
    (root / "guest-agent" / "qemu-ga-x86_64.msi").write_bytes(b"msi")
    return root


def run(*args):
    subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], check=True,
                   capture_output=True)  # fmt: skip


def test_current_legacy_and_winpe(tmp_path):
    out, tools = tmp_path / "virtio-win", tmp_path / "tools"
    run(fake_iso(tmp_path / "new", ["2k12R2", "2k16", "2k22", "w11", "w8.1"]), out)
    run(fake_iso(tmp_path / "old", ["2k12R2", "w8.1", "2k16"]), out, "--legacy")
    have = {p.parent.parent.name for p in out.glob("viostor/*/amd64/viostor.inf")}
    assert have == {"2k16", "2k22", "w11", "2k12R2", "w8.1"}
    # 2012 R2 comes from the old release (the new one is not signed for it).
    assert (out / "legacy" / "virtio-win-guest-tools.exe").exists()
    assert (out / "virtio-win-guest-tools.exe").exists()
    run("--winpe", out, tools)
    winpe = {p.parent.parent.name for p in (tools / "$WinPEDriver$").glob("viostor/*/amd64/*.inf")}
    assert winpe == {"2k16", "2k22", "w11"}
    assert not (tools / "$WinPEDriver$" / "Balloon").exists()
