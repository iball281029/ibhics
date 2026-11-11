#!/bin/bash
# Target filesystem only. Does not reset credentials or alter partition/filesystem layout.
set -euo pipefail
SRC=/opt/arbiter-repair
exec > >(tee "$SRC/repair.log") 2>&1
export DEBIAN_FRONTEND=noninteractive
backup=/root/arbiter-repair-backup-$(date +%Y%m%d-%H%M%S)
mkdir -m 700 "$backup"
cp -a /opt/ibhics/venv "$backup/venv"
cp -a /etc/ibhics "$backup/ibhics-config"
cp -a /etc/network/interfaces /etc/shadow "$backup/"
printf 'deb [trusted=yes] file:%s/repo ./\n' "$SRC" >/tmp/arbiter-repair.list
APT=(-o Dir::Etc::sourcelist=/tmp/arbiter-repair.list -o Dir::Etc::sourceparts=-)
apt-get "${APT[@]}" update
apt-get "${APT[@]}" install -y --no-install-recommends dbus
/opt/ibhics/venv/bin/pip install --no-index --no-deps --force-reinstall "$SRC"/wheelhouse/ibhics-*.whl
install -m 644 "$SRC/ibhics-firstboot.service" /etc/systemd/system/ibhics-firstboot.service
if [ -e /etc/ibhics/configured ]; then
    mv /etc/ibhics/configured "$backup/configured-before-repair"
fi
printf '\nArbiter setup pending. Complete setup on this console.\n' >/etc/issue
systemctl enable dbus.socket ibhics-firstboot.service
/opt/ibhics/venv/bin/python -c 'import ibhics; print(ibhics.__version__)' >/etc/ibhics/version
rm -f /tmp/arbiter-repair.list
printf 'Repair packages installed. Previous configuration saved under %s\n' "$backup"
