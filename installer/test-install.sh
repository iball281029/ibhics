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
BOOT_DISK=""
[ -w /dev/kvm ] && ACCEL=(-enable-kvm -cpu host)
echo ">> Accelerator: ${ACCEL[*]}"
# Like a server with a big RAID volume that comes up first and a small boot drive:
# ibHiCS must go on the small one (disk.qcow2) and leave the big one free for VMs.
qemu-img create -q -f qcow2 raid.qcow2 400G
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
        -drive file=raid.qcow2,if=none,id=raid -device virtio-blk-pci,drive=raid,serial=RAID0 \
        -drive file=disk.qcow2,if=none,id=boot -device "virtio-blk-pci,drive=boot,serial=BOOT$BOOT_DISK" \
        -monitor unix:mon.sock,server,nowait "$@" &
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

raid_used=$(qemu-img info --output=json raid.qcow2 | python3 -c 'import json,sys; print(json.load(sys.stdin)["actual-size"])')
[ "$raid_used" -lt $((64 * 1024 * 1024)) ] \
    || { echo "!! the installer wrote to the big disk ($raid_used bytes): it must use the small one"; exit 1; }
echo ">> The big disk is untouched; ibHiCS went on the small one"

echo ">> 2/4 Writing an answers file so first boot runs without the console wizard"
sudo modprobe nbd max_part=16
sudo qemu-nbd -c /dev/nbd0 disk.qcow2
trap 'sudo umount -q mnt/var/lib/ibhics 2>/dev/null; sudo umount -q mnt 2>/dev/null; sudo vgchange -q -an ibhics >/dev/null 2>&1; sudo qemu-nbd -d /dev/nbd0 >/dev/null 2>&1 || true' EXIT
sleep 2
sudo vgscan -q >/dev/null
sudo vgchange -q -ay ibhics
mkdir -p mnt
sudo mount /dev/ibhics/root mnt
test -x mnt/opt/ibhics/venv/bin/ibhics-manager || { echo "!! ibHiCS is not installed in /opt/ibhics"; exit 1; }
test -f mnt/usr/share/virtio-win/viostor/2k19/amd64/viostor.sys \
    || { echo "!! virtio-win drivers are missing: Windows imports would fail"; exit 1; }
# /var/lib/ibhics is its own volume (data), not part of root.
sudo mount /dev/ibhics/data mnt/var/lib/ibhics
test -s mnt/var/lib/ibhics/tools/guest-tools.iso || { echo "!! the guest tools CD is missing"; exit 1; }
sudo umount mnt/var/lib/ibhics
for exe in rhsrvany.exe pnp_wait.exe; do
    test -s "mnt/usr/share/virt-tools/$exe" \
        || { echo "!! $exe is missing: virt-v2v cannot convert Windows guests"; exit 1; }
done
test -x mnt/usr/bin/virt-customize || { echo "!! virt-customize (guestfs-tools) is missing"; exit 1; }
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
# SeaBIOS tries only the first hard disk (the big, empty one), as a server's firmware
# boots only the drive set in its boot order: point it at the drive we installed to.
BOOT_DISK=,bootindex=1
run_vm firstboot 30 -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:8888-:8888 &
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
import json, ssl, sys, time, urllib.error, urllib.request

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
assert health["release"] == "ibHiCS Hyper-Converged Infrastructure beta version 0.9", health
print("ok  web console answers over HTTPS:", health["release"], health["version"])

token = call("POST", "/auth/login", {"username": "admin", "password": password})["token"]
print("ok  admin signs in with the password from setup")

cluster = call("GET", "/cluster", token=token)
assert cluster["quorum"] and cluster["members"][0]["healthy"], cluster
host = call("GET", f"/nodes/{cluster['members'][0]['name']}/host", token=token)
assert host["hypervisor"] != "fake", host
print("ok  node is healthy and uses", host["hypervisor"], "with", host["cpus"], "CPUs")

settings = call("GET", f"/nodes/{cluster['members'][0]['name']}/settings", token=token)
assert settings["dns"] == ["10.0.2.3"], settings
print("ok  /etc/resolv.conf has the DNS server from setup")

disks = call("GET", f"/nodes/{cluster['members'][0]['name']}/disks", token=token)
assert any(d["serial"] == "RAID0" and d["empty"] for d in disks), disks
print("ok  the big disk is listed as free, ready to add as a pool")

pools = call("GET", "/pools", token=token)
assert any(p["name"] == "default" for p in pools), pools
print("ok  default storage pool exists")

vm = call("POST", "/vms", {"name": "smoke", "vcpus": 1, "memory_mb": 512,
                           "disks": [{"size_gb": 1}]}, token=token)
assert vm["state"] == "stopped", vm
print("ok  libvirt defines a VM with a disk in the default pool")
# Powering on is where AppArmor, disk permissions and the domain XML really meet QEMU.
kvm = True
try:
    started = call("POST", f"/vms/{vm['uuid']}/start", token=token)
    assert started["state"] == "running", started
    print("ok  the VM powers on (QEMU opens its disk)")
    snap = call("POST", f"/vms/{vm['uuid']}/snapshots", {"name": "smoke-snap"}, token=token)
    assert snap["vm_state"] == "running", snap
    call("POST", f"/vms/{vm['uuid']}/stop", {"force": True}, token=token)
    back = call("POST", f"/vms/{vm['uuid']}/snapshots/smoke-snap/revert", token=token)
    assert back["state"] == "running", back
    print("ok  a snapshot with memory is taken, and reverting brings the VM back running")
    call("POST", f"/vms/{vm['uuid']}/stop", {"force": True}, token=token)
    call("DELETE", f"/vms/{vm['uuid']}/snapshots/smoke-snap", token=token)
    assert call("GET", f"/vms/{vm['uuid']}/snapshots", token=token) == []
