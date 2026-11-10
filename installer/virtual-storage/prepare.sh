#!/bin/bash
# Install the storage software only. Never partition, format, wipe or create a pool.
set -Eeuo pipefail
role=${1:?compute or witness required}
[[ $role == compute || $role == witness ]] || exit 2
controller=${2:-no}
[[ $controller == no || ( $controller == yes && $role == compute ) ]] || exit 2
[[ $(id -u) == 0 ]] || { echo 'Run as root'; exit 1; }
. /etc/os-release
[[ $ID == debian && $VERSION_ID == 13 ]] || { echo 'Debian 13 required'; exit 1; }
exec 9>/run/lock/ibhics-storage-prepare.lock
flock -n 9 || { echo 'Storage preparation is already running'; exit 1; }
export DEBIAN_FRONTEND=noninteractive
work=/var/cache/ibhics/virtual-storage
install -d -m 700 "$work"
cd "$work"
if ! command -v gpg >/dev/null || ! command -v curl >/dev/null; then
    apt-get update
    apt-get install -y --no-install-recommends gpg curl ca-certificates
fi
curl -fL --retry 3 https://packages.linbit.com/package-signing-pubkey.asc -o linbit.asc
fingerprint=4E5385546726D13CB649872CFC05A31DB826FE48
gpg --show-keys --with-colons linbit.asc | awk -F: '$1=="fpr" {print $10}' | grep -qx "$fingerprint"
gpg --batch --yes --dearmor -o /usr/share/keyrings/ibhics-linbit.gpg linbit.asc
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/ibhics-linbit.gpg] https://packages.linbit.com/public trixie misc' >/etc/apt/sources.list.d/ibhics-linbit.list
apt-get update
packages=("linux-headers-$(uname -r)" build-essential dkms dbus drbd-utils=9.35.0-1
          linstor-common=1.35.3-1 linstor-satellite=1.35.3-1)
if [[ $role == compute ]]; then
    packages+=(drbd-reactor=1.12.0-1 resource-agents nfs-kernel-server xfsprogs lvm2 mdadm
               iptables iputils-arping)
fi
if [[ $controller == yes ]]; then
    packages+=(linstor-controller=1.35.3-1 linstor-client=1.29.1-1 python-linstor=1.29.1-1)
    # Set before installing, so the first start cannot expose the controller API.
    install -d /etc/linstor
    cat >/etc/linstor/linstor.toml <<'EOF'
[http]
listen_addr = "127.0.0.1"
port = 3370
EOF
fi
apt-get install -y --no-install-recommends "${packages[@]}"
if [[ ! -f /etc/linstor/linstor_satellite.toml ]]; then
    install -d /etc/linstor /etc/drbd-reactor.d
    cat >/etc/linstor/linstor_satellite.toml <<'EOF'
# ibHiCS-managed gateway service configuration distribution
[files]
allowExtFiles = ["/etc/systemd/system", "/etc/systemd/system/linstor-satellite.service.d", "/etc/drbd-reactor.d"]
EOF
    systemctl restart linstor-satellite
fi
if ! dkms status -m drbd -v 9.3.4 | grep -q "$(uname -r).*installed"; then
    curl -fL --retry 3 https://pkg.linbit.com/downloads/drbd/9/drbd-9.3.4.tar.gz -o drbd-9.3.4.tar.gz
    printf '%s\n' 'a2de4b7ffc43fdabad01b0bf7283b910eb6401b0f788e33a65ae94fc0588545c  drbd-9.3.4.tar.gz' | sha256sum -c -
    if [[ ! -f /usr/src/drbd-9.3.4/dkms.conf ]]; then
        install -d /usr/src/drbd-9.3.4/src
        tar xzf drbd-9.3.4.tar.gz -C /usr/src/drbd-9.3.4/src --strip-components=1
        sed 's/#MODULE_VERSION#/9.3.4/' /usr/src/drbd-9.3.4/src/misc/dkms.conf >/usr/src/drbd-9.3.4/dkms.conf
    fi
    if ! dkms status -m drbd -v 9.3.4 | grep -q .; then dkms add -m drbd -v 9.3.4; fi
    dkms install -m drbd -v 9.3.4 -k "$(uname -r)"
fi
if [[ -r /sys/module/drbd/version ]] && ! grep -q '^9\.' /sys/module/drbd/version; then
    echo 'DRBD 8 is loaded. Reboot into the installed DRBD 9 module before creating storage.'
    exit 1
fi
modprobe drbd
printf 'drbd\n' >/etc/modules-load.d/ibhics-storage.conf
systemctl enable --now linstor-satellite
systemctl start dbus
if [[ $role == compute ]]; then systemctl enable --now drbd-reactor; fi
if [[ $controller == yes ]]; then
    curl -fL --retry 3 https://github.com/LINBIT/linstor-gateway/releases/download/v2.3.0/linstor-gateway-linux-amd64 -o linstor-gateway
    printf '%s\n' 'a5cd30cc79941d8cbf742b1539a57eefa33d5a001f3b26ef671ff027f2c8e4ef  linstor-gateway' | sha256sum -c -
    install -m 755 linstor-gateway /usr/local/sbin/linstor-gateway
    cat >/etc/systemd/system/ibhics-storage-gateway.service <<'EOF'
[Unit]
Description=ibHiCS Virtual Storage gateway controller
After=network-online.target linstor-controller.service
Wants=network-online.target linstor-controller.service
[Service]
ExecStart=/usr/local/sbin/linstor-gateway server --addr=127.0.0.1:8337 --controllers=localhost:3370
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now linstor-controller ibhics-storage-gateway
fi
printf 'DRBD module: '; modinfo -F version drbd
echo 'Software preparation complete. No disks or storage pools have been changed.'
