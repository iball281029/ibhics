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
# A free port for the VM's web console: on an ibHiCS node, 8888 is the host's own console.
PORT=${PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}
API=https://127.0.0.1:$PORT/api/v1

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
    local pid=$! i=0 shot last="" same=0 start=$SECONDS
    while kill -0 $pid 2>/dev/null; do
        # Wait a minute with bash's own timer (an external sleep that returns at once
        # would make every limit below pass in a moment), and count real time.
        read -rt 60 <> <(:) || true
        kill -0 $pid 2>/dev/null || break
        i=$(( (SECONDS - start) / 60 ))
        [ $i -ge 1 ] || continue
        shot=shots/$name-$(printf %03d $i).ppm
        [ -e "$shot" ] && continue
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

pause() { read -rt "$1" <> <(:) || true; }  # bash's own timer: no external sleep
echo ">> 2/4 Writing an answers file so first boot runs without the console wizard"
answers() {
    cat <<EOF
ROLE=manager
HOSTNAME=node1
ADDRESS=10.0.2.15/24
GATEWAY=10.0.2.2
DNS=10.0.2.3
ROOT_PASSWORD=Root-pw-123
ADMIN_PASSWORD=$ADMIN_PASSWORD
EOF
}
need() { echo "test -e $1 || { echo '!! $2 is missing'; exit 1; }"; }
if command -v virt-customize >/dev/null; then
# ibHiCS nodes have virt-customize (guestfs-tools), not always guestfish. libguestfs opens
# the disk in its own small VM: the host's LVM never sees the test disk's volume group
# (also called ibhics on an ibHiCS node), so nothing clashes and nothing is renamed.
answers >answers.conf
checks="$(need /opt/ibhics/venv/bin/ibhics-manager "ibHiCS in /opt/ibhics")
$(need /usr/share/virtio-win/viostor/2k19/amd64/viostor.sys "virtio-win drivers (Windows imports)")
$(need /var/lib/ibhics/tools/guest-tools.iso "the guest tools CD")
$(need /usr/share/virt-tools/rhsrvany.exe "rhsrvany.exe (virt-v2v)")
$(need /usr/share/virt-tools/pnp_wait.exe "pnp_wait.exe (virt-v2v)")
$(need /usr/bin/virt-customize "virt-customize (guestfs-tools)")
ls /usr/lib/*/nbdkit/plugins/nbdkit-ssh-plugin.so >/dev/null 2>&1 || { echo '!! nbdkit ssh plugin is missing'; exit 1; }
ls /usr/lib/*/nbdkit/plugins/nbdkit-curl-plugin.so >/dev/null 2>&1 || { echo '!! nbdkit curl plugin is missing'; exit 1; }
cat /etc/ibhics/version"
LIBGUESTFS_BACKEND=direct virt-customize -a disk.qcow2 --no-network \
    --run-command "$checks" \
    --upload answers.conf:/etc/ibhics/answers.conf \
    --chmod 0600:/etc/ibhics/answers.conf \
    || { rm -f answers.conf; echo "!! the installed disk failed its checks (see above)"; exit 1; }
rm -f answers.conf
elif command -v guestfish >/dev/null; then
# libguestfs opens the disk in its own small VM: the host's LVM never sees the test
# disk's volume group (also called ibhics on an ibHiCS node), so nothing clashes.
answers >answers.conf
out=$(LIBGUESTFS_BACKEND=direct guestfish --rw -a disk.qcow2 -i <<'GF'
is-file /opt/ibhics/venv/bin/ibhics-manager
is-file /usr/share/virtio-win/viostor/2k19/amd64/viostor.sys
is-file /var/lib/ibhics/tools/guest-tools.iso
is-file /usr/share/virt-tools/rhsrvany.exe
is-file /usr/share/virt-tools/pnp_wait.exe
is-file /usr/bin/virt-customize
glob-expand /usr/lib/*/nbdkit/plugins/nbdkit-ssh-plugin.so
glob-expand /usr/lib/*/nbdkit/plugins/nbdkit-curl-plugin.so
upload answers.conf /etc/ibhics/answers.conf
chmod 0600 /etc/ibhics/answers.conf
cat /etc/ibhics/version
GF
) || { echo "!! guestfish could not open the installed disk"; exit 1; }
rm -f answers.conf
mapfile -t found <<<"$out"
missing=("ibHiCS in /opt/ibhics" "virtio-win drivers (Windows imports)" "the guest tools CD"
         "rhsrvany.exe (virt-v2v)" "pnp_wait.exe (virt-v2v)" "virt-customize (guestfs-tools)")
