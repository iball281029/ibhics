#!/bin/sh
# Runs in Debian installer. Only repairs a positively identified Arbiter installation.
set -eu
SRC=/cdrom/ibhics
TARGET=/target
fail() {
    echo "Arbiter repair stopped: $*" >/dev/console
    echo 'No partitioning or formatting will be performed.' >/dev/console
    exit 1
}
disk=$(sh "$SRC/pick-disk.sh") || fail 'requires exactly one dedicated VM disk'
sh "$SRC/find-ibhics.sh" "$disk" || fail 'Arbiter volume group was not found on this disk'
anna-install lvm2-udeb || fail 'could not load the LVM installer tools'
lvm vgchange -ay arbiter-01 || fail 'could not activate the Arbiter volume group'
mkdir -p "$TARGET"
mount -t ext4 -o ro /dev/arbiter-01/root "$TARGET" || fail 'could not mount the Arbiter root filesystem'
[ "$(cat "$TARGET/etc/ibhics/appliance" 2>/dev/null)" = Arbiter ] || fail 'appliance identity check failed'
[ -f "$TARGET/opt/ibhics/venv/pyvenv.cfg" ] || fail 'installed Python environment not found'
mount -o remount,rw "$TARGET" || fail 'could not write the Arbiter filesystem'
mkdir -p "$TARGET/opt/arbiter-repair"
cp -a "$SRC/repo" "$SRC/wheelhouse" "$SRC/repair-target.sh" "$SRC/ibhics-firstboot.service" "$TARGET/opt/arbiter-repair/"
mount --bind /dev "$TARGET/dev"
mount -t proc proc "$TARGET/proc"
mount -t sysfs sysfs "$TARGET/sys"
# Stop service activation in chroot; the repaired appliance starts them after reboot.
policy="$TARGET/usr/sbin/policy-rc.d"
[ ! -e "$policy" ] || cp -a "$policy" "$TARGET/opt/arbiter-repair/policy-rc.d.saved"
printf '#!/bin/sh\nexit 101\n' >"$policy"
chmod 755 "$policy"
chroot "$TARGET" /bin/bash /opt/arbiter-repair/repair-target.sh || fail 'package repair failed; retained logs under /opt/arbiter-repair'
if [ -e "$TARGET/opt/arbiter-repair/policy-rc.d.saved" ]; then
    cp -a "$TARGET/opt/arbiter-repair/policy-rc.d.saved" "$policy"
else
    rm -f "$policy"
fi
sync
umount "$TARGET/sys" "$TARGET/proc" "$TARGET/dev"
umount "$TARGET"
echo 'Arbiter repair completed. Disconnect the ISO and boot the virtual disk to finish setup.' >/dev/console
sleep 3
reboot
while :; do sleep 3600; done
