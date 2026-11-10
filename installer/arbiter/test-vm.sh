#!/bin/bash
# Isolated QEMU installation. Dedicated qcow2 only; no host disk access or LAN bridge.
set -euo pipefail
ISO=$(realpath "${1:?ISO path}")
OUT=${2:?new empty test directory}
mkdir -p "$OUT"
[ ! -e "$OUT/install.qcow2" ] || { echo 'Test disk exists; choose another test directory'; exit 1; }
xorriso -osirrox on -indev "$ISO" -extract /install.amd/vmlinuz "$OUT/vmlinuz" -extract /install.amd/initrd.gz "$OUT/initrd.gz" >"$OUT/extract.log" 2>&1
qemu-img create -f qcow2 "$OUT/install.qcow2" 20G
qemu-system-x86_64 -enable-kvm -m 4096 -smp 4 \
    -drive file="$OUT/install.qcow2",if=virtio,format=qcow2 -cdrom "$ISO" \
    -kernel "$OUT/vmlinuz" -initrd "$OUT/initrd.gz" \
    -append 'auto=true priority=critical preseed/file=/cdrom/ibhics/preseed.cfg console=ttyS0,115200n8 DEBIAN_FRONTEND=text --- console=ttyS0,115200n8' \
    -display none -chardev socket,id=serial,path="$OUT/serial.sock",server=on,wait=off,logfile="$OUT/serial.log" \
    -serial chardev:serial -monitor unix:"$OUT/monitor.sock",server,nowait \
    -nic user,model=virtio-net-pci -no-reboot >"$OUT/qemu.log" 2>&1