for i in "${!missing[@]}"; do
    [ "${found[$i]}" = true ] || { echo "!! ${missing[$i]} is missing"; exit 1; }
done
for plugin in ssh curl; do
    grep -q "nbdkit-$plugin-plugin.so" <<<"$out" \
        || { echo "!! nbdkit $plugin plugin is missing: VMware imports would fail"; exit 1; }
done
grep -v -e '^true$' -e '^false$' -e nbdkit <<<"$out"  # the version file
else
sudo modprobe nbd max_part=16
sudo qemu-nbd -c /dev/nbd0 disk.qcow2
# The test VM's volume group is called ibhics, like the host's when the test runs on an
# ibHiCS node. LVM here sees only the test disk, and the VG gets a name of its own while
# mounted (its device-mapper names would clash with the host's), then its own name back.
TVG=ibhicstest
lvm_nbd() { sudo "$@" --config 'devices { filter = [ "a|^/dev/nbd0|", "r|.*|" ] use_devicesfile = 0 }'; }
put_back() {
    # The VG must be inactive before it gets its name back: active, its device-mapper
    # names (ibhics-root, ...) would clash with the host's, and the rename fails.
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        lvm_nbd vgchange -q -an "$TVG" >/dev/null 2>&1 && break
        pause 2
    done
    if lvm_nbd vgs "$TVG" >/dev/null 2>&1; then
        # Without its own name back the test VM cannot find its root at first boot.
        lvm_nbd vgrename -q "$TVG" ibhics \
            || { echo "!! could not rename the test disk's VG $TVG back to ibhics"; RENAME_FAILED=1; }
    fi
}
release_disk() {
    local i
    sync
    sudo umount -q mnt/var/lib/ibhics 2>/dev/null || true
    for i in 1 2 3 4 5; do
        mountpoint -q mnt || break
        sudo umount mnt 2>/dev/null && break
        pause 2
    done
    mountpoint -q mnt && { sudo umount -l mnt 2>/dev/null; pause 3; }
    put_back
    sudo qemu-nbd -d /dev/nbd0 >/dev/null 2>&1 || true
}
trap release_disk EXIT
pause 2
lvm_nbd vgrename -q ibhics "$TVG" || { echo "!! no ibhics volume group on the installed disk"; exit 1; }
lvm_nbd vgchange -q -ay "$TVG"
mkdir -p mnt
sudo mount "/dev/$TVG/root" mnt
test -x mnt/opt/ibhics/venv/bin/ibhics-manager || { echo "!! ibHiCS is not installed in /opt/ibhics"; exit 1; }
test -f mnt/usr/share/virtio-win/viostor/2k19/amd64/viostor.sys \
    || { echo "!! virtio-win drivers are missing: Windows imports would fail"; exit 1; }
# /var/lib/ibhics is its own volume (data), not part of root.
sudo mount "/dev/$TVG/data" mnt/var/lib/ibhics
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
answers | sudo tee mnt/etc/ibhics/answers.conf >/dev/null
sudo chmod 600 mnt/etc/ibhics/answers.conf
cat mnt/etc/ibhics/version
trap - EXIT
release_disk
[ -z "${RENAME_FAILED:-}" ] || exit 1
fi

echo ">> 3/4 First boot"
# SeaBIOS tries only the first hard disk (the big, empty one), as a server's firmware
# boots only the drive set in its boot order: point it at the drive we installed to.
BOOT_DISK=,bootindex=1
run_vm firstboot 30 -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:$PORT-:8888 &
VM=$!
trap 'monitor quit 2>/dev/null || true' EXIT
for _ in $(seq 60); do
    if curl -skf "$API/health" -o health.json; then break; fi
    # QEMU stops at once when it cannot forward the port (or fails otherwise).
    kill -0 "$VM" 2>/dev/null || { echo "!! the VM stopped during first boot (port $PORT taken?)"; exit 1; }
    pause 10
done
cat health.json 2>/dev/null || { echo "!! the web console did not answer on port $PORT"; exit 1; }
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
assert health["release"] == "ibHiCS Hyper-Converged Infrastructure beta version 1.3", health
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

assert call("GET", "/containers", token=token) == []
assert any(t["id"] == "app:nginx" for t in call("GET", "/containers/templates", token=token))
print("ok  LXC is installed and the containers API answers")

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
