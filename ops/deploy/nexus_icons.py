"""Guarded CSS-only icon alignment hotfix. No service restart or state migration."""
import base64
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

PAYLOAD = None  # substituted by the release builder
ROOT = Path('/opt/ibhics')
RELATIVE = 'manager/static/spatial.css'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(target, data, mode):
    temp = target.with_name(target.name+'.icons-'+uuid.uuid4().hex)
    try:
        with temp.open('xb') as stream:
            stream.write(data)
        temp.chmod(mode)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def main(payload, preflight=False):
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise RuntimeError('Run as root on the Linux compute node')
    if payload is None:
        raise RuntimeError('Use the generated standalone installer')
    venv = ROOT/'venv'
    result = subprocess.run([str(venv/'bin/python'),'-c',
        'import pathlib,ibhics;print(pathlib.Path(ibhics.__file__).parent)'],
        capture_output=True,text=True,check=True,timeout=15)
    package = Path(result.stdout.strip())
    if not package.resolve().is_relative_to(venv.resolve()):
        raise RuntimeError('Installed package is outside expected venv')
    target = package/RELATIVE
    if target.is_symlink() or not target.resolve().is_relative_to(package.resolve()):
        raise RuntimeError('Unexpected symlink in target')
    original = target.read_bytes()
    previous = sha(original)
    if previous not in payload['expected']:
        raise RuntimeError('CSS differs from reviewed release; no update made')
    data = base64.b64decode(payload['data'])
    if sha(data) != payload['sha256']:
        raise RuntimeError('Payload checksum mismatch')
    if preflight:
        print('NEXUS_ICONS_PREFLIGHT_OK',flush=True)
        return
    os.umask(0o077)
    with (ROOT/'.backup-spatial-install.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if sha(target.read_bytes()) != previous:
            raise RuntimeError('CSS changed since preflight; no update made')
        backup = ROOT/'release-backups'/(time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-icons-'+uuid.uuid4().hex[:8])
        backup.mkdir(parents=True,mode=0o700)
        shutil.copy2(target,backup/'spatial.css')
        mode = target.stat().st_mode & 0o777
        try:
            atomic_write(target,data,mode)
            if sha(target.read_bytes()) != payload['sha256']:
                raise RuntimeError('Post-install CSS hash mismatch')
        except BaseException:
            atomic_write(target,original,mode)
            print('ICONS_UPDATE_FAILED: previous CSS restored',flush=True)
            raise
        receipt = {'release':'nexus-icons-20261120','installed':time.time(),
            'files':{RELATIVE:payload['sha256']},'previous_sha256':previous,
            'backup':str(backup),'services_restarted':[]}
        (backup/'install-receipt.json').write_text(json.dumps(receipt,indent=2))
        atomic_write(ROOT/'nexus-icons-release.json',json.dumps(receipt,indent=2).encode(),0o600)
        print('NEXUS_ICONS_UPDATE_OK: icons centered; no service restart',flush=True)
        print('BACKUP:',backup,flush=True)


if __name__=='__main__':
    main(PAYLOAD,preflight=sys.argv[1:]==['--preflight'])
