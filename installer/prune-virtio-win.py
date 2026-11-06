"""Keep from an extracted virtio-win ISO only what virt-v2v uses for 64-bit Windows Server
2012 R2 and later and Windows 10/11: about 40 MB instead of 700.

    python3 prune-virtio-win.py EXTRACTED_ISO OUT_DIR
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
WINDOWS = {"2k12R2", "2k16", "2k19", "2k22", "2k25", "w10", "w11"}


def main(src: Path, out: Path) -> None:
    kept = 0
    for driver in sorted(p for p in src.iterdir() if p.is_dir() and p.name in DRIVERS):
        for version in driver.iterdir():
            arch = version / "amd64"
            if version.name in WINDOWS and arch.is_dir():
                shutil.copytree(arch, out / driver.name / version.name / "amd64")
                kept += 1
    agent = src / "guest-agent" / "qemu-ga-x86_64.msi"
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
    main(Path(sys.argv[1]), Path(sys.argv[2]))
