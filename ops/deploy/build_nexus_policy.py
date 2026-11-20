"""Build the narrow November 20 manager/UI update from its reviewed predecessor."""
import base64
import hashlib
import json
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT.parents[1] / 'ibhics-delivery' / 'nexus-policy-release'
RELEASE = 'nexus-policy-20261120'
FILES = ['manager/app.py', 'manager/backups.py', 'manager/static/app.js',
         'manager/static/index.html', 'manager/static/spatial.js', 'manager/static/spatial.css']


def build():
    OUT.mkdir(exist_ok=True)
    baseline = json.loads((OUT.parent / 'backup-spatial-manifest.json').read_text())
    files = {name: base64.b64encode((ROOT/'ibhics'/name).read_bytes()).decode() for name in FILES}
    hashes = {name: hashlib.sha256(base64.b64decode(data)).hexdigest() for name,data in files.items()}
    payload = {'files': files, 'new_hashes': hashes,
               'expected': {name: list(dict.fromkeys([value, hashes.get(name, value)]))
                            for name,value in baseline['files'].items()},
               'stage_sha': baseline['stage_sha']}
    source = (ROOT/'ops/deploy/backup_spatial.py').read_text()
    source = source.replace('SERVICES = ("ibhics-manager", "ibhics-agent")',
                            'SERVICES = ("ibhics-manager",)')
    source = source.replace('def run(*args):\n    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()',
        'def run(*args):\n    if args[0] == "systemctl":\n        print("STEP:", " ".join(args), flush=True)\n    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=90).stdout.strip()')
    source = source.replace('def check_idle():', 'def check_idle_impl():', 1)
    source = source.replace('def health_body_ok(', 'def check_idle():\n    run(str(VENV / "bin/python"), str(Path(__file__).resolve()), "--check-idle")\n\n\ndef health_body_ok(', 1)
    source = source.replace('if "ibhics-agent" not in services:', 'if "ibhics-manager" not in services or not active("ibhics-agent"):')
    source = source.replace('Expected an active compute agent; no update made', 'Expected active manager and compute agent; no update made')
    source = source.replace('if envs["ibhics-agent"].get("IBHICS_ROLE", "compute") != "compute":',
                            'if environment("ibhics-agent").get("IBHICS_ROLE", "compute") != "compute":')
    a=source.index('        print("Running isolated Linux backup verification')
    z=source.index('    else:\n        print("VERIFIED_STAGE:', a)
    source=source[:a]+'        raise RuntimeError("Verified advanced-backup stage receipt missing; no update made")\n'+source[z:]
    source=source.replace('        if (backup / "backup-state.json").exists():\n            shutil.copy2(backup / "backup-state.json", state_path)\n', '')
    source=source.replace('"backup-spatial-20261119"', repr(RELEASE))
    source=source.replace('"backup-spatial-release.json"', '"nexus-policy-release.json"')
    source=source.replace('BACKUP_SPATIAL_UPDATE_OK: advanced backup + approved floating navigation',
                          'NEXUS_POLICY_UPDATE_OK: animated overview, resource chart, editable schedules')
    source=source.replace('"Existing recovery points remain unchanged; new policies are configured in Backup."',
                          '"Backup → Schedules → Edit. Existing history and restore points are preserved."')
    source=source.replace('Refresh https://172.16.12.104:8888 with Command+Shift+R.',
                          'Refresh https://172.16.12.111:8888 with Command+Shift+R.')
    source=source.replace('    main(PAYLOAD)', '    if sys.argv[1:] == ["--check-idle"]:\n        check_idle_impl()\n    elif sys.argv[1:] == ["--preflight"]:\n        package = Path(run(str(VENV / "bin/python"), "-c", "import pathlib,ibhics;print(pathlib.Path(ibhics.__file__).parent)"))\n        check_files(package, PAYLOAD["expected"])\n        if not active("ibhics-manager") or not active("ibhics-agent"):\n            raise RuntimeError("Expected active manager and agent")\n        check_idle()\n        print("NEXUS_POLICY_PREFLIGHT_OK", flush=True)\n    else:\n        main(PAYLOAD)')
    # Keep the source importable for the transaction tests; the delivery alone embeds code.
    (ROOT/'ops/deploy/nexus_policy.py').write_text(source)
    encoded = base64.b64encode(zlib.compress(json.dumps(payload).encode(),9)).decode()
    source=source.replace('PAYLOAD = None  # substituted only in the standalone delivery',
        f'PAYLOAD = json.loads(zlib.decompress(base64.b64decode({encoded!r})))')
    compile(source,'install-nexus-policy.py','exec')
    installer=OUT/'install-nexus-policy.py';installer.write_text(source)
    manifest={'release':RELEASE,'files':hashes,'expected':payload['expected'],
              'installer_sha256':hashlib.sha256(installer.read_bytes()).hexdigest(),
              'restart_services':['ibhics-manager'],'preserve_backup_state':True,
              'reviewed_stage_sha256':baseline['stage_sha'],'production_deployed':False}
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print('NEXUS_POLICY_DELIVERY_READY:',len(files),'runtime files')


if __name__ == '__main__':
    build()
