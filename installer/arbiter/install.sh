#!/bin/bash
# Runs inside the new appliance; never invoked against a compute host.
set -euo pipefail
SRC=/opt/ibhics-installer
export DEBIAN_FRONTEND=noninteractive
printf 'deb [trusted=yes] file:%s/repo ./\n' "$SRC" >/tmp/arbiter.list
APT=(-o Dir::Etc::sourcelist=/tmp/arbiter.list -o Dir::Etc::sourceparts=-)
apt-get "${APT[@]}" update
apt-get "${APT[@]}" install -y --no-install-recommends $(cat "$SRC/packages.txt")
rm /tmp/arbiter.list
install -d /usr/src/drbd-9.3.4/src
cd /usr/src/drbd-9.3.4/src
tar xzf "$SRC/drbd-9.3.4.tar.gz" --strip-components=1
sed 's/#MODULE_VERSION#/9.3.4/' misc/dkms.conf >/usr/src/drbd-9.3.4/dkms.conf
dkms add -m drbd -v 9.3.4
for modules in /lib/modules/*; do
    [ -d "$modules/build" ] || continue
    dkms install -m drbd -v 9.3.4 -k "${modules##*/}"
done
printf 'drbd\n' >/etc/modules-load.d/arbiter.conf
python3 -m venv /opt/ibhics/venv
/opt/ibhics/venv/bin/pip install --no-index --find-links "$SRC/wheelhouse" ibhics
install -d /etc/ibhics /var/lib/ibhics /var/log/ibhics
install -m 644 "$SRC/ibhics-agent.service" "$SRC/ibhics-firstboot.service" \
    "$SRC/ibhics-arbiter-ui.service" /etc/systemd/system/
printf '#!/bin/sh\nexec /opt/ibhics/venv/bin/python -m ibhics.installer.arbiter "$@"\n' >/usr/local/sbin/arbiter-setup
chmod 755 /usr/local/sbin/arbiter-setup
systemctl enable ibhics-firstboot.service open-vm-tools.service ibhics-arbiter-ui.service
cp "$SRC/VERSION" /etc/ibhics/version
printf 'Arbiter\n' >/etc/ibhics/appliance
# Package sources for later maintenance.
cat >/etc/apt/sources.list <<'LIST'
deb http://deb.debian.org/debian trixie main
deb http://security.debian.org/debian-security trixie-security main
LIST
passwd -l root
# Source, license and lock are retained for GPL compliance and future DKMS builds.
install -d /usr/share/arbiter
cp "$SRC/drbd-9.3.4.tar.gz" "$SRC/drbd-source.lock.json" /usr/share/arbiter/
rm -rf "$SRC"
