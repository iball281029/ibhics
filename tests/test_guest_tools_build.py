"""The guest tools CD: current virtio-win for 2016+/10/11, 0.1.189 for older Windows,
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


def test_legacy_hard_links_become_whole_files(tmp_path):
    # virtio-win 0.1.189: the 2k12R2 drivers are hard links to the w8.1 ones.
    src, out = fake_iso(tmp_path / "old", ["w8.1"]), tmp_path / "virtio-win"
    (src / "viostor/w8.1/amd64/viostor.sys").write_bytes(b"driver")
    for drv in ("viostor", "NetKVM", "vioscsi", "Balloon"):
        (src / drv / "2k12R2" / "amd64").mkdir(parents=True)
        for f in (src / drv / "w8.1" / "amd64").iterdir():
            (src / drv / "2k12R2" / "amd64" / f.name).hardlink_to(f)
    run(src, out, "--legacy")
    sys_file = out / "viostor/2k12R2/amd64/viostor.sys"
    assert sys_file.read_bytes() == b"driver" and sys_file.stat().st_nlink == 1
    (src / "viostor/w8.1/amd64/viostor.sys").write_bytes(b"")
    (out / "viostor").rename(tmp_path / "gone")
    import shutil

    shutil.rmtree(out)
    r = subprocess.run([sys.executable, str(SCRIPT), str(src), str(out), "--legacy"],
                       capture_output=True, text=True)  # fmt: skip
    assert r.returncode and "empty files" in r.stderr


def test_legacy_install_script_on_the_cd():
    cmd = Path("installer/guest-tools/install-legacy.cmd").read_bytes().decode()
    assert "\r\n" in cmd  # Windows line ends
    for step in ("pnputil -f -d", "Red Hat", "qemu-ga-x86_64.msi", "/qn", "pnputil -i -a",
                 "viogpudo", "qxldod", "blnsvr.exe\" -i", "2k12R2", "w8.1"):  # fmt: skip
        assert step in cmd, step
    assert "install-legacy.cmd" in Path("installer/build-iso.sh").read_text()
