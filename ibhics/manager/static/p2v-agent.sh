#!/bin/bash
# ibHiCS P2V agent: copies this Linux machine's disks to an ibHiCS cluster, which turns
# them into a VM. It only reads this machine; nothing here is changed or installed.
#
#   curl -fsS --pinnedpubkey 'sha256//PIN' -k https://MANAGER:8888/p2v-agent.sh |
#     sudo bash -s -- --server https://MANAGER:8888 --session ID --token TOKEN --pin PIN
#
# The copy is taken while the system runs. Stop databases and busy services first (or
# boot this machine from a live USB and run the agent there) for a clean copy.
set -euo pipefail

server="" session="" token="" pin="" yes=0 only=""
while [ $# -gt 0 ]; do
    case "$1" in
        --server) server=$2; shift 2;;
        --session) session=$2; shift 2;;
        --token) token=$2; shift 2;;
        --pin) pin=$2; shift 2;;
        --disks) only=$2; shift 2;;  # e.g. "sda nvme0n1": copy only these
        --yes) yes=1; shift;;
        *) echo "unknown option: $1" >&2; exit 2;;
    esac
done
[ -n "$server" ] && [ -n "$session" ] && [ -n "$token" ] || {
    echo "usage: --server URL --session ID --token TOKEN [--pin SHA256] [--disks 'sda sdb'] [--yes]" >&2
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
while read -r name size type; do
    [ "$type" = disk ] || continue
    case "$name" in loop*|ram*|zram*|sr*|fd*) continue;; esac
    if [ -n "$only" ] && ! grep -qw "$name" <<<"$only"; then continue; fi
    [ "$size" -ge 1048576 ] || continue
    disks+=("$name:$size")
done < <(lsblk -b -d -n -o NAME,SIZE,TYPE)
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
