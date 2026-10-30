#!/bin/bash
# Download installer/packages.txt and all their dependencies into a local apt repository.
# Runs inside a debian:trixie container (build-iso.sh starts it):
#   build-repo.sh SRC_DIR OUT_DIR
set -euo pipefail
SRC=$1
OUT=$2
PKGS=$(grep -v '^\s*#' "$SRC/installer/packages.txt" | xargs)

sed -i 's/^Components: main$/Components: main contrib non-free non-free-firmware/' \
    /etc/apt/sources.list.d/debian.sources
apt-get update -q
apt-get install -y -q --no-install-recommends dpkg-dev >/dev/null

# An empty dpkg status makes apt treat nothing as installed, so every dependency is
# downloaded, including newer versions of base packages than the ISO carries.
touch /tmp/empty-status
mkdir -p /tmp/archives/partial
apt-get install -y -q --download-only --no-install-recommends \
    -o Dir::State::status=/tmp/empty-status -o Dir::Cache::archives=/tmp/archives $PKGS

mkdir -p "$OUT"
mv /tmp/archives/*.deb "$OUT/"
cd "$OUT"
dpkg-scanpackages --multiversion . /dev/null 2>/dev/null > Packages
gzip -9 -k Packages
echo ">> $(ls *.deb | wc -l) packages, $(du -sh . | cut -f1)"
