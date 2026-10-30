#!/usr/bin/env bash
# Build the ibHiCS installer ISO from a Debian 13 (trixie) amd64 ISO.
#
#   installer/build-iso.sh                      # downloads the current Debian 13 netinst
#   installer/build-iso.sh --source debian.iso  # or use an ISO you already have
#
# Needs: xorriso, python3 with pip, curl. The result keeps Debian's BIOS and UEFI boot
# setup and adds the ibHiCS boot menu, the unattended-install answers and, so the
# install needs no PyPI access, ibHiCS with all its Python dependencies as wheels.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
MIRROR=${DEBIAN_CD_MIRROR:-https://cdimage.debian.org/debian-cd/current/amd64/iso-cd}
TARGET_PYTHON=3.13  # Debian 13's python3: the wheels must match it
SOURCE=""
OUT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --source) SOURCE=$2; shift 2 ;;
        --out) OUT=$2; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
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
python3 -m pip download --quiet --dest "$PAYLOAD/wheelhouse" \
    --only-binary=:all: --implementation cp --python-version "$TARGET_PYTHON" \
    --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 \
    --platform manylinux2014_x86_64 \
    "$PAYLOAD"/wheelhouse/ibhics-*.whl
cp "$HERE/preseed.cfg" "$HERE/install-ibhics.sh" "$PAYLOAD/"
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
