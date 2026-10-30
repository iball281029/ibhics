#!/bin/sh
# Upgrade an installed ibHiCS node in place from this ISO, like a firmware upgrade:
#
#   mount -o loop,ro ibhics-0.2.0b1.iso /mnt && sh /mnt/ibhics/upgrade.sh
#
# VMs keep running. Kept as they are: /etc/ibhics (config, TLS, users), /var/lib/ibhics,
# libvirt VMs, storage pools and networks, /root/.ssh. Replaced: the ibHiCS program in
# /opt/ibhics/venv, its systemd units, the virtio-win drivers and the guest tools CD.
# Debian packages the new version needs come from the ISO; nothing is downloaded.
#
# Before changing anything it saves a backup under /var/backups/ibhics/. If the new
# version does not answer on its health check, the old one is put back by itself; to go
# back by hand later, run the rollback.sh in that backup.
set -eu
SRC=$(cd "$(dirname "$0")" && pwd)
NEW=$(cat "$SRC/VERSION")
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=/var/backups/ibhics/upgrade-$STAMP
VENV=/opt/ibhics/venv
SERVICES="ibhics-manager ibhics-agent"
export DEBIAN_FRONTEND=noninteractive

say() { printf '\n>> %s\n' "$*"; }
die() { printf '\n!! %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run this as root"
[ -x "$VENV/bin/python" ] || die "no ibHiCS in $VENV: install from the ISO instead"
[ -d "$SRC/wheelhouse" ] || die "$SRC is not an ibHiCS ISO payload"
OLD=$(cat /etc/ibhics/version 2>/dev/null || "$VENV/bin/python" -c \
    "import ibhics; print(ibhics.__version__)")
say "Upgrading ibHiCS $OLD to $NEW"

active=""
for unit in $SERVICES; do
    if systemctl is-active --quiet "$unit"; then active="$active $unit"; fi
done

say "Saving a backup in $BACKUP"
mkdir -p "$BACKUP/libvirt" "$BACKUP/systemd"
cp /etc/systemd/system/ibhics-*.service "$BACKUP/systemd/" 2>/dev/null || true
# Settings and state, not VM disks: pools and uploads stay where they are.
tar -C / -czf "$BACKUP/state.tar.gz" --ignore-failed-read \
    --exclude=var/lib/ibhics/pools --exclude=var/lib/ibhics/uploads \
    --exclude=var/lib/ibhics/scratch \
    etc/ibhics var/lib/ibhics root/.ssh
for vm in $(virsh list --all --uuid); do
    virsh dumpxml --inactive "$vm" > "$BACKUP/libvirt/vm-$vm.xml"
done
for pool in $(virsh pool-list --all --name); do
    virsh pool-dumpxml "$pool" > "$BACKUP/libvirt/pool-$pool.xml"
done
for net in $(virsh net-list --all --name); do
    virsh net-dumpxml "$net" > "$BACKUP/libvirt/net-$net.xml"
done
echo "$OLD" > "$BACKUP/version"
cat > "$BACKUP/rollback.sh" <<EOF
#!/bin/sh
# Put back ibHiCS $OLD as it was before the upgrade of $STAMP. VMs and pools are not
# touched; their definitions from that time are in libvirt/ if ever needed.
set -eu
systemctl stop $SERVICES 2>/dev/null || true
[ -d $VENV.prev ] || { echo "no $VENV.prev to go back to" >&2; exit 1; }
rm -rf $VENV.failed && mv $VENV $VENV.failed && mv $VENV.prev $VENV
cp $BACKUP/systemd/*.service /etc/systemd/system/ 2>/dev/null || true
echo $OLD > /etc/ibhics/version
systemctl daemon-reload
for unit in $active; do systemctl start "\$unit"; done
echo "ibHiCS is back to $OLD"
EOF
chmod +x "$BACKUP/rollback.sh"

# Only what is missing: packages already there stay at their version, so libvirt and
# QEMU are not restarted under running VMs.
missing=$(grep -v '^[[:space:]]*#' "$SRC/packages.txt" | while read -r pkg; do
    [ -n "$pkg" ] && ! dpkg -s "$pkg" >/dev/null 2>&1 && echo "$pkg"
done || true)
if [ -n "$missing" ]; then
    say "Installing the Debian packages $NEW needs:" $missing
    LIST=$(mktemp)
    if [ -s "$SRC/repo/Packages.gz" ] || [ -s "$SRC/repo/Packages" ]; then
        echo "deb [trusted=yes] file:$SRC/repo ./" > "$LIST"   # from the ISO, offline
        APT="-o Dir::Etc::sourcelist=$LIST -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0"
    else
        echo "   (no package repository beside this script: using the node's apt sources)"
        APT=""
    fi
    # shellcheck disable=SC2086
    if ! { apt-get $APT update -qq && apt-get $APT install -y --no-install-recommends $missing; }
    then
        [ -n "$APT" ] || die "could not install: $missing"
        echo "   (not all of them are on the ISO; trying the node's apt sources)"
        # shellcheck disable=SC2086
        apt-get update -qq && apt-get install -y --no-install-recommends $missing \
            || die "could not install: $missing"
    fi
    rm -f "$LIST"
fi

say "Installing ibHiCS $NEW beside the running version"
rm -rf "$VENV.new"
python3 -m venv --system-site-packages "$VENV.new"
"$VENV.new/bin/pip" install --quiet --no-index --find-links "$SRC/wheelhouse" ibhics
# venv scripts name their interpreter by path; point them at the final place.
sed -i "1s|^#!$VENV.new/|#!$VENV/|" "$VENV.new"/bin/*
sed -i "s|$VENV.new|$VENV|g" "$VENV.new/bin/activate" "$VENV.new/pyvenv.cfg" 2>/dev/null || true

say "Switching over (VMs keep running)"
# shellcheck disable=SC2086
systemctl stop $SERVICES 2>/dev/null || true
rm -rf "$VENV.prev"
mv "$VENV" "$VENV.prev"
mv "$VENV.new" "$VENV"
install -m 0644 "$SRC"/systemd/*.service /etc/systemd/system/
if [ -d "$SRC/virtio-win" ]; then
    # Added over what is there: a virtio-win.iso someone put in by hand stays.
    mkdir -p /usr/share/virtio-win
    cp -r "$SRC/virtio-win/." /usr/share/virtio-win/
fi
if [ -f "$SRC/guest-tools.iso" ]; then
    install -D -m 0644 "$SRC/guest-tools.iso" /usr/share/ibhics/guest-tools.iso
fi
for tool in ibhics-manager ibhics-agent ibhics-setup; do
    ln -sf "$VENV/bin/$tool" "/usr/local/sbin/$tool"
done
echo "$NEW" > /etc/ibhics/version
systemctl daemon-reload
for unit in $active; do systemctl start "$unit"; done

healthy() {
    case "$active" in
        *ibhics-manager*) ;;
        *) for unit in $active; do systemctl is-active --quiet "$unit" || return 1; done
           return 0 ;;
    esac
    PORT=$(sed -n 's/^IBHICS_PORT=//p' /etc/ibhics/ibhics.env 2>/dev/null)
    "$VENV/bin/python" - "${PORT:-8888}" "$NEW" <<'PY'
import json, ssl, sys, urllib.request
ctx = ssl.create_default_context()
ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # the node's own certificate
for scheme in ("https", "http"):
    try:
        with urllib.request.urlopen(
            f"{scheme}://127.0.0.1:{sys.argv[1]}/api/v1/health", context=ctx, timeout=5
        ) as resp:
            sys.exit(0 if json.load(resp)["version"] == sys.argv[2] else 1)
    except Exception:
        continue
sys.exit(1)
PY
}

say "Checking that ibHiCS $NEW answers"
tries=0
until healthy; do
    tries=$((tries + 1))
    if [ "$tries" -ge 30 ]; then
        printf '\n!! ibHiCS %s did not come up; putting %s back\n' "$NEW" "$OLD" >&2
        journalctl -u ibhics-manager -n 30 --no-pager >&2 || true
        sh "$BACKUP/rollback.sh"
        exit 1
    fi
    sleep 2
done

say "Done: ibHiCS $NEW is running. VMs:"
virsh list --all
echo "Backup and rollback: $BACKUP (sh $BACKUP/rollback.sh goes back to $OLD)"
