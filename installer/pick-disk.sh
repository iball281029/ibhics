#!/bin/sh
# Runs in the Debian installer (partman/early_command): prints the disk to install
# ibHiCS on. That is the smallest internal disk of at least 32 GB, so a server with a
# small boot drive (M.2, BOSS, SATADOM) and a big RAID volume keeps the RAID for VM
# storage, whatever order the controllers come up in. The USB stick or DVD the
# installer runs from, removable disks and disks under 32 GB are never picked. If no
# disk qualifies, it prints the first disk, as the installer itself would.
MIN_SECTORS=$((32 * 1024 * 1024 * 2))   # 32 GiB in 512-byte sectors
SYS=${SYS:-/sys/block}            # tests point these at a fake system
MOUNTS=${MOUNTS:-/proc/mounts}

media=$(awk '$2 == "/cdrom" { print $1 }' "$MOUNTS" | sed 's|^/dev/||; s|[0-9]*$||; s|p$||')
best="" best_size=""
for dev in $(list-devices disk); do
    name=${dev#/dev/}
    sys=$SYS/$name
    [ -d "$sys" ] || continue
    [ "$name" = "$media" ] && continue
    [ "$(cat "$sys/removable" 2>/dev/null)" = "1" ] && continue
    # USB disks: their sysfs path goes through a USB controller.
    readlink -f "$sys/device" 2>/dev/null | grep -q '/usb[0-9]' && continue
    size=$(cat "$sys/size" 2>/dev/null || echo 0)
    [ "$size" -ge "$MIN_SECTORS" ] || continue
    if [ -z "$best" ] || [ "$size" -lt "$best_size" ]; then
        best=$dev best_size=$size
    fi
done
[ -n "$best" ] || best=$(list-devices disk | head -n1)
echo "$best"
