"""Keep from an extracted virtio-win ISO only what virt-v2v uses for 64-bit Windows: about
40 MB instead of 700. The current release covers Server 2016+ and Windows 10/11; Server
2012 R2 and older come from virtio-win 0.1.189 (later drivers are not signed for them).

    python3 prune-virtio-win.py EXTRACTED_ISO OUT_DIR [--legacy]
    python3 prune-virtio-win.py --winpe DRIVERS_DIR TOOLS_DIR
"""

import shutil
import sys
from pathlib import Path

DRIVERS = {
    "viostor",
    "vioscsi",
    "NetKVM",
    "Balloon",
    "vioserial",
    "pvpanic",
    "viorng",
    "qxldod",
    "viogpudo",
    "vioinput",
    "fwcfg",
    "smbus",
    "qemupciserial",
}
WINDOWS = {"2k16", "2k19", "2k22", "2k25", "w10", "w11"}
LEGACY = {"2k12R2", "2k12", "w8.1", "w8", "2k8R2", "w7"}
# Drivers Windows Setup loads by itself from $WinPEDriver$ at the root of a CD: the disk
# and network, so Setup sees the disk without "Load driver".
WINPE = ("viostor", "vioscsi", "NetKVM")


def winpe(drivers: Path, tools: Path) -> None:
    out = tools / "$WinPEDriver$"
    for driver in WINPE:
        for version in sorted(WINDOWS):
            arch = drivers / driver / version / "amd64"
            if arch.is_dir():
                shutil.copytree(arch, out / driver / version / "amd64")
    print(f"$WinPEDriver$: {sum(1 for _ in out.rglob('*.inf'))} drivers")


def main(src: Path, out: Path, legacy: bool = False) -> None:
    windows = LEGACY if legacy else WINDOWS
    kept = 0
    for driver in sorted(p for p in src.iterdir() if p.is_dir() and p.name in DRIVERS):
        for version in driver.iterdir():
            arch = version / "amd64"
            if version.name in windows and arch.is_dir():
                dest = out / driver.name / version.name / "amd64"
                # copytree copies file contents, so hard links (0.1.189's 2k12R2 files
                # are links to w8.1's) become whole files of their own.
                shutil.copytree(arch, dest)
                empty = [p.name for p in dest.iterdir() if p.is_file() and not p.stat().st_size]
                if empty:
                    sys.exit(f"{dest}: empty files {empty}: the ISO did not extract right")
                kept += 1
    agent = src / "guest-agent" / "qemu-ga-x86_64.msi"
    if legacy:
        # Its installers, for the guest tools CD's legacy folder (the current ones refuse
        # anything before Windows 10).
        (out / "legacy").mkdir(parents=True, exist_ok=True)
        for name in ("virtio-win-guest-tools.exe", "virtio-win-gt-x64.msi"):
            if (src / name).exists():
                shutil.copy2(src / name, out / "legacy" / name)
        if agent.exists():
            shutil.copy2(agent, out / "legacy" / agent.name)
        print(f"kept {kept} legacy driver folders")
        return
    if agent.exists():  # virt-v2v installs it in the guest at first boot
        (out / "guest-agent").mkdir(parents=True, exist_ok=True)
        shutil.copy2(agent, out / "guest-agent" / agent.name)
    # The all-in-one installer (drivers + guest agent) for the guest tools CD.
    for name in ("virtio-win-guest-tools.exe", "virtio-win-gt-x64.msi"):
        if (src / name).exists():
            shutil.copy2(src / name, out / name)
    if not kept:
        sys.exit(f"no drivers found in {src}: is it an extracted virtio-win ISO?")
    print(f"kept {kept} driver folders")


if __name__ == "__main__":
    if sys.argv[1] == "--winpe":
        winpe(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        main(Path(sys.argv[1]), Path(sys.argv[2]), "--legacy" in sys.argv[3:])
