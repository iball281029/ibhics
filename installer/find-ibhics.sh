#!/bin/sh
# Runs in the Debian installer: exits 0 when DISK already holds an ibHiCS install, that
# is a partition with the LVM volume group "ibhics" (preseed.cfg names it so). Used to
# stop the default menu entry from erasing a node that only booted the installer again
# because its ISO (often BMC virtual media, which ignores the eject) is still attached.
disk=${1:?usage: find-ibhics.sh /dev/DISK}
SYS=${SYS:-/sys/block}            # tests point these at a fake system
DEV=${DEV:-/dev}
name=${disk#/dev/}
for part in "$SYS/$name/$name"*; do
    [ -d "$part" ] || continue
    p=${part##*/}
    # LVM keeps its text metadata ("ibhics {" ...) in the first MiB of the PV.
    dd if="$DEV/$p" bs=4096 count=256 2>/dev/null | tr -d '\000' | grep -q 'ibhics {' && exit 0
done
exit 1
