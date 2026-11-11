#!/bin/bash
# Upgrade web managers/agents only. Does not touch libvirt, networks or data volumes.
set -Eeuo pipefail
wheel=$(realpath "${1:?wheel required}")
venv=/opt/ibhics/venv
current=$("$venv/bin/python" -c 'import ibhics; print(ibhics.__version__)')
[[ $current == 1.3.0b2+history2 ]] || { echo "Unexpected installed release: $current"; exit 1; }
backup=/opt/ibhics/vstore1-backup-$(date +%Y%m%d-%H%M%S)
mkdir -p "$backup"
cp -a "$venv" "$backup/venv"
rollback() {
    trap - ERR
    systemctl stop ibhics-manager ibhics-agent || true
    mv "$venv" "$backup/failed-venv"
    mv "$backup/venv" "$venv"
    systemctl start ibhics-agent ibhics-manager
    exit 1
}
trap rollback ERR
systemctl stop ibhics-manager ibhics-agent
"$venv/bin/python" -m pip install --no-index --no-deps --force-reinstall "$wheel"
systemctl start ibhics-agent ibhics-manager
"$venv/bin/python" - <<'PY'
import json, ssl, time, urllib.request
for _ in range(15):
    try:
        with urllib.request.urlopen('https://127.0.0.1:8888/api/v1/health',
                                    context=ssl._create_unverified_context(), timeout=3) as r:
            data=json.load(r)
        if data.get('status')=='ok' and data.get('version')=='1.3.0b2+vstore1':
            print('Manager health passed:', data['version'])
            break
    except Exception:
        pass
    time.sleep(1)
else:
    raise SystemExit('Manager health check failed')
PY
systemctl is-active --quiet ibhics-agent
trap - ERR
printf 'Backup: %s\n' "$backup"
