#!/bin/bash
# Debian 13 build host. Downloads a full dependency closure; installs nothing on host.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?output directory required}
mkdir -p "$OUT/partial"
apt-get -o Dir::State::status=/dev/null -o Dir::Cache::archives="$OUT" \
    --download-only --no-install-recommends -y install $(cat "$HERE/packages.txt")
python3 - "$OUT" <<'PY'
import gzip, pathlib, subprocess, sys
out = pathlib.Path(sys.argv[1])
records = []
for deb in sorted(out.glob('*.deb')):
    record = subprocess.check_output(['dpkg-deb', '-f', str(deb)], text=True).rstrip()
    import hashlib
    records.append(record + '\nFilename: ./' + deb.name + '\nSize: ' + str(deb.stat().st_size)
                   + '\nSHA256: ' + hashlib.sha256(deb.read_bytes()).hexdigest() + '\n')
with gzip.open(out / 'Packages.gz', 'wt') as f:
    f.write('\n'.join(records))
print(len(records), 'offline packages')
PY
