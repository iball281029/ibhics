#!/bin/bash
# Application-only upgrade. Does not stop libvirt, reboot hosts or create storage.
set -Eeuo pipefail
wheel=$(realpath "${1:?wheel required}")
role=${2:?compute or witness required}
[[ $role == compute || $role == witness ]] || exit 2
venv=/opt/ibhics/venv
current=$("$venv/bin/python" -c 'import ibhics; print(ibhics.__version__)')
case "$current" in
    1.3.0b2+vstore3.1|1.3.0b2+vstore3|1.3.0b2+vstore2|1.3.0b2+vstore1|1.3.0b2+arbiterfix1) ;;
    *) echo "Unexpected installed release: $current"; exit 1;;
esac
backup=/opt/ibhics/vstore3.2-backup-$(date +%Y%m%d-%H%M%S)
mkdir -m 700 "$backup"
cp -a "$venv" "$backup/venv"
services=(ibhics-agent)
if [[ $role == compute ]]; then services+=(ibhics-manager); fi
if [[ $(systemctl show -p LoadState --value ibhics-arbiter-ui) == loaded ]]; then
    services+=(ibhics-arbiter-ui)
fi
rollback() {
    trap - ERR
    systemctl stop "${services[@]}" || true
    mv "$venv" "$backup/failed-venv"
    mv "$backup/venv" "$venv"
    systemctl start "${services[@]}"
    echo "Application upgrade rolled back; backup: $backup"
    exit 1
}
trap rollback ERR
systemctl stop "${services[@]}"
"$venv/bin/python" -m pip install --no-index --no-deps --force-reinstall "$wheel"
systemctl start "${services[@]}"
"$venv/bin/python" - "$role" <<'PY'
import json, ssl, sys, time, urllib.request
from pathlib import Path
env = {line.split('=', 1)[0]: line.split('=', 1)[1].strip().strip('"\'')
       for line in Path('/etc/ibhics/ibhics.env').read_text().splitlines()
       if '=' in line and not line.startswith('#')}
token = env['IBHICS_CLUSTER_TOKEN']
host = env.get('IBHICS_AGENT_HOST', '0.0.0.0')
if host == '0.0.0.0': host = '127.0.0.1'
agent_url = 'http://' + host + ':' + env.get('IBHICS_AGENT_PORT', '8007') + '/agent/v1/health'
context = ssl._create_unverified_context()
for _ in range(20):
    try:
        req = urllib.request.Request(agent_url, headers={'X-Ibhics-Token': token})
        with urllib.request.urlopen(req, timeout=3) as response:
            agent = json.load(response)
        assert agent['version'] == '1.3.0b2+vstore3.2'
        if sys.argv[1] == 'compute':
            with urllib.request.urlopen('https://127.0.0.1:8888/api/v1/health',
                                        context=context, timeout=3) as response:
                manager = json.load(response)
            assert manager['status'] == 'ok' and manager['version'] == agent['version']
        print('Application health passed:', agent['name'], agent['role'], agent['version'])
        break
    except Exception:
        time.sleep(.5)
else:
    raise SystemExit('Application health check failed')
PY
systemctl is-active --quiet "${services[@]}"
trap - ERR
printf 'Application backup: %s\n' "$backup"
