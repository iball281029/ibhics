#!/bin/sh
# Refuse ambiguous hardware. Intended only for a dedicated VMware VM.
set -eu
SYS=${SYS:-/sys/block}
count=0
chosen=
for dev in $(list-devices disk); do
    name=${dev#/dev/}
    [ -d "$SYS/$name" ] || continue
    [ "$(cat "$SYS/$name/removable")" = 0 ] || continue
    readlink -f "$SYS/$name/device" | grep -q '/usb[0-9]' && continue
    count=$((count+1))
    chosen=$dev
    size=$(cat "$SYS/$name/size")
done
if [ "$count" -ne 1 ] || [ "${size:-0}" -lt 25165824 ]; then
    echo 'Arbiter requires exactly one non-removable disk of at least 12 GiB. Installation stopped.' >&2
    exit 1
fi
echo "$chosen"
