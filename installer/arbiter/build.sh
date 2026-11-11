#!/bin/bash
# Build on Debian 13 amd64, using the existing full-node ISO only as Debian boot media.
# Usage: build.sh BASE_ISO EXTRACTED_PAYLOAD OFFLINE_REPO WHEEL OUTPUT_ISO
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=${1:?}; OLD=${2:?}; DEBS=${3:?}; WHEEL=${4:?}; OUT=${5:?}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
PAYLOAD=$WORK/payload
mkdir -p "$PAYLOAD/wheelhouse"
cp "$OLD"/wheelhouse/*.whl "$PAYLOAD/wheelhouse/"
rm -f "$PAYLOAD"/wheelhouse/ibhics-*.whl
cp "$WHEEL" "$PAYLOAD/wheelhouse/"
cp -a "$DEBS" "$PAYLOAD/repo"
cp "$HERE"/{preseed.cfg,pick-disk.sh,packages.txt,install.sh,drbd-source.lock.json,repair.sh,repair-target.sh,repair.cfg} "$PAYLOAD/"
cp "$HERE/find-ibhics.sh" "$PAYLOAD/"
cp "$HERE/../"{already-installed.sh,ibhics.templates} "$PAYLOAD/"
python3 - "$HERE/drbd-source.lock.json" "$HERE/drbd-9.3.4.tar.gz" <<'PY'
import hashlib, json, pathlib, sys
lock=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert hashlib.sha256(pathlib.Path(sys.argv[2]).read_bytes()).hexdigest() == lock['sha256']
PY
cp "$HERE/drbd-9.3.4.tar.gz" "$PAYLOAD/"
# Dedicated witness services: no libvirt dependency or compute manager on appliance.
sed 's/ libvirtd.service//' "$HERE/../systemd/ibhics-agent.service" >"$PAYLOAD/ibhics-agent.service"
sed -e 's/ libvirtd.service//' -e 's|ExecStart=.*|ExecStart=/usr/local/sbin/arbiter-setup|' \
    "$HERE/../systemd/ibhics-firstboot.service" >"$PAYLOAD/ibhics-firstboot.service"
echo '1.3.0b2+arbiterfix1' >"$PAYLOAD/VERSION"
cat >"$WORK/grub.cfg" <<'EOF'
set timeout=-1
set default=0
menuentry 'Repair existing Arbiter (keeps the VM disk)' {
 linux /install.amd/vmlinuz auto=true priority=critical preseed/file=/cdrom/ibhics/repair.cfg vga=788 --- quiet
 initrd /install.amd/initrd.gz
}
menuentry 'Install Arbiter (erases the dedicated VM disk)' {
 linux /install.amd/vmlinuz auto=true priority=critical preseed/file=/cdrom/ibhics/preseed.cfg vga=788 --- quiet
 initrd /install.amd/initrd.gz
}
EOF
cat >"$WORK/isolinux.cfg" <<'EOF'
default vesamenu.c32
timeout 0
menu title Arbiter - ibHiCS witness installer
label repair
 menu label Repair existing Arbiter (keeps the VM disk)
 menu default
 kernel /install.amd/vmlinuz
 append initrd=/install.amd/initrd.gz auto=true priority=critical preseed/file=/cdrom/ibhics/repair.cfg vga=788 --- quiet
label arbiter
 menu label Install Arbiter (erases the dedicated VM disk)
 kernel /install.amd/vmlinuz
 append initrd=/install.amd/initrd.gz auto=true priority=critical preseed/file=/cdrom/ibhics/preseed.cfg vga=788 --- quiet
EOF
mkdir -p "$(dirname "$OUT")"
[ ! -e "$OUT" ] || { echo 'Output exists; choose a new filename.' >&2; exit 1; }
xorriso -indev "$BASE" -outdev "$OUT" -boot_image any replay \
    -rm_r /ibhics -- -map "$PAYLOAD" /ibhics \
    -map "$WORK/grub.cfg" /boot/grub/grub.cfg \
    -map "$WORK/isolinux.cfg" /isolinux/isolinux.cfg
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" >"$(basename "$OUT").sha256")