except urllib.error.HTTPError as exc:
    detail = exc.read().decode(errors="replace")
    if "Permission denied" in detail or "kvm" not in detail.lower():
        raise SystemExit(f"!! the VM did not power on: {exc.code} {detail}")
    print("--  no nested KVM on this runner, power-on not tested:", detail[:200])
    kvm = False

# Templates: a linked clone is a qcow2 overlay on the template's disk, and its
# customization CD is built with xorriso; powering it on proves QEMU may open both.
tpl = call("POST", f"/vms/{vm['uuid']}/template", {"template": True}, token=token)
assert tpl["template"], tpl
custom = {"hostname": "smoke2", "username": "ops", "password": "Smoke-pass1"}
job = call("POST", f"/vms/{vm['uuid']}/clone", {"name": "smoke-clone", "linked": True,
           "power_on": kvm, "customization": custom}, token=token)
for _ in range(90):
    job = next(j for j in call("GET", "/deploys", token=token) if j["id"] == job["id"])
    if job["state"] in ("done", "failed"):
        break
    time.sleep(2)
assert job["state"] == "done", job
clone = call("GET", f"/vms/{job['result']}", token=token)
assert clone["linked_to"] == vm["uuid"] and clone["iso"].endswith("smoke-clone-seed.iso"), clone
print("ok  a linked clone with a customization CD deploys from the template")
if kvm:
    assert clone["state"] == "running", clone
    print("ok  and powers on (QEMU opens the overlay, its template disk and the CD)")
    call("POST", f"/vms/{clone['uuid']}/stop", {"force": True}, token=token)
call("DELETE", f"/vms/{clone['uuid']}", token=token)
bin_ = call("GET", "/recycle-bin", token=token)
assert bin_["days"] == 7 and [r["vm"]["name"] for r in bin_["vms"]] == ["smoke-clone"], bin_
call("POST", f"/recycle-bin/{clone['uuid']}/restore", token=token)
print("ok  a deleted VM goes to the recycle bin and can be restored")
call("DELETE", f"/vms/{clone['uuid']}?permanent=true", token=token)
call("DELETE", f"/vms/{vm['uuid']}?permanent=true", token=token)
assert call("GET", "/vms", token=token) == []
print("ok  and deletes them for good")

if kvm:
    # v0.8: a running UEFI VM takes a snapshot (its UEFI variables move to qcow2 while it
    # is off) and is backed up while it runs (QEMU writes the copy to the scratch folder).
    efi = call("POST", "/vms", {"name": "smoke-efi", "vcpus": 1, "memory_mb": 512,
                                "firmware": "uefi", "disks": [{"size_gb": 1}]}, token=token)
    call("POST", f"/vms/{efi['uuid']}/start", token=token)  # libvirt picks the firmware
    call("POST", f"/vms/{efi['uuid']}/stop", {"force": True}, token=token)
    call("POST", f"/vms/{efi['uuid']}/start", token=token)  # variables are qcow2 now
    snap = call("POST", f"/vms/{efi['uuid']}/snapshots", {"name": "smoke-efi-20261106-1200"},
                token=token)
    assert snap["vm_state"] == "running", snap
    print("ok  a running UEFI VM takes a snapshot")
    call("POST", "/backup/targets", {"name": "smoke-bk", "type": "local",
                                     "path": "/var/lib/ibhics/smoke-backups"}, token=token)
    job = call("POST", f"/vms/{efi['uuid']}/backup", {"target": "smoke-bk"}, token=token)
    for _ in range(150):
        job = next(j for j in call("GET", "/backup/jobs", token=token) if j["id"] == job["id"])
        if job["state"] in ("done", "failed"):
            break
        time.sleep(2)
    assert job["state"] == "done", job
    print("ok  a running VM is backed up (QEMU may write the scratch folder)")
    call("POST", f"/vms/{efi['uuid']}/stop", {"force": True}, token=token)
    call("DELETE", f"/vms/{efi['uuid']}/snapshots/smoke-efi-20261106-1200", token=token)
    call("DELETE", f"/vms/{efi['uuid']}?permanent=true", token=token)
    try:
        call("DELETE", "/backup/targets/smoke-bk", token=token)
    except urllib.error.HTTPError:
        pass  # a repository that holds backups may refuse; it does no harm here

name = cluster["members"][0]["name"]
out = call("POST", f"/nodes/{name}/terminal", {"command": "ip -br addr"}, token=token)
assert out["code"] == 0 and "br0" in out["output"], out
print("ok  the Terminal runs diagnostic commands on the node")

# v0.7 network: a VLAN with a router (gateway, NAT, DHCP), a firewall rule and a ping
# of the gateway, all on the real node (ip, nft, dnsmasq).
call("POST", "/networks", {"name": "edge-test", "vlan": 77}, token=token)
edge = call("PUT", "/edge/routers/edge-test", {"gateway": "10.77.0.1/24"}, token=token)
assert edge["routers"][0]["network"] == "edge-test", edge
rules = [{"name": "no-ssh", "action": "deny", "src": "any", "dst": "edge-test",
          "proto": "tcp", "port": 22}]
assert call("PUT", "/edge/rules", rules, token=token)["rules"][0]["name"] == "no-ssh"
ping = call("POST", "/edge/test", {"kind": "ping", "target": "10.77.0.1",
                                   "network": "edge-test"}, token=token)
assert ping["ok"], ping
print("ok  a VLAN gets a router with NAT and DHCP, a firewall rule, and its gateway answers")
call("PUT", "/edge/rules", [], token=token)
call("DELETE", "/edge/routers/edge-test", token=token)
call("DELETE", "/networks/edge-test", token=token)
print("ok  and the router and network are removed again")
EOF
echo ">> All checks passed"
