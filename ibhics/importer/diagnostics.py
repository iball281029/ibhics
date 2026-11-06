"""Read-only, bounded source diagnostics. No SSH service/key setup is performed."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

from .sources import Source, SourceType


def _bounded(call, timeout):
    """DNS and third-party bindings may ignore timeouts; never wait for their threads."""
    result = Queue(maxsize=1)

    def work():
        try:
            result.put((True, call()))
        except Exception as exc:
            result.put((False, exc))

    Thread(target=work, daemon=True).start()
    try:
        ok, value = result.get(timeout=timeout)
    except Empty:
        raise TimeoutError from None
    if not ok:
        raise value
    return value


def _node_address(host):
    # UDP connect chooses a local route without sending packets to the source.
    with socket.socket(
        socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_DGRAM
    ) as sock:
        sock.connect((host, 443))
        return sock.getsockname()[0]


def _connect(host, port, timeout):
    with socket.create_connection((host, port), timeout=timeout) as conn:
        return conn.getsockname()[0]


def _tls(source, timeout):
    ctx = ssl.create_default_context()
    if not source.verify_tls:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    with socket.create_connection((source.host, 443), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=source.host) as conn:
            fingerprint = hashlib.sha256(conn.getpeercert(binary_form=True)).hexdigest()
            return f"{conn.version()}; SHA256 {fingerprint}; certificate " + (
                "verified" if source.verify_tls else "not verified"
            )


def _ssh(source, command, timeout):
    # Password via an ephemeral askpass file, never argv, logs or authorized_keys.
    with tempfile.TemporaryDirectory() as folder:
        secret = Path(folder) / "password"
        secret.write_text(source.password or "")
        secret.chmod(0o600)
        askpass = Path(folder) / "askpass"
        askpass.write_text(f"#!/bin/sh\ncat '{secret}'\n")
        askpass.chmod(0o700)
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=3",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
                f"{source.username or 'root'}@{source.host}",
                command,
            ],
            env={
                **os.environ,
                "SSH_ASKPASS": str(askpass),
                "SSH_ASKPASS_REQUIRE": "force",
                "DISPLAY": os.environ.get("DISPLAY") or ":0",
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            start_new_session=True,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()


def _login(source, timeout):
    if source.type == SourceType.KVM:
        _ssh(source, "true", timeout)
    else:
        from .esxi_api import EsxiApi, https_transport

        EsxiApi(source, https_transport(source, timeout=timeout)).login()
    return "Authenticated (read-only test)"


def _inventory(source, timeout):
    if source.type == SourceType.KVM:
        output = _ssh(
            source,
            "if command -v qm >/dev/null; then qm list; else virsh list --all --name; fi",
            timeout,
        )
        lines = [line for line in output.splitlines() if line.strip()]
        return len(lines) - int(bool(lines and lines[0].strip().startswith("VMID")))
    # Keep libvirt's potentially unbounded native calls in a killable child process.
    code = """
import json, sys
from ibhics.importer.sources import Source, open_vmware
source = Source.from_dict(json.load(sys.stdin))
conn = open_vmware(source)
try:
    print(len(conn.listAllDomains()))
finally:
    conn.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(source.to_dict()),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return int(result.stdout.strip())


def _path(source, timeout):
    folder = Path(source.path)
    if not folder.is_dir():
        raise FileNotFoundError("Source folder does not exist on this node")
    with os.scandir(folder) as entries:
        next(entries, None)
    if not os.access(folder, os.R_OK | os.X_OK):
        raise PermissionError("Source folder is not readable")
    return "Folder exists and is readable"


