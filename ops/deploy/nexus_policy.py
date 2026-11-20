"""Transactional, guarded source update for an existing ibHiCS virtual environment.

The delivery builder replaces PAYLOAD with checksum-verified JSON. No VM restart.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib  # noqa: F401 - used by the generated embedded payload
from pathlib import Path

PAYLOAD = None  # substituted only in the standalone delivery
ROOT = Path("/opt/ibhics")
VENV = ROOT / "venv"
SERVICES = ("ibhics-manager",)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(*args):
    if args[0] == "systemctl":
        print("STEP:", " ".join(args), flush=True)
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=90).stdout.strip()


def active(name):
    return subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0


def environment(service):
    pid = run("systemctl", "show", service, "-p", "MainPID", "--value")
    return dict(
        part.split("=", 1)
        for part in Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0")
        if "=" in part
    )


def check_files(package, manifest):
    unknown = []
    for relative, hashes in manifest.items():
        target = package / relative
        if target.is_symlink():
            unknown.append(relative + " (symlink)")
            continue
        actual = digest(target.read_bytes()) if target.is_file() else None
        if actual not in hashes:
            unknown.append(relative)
    if unknown:
        raise RuntimeError(
            "Installed files differ from reviewed versions; no update made: "
            + ", ".join(unknown[:20])
        )


def check_idle_impl():
    # Check the hypervisor directly; no authentication bypass or session creation.
    import libvirt

    connection = libvirt.open("qemu:///system")
    if connection is None:
        raise RuntimeError("Cannot inspect active hypervisor jobs")
    try:
        for domain in connection.listAllDomains():
            if domain.isActive() and domain.jobStats().get("type", 0) != 0:
                raise RuntimeError(
                    "Active hypervisor job; retry after it finishes: " + domain.name()
                )
    finally:
        connection.close()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            name = (proc / "comm").read_text().strip()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if name in ("qemu-img", "virt-v2v", "virt-v2v-in", "nbdkit"):
            raise RuntimeError(
                "Disk conversion/export process is active; retry when finished: " + name
            )
    for base in (Path("/var/lib/ibhics/backup"), Path("/mnt/ibhics-backup")):
        if base.exists():
            for pattern in (".repository-lease", ".backup.gc", ".backup.lock"):
                if next(base.rglob(pattern), None) is not None:
                    raise RuntimeError(
                        "Backup repository has an active or orphaned lock; review before upgrading"
                    )


def check_idle():
    run(str(VENV / "bin/python"), str(Path(__file__).resolve()), "--check-idle")


def health_body_ok(service, status, body, env):
    if service == "ibhics-manager" and status == 503:
        # The existing HA standby endpoint deliberately has no version field.
        return bool(
            env.get("IBHICS_SHARED_STATE")
            and body.get("status") == "standby"
            and isinstance(body.get("node"), str)
            and body.get("node")
            and body.get("active_node")
            and body["active_node"] != body["node"]
            and (not env.get("IBHICS_NODE_NAME") or body["node"] == env["IBHICS_NODE_NAME"])
        )
    return (
        status == 200
        and body.get("version") == "1.3.0b2+vstore3.27"
        and (service != "ibhics-manager" or body.get("status") == "ok")
    )


def check_health(services, envs):
    context = ssl._create_unverified_context()  # loopback to existing self-signed listener
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    for service in services:
        env = envs[service]
        if service == "ibhics-manager":
            port = env.get("IBHICS_PORT", "8888")
            paths = [
                f"https://127.0.0.1:{port}/api/v1/health",
                f"http://127.0.0.1:{port}/api/v1/health",
            ]
            headers = {}
        else:
            port = env.get("IBHICS_AGENT_PORT", "8007")
            paths = [f"http://127.0.0.1:{port}/agent/v1/health"]
            headers = {"X-ibhics-token": env.get("IBHICS_CLUSTER_TOKEN", "")}
        passed = False
        reason = "No health response"
        for _ in range(25):
            for url in paths:
                try:
                    request = urllib.request.Request(url, headers=headers)
                    try:
                        response = opener.open(request, timeout=2)
                    except urllib.error.HTTPError as exc:
                        response = exc
                    with response:
                        status = response.code
                        body = json.load(response)
                    if health_body_ok(service, status, body, env) and active(service):
                        passed = True
                        if body.get("status") == "standby":
                            print(
                                "MANAGER_MODE: standby; active node:",
                                body["active_node"],
                                flush=True,
                            )
                            print("ACTIVE_MANAGER_URL:", body.get("active_url"), flush=True)
                        break
                    reason = f"HTTP {status}; status={body.get('status')!r}"
                except Exception as exc:
                    reason = type(exc).__name__
            if passed:
                break
            time.sleep(1)
        if not passed:
            raise RuntimeError("Service did not pass health check: " + service + "; " + reason)


def apply_files(package, files):
    for relative, encoded in files.items():
        data = base64.b64decode(encoded)
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".deploy-" + uuid.uuid4().hex)
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        try:
            temp.write_bytes(data)
            temp.chmod(mode)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    for path in package.rglob("__pycache__"):
        shutil.rmtree(path)


def rollback_code(package, backup, files):
    for relative in files:
        original = backup / "files" / relative
        target = package / relative
        if original.is_file():
            shutil.copy2(original, target)
        else:
            target.unlink(missing_ok=True)
    for path in package.rglob("__pycache__"):
        shutil.rmtree(path)


def main(payload):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Run as root on the existing Linux compute node")
    if payload is None:
        raise RuntimeError("Use the generated standalone delivery file")
    os.umask(0o077)
    ROOT.mkdir(exist_ok=True)
    lock = (ROOT / ".backup-spatial-install.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    package = Path(
        run(
            str(VENV / "bin/python"),
            "-c",
            "import pathlib,ibhics;print(pathlib.Path(ibhics.__file__).parent)",
        )
    )
    if not package.is_relative_to(VENV.resolve()):
        raise RuntimeError("Installed package is outside the expected venv")
    check_files(package, payload["expected"])
    services = [s for s in SERVICES if active(s)]
    if "ibhics-manager" not in services or not active("ibhics-agent"):
        raise RuntimeError("Expected active manager and compute agent; no update made")
    envs = {s: environment(s) for s in services}
    if environment("ibhics-agent").get("IBHICS_ROLE", "compute") != "compute":
        raise RuntimeError("This release is for compute nodes, not the Arbiter")
    import nbd  # noqa: F401
    import zstandard  # noqa: F401

    check_idle()
    # Use the exact source bundle that passed on .104, or validate it on this node.
    receipt = None
    stages = ROOT / "backup-staging"
    for path in sorted(stages.glob("*/stage-receipt.json"), reverse=True):
        item = json.loads(path.read_text())
        if item.get("source_sha256") == payload["stage_sha"] and item.get("state") == "passed":
            receipt = str(path)
            break
    if receipt is None:
        raise RuntimeError("Verified advanced-backup stage receipt missing; no update made")
    else:
        print("VERIFIED_STAGE:", receipt, flush=True)
    check_idle()
    check_files(package, payload["expected"])
    files = payload["files"]
    for relative, encoded in files.items():
        data = base64.b64decode(encoded)
        if digest(data) != payload["new_hashes"][relative]:
            raise RuntimeError("Delivery checksum mismatch: " + relative)
        if relative.endswith(".py"):
            compile(data, relative, "exec")
    required = sum(len(base64.b64decode(x)) for x in files.values()) * 3 + 50 * 1024 * 1024
    if shutil.disk_usage(ROOT).free < required:
        raise RuntimeError("Insufficient free space for backup and update")
    backup = (
        ROOT
        / "release-backups"
        / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
    )
    backup.mkdir(parents=True, mode=0o700)
    for relative in files:
        source = package / relative
        if source.exists():
            dest = backup / "files" / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    state_path = Path(
        envs.get("ibhics-manager", {}).get(
            "IBHICS_BACKUP_STATE_FILE", "/var/lib/ibhics/backup.json"
        )
    )
    existed = state_path.exists()
    # Stop only ibHiCS services, never libvirt or guests. systemd uses graceful SIGTERM.
    stopped = []
    pause = ROOT / ".backup-upgrade-in-progress"
    with pause.open("x") as marker:
        marker.write(str(backup))
    try:
        for service in services:
            run("systemctl", "stop", service)
            stopped.append(service)
        check_idle()
        print("BACKUP:", backup, flush=True)
        if existed:
            shutil.copy2(state_path, backup / "backup-state.json")
        apply_files(package, files)
        run(
            str(VENV / "bin/python"),
            "-c",
            "from ibhics.backup.compression import compress;"
            "from ibhics.hypervisor.backup_cbt import capture;"
            "from ibhics.manager.app import create_app",
        )
        for service in reversed(services):
            run("systemctl", "start", service)
        check_health(services, envs)
        for relative, expected in payload["new_hashes"].items():
            if digest((package / relative).read_bytes()) != expected:
                raise RuntimeError("Post-install file verification failed: " + relative)
    except BaseException:
        for service in stopped:
            subprocess.run(["systemctl", "stop", service], capture_output=True)
        rollback_code(package, backup, files)
        pause.unlink(missing_ok=True)
        for service in reversed(stopped):
            subprocess.run(["systemctl", "start", service], capture_output=True)
        print("INSTALL_FAILED: restored previous application files. Backup:", backup, flush=True)
        raise
    pause.unlink()
    receipt = {
        "release": 'nexus-policy-20261120',
        "installed": time.time(),
        "files": payload["new_hashes"],
        "services": services,
        "backup": str(backup),
        "state_path": str(state_path),
    }
    (backup / "install-receipt.json").write_text(json.dumps(receipt, indent=2))
    (ROOT / "nexus-policy-release.json").write_text(json.dumps(receipt, indent=2))
    print("NEXUS_POLICY_UPDATE_OK: animated overview, resource chart, editable schedules", flush=True)
    print("SERVICES:", json.dumps({s: active(s) for s in services}), flush=True)
    print("BACKUP:", backup, flush=True)
    print(
        "Backup → Schedules → Edit. Existing history and restore points are preserved.",
        flush=True,
    )
    print("Refresh https://172.16.12.111:8888 with Command+Shift+R.", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-idle"]:
        check_idle_impl()
    elif sys.argv[1:] == ["--preflight"]:
        package = Path(run(str(VENV / "bin/python"), "-c", "import pathlib,ibhics;print(pathlib.Path(ibhics.__file__).parent)"))
        check_files(package, PAYLOAD["expected"])
        if not active("ibhics-manager") or not active("ibhics-agent"):
            raise RuntimeError("Expected active manager and agent")
        check_idle()
        print("NEXUS_POLICY_PREFLIGHT_OK", flush=True)
    else:
        main(PAYLOAD)
