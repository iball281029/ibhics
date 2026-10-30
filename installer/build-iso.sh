#!/usr/bin/env bash
# Build the ibHiCS installer ISO from a Debian 13 (trixie) amd64 ISO.
#
#   installer/build-iso.sh                      # downloads the current Debian 13 netinst
#   installer/build-iso.sh --source debian.iso  # or use an ISO you already have
#   installer/build-iso.sh --repo DIR           # reuse a package repo from build-repo.sh
#   installer/build-iso.sh --no-virtio-win      # leave out the Windows virtio drivers
#
# Needs: xorriso, python3 with pip, curl, and docker (for the package repository). The
# result keeps Debian's BIOS and UEFI boot setup and adds the ibHiCS boot menu, the
# unattended-install answers, every Debian package a node needs (a local apt repository)
# and ibHiCS with its Python dependencies as wheels, so installing needs no network.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
MIRROR=${DEBIAN_CD_MIRROR:-https://cdimage.debian.org/debian-cd/current/amd64/iso-cd}
TARGET_PYTHON=3.13  # Debian 13's python3: the wheels must match it
VIRTIO_WIN_URL=${VIRTIO_WIN_URL:-https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso}
NO_VIRTIO_WIN=""
SOURCE=""
OUT=""
DEBREPO=""

while [ $# -gt 0 ]; do
    case "$1" in
        --source) SOURCE=$2; shift 2 ;;
        --out) OUT=$2; shift 2 ;;
        --repo) DEBREPO=$(realpath "$2"); shift 2 ;;
        --no-virtio-win) NO_VIRTIO_WIN=1; shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

for tool in xorriso python3 curl; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

VERSION=$(python3 -c "import tomllib,sys; print(tomllib.load(open(sys.argv[1],'rb'))['project']['version'])" "$REPO/pyproject.toml")
SHORT=${VERSION%.*}
OUT=${OUT:-$REPO/dist/ibhics-$SHORT-beta-amd64.iso}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

if [ -z "$SOURCE" ]; then
    echo ">> Finding the current Debian 13 netinst ISO"
    curl -fsSL "$MIRROR/SHA512SUMS" -o "$WORK/SHA512SUMS"
    NAME=$(awk '$2 ~ /^debian-13[0-9.]*-amd64-netinst\.iso$/ {print $2; exit}' "$WORK/SHA512SUMS")
    [ -n "$NAME" ] || { echo "no Debian 13 netinst ISO listed at $MIRROR" >&2; exit 1; }
    echo ">> Downloading $NAME"
    curl -fL --retry 3 -o "$WORK/$NAME" "$MIRROR/$NAME"
    (cd "$WORK" && grep " $NAME\$" SHA512SUMS | sha512sum -c -)
    SOURCE=$WORK/$NAME
fi

xorriso -osirrox on -indev "$SOURCE" -extract /.disk/info "$WORK/disk-info" >/dev/null 2>&1 || true
if ! grep -qs "Debian GNU/Linux 13" "$WORK/disk-info"; then
    echo "warning: $SOURCE does not look like a Debian 13 ISO ($(cat "$WORK/disk-info" 2>/dev/null))" >&2
fi

echo ">> Building ibHiCS $VERSION and collecting its dependencies for Python $TARGET_PYTHON"
PAYLOAD=$WORK/ibhics
mkdir -p "$PAYLOAD/wheelhouse" "$PAYLOAD/systemd"
python3 -m pip wheel --quiet --no-deps --wheel-dir "$PAYLOAD/wheelhouse" "$REPO"
# The web console needs noVNC's pako, which a stray ignore rule once left out.
python3 -m zipfile -l "$PAYLOAD"/wheelhouse/ibhics-*.whl \
    | grep -q 'static/novnc/vendor/pako/lib/zlib/inflate.js' \
    || { echo "!! the wheel is missing the web console's files" >&2; exit 1; }
python3 -m pip download --quiet --dest "$PAYLOAD/wheelhouse" \
    --only-binary=:all: --implementation cp --python-version "$TARGET_PYTHON" \
    --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 \
    --platform manylinux2014_x86_64 \
    "$PAYLOAD"/wheelhouse/ibhics-*.whl
cp "$HERE/preseed.cfg" "$HERE/install-ibhics.sh" "$HERE/packages.txt" "$PAYLOAD/"

if [ -z "$DEBREPO" ]; then
    echo ">> Downloading the Debian packages a node needs (in a debian:trixie container)"
    command -v docker >/dev/null || { echo "missing tool: docker (or pass --repo)" >&2; exit 1; }
    DEBREPO=$WORK/repo
    mkdir -p "$DEBREPO"
    docker run --rm -v "$REPO:/src:ro" -v "$DEBREPO:/out" debian:trixie \
        bash /src/installer/build-repo.sh /src /out
fi
[ -s "$DEBREPO/Packages.gz" ] || { echo "no Packages.gz in $DEBREPO" >&2; exit 1; }
cp -r "$DEBREPO" "$PAYLOAD/repo"

# Windows guests need the virtio drivers when virt-v2v converts them. Debian does not
# package them, so the drivers for current 64-bit Windows come from the virtio-win ISO.
if [ -z "$NO_VIRTIO_WIN" ]; then
    echo ">> Adding the virtio-win drivers for Windows guests"
    curl -fL --retry 3 -o "$WORK/virtio-win.iso" "$VIRTIO_WIN_URL"
    mkdir -p "$WORK/virtio-all"
    xorriso -osirrox on -indev "$WORK/virtio-win.iso" -extract / "$WORK/virtio-all" >/dev/null 2>&1
    chmod -R u+w "$WORK/virtio-all"
    python3 "$HERE/prune-virtio-win.py" "$WORK/virtio-all" "$PAYLOAD/virtio-win"
    # The guest tools CD that "Install guest tools" puts in a VM's drive.
    mkdir -p "$WORK/tools/linux"
    cp -r "$PAYLOAD/virtio-win/." "$WORK/tools/"
    cp "$HERE/guest-tools/install.sh" "$WORK/tools/linux/install.sh"
    cp "$HERE/guest-tools/README.txt" "$WORK/tools/README.txt"
    xorriso -as mkisofs -quiet -J -R -V IBHICS_TOOLS -o "$PAYLOAD/guest-tools.iso" "$WORK/tools"
fi
cp "$HERE"/systemd/*.service "$PAYLOAD/systemd/"
echo "$VERSION" > "$PAYLOAD/VERSION"

echo ">> Writing $OUT"
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
xorriso -indev "$SOURCE" -outdev "$OUT" \
    -boot_image any replay \
    -map "$PAYLOAD" /ibhics \
    -map "$HERE/boot/isolinux.cfg" /isolinux/isolinux.cfg \
    -map "$HERE/boot/grub.cfg" /boot/grub/grub.cfg \
    2>&1 | grep -E "^xorriso : (FAILURE|SORRY|WARNING)" >&2 || true
[ -s "$OUT" ] || { echo "xorriso did not write $OUT" >&2; exit 1; }
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
echo ">> Done: $OUT ($(du -h "$OUT" | cut -f1))"
