#!/bin/sh
# Runs inside the freshly installed system (from the preseed late_command): installs the
# Debian packages and ibHiCS from the ISO, with no network, and arranges for ibhics-setup
# to run on first boot.
set -eu
SRC=/opt/ibhics-installer
export DEBIAN_FRONTEND=noninteractive

# Debian packages from the repository on the ISO, and only from there.
echo "deb [trusted=yes] file:$SRC/repo ./" > /tmp/ibhics-install.list
APT="-o Dir::Etc::sourcelist=/tmp/ibhics-install.list -o Dir::Etc::sourceparts=-"
# shellcheck disable=SC2086
apt-get $APT update
# shellcheck disable=SC2046,SC2086
apt-get $APT install -y --no-install-recommends $(grep -v '^[[:space:]]*#' "$SRC/packages.txt")
rm /tmp/ibhics-install.list

# Later updates come from the Debian mirror once the node is on a network.
cat > /etc/apt/sources.list <<'LIST'
deb http://deb.debian.org/debian trixie main contrib non-free-firmware
deb http://deb.debian.org/debian trixie-updates main contrib non-free-firmware
deb http://security.debian.org/debian-security trixie-security main contrib non-free-firmware
LIST

python3 -m venv --system-site-packages /opt/ibhics/venv   # libvirt comes from python3-libvirt
/opt/ibhics/venv/bin/pip install --no-index --find-links "$SRC/wheelhouse" ibhics
for tool in ibhics-manager ibhics-agent ibhics-setup; do
    ln -sf "/opt/ibhics/venv/bin/$tool" "/usr/local/sbin/$tool"
done

install -d -m 0755 /etc/ibhics /var/lib/ibhics/pools/default /var/log/ibhics
install -m 0644 "$SRC"/systemd/*.service /etc/systemd/system/
systemctl enable ibhics-firstboot.service
systemctl enable libvirtd.service

cp "$SRC/VERSION" /etc/ibhics/version
rm -rf "$SRC"
