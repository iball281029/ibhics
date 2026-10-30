#!/bin/bash
# End-to-end test of the installer ISO in QEMU: unattended install from the default menu
# entry, first boot with an answers file instead of the console wizard, then checks that
# the web console answers, sign-in works and libvirt can define a VM.
#
#   installer/test-install.sh dist/ibhics-0.1-beta-amd64.iso
#
# Needs qemu-system-x86, qemu-utils, lvm2, curl, python3 and sudo (for qemu-nbd), and
# tesseract-ocr to print the screen of a stuck install. Uses KVM when /dev/kvm is usable.
# Screenshots of the VM's screen go to $WORK/shots.
set -euo pipefail

ISO=$(realpath "$1")
WORK=${WORK:-$PWD/install-test}
INSTALL_MINUTES=${INSTALL_MINUTES:-60}
STALL_MINUTES=${STALL_MINUTES:-12}
ADMIN_PASSWORD=Admin-pw-123
API=https://127.0.0.1:8888/api/v1

rm -rf "$WORK" && mkdir -p "$WORK/shots"
cd "$WORK"
ACCEL=(-accel tcg)
[ -w /dev/kvm ] && ACCEL=(-enable-kvm -cpu host)
echo ">> Accelerator: ${ACCEL[*]}"
qemu-img create -q -f qcow2 disk.qcow2 80G

monitor() {  # send one command to the QEMU monitor
    python3 - "$1" <<'EOF'
import socket, sys, time
s = socket.socket(socket.AF_UNIX)
s.connect("mon.sock")
s.sendall(sys.argv[1].encode() + b"\n")
time.sleep(0.5)
s.close()
EOF
}

screen_text() {  # print what a screenshot says, when tesseract is installed
    [ -s "$1" ] && command -v tesseract >/dev/null || return 0
    echo "----- text on the screen ($1):"
    tesseract "$1" - 2>/dev/null || true
    echo "-----"
}

run_vm() {  # run_vm NAME MINUTES QEMU-ARGS...: screenshot every minute until QEMU exits
    local name=$1 minutes=$2
    shift 2
    qemu-system-x86_64 "${ACCEL[@]}" -m 4096 -smp 2 -display none \
        -drive file=disk.qcow2,if=virtio -monitor unix:mon.sock,server,nowait "$@" &
    local pid=$! i=0 shot last="" same=0
    while kill -0 $pid 2>/dev/null; do
        sleep 60
        i=$((i + 1))
        shot=shots/$name-$(printf %03d $i).ppm
        monitor "screendump $shot" 2>/dev/null || true
        # A screen that has not changed for STALL_MINUTES means the installer waits for input.
        if [ -s "$shot" ] && [ "$(md5sum <"$shot")" = "$last" ]; then
            same=$((same + 1))
        else
            same=0
            [ -s "$shot" ] && last=$(md5sum <"$shot")
        fi
        if [ $i -ge "$minutes" ] || [ $same -ge "$STALL_MINUTES" ]; then
            echo "!! $name did not finish: $i minutes, screen unchanged for $same"
            screen_text "$shot"
            kill $pid
            return 1
        fi
    done
    wait $pid
}

echo ">> 1/4 Unattended install from the default menu entry, with no network at all"
run_vm install "$INSTALL_MINUTES" -cdrom "$ISO" -boot d -no-reboot -nic none
echo ">> Install finished (the installer rebooted, so QEMU exited)"

echo ">> 2/4 Writing an answers file so first boot runs without the console wizard"
sudo modprobe nbd max_part=16
sudo qemu-nbd -c /dev/nbd0 disk.qcow2
trap 'sudo umount -q mnt 2>/dev/null; sudo vgchange -q -an ibhics >/dev/null 2>&1; sudo qemu-nbd -d /dev/nbd0 >/dev/null 2>&1 || true' EXIT
sleep 2
sudo vgscan -q >/dev/null
sudo vgchange -q -ay ibhics
mkdir -p mnt
sudo mount /dev/ibhics/root mnt
test -x mnt/opt/ibhics/venv/bin/ibhics-manager || { echo "!! ibHiCS is not installed in /opt/ibhics"; exit 1; }
# virt-v2v copies VMware disks with nbdkit's ssh plugin (and its curl plugin for HTTPS).
for plugin in ssh curl; do
    ls mnt/usr/lib/*/nbdkit/plugins/nbdkit-$plugin-plugin.so >/dev/null 2>&1 \
        || { echo "!! nbdkit $plugin plugin is missing: VMware imports would fail"; exit 1; }
done
sudo tee mnt/etc/ibhics/answers.conf >/dev/null <<EOF
ROLE=manager
HOSTNAME=node1
ADDRESS=10.0.2.15/24
GATEWAY=10.0.2.2
DNS=10.0.2.3
ROOT_PASSWORD=Root-pw-123
ADMIN_PASSWORD=$ADMIN_PASSWORD
EOF
sudo chmod 600 mnt/etc/ibhics/answers.conf
cat mnt/etc/ibhics/version
sudo umount mnt
sudo vgchange -q -an ibhics
sudo qemu-nbd -d /dev/nbd0
trap - EXIT

echo ">> 3/4 First boot"
run_vm firstboot 30 -boot c -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:8888-:8888 &
VM=$!
trap 'monitor quit 2>/dev/null || true' EXIT
for _ in $(seq 60); do
    if curl -skf "$API/health" -o health.json; then break; fi
    sleep 10
done
cat health.json 2>/dev/null || { echo "!! the web console did not answer on :8888"; exit 1; }
echo

echo ">> 4/4 Checks"
python3 - "$API" "$ADMIN_PASSWORD" <<'EOF'
import json, ssl, sys, urllib.request

api, password = sys.argv[1], sys.argv[2]
ctx = ssl._create_unverified_context()  # the node's certificate is self-signed

def call(method, path, body=None, token=None):
    req = urllib.request.Request(api + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        return json.loads(resp.read() or b"null")

health = call("GET", "/health")
assert health["status"] == "ok" and health["auth"], health
assert health["release"] == "ibHiCS Hyper-Converged Infrastructure beta version 0.1", health
print("ok  web console answers over HTTPS:", health["release"], health["version"])

token = call("POST", "/auth/login", {"username": "admin", "password": password})["token"]
print("ok  admin signs in with the password from setup")

cluster = call("GET", "/cluster", token=token)
assert cluster["quorum"] and cluster["members"][0]["healthy"], cluster
host = call("GET", f"/nodes/{cluster['members'][0]['name']}/host", token=token)
assert host["hypervisor"] != "fake", host
print("ok  node is healthy and uses", host["hypervisor"], "with", host["cpus"], "CPUs")

pools = call("GET", "/pools", token=token)
assert any(p["name"] == "default" for p in pools), pools
print("ok  default storage pool exists")

vm = call("POST", "/vms", {"name": "smoke", "vcpus": 1, "memory_mb": 512,
                           "disks": [{"size_gb": 1}]}, token=token)
assert vm["state"] == "stopped", vm
print("ok  libvirt defines a VM with a disk in the default pool")
call("DELETE", f"/vms/{vm['uuid']}", token=token)
print("ok  and deletes it")
EOF
echo ">> All checks passed"
