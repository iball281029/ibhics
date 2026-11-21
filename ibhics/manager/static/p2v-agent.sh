#!/bin/bash
# ibHiCS P2V agent: copies this Linux machine's disks to an ibHiCS cluster, which turns
# them into a VM. --replicate may create and remove owned temporary LVM snapshots.
#
#   curl -fsS --pinnedpubkey 'sha256//PIN' -k https://MANAGER:8888/p2v-agent.sh |
#     sudo bash -s -- --server https://MANAGER:8888 --session ID --token TOKEN --pin PIN
#
# The copy is taken while the system runs. Stop databases and busy services first (or
# boot this machine from a live USB and run the agent there) for a clean copy.
set -euo pipefail

server="" session="" token="" pin="" yes=0 only="" replicate=0
while [ $# -gt 0 ]; do
    case "$1" in
        --server) server=$2; shift 2;;
        --session) session=$2; shift 2;;
        --token) token=$2; shift 2;;
        --pin) pin=$2; shift 2;;
        --disks) only=$2; shift 2;;  # e.g. "sda nvme0n1": copy only these
        --replicate) replicate=1; shift;;
        --yes) yes=1; shift;;
        *) echo "unknown option: $1" >&2; exit 2;;
    esac
done
[ -n "$server" ] && [ -n "$session" ] && [ -n "$token" ] || {
    echo "usage: --server URL --session ID --token TOKEN [--pin SHA256] [--disks 'sda sdb'] [--yes] [--replicate]" >&2
    exit 2
}
[ "$(id -u)" = 0 ] || { echo "Run as root (sudo): disks can only be read by root." >&2; exit 1; }
for tool in curl gzip dd lsblk; do
    command -v "$tool" >/dev/null || { echo "Missing $tool; install it first." >&2; exit 1; }
done

tls=(-fsS)
if [ -n "$pin" ]; then tls+=(-k --pinnedpubkey "sha256//$pin"); else tls+=(-k); fi
api="$server/api/v1/p2v/upload/$session"
auth=(-H "X-P2V-Token: $token")

json_escape() { sed 's/\\/\\\\/g; s/"/\\"/g' <<<"$1"; }

disks=()
if [ "$replicate" = 1 ] && [ -n "$only" ]; then
    # Explicit logical volumes are supported; whole disks cannot use an LV snapshot.
    for name in $only; do
        [[ "$name" =~ ^[A-Za-z0-9._/-]+$ ]] && [[ "$name" != *..* ]] || exit 2
        name=${name#/dev/}
        size=$(blockdev --getsize64 "/dev/$name")
        disks+=("$name:$size")
    done
else
while read -r name size type; do
    [ "$type" = disk ] || continue
    case "$name" in loop*|ram*|zram*|sr*|fd*) continue;; esac
    if [ -n "$only" ] && ! grep -qw "$name" <<<"$only"; then continue; fi
    [ "$size" -ge 1048576 ] || continue
    disks+=("$name:$size")
