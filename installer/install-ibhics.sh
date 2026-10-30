#!/bin/sh
# Runs inside the freshly installed system (from the preseed late_command): installs
# ibHiCS from the wheels on the ISO and arranges for ibhics-setup to run on first boot.
set -eu
SRC=/opt/ibhics-installer

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
