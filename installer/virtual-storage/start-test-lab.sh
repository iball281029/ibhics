#!/bin/bash
# Disposable isolated storage lab. Run on the existing Node2 QEMU build host.
# Every guest disk is a qcow2 file; no physical disk is attached to a guest.
set -Eeuo pipefail
lab=/root/ibhics-vstorage-lab
base=/root/arbiter-build/test-r2/install.qcow2
[[ $(id -u) == 0 && -f $base ]] || exit 1
[[ ! -e $lab ]] || { echo 'Existing lab: inspect/stop it before starting another'; exit 1; }
mkdir -m 700 "$lab"
ssh-keygen -q -t ed25519 -N '' -f "$lab/key"
# This bridge has no physical uplink and cannot change the management bridge.
ip link add ibhvs-test type bridge
ip address add 192.0.2.254/24 dev ibhvs-test
ip link set ibhvs-test up
for i in 1 2 3; do
    dir=$lab/node$i
    mkdir "$dir"
    qemu-img create -q -f qcow2 -F qcow2 -b "$base" "$dir/os.qcow2"
    printf 'auto lo\niface lo inet loopback\nauto ens3\niface ens3 inet static\n address 10.0.2.15/24\n gateway 10.0.2.2\nauto ens4\niface ens4 inet static\n address 192.0.2.1%s/24\n' "$i" >"$dir/interfaces"
    virt-customize --no-network -a "$dir/os.qcow2" \
        --hostname "vs-test$i" --ssh-inject "root:file:$lab/key.pub" \
        --run-command 'usermod -p "$(openssl rand -base64 36 | openssl passwd -6 -stdin)" root' \
        --run-command 'systemctl disable arbiter-smoketest.service ibhics-firstboot.service ibhics-agent.service || true' \
        --run-command 'rm -f /etc/network/interfaces.d/*' \
        --run-command 'printf "nameserver 1.1.1.1\nnameserver 8.8.8.8\noptions timeout:1 attempts:1\n" >/etc/resolv.conf' \
        --upload "$dir/interfaces:/etc/network/interfaces" >"$dir/customize.log" 2>&1
    ip tuntap add "ibhvs-t$i" mode tap
    ip link set "ibhvs-t$i" master ibhvs-test
    ip link set "ibhvs-t$i" up
    disks=()
    if [[ $i != 3 ]]; then
        qemu-img create -q -f qcow2 "$dir/data.qcow2" 3G
        qemu-img create -q -f qcow2 "$dir/cache.qcow2" 1G
        disks=(-drive "file=$dir/data.qcow2,if=none,id=data,format=qcow2"
               -device "virtio-blk-pci,drive=data,serial=VS-TEST-DATA-$i"
               -drive "file=$dir/cache.qcow2,if=none,id=cache,format=qcow2"
               -device "virtio-blk-pci,drive=cache,serial=VS-TEST-CACHE-$i")
    fi
    qemu-system-x86_64 -enable-kvm -cpu host -smp 2 -m 3072 -display none \
        -daemonize -pidfile "$dir/qemu.pid" -serial "file:$dir/serial.log" \
        -netdev "user,id=nat,hostfwd=tcp:127.0.0.1:222$i-:22" \
        -device "e1000,netdev=nat,addr=0x3,mac=52:54:00:51:10:0$i" \
        -netdev "tap,id=storage,ifname=ibhvs-t$i,script=no,downscript=no" \
        -device "e1000,netdev=storage,addr=0x4,mac=52:54:00:51:20:0$i" \
        -drive "file=$dir/os.qcow2,if=none,id=os,format=qcow2" \
        -device "virtio-blk-pci,drive=os,bootindex=1" "${disks[@]}"
done
echo "Lab started; SSH key: $lab/key, guest addresses: 192.0.2.11–13."
