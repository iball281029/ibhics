#!/usr/bin/env bash
# Install the reviewed home2 wheel locally on an ibHiCS development server.
# Run as root; pass the wheel path as the only argument. No internet access required.
set -Eeuo pipefail

[[ $(id -u) == 0 ]] || { echo "Run this installer as root." >&2; exit 1; }
wheel=${1:?Usage: install-home-metrics.sh /path/to/ibhics-1.3.0b2+home2-py3-none-any.whl}
wheel=$(realpath "$wheel")
venv=${IBHICS_DEPLOY_VENV:-/opt/ibhics/venv}
health_url=${IBHICS_DEPLOY_HEALTH_URL:-https://127.0.0.1:8888/api/v1/health}
[[ -f $wheel && -x $venv/bin/python ]] || { echo "Wheel or installed environment missing." >&2; exit 1; }
systemctl is-active --quiet ibhics-manager
current=$("$venv/bin/python" -c 'import ibhics; print(ibhics.__version__)')
[[ $current == 1.3.0b2+home1 ]] || { echo "Expected 1.3.0b2+home1; found $current. Review the installed version first." >&2; exit 1; }

backup=/opt/ibhics/home2-backup-$(date +%Y%m%d-%H%M%S)
mkdir -p "$backup"
cp -a "$venv" "$backup/venv"
printf '%s\n' "$venv" > "$backup/venv-path.txt"

rollback() {
    trap - ERR
    set +e
    echo "Installation failed; restoring $backup/venv" >&2
    systemctl stop ibhics-manager
    mv "$venv" "$backup/failed-venv"
    mv "$backup/venv" "$venv"
    systemctl start ibhics-manager
    exit 1
}
trap rollback ERR

systemctl stop ibhics-manager
"$venv/bin/python" -m pip install --no-index --no-deps --force-reinstall "$wheel"
systemctl start ibhics-manager
"$venv/bin/python" - "$health_url" <<'PY'
import json
import ssl
import sys
import time
import urllib.request

for attempt in range(15):
    try:
        with urllib.request.urlopen(
            sys.argv[1], context=ssl._create_unverified_context(), timeout=3
        ) as response:
            health = json.load(response)
        if health.get("status") == "ok" and health.get("version") == "1.3.0b2+home2":
            print("Health check passed:", health["version"])
            break
    except Exception:
        pass
    time.sleep(1)
else:
    raise SystemExit("The upgraded manager did not pass its health check.")
PY
trap - ERR
echo "Installed. Backup: $backup"
echo "Refresh the console and sign in again. Existing VMs keep running."
echo "To roll back later: stop ibhics-manager, move $venv aside, restore $backup/venv to $venv, then start ibhics-manager."
