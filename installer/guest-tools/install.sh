#!/bin/sh
# ibHiCS guest tools for Linux: the QEMU guest agent, which reports the guest's OS, name
# and addresses to ibHiCS and lets it shut the guest down cleanly. Run as root:
#   sh /media/cdrom/linux/install.sh      (or wherever the IBHICS_TOOLS CD is mounted)
set -e
if command -v apt-get >/dev/null; then
    apt-get update && apt-get install -y qemu-guest-agent
elif command -v dnf >/dev/null; then
    # AlmaLinux 8 re-signed its packages in 2024; older installs need the new key.
    if grep -qs 'AlmaLinux release 8' /etc/almalinux-release; then
        rpm --import https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux
    fi
    dnf install -y qemu-guest-agent
elif command -v yum >/dev/null; then
    yum install -y qemu-guest-agent
elif command -v zypper >/dev/null; then
    zypper --non-interactive install qemu-guest-agent
else
    echo "Install the qemu-guest-agent package with this distribution's package manager." >&2
    exit 1
fi
systemctl enable --now qemu-guest-agent 2>/dev/null || service qemu-guest-agent start
echo "Done: ibHiCS now sees this guest's details."
