"""Application-only updates. Workers run in separate systemd units across service restarts.

Only ibHiCS services and /opt/ibhics/venv are changed. Commands never use a shell.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from . import __version__
from .manager.settings import MAX_PACKAGE, Settings, private_json
from .signatures import Ed25519Verifier


def command(args):
    result = subprocess.run(args, capture_output=True, timeout=300, check=False)
    if result.returncode and not (args == ["pgrep", "-x", "virt-v2v"] and result.returncode == 1):
        raise subprocess.CalledProcessError(result.returncode, args)
    return result


def launch(unit, args, runner=command):
    runner(
        [
            "systemd-run",
            "--collect",
            "--unit",
            unit,
            "--property=EnvironmentFile=/etc/ibhics/ibhics.env",
            *args,
        ]
    )


class NodeApplicationUpdater:
    def __init__(
        self,
        state=Path("/var/lib/ibhics"),
        role="compute",
        venv=Path("/opt/ibhics/venv"),
        runner=command,
        health=None,
        guard=Path("/run/lock/ibhics-replication.lock"),
    ):
        self.root = state / "application-update"
        self.state = state
        self.role, self.venv, self.runner = role, venv, runner
        self.probe = health or self.health
        self.guard = guard
        self.verifier = Ed25519Verifier(state)
        self.lock = threading.RLock()

    @contextmanager
    def exclusive(self):
        import fcntl

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock, (self.root / "lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def read(self):
        path = self.root / "state.json"
        return json.loads(path.read_text()) if path.exists() else {"state": "empty"}

    def save(self, data):
        private_json(self.root / "state.json", data)

    def validate(self, envelope, package):
        verifier = Settings(verifier=self.verifier)
        manifest = verifier.verify(envelope)
        if not re.fullmatch(r"ibhics-[\w.+-]+\.whl", manifest.get("filename", "")):
            raise ValueError("only an ibhics wheel is supported")
        if not package or len(package) > MAX_PACKAGE:
            raise ValueError("invalid wheel size")
        if hashlib.sha256(package).hexdigest() != manifest.get("sha256"):
            raise ValueError("package checksum mismatch")
        from .manager.settings import version_key

        version_key(manifest["version"])
        return manifest

    def prepare(self, envelope, package):
        manifest = self.validate(envelope, package)
        with self.exclusive():
            current = self.read()
            if current["state"] in ("queued", "running"):
                raise ValueError("a node update is already running")
            if current["state"] == "prepared":
                if current["envelope"] == envelope:
                    return {"state": "prepared", "id": current["id"]}
                raise ValueError("a different application update is already prepared")
            identifier = secrets.token_hex(12)
            folder = self.root / identifier
            folder.mkdir(mode=0o700)
            wheel = folder / manifest["filename"]
            fd = os.open(wheel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(package)
            # Same application backup as install-vstore3.sh; completed on all nodes first.
            self.runner(["cp", "-a", str(self.venv), str(folder / "venv")])
            data = {
                "id": identifier,
                "state": "prepared",
                "envelope": envelope,
                "wheel": str(wheel),
                "backup": str(folder / "venv"),
                "old_version": __version__,
                "previous_available": True,
            }
            self.save(data)
            return {"state": "prepared", "id": identifier}

    def start(self, rollback=False, operation_id=None):
        operation_id = operation_id or secrets.token_hex(16)
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("invalid operation id")
        with self.exclusive():
            data = self.read()
            if data.get("operation_id") == operation_id:
                return {k: data[k] for k in ("id", "state", "operation_id")}
            if data["state"] in ("queued", "running"):
                raise ValueError("a node update is already running")
            if not data.get("previous_available") or (not rollback and data["state"] != "prepared"):
                raise ValueError("no prepared application/backup available")
            data.update(
                state="queued",
                operation="rollback" if rollback else "install",
                operation_id=operation_id,
            )
            self.save(data)
            try:
                launch(
                    "ibhics-app-" + secrets.token_hex(8),
                    [
                        str(self.venv / "bin/python"),
                        "-m",
                        "ibhics.updater",
                        "node",
                        str(self.state),
                        self.role,
                    ],
                    self.runner,
                )
            except Exception:
                data["state"] = "failed"
                self.save(data)
                raise
            return {"id": data["id"], "state": "queued", "operation_id": operation_id}

    def services(self):
        services = ["ibhics-agent"]
        if self.role == "compute":
            services.append("ibhics-manager")
        result = self.runner(
            ["systemctl", "show", "-p", "LoadState", "--value", "ibhics-arbiter-ui"]
        )
        if result.stdout.strip() == b"loaded":
            services.append("ibhics-arbiter-ui")
        return services

    def restore(self, data, services):
        self.runner(["systemctl", "stop", *services])
        # Retain both the backup and the failed application for manual investigation.
        failed = self.root / ("failed-" + secrets.token_hex(8))
        self.runner(["mv", str(self.venv), str(failed)])
        self.runner(["cp", "-a", data["backup"], str(self.venv)])
        self.runner(["systemctl", "start", *services])
        if not self.probe(data["old_version"]):
            raise ValueError("rollback health failed")

    def execute(self):
        import fcntl

        with self.exclusive():
            data = self.read()
            if data["state"] != "queued":
                raise ValueError("no queued node update")
            data["state"] = "running"
            self.save(data)
            changed = False
            services = []
            guard = None
            try:
                # Keep this lock through recovery, like install-vstore3.sh.
                guard = self.guard.open("a")
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = self.runner(["pgrep", "-x", "virt-v2v"])
                if busy.returncode != 1:
                    raise ValueError("VM import running or import check failed")
                services = self.services()
                if data["operation"] == "rollback":
                    changed = True
                    self.restore(data, services)
                else:
                    manifest = self.validate(data["envelope"], Path(data["wheel"]).read_bytes())
                    changed = True
                    self.runner(["systemctl", "stop", *services])
                    self.runner(
                        [
                            str(self.venv / "bin/python"),
                            "-m",
                            "pip",
                            "install",
                            "--no-index",
                            "--no-deps",
                            "--force-reinstall",
                            data["wheel"],
                        ]
                    )
                    self.runner(["systemctl", "start", *services])
                    if not self.probe(manifest["version"]):
                        raise ValueError("application health failed")
                self.runner(["systemctl", "is-active", "--quiet", *services])
                data.update(state="done", detail="Application healthy")
            except Exception:
                data.update(state="failed", detail="Application update failed")
                if changed and data["operation"] == "install":
                    try:
                        self.restore(data, services)
                        data["detail"] = "Rolled back application after failure"
                    except Exception:
                        data.update(state="interrupted", detail="Rollback failed; inspect node")
            finally:
                if guard is not None:
                    guard.close()
                self.save(data)

    def health(self, version):
        from .cluster.remote import TOKEN_HEADER

        host = os.environ.get("IBHICS_AGENT_HOST", "127.0.0.1")
        if host == "0.0.0.0":
            host = "127.0.0.1"
        port = os.environ.get("IBHICS_AGENT_PORT", "8007")
        headers = {TOKEN_HEADER: os.environ.get("IBHICS_CLUSTER_TOKEN", "")}
        for _ in range(40):
            try:
                with httpx.Client(timeout=3, verify=False) as client:
                    agent = client.get(f"http://{host}:{port}/agent/v1/health", headers=headers)
                    agent.raise_for_status()
                    if agent.json().get("version") != version:
                        raise ValueError("agent version mismatch")
                    if self.role == "compute":
                        manager = client.get("https://127.0.0.1:8888/api/v1/health")
                        content = manager.json()
                        if not (
                            (manager.status_code == 503 and content.get("status") == "standby")
                            or (
                                manager.is_success
                                and content.get("version") == version
                                and content.get("status") == "ok"
                            )
                        ):
                            raise ValueError("manager health failed")
                return True
            except Exception:
                time.sleep(0.5)
        return False


class ClusterUpdater:
    def __init__(self, inventory, active, preflight=lambda: None):
        self.inventory = inventory
        self.active_node = active
        self.preflight = preflight
        self.envelope = None
        self.rollback_version = None
        self.last_rollback = set()

    def nodes(self):
        return list(self.inventory)

    def active(self):
        return self.active_node

    def request(self, node, method, path, **kwargs):
        from .cluster.remote import AgentClient

        entry = self.inventory[node]
        client = AgentClient(entry["address"], entry["token"], timeout=300)
        try:
            response = client.request(method, "/application-update" + path, **kwargs)
            response.raise_for_status()
            return response.json()
        finally:
            client._http.close()

    def prepare(self, node, package, manifest):
        if self.envelope is None or self.envelope["payload"] != manifest:
            raise ValueError("a signed envelope is required")
        self.request(
            node,
            "POST",
            "/prepare",
            json={
                "envelope": self.envelope,
                "package": base64.b64encode(package).decode(),
            },
        )

    def legacy_health(self, node):
        """A restored older wheel may predate the updater endpoint."""
        from urllib.parse import urlsplit

        from .cluster.remote import AgentClient

        if not self.rollback_version:
            return False
        entry = self.inventory[node]
        client = AgentClient(entry["address"], entry["token"])
        try:
            health = client.health()
            if health["name"] != node or health["version"] != self.rollback_version:
                return False
            if health["role"] == "compute":
                host = urlsplit(entry["address"]).hostname
                if ":" in host:
                    host = f"[{host}]"
                with httpx.Client(timeout=5, verify=False) as manager:
                    response = manager.get(f"https://{host}:8888/api/v1/health")
                    data = response.json()
                    if not (
                        (response.status_code == 503 and data.get("status") == "standby")
                        or (
                            response.is_success
                            and data.get("status") == "ok"
                            and data.get("version") == self.rollback_version
                        )
                    ):
                        return False
            return True
        except Exception:
            return False
        finally:
            client._http.close()

    def wait(self, node, rollback=False):
        operation_id = secrets.token_hex(16)
        path = "/rollback" if rollback else "/install"
        deadline = time.monotonic() + 900
        # Retry with the same id if restarting the agent interrupts the accepted response.
        submitted = False
        while time.monotonic() < deadline:
            try:
                if not submitted:
                    self.request(node, "POST", path, json={"operation_id": operation_id})
                    submitted = True
                status = self.request(node, "GET", "")
            except Exception:
                if rollback and self.legacy_health(node):
                    self.last_rollback.add(node)
                    return
                time.sleep(1)
                continue
            if status.get("operation_id") != operation_id:
                submitted = False
                time.sleep(1)
                continue
            if status["state"] == "done":
                if rollback:
                    self.last_rollback.add(node)
                return
            if status["state"] in ("failed", "interrupted"):
                raise ValueError("node application operation failed")
            time.sleep(1)
        raise ValueError("node update timed out; reconcile node")

    def install(self, node):
        self.wait(node)

    def rollback(self, node):
        self.wait(node, rollback=True)

    def healthy(self, node):
        try:
            status = self.request(node, "GET", "")
            return status["state"] == "done"
        except Exception:
            return node in self.last_rollback and self.legacy_health(node)

    def job_status(self, job):
        return self.request(job["coordinator"], "GET", "/cluster-jobs/" + job["id"])

    def launch(self, settings, job):
        inventory = self.preflight()
        if isinstance(inventory, dict):
            self.inventory = inventory
        if not self.inventory or not all(
            entry.get("address") and entry.get("token") for entry in self.inventory.values()
        ):
            raise ValueError("all nodes need a configured agent address and token")
        if settings.path is None:
            raise ValueError("durable state is required for production updates")
        root = settings.path.parent / "update-jobs"
        root.mkdir(mode=0o700, exist_ok=True)
        config = root / (job["id"] + ".config.json")
        job["external"] = True
        job["coordinator"] = self.active_node
        private_json(
            config,
            {
                "inventory": self.inventory,
                "active": self.active_node,
                "state": str(settings.path.parent),
                "data": settings.data,
            },
        )
        private_json(root / (job["id"] + ".json"), job)
        launch(
            "ibhics-cluster-update-" + job["id"],
            [sys.executable, "-m", "ibhics.updater", "cluster", str(config), job["id"]],
        )


def cluster_worker(config, identifier):
    payload = json.loads(config.read_text())
    adapter = ClusterUpdater(payload["inventory"], payload["active"])
    settings = Settings(verifier=Ed25519Verifier(Path(payload["state"])), updater=adapter)
    settings.data = payload["data"]
    job = next(j for j in settings.data["jobs"] if j["id"] == identifier)
    adapter.rollback_version = job.get("previous_version")
    staged = settings.data["staged"]
    target = config.with_name(identifier + ".json")
    settings.save = lambda: private_json(target, job)
    try:
        if job["kind"] == "update":
            adapter.envelope = staged["envelope"]
            settings.verify(adapter.envelope)
        settings.run_job(job)
    except Exception:
        job.update(state="interrupted", detail="Worker failed; inspect nodes before retrying")
        settings.save()
    finally:
        # Config contains a package and node tokens. Retain only the result file.
        config.unlink()


if __name__ == "__main__":
    if sys.argv[1] == "node":
        time.sleep(2)
        NodeApplicationUpdater(Path(sys.argv[2]), sys.argv[3]).execute()
    elif sys.argv[1] == "cluster":
        cluster_worker(Path(sys.argv[2]), sys.argv[3])