def test_source(
    source: Source,
    *,
    connect=_connect,
    resolve=socket.getaddrinfo,
    tls=_tls,
    login=_login,
    inventory=_inventory,
    path_check=_path,
    node_ip=None,
    timeout=4.0,
    budget=28.0,
) -> dict:
    """Return ordered checks; stop at first required failure, NFC is advisory.

    All injected operations run with the same per-step and overall wait limits.
    Exceptions are classified, never echoed (they may contain credentials).
    """
    source.validate()
    deadline = time.monotonic() + min(budget, 28.0)
    timeout = min(timeout, 5.0)
    checks = []
    node = node_ip or "unknown (no route available)"
    if node_ip is None:
        try:
            node = _bounded(lambda: socket.gethostbyname(socket.gethostname()), 0.25)
        except Exception:
            pass
    host = source.host or "local folder"

    def check(step, call, *, advisory=False, port=None):
        nonlocal node
        target_port = port or (22 if source.type == SourceType.KVM else 443)
        try:
            remaining = max(0.001, min(timeout, deadline - time.monotonic()))
            value = _bounded(lambda: call(remaining), remaining)
            if step.startswith("TCP") and value:
                node = str(value)
            detail = f"Node IP: {node}; {host}; {value or 'OK'}"
            hint = ""
            ok = True
        except Exception as exc:
            text = str(exc).lower()
            detail = f"Node IP: {node}; {host}; {step} failed"
            if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
                hint = (
                    f"No answer from {host}: check that the host is on, the IP is right, "
                    f"and that routing/firewall allows {node} → {host}:{target_port}"
                )
            elif isinstance(exc, ConnectionRefusedError):
                hint = (
                    f"Port closed: enable the service on {host}:{target_port} "
                    "and check firewall rules."
                )
            elif step == "DNS":
                hint = "Check the hostname and this node's DNS server configuration."
            elif step == "HTTPS/TLS":
                hint = (
                    "Check certificate trust, hostname and expiry; "
                    "install the correct CA or review Verify TLS."
                )
            elif any(s in text for s in ("401", "password", "permission denied", "invalidlogin")):
                hint = "Wrong user/password: check the source credentials and account permissions."
            elif "host key" in text:
                hint = (
                    "Verify the SSH host key and provision this node's known_hosts before retrying."
                )
            elif step == "Path":
                hint = "Mount the source folder on this node and allow read/traverse access."
            else:
                hint = (
                    "Check source service availability, read permissions "
                    "and required client tools on this node."
                )
            if advisory:
                hint = "Warning only (NFC): " + hint
            ok = False
        # Redact even injected success strings, not just exception messages.
        if source.password:
            detail = detail.replace(source.password, "[redacted]")
            hint = hint.replace(source.password, "[redacted]")
        checks.append(dict(step=step, ok=ok, detail=detail, hint=hint))
        return ok or advisory

    if source.type not in (SourceType.VMWARE, SourceType.KVM):
        check("Path", lambda t: path_check(source, t))
    else:
        try:
            ipaddress.ip_address(host)
        except ValueError:

            def dns(timeout):
                addresses = sorted({r[4][0] for r in resolve(host, None)})
                if not addresses:
                    raise socket.gaierror("No addresses found")
                return ", ".join(addresses)

            if not check("DNS", dns):
                return {"checks": checks}
        if node_ip is None:
            try:
                node = _bounded(lambda: _node_address(host), 0.25)
            except Exception:
                pass
        port = 443 if source.type == SourceType.VMWARE else 22
        if not check(f"TCP {port}", lambda t: connect(host, port, t), port=port):
            return {"checks": checks}
        if source.type == SourceType.VMWARE:
            if not check("HTTPS/TLS", lambda t: tls(source, t)):
                return {"checks": checks}
        if not check("Login", lambda t: login(source, t)):
            return {"checks": checks}
        if not check("List VMs", lambda t: f"{inventory(source, t)} VMs found"):
            return {"checks": checks}
        if source.type == SourceType.VMWARE:
            if source.transport == "ssh":
                if not check("TCP 22", lambda t: connect(host, 22, t), port=22):
                    return {"checks": checks}
            check("TCP 902", lambda t: connect(host, 902, t), port=902, advisory=True)
    return {"checks": checks}
