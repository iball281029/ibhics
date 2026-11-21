"""Subscription and update workflows. Production trust/execution adapters fail closed.

No signing key or node command is accepted from an HTTP client. The deployment must
supply a trusted verifier and application-only node adapter before installs work.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .. import __version__
from .auth import Forbidden

MAX_PACKAGE = 32 * 1024 * 1024


def version_key(version: str) -> tuple:
    """Compare the project's numeric releases, prereleases and vstore build suffixes.

    Unknown version formats are rejected rather than guessed or sorted as text.
    """
    match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\+vstore(\d+)(?:\.(\d+))?)?",
        version,
    )
    if not match:
        raise ValueError("unsupported ibHiCS version format")
    major, minor, patch, phase, number, store, build = match.groups()
    return (
        int(major),
        int(minor),
        int(patch),
        {"a": 0, "b": 1, "rc": 2, None: 3}[phase],
        int(number or 0),
        int(store or 0),
        int(build or 0),
    )


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(data, out)
    tmp.replace(path)


def https_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("use an HTTPS URL without credentials")
    if parts.fragment:
        raise ValueError("URL fragments are not supported")
    return url


def fetch_bytes(url: str, limit: int = MAX_PACKAGE) -> bytes:
    https_url(url)
    with httpx.stream("GET", url, timeout=30, follow_redirects=False) as response:
        response.raise_for_status()
        chunks, total = [], 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise ValueError("download exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks)


def cluster_key(value: str) -> str:
    """Compare cluster IDs in either form: the UUID or IBHX-XXXX-... shown on the page."""
    text = str(value).strip().lower()
    if text.startswith("ibhx-"):
        text = text[5:]
    return text.replace("-", "")


class NodeUpdater(Protocol):
    """Implementations must only stop/start ibHiCS services; never VMs or hosts.

    prepare must retain the previous application on *every* node before any
    install. rollback must restore that retained application and be idempotent.
    The adapter owns quorum, import/replication interlocks and leader handover.
    """

    def nodes(self) -> list[str]: ...
    def active(self) -> str: ...
    def prepare(self, node: str, package: bytes, manifest: dict) -> None: ...
    def install(self, node: str) -> None: ...
    def healthy(self, node: str) -> bool: ...
    def rollback(self, node: str) -> None: ...


class Settings:
    def __init__(
        self,
        path: Path | None = None,
        cluster_id: Callable[[], str] = lambda: "",
        node_count: Callable[[], int] = lambda: 1,
        verifier: Callable[[bytes, bytes], bool] | None = None,
        updater: NodeUpdater | None = None,
        fetch: Callable[[str], bytes] = fetch_bytes,
        send: Callable[[str, dict], None] | None = None,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
        current_version: str = __version__,
    ):
        self.path = path
        self.current_version = current_version
        self.cluster_id = cluster_id
        self.node_count = node_count
        from ..signatures import Ed25519Verifier

        self.verifier = verifier or Ed25519Verifier(path.parent if path else None)
        self.updater = updater
        self.approval_required = lambda: False
        self.fetch, self.send, self.today = fetch, send, today
        self.lock = threading.RLock()
        self.data = {
            "license": None,
            "support_endpoint": "",
            "channels": {"stable": "", "beta": ""},
            "channel": "stable",
            "auto_install": False,
            "last_check": None,
            "check_error": "",
            "cases": [],
            "staged": None,
            "jobs": [],
            "previous_available": False,
            "previous_version": None,
        }
        if path and path.exists():
            self.data.update(json.loads(path.read_text()))
            for job in self.data["jobs"]:
                if job["state"] in ("queued", "running") and not job.get("external"):
                    job.update(state="interrupted", detail="Manager restarted; inspect every node")

    def save(self):
        with self.lock:
            if self.path:
                private_json(self.path, self.data)

    def verify(self, envelope: dict) -> dict:
        if self.verifier is None:
            raise ValueError("trusted Ed25519 verification unavailable")
        try:
            payload = envelope["payload"]
            signature = base64.b64decode(envelope["signature"], validate=True)
            message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            valid = isinstance(payload, dict) and self.verifier(message, signature)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("invalid signed file")
        return dict(payload)

    def license(self) -> dict | None:
        if not self.data["license"]:
            return None
        try:
            payload = self.verify(self.data["license"])
            for key in ("customer", "cluster_id", "nodes"):
                if key not in payload:
                    raise ValueError("license field missing")
            # A license may sell Update, Support or both.
            if "update_until" not in payload and "support_until" not in payload:
                raise ValueError("license has no subscription")
            if not isinstance(payload["customer"], str) or not payload["customer"].strip():
                raise ValueError("customer is required")
            if not self.cluster_id() or cluster_key(payload["cluster_id"]) != cluster_key(
                self.cluster_id()
            ):
                raise ValueError("license belongs to a different or uninitialized cluster")
            if type(payload["nodes"]) is not int or payload["nodes"] < self.node_count():
                raise ValueError("license does not cover this cluster's nodes")
            for key in ("update_until", "support_until"):
                if key in payload:
                    date.fromisoformat(payload[key])
            return payload
        except (ValueError, TypeError):
            return None

    def subscription(self) -> dict:
        payload = self.license()
        result = {"customer": payload["customer"] if payload else None}
        for kind in ("update", "support"):
            until = payload.get(f"{kind}_until") if payload else None
            days = (date.fromisoformat(until) - self.today()).days if until else None
            result[kind] = {
                "status": "None" if days is None else "Active" if days >= 0 else "Expired",
                "until": until,
                "days": max(0, days) if days is not None else None,
            }
        return result

    def upload_license(self, envelope: dict) -> dict:
        with self.lock:
            self.verify(envelope)
            old = self.data["license"]
            self.data["license"] = envelope
            if not self.license():
                self.data["license"] = old
                raise ValueError("invalid license fields, cluster identity or node entitlement")
            self.save()
            return self.subscription()

    def configure(self, body: dict):
        with self.lock:
            config = {
                k: body[k] for k in ("support_endpoint", "channels", "channel", "auto_install")
            }
            if config["channel"] not in ("stable", "beta"):
                raise ValueError("select stable or beta")
            if type(config["auto_install"]) is not bool:
                raise ValueError("auto_install must be boolean")
            if config["auto_install"] and (not self.updater or not self.verifier):
                raise ValueError("automatic installation needs trusted verifier and node adapter")
            if set(config["channels"]) != {"stable", "beta"}:
                raise ValueError("supply stable and beta URLs")
            for url in [config["support_endpoint"], *config["channels"].values()]:
                if url:
                    https_url(url)
            self.data.update(config)
            self.save()

    def open_case(self, user: str, body: dict) -> dict:
        with self.lock:
            if self.subscription()["support"]["status"] != "Active":
                raise Forbidden("An active Support subscription is required to open a case")
            subject, details = body.get("subject", ""), body.get("details", "")
            severity = body.get("severity", "normal")
            if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 200:
                raise ValueError("subject must contain 1-200 characters")
            if not isinstance(details, str) or not 1 <= len(details.strip()) <= 20000:
                raise ValueError("details must contain 1-20000 characters")
            if severity not in ("low", "normal", "high", "critical"):
                raise ValueError("invalid severity")
            attachment = body.get("diagnostic")
            if attachment:
                try:
                    raw = base64.b64decode(attachment, validate=True)
                except (ValueError, TypeError):
                    raise ValueError("invalid diagnostic attachment") from None
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("diagnostic bundle limit is 4 MiB")
            case = {
                "id": secrets.token_hex(8),
                "user": user,
                "subject": subject,
                "severity": severity,
                "details": details,
                "diagnostic": attachment,
                "created": self.today().isoformat(),
                "delivery": "local",
            }
            self.data["cases"].append(case)
            self.save()  # preserve the case even when the remote endpoint fails
            endpoint = self.data["support_endpoint"]
            if endpoint:
                try:
                    if self.send:
                        self.send(endpoint, dict(case))
                    else:
                        response = httpx.post(
                            endpoint, json=case, timeout=30, follow_redirects=False
                        )
                        response.raise_for_status()
                    case["delivery"] = "sent"
                except Exception:
                    case["delivery"] = "local; delivery failed"
                self.save()
            return {k: v for k, v in case.items() if k != "diagnostic"}

    def eligible_release(self, manifest: dict):
        payload = self.license()
        if not payload:
            raise Forbidden("A valid Update subscription is required")
        try:
            released = date.fromisoformat(manifest.get("released", ""))
        except (TypeError, ValueError):
            raise ValueError("release date must use YYYY-MM-DD") from None
        if released > self.today() or released > date.fromisoformat(
            payload.get("update_until") or "0001-01-01"
        ):
            raise Forbidden("This release is outside the Update subscription period")

    def stage(self, envelope: dict, package: bytes) -> dict:
        with self.lock:
            self.refresh_jobs()
            if any(job["state"] in ("queued", "running") for job in self.data["jobs"]):
                raise ValueError("cannot replace a package while an update is running")
            manifest = self.verify(envelope)
            if not re.fullmatch(r"ibhics-[\w.+-]+\.whl", manifest.get("filename", "")):
                raise ValueError("only an ibhics wheel is supported")
            if not isinstance(manifest.get("version"), str) or not manifest["version"]:
                raise ValueError("release version is required")
            if not isinstance(manifest.get("notes", ""), str):
                raise ValueError("release notes must be text")
            if len(package) > MAX_PACKAGE or not package:
                raise ValueError("wheel size must be between 1 byte and 32 MiB")
            if hashlib.sha256(package).hexdigest() != manifest.get("sha256"):
                raise ValueError("package checksum mismatch")
            self.eligible_release(manifest)
            if version_key(manifest["version"]) <= version_key(self.current_version):
                raise ValueError("use Rollback to restore a retained previous version")
            self.data["staged"] = {
                "envelope": envelope,
                "package": base64.b64encode(package).decode(),
            }
            self.save()
            return manifest

    def check(self, daily: bool = False):
        with self.lock:
            today = self.today().isoformat()
            if daily and self.data["last_check"] == today:
                return
            if not self.data["channels"][self.data["channel"]]:
                return
            self.data.update(last_check=today, check_error="")
            self.save()
            try:
                envelope = json.loads(self.fetch(self.data["channels"][self.data["channel"]]))
                manifest = self.verify(envelope)
                if version_key(manifest["version"]) <= version_key(self.current_version):
                    return
                if self.subscription()["update"]["status"] != "Active":
                    raise Forbidden("An active Update subscription is required for auto download")
                self.eligible_release(manifest)
                package = self.fetch(https_url(manifest["url"]))
                self.stage(envelope, package)
                if self.data["auto_install"] and not self.approval_required():
                    self.start_job()
            except Exception:
                self.data["check_error"] = "Update check failed; verify channel and entitlement"
                self.save()
                if not daily:
                    raise ValueError(self.data["check_error"]) from None

    def refresh_jobs(self):
        if not self.path:
            return
        for job in self.data["jobs"]:
            if not job.get("external"):
                continue
            path = self.path.parent / "update-jobs" / (job["id"] + ".json")
            if path.exists():
                result = json.loads(path.read_text())
            elif hasattr(self.updater, "job_status"):
                try:
                    result = self.updater.job_status(job)
                except Exception:
                    continue
            else:
                continue
            job.update(result)
            if job["state"] == "done":
                self.data["previous_available"] = job["kind"] == "update"
                if job["kind"] == "update":
                    self.data["previous_version"] = job.get("previous_version")

    def approval_context(self, rollback=False):
        with self.lock:
            self.refresh_jobs()
            if rollback:
                return {
                    "previous_version": self.data["previous_version"],
                    "latest_job": self.data["jobs"][-1]["id"] if self.data["jobs"] else None,
                }
            staged = self.data["staged"]
            if not staged:
                return {"version": None, "manifest_digest": None}
            envelope = staged["envelope"]
            return {
                "version": envelope["payload"].get("version"),
                "sha256": envelope["payload"].get("sha256"),
                "manifest_digest": hashlib.sha256(
                    json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }

    def start_job(self, rollback: bool = False) -> dict:
        with self.lock:
            self.refresh_jobs()
            if not self.updater or not self.verifier:
                raise ValueError(
                    "Installation unavailable: trusted node adapter/verifier not configured"
                )
            if any(j["state"] in ("queued", "running") for j in self.data["jobs"]):
                raise ValueError("another application update is running")
            if any(j["state"] == "interrupted" for j in self.data["jobs"]):
                raise ValueError("an interrupted update requires node reconciliation")
            if rollback and not self.data["previous_available"]:
                raise ValueError("no previous version has been retained")
            if not rollback:
                staged = self.data["staged"]
                if not staged:
                    raise ValueError("upload and verify a package first")
                # Recheck signatures, checksum, identity and entitlement at install time.
                self.stage(staged["envelope"], base64.b64decode(staged["package"]))
            job = {
                "id": secrets.token_hex(8),
                "kind": "rollback" if rollback else "update",
                "state": "queued",
                "nodes": [],
                "detail": "",
                "created": datetime.now(UTC).isoformat(),
                "previous_version": self.data["previous_version"]
                if rollback
                else self.current_version,
            }
            self.data["jobs"].append(job)
            self.save()
            if hasattr(self.updater, "launch"):
                try:
                    self.updater.launch(self, job)
                except Exception:
                    job.update(state="failed", detail="Cannot launch durable update worker")
                    self.save()
                    raise
                self.save()
            else:
                threading.Thread(target=self.run_job, args=(job,), daemon=True).start()
            return dict(job)

    def run_job(self, job: dict):
        adapter = self.updater
        touched = []
        try:
            with self.lock:
                job["state"] = "running"
                staged = self.data["staged"]
                self.save()
            nodes = adapter.nodes()
            active = adapter.active()
            if not nodes or active not in nodes or len(set(nodes)) != len(nodes):
                raise ValueError("invalid node inventory")
            nodes = sorted(nodes, key=lambda node: node == active)
            if job["kind"] == "update":
                for node in nodes:
                    adapter.prepare(
                        node, base64.b64decode(staged["package"]), staged["envelope"]["payload"]
                    )
                self.data["previous_available"] = True
            for node in nodes:
                touched.append(node)  # include partially failed installs in recovery
                if job["kind"] == "update":
                    adapter.install(node)
                else:
                    adapter.rollback(node)
                if not adapter.healthy(node):
                    raise ValueError("application health failed")
                job["nodes"].append(node)
                self.save()
            job["state"] = "done"
            if job["kind"] == "rollback":
                self.data["previous_available"] = False
        except Exception:
            job.update(state="failed", detail="Application update failed")
            if job["kind"] == "update":
                recovered = True
                for node in reversed(touched):
                    try:
                        adapter.rollback(node)
                        recovered = adapter.healthy(node) and recovered
                    except Exception:
                        recovered = False
                job["detail"] = (
                    "Rolled back changed nodes"
                    if recovered and touched
                    else "Check node recovery before another update"
                )
                if not recovered or not touched:
                    job["state"] = "interrupted"
            else:
                job["state"] = "interrupted"
                job["detail"] = "Rollback failed; inspect every node before continuing"
        finally:
            with self.lock:
                self.save()

    def view(self) -> dict:
        with self.lock:
            self.refresh_jobs()
            staged = self.data["staged"]
            return {
                "subscription": self.subscription(),
                "support_delivery_configured": bool(self.data["support_endpoint"]),
                "update_channel_configured": bool(self.data["channels"][self.data["channel"]]),
                "verification_available": self.verifier is not None,
                "installation_available": self.updater is not None and self.verifier is not None,
                **{
                    k: self.data[k]
                    for k in (
                        "support_endpoint",
                        "channels",
                        "channel",
                        "auto_install",
                        "last_check",
                        "check_error",
                        "jobs",
                        "previous_available",
                    )
                },
                "staged": staged["envelope"]["payload"] if staged else None,
                "cases": [
                    {k: v for k, v in c.items() if k not in ("diagnostic", "details")}
                    for c in self.data["cases"]
                ],
            }