done < <(lsblk -b -d -n -o NAME,SIZE,TYPE)
fi
[ ${#disks[@]} -gt 0 ] || { echo "No disks found to copy." >&2; exit 1; }

hostname=$(hostname -s | tr -c 'A-Za-z0-9._-\n' '-')
cpus=$(nproc)
memory_mb=$(( $(awk '/^MemTotal:/ {print $2}' /proc/meminfo) / 1024 ))
firmware=bios; [ -d /sys/firmware/efi ] && firmware=uefi
os=$( . /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-Linux}" || echo Linux)
macs=""
for nic in /sys/class/net/*; do
    [ "${nic##*/}" = lo ] && continue
    [ -e "$nic/device" ] || continue  # physical ports only, not bridges or VPNs
    macs+="\"$(cat "$nic/address")\","
done
disk_json=""
for d in "${disks[@]}"; do disk_json+="{\"name\":\"${d%%:*}\",\"size_bytes\":${d##*:}},"; done

echo "ibHiCS P2V: $hostname · $cpus CPU · $memory_mb MB RAM · $firmware · $os"
for d in "${disks[@]}"; do echo "  disk ${d%%:*}: $(( ${d##*:} / 1024 / 1024 / 1024 )) GiB"; done
echo "The disks are read while the system runs: stop busy services first for a clean copy."
if [ "$yes" != 1 ]; then
    read -r -p "Copy these disks to $server? [y/N] " answer </dev/tty
    [ "$answer" = y ] || [ "$answer" = Y ] || { echo "Cancelled."; exit 1; }
fi

body="{\"hostname\":\"$(json_escape "$hostname")\",\"cpus\":$cpus,\"memory_mb\":$memory_mb,"
body+="\"firmware\":\"$firmware\",\"os\":\"$(json_escape "$os")\",\"macs\":[${macs%,}],\"disks\":[${disk_json%,}]}"
if [ "$replicate" = 1 ]; then
    command -v python3 >/dev/null || { echo "--replicate requires python3." >&2; exit 1; }
    [ -n "$pin" ] && [[ "$server" == https://* ]] || { echo "--replicate requires HTTPS and --pin." >&2; exit 2; }
    export IBHICS_P2V_API="$api" IBHICS_P2V_TOKEN="$token" IBHICS_P2V_PIN="$pin" IBHICS_P2V_MACHINE="$body"
    python3 - "${disks[@]}" <<'PYDELTA'
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import uuid

CHUNK = 4 << 20
api = os.environ["IBHICS_P2V_API"]
auth = os.environ["IBHICS_P2V_TOKEN"]
pin = os.environ["IBHICS_P2V_PIN"]
machine = json.loads(os.environ["IBHICS_P2V_MACHINE"])
sources, snapshots = [], []

# Ensure termination also unwinds owned LVM snapshots.
def terminate(signum, frame):
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, terminate)

def run(args):
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True).strip()

def request(method, path, data=None):
    result = subprocess.run(
        ["curl", "-fsS", "-k", "--pinnedpubkey", "sha256//" + pin,
         "-H", "X-P2V-Token: " + auth, "-H", "Content-Type: application/json",
         "-X", method, "--data-binary", "@-", api + path],
        input=data or b"", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError("manager request failed; no round was committed")
    return json.loads(result.stdout)

def hashes(path, size):
    result = []
    with open(path, "rb", buffering=0) as source:
        for offset in range(0, size, CHUNK):
            length = min(CHUNK, size - offset)
            data = source.read(length)
            if len(data) != length:
                raise RuntimeError("short disk read")
            result.append(hashlib.sha256(data).hexdigest())
    return result

try:
    for i, entry in enumerate(sys.argv[1:]):
        name, size = entry.rsplit(":", 1)
        path = "/dev/" + name
        machine["disks"][i]["name"] = name.replace("/", "-")
        # Snapshot only an explicitly selected LV, never pretend the physical disk
        # containing LVs is covered by a volume snapshot.
        try:
            vg = run(["lvs", "--noheadings", "-o", "vg_name", path])
        except (OSError, subprocess.CalledProcessError):
            vg = ""
        if vg:
            snap = "ibhics-repl-" + uuid.uuid4().hex
            target = "/dev/" + vg + "/" + snap
            free = float(run(["vgs", "--noheadings", "--units", "b", "--nosuffix", "-o", "vg_free", vg]))
            if free < (1 << 30):
                raise RuntimeError("LVM snapshot requires 1 GiB free in its VG")
            snapshots.append(target)  # Also clean up if lvcreate partially fails.
            run(["lvcreate", "--snapshot", "--size", "1G", "--name", snap, path])
            path = target
            print("Using LVM snapshot for " + name, flush=True)
        else:
            print("WARNING: " + name + " is read LIVE; crash-consistent only, quiesce writers.", file=sys.stderr)
        sources.append((path, int(size)))
    values = [hashes(path, size) for path, size in sources]
    plan = request("POST", "/delta/begin", json.dumps({"machine": machine, "hashes": values}).encode())
    if plan.get("chunk_size") != CHUNK or not re.fullmatch(r"[0-9a-f]{32}", plan.get("round", "")):
        raise RuntimeError("invalid delta plan")
    if len(plan["needed"]) != len(sources):
        raise RuntimeError("invalid delta disk count")
    base = "/delta/" + plan["round"]
    for i, (path, size) in enumerate(sources):
        with open(path, "rb", buffering=0) as source:
            for chunk in plan["needed"][i]:
                if type(chunk) is not int or not 0 <= chunk < len(values[i]):
                    raise RuntimeError("invalid requested chunk")
                source.seek(chunk * CHUNK)
                data = source.read(min(CHUNK, size - chunk * CHUNK))
                if hashlib.sha256(data).hexdigest() != values[i][chunk]:
                    raise RuntimeError("source changed; retry with quiesced writers")
                request("PUT", base + "/disks/%d/chunks/%d" % (i, chunk), data)
    # Detect changes to skipped chunks too. Live reads remain crash-consistent only.
    if values != [hashes(path, size) for path, size in sources]:
        raise RuntimeError("source changed during round; no commit, retry")
    for target in snapshots:
        attr = run(["lvs", "--noheadings", "-o", "lv_attr", target])
        if len(attr) < 5 or attr[4] != "a":
            raise RuntimeError("LVM snapshot invalid or overflowed; no commit")
    # All reads are finished. Remove snapshots before publishing the manager round.
    for target in list(reversed(snapshots)):
        run(["lvremove", "--force", target])
        snapshots.remove(target)
    result = request("POST", base + "/commit")
    print("Replication round committed: " + result["generation"])
except Exception:
    # Do not print commands, request bodies, or credentials.
    print("Replication failed; inspect disk stability, snapshot space and manager status.", file=sys.stderr)
    sys.exit(1)
finally:
    for target in reversed(snapshots):
        try:
            run(["lvremove", "--force", target])
        except (OSError, subprocess.CalledProcessError):
            print("Snapshot cleanup failed: " + target, file=sys.stderr)
PYDELTA
    exit $?
fi
curl "${tls[@]}" "${auth[@]}" -H "Content-Type: application/json" -X POST \
    --data-binary "$body" "$api/inventory" >/dev/null
sync

index=0
for d in "${disks[@]}"; do
    name=${d%%:*}
    echo "Copying /dev/$name ($(( ${d##*:} / 1024 / 1024 / 1024 )) GiB)…"
    dd if="/dev/$name" bs=4M status=progress iflag=fullblock | gzip -1 |
        curl "${tls[@]}" "${auth[@]}" -H "Content-Type: application/octet-stream" \
            -H "Content-Encoding: gzip" -X PUT -T - "$api/disks/$index" >/dev/null
    index=$((index + 1))
done
curl "${tls[@]}" "${auth[@]}" -X POST "$api/finish" >/dev/null
echo "Done. In ibHiCS open Migrate → Physical machines and import $hostname."
