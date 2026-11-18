"""Sangfor HCI as a Migrate source, through the API its own web console uses.

Sign-in follows the console: the page carries an RSA modulus; the password plus a
one-time secret is RSA (PKCS#1 v1.5) encrypted and posted to the ticket endpoint,
which answers with a session cookie and a CSRF token. Importing asks Sangfor to export
the VM as OVA (its own export feature), downloads the file, removes the export on
the Sangfor side, and lets virt-v2v convert the OVA like any other.

Sangfor does not publish this API, so answers are read tolerantly and their shape is
written (never values that could be secret) to a debug log to adjust against.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from ..hypervisor.base import Firmware
from .sources import GIB, Source, SourceVM

log = logging.getLogger(__name__)
DEBUG_LOG = Path("/var/log/ibhics/sangfor-api.log")
MODULUS = re.compile(r"RSAModulus\s*[:=]\s*[\"']([0-9A-Fa-f]{128,})[\"']")


class SangforError(RuntimeError):
    pass


def pkcs1_encrypt_hex(message: str, modulus_hex: str, exponent: int = 0x10001) -> str:
    """RSA PKCS#1 v1.5 encryption as the console's jsbn code does it, hex out."""
    n = int(modulus_hex, 16)
    k = (n.bit_length() + 7) // 8
    data = message.encode()
    if len(data) > k - 11:
        raise SangforError("password too long for the console's RSA key")
    padding = b""
    while len(padding) < k - 3 - len(data):
        padding += bytes(b for b in os.urandom(k) if b)
    block = b"\x00\x02" + padding[: k - 3 - len(data)] + b"\x00" + data
    cipher = pow(int.from_bytes(block, "big"), exponent, n)
    text = format(cipher, "x")
    return text if len(text) % 2 == 0 else "0" + text


def _shape(value, depth=0):
    """Keys and types only: for the debug log, never values."""
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1), f"×{len(value)}"] if value else []
    return type(value).__name__


class Sangfor:
    def __init__(self, source: Source, http=None, sleep: Callable[[float], None] = time.sleep):
        import httpx

        self.source = source
        self._http = http or httpx.Client(
            base_url=f"https://{source.host}",
            verify=source.verify_tls,
            timeout=httpx.Timeout(30.0, read=900.0),
            follow_redirects=False,
        )
        self._sleep = sleep
        self._csrf = ""

    def _debug(self, what: str, payload) -> None:
        try:
            DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
            with DEBUG_LOG.open("a") as f:
                f.write(
                    json.dumps({"at": time.time(), "what": what, "shape": _shape(payload)}) + "\n"
                )
        except OSError:
            pass

    def _json(self, resp, what: str):
        if resp.status_code in (401, 403):
            raise SangforError(f"Sangfor refused {what} (HTTP {resp.status_code})")
        try:
            body = resp.json()
        except ValueError:
            raise SangforError(f"Sangfor answered {what} with HTTP {resp.status_code}") from None
        self._debug(what, body)
        if isinstance(body, dict) and body.get("success") in (0, False) and body.get("message"):
            raise SangforError(f"{what}: {body['message']}")
        return body

    def _headers(self) -> dict:
        return {"CSRFPreventionToken": self._csrf} if self._csrf else {}

    def login(self) -> None:
        page = self._http.get("/login")
        found = MODULUS.search(page.text)
        if not found:
            raise SangforError("the Sangfor login page has no RSA key; is this Sangfor HCI?")
        user = self.source.username or "admin"
        rand = self._json(
            self._http.post("/vapi/json/access/srand", data={"username": user}), "srand"
        )
        secret_body = self._json(self._http.get("/vapi/json/access/secret"), "secret")
        secret = _dig(secret_body, "secret") or ""
        if not secret:
            raise SangforError("Sangfor gave no sign-in secret")
        form = {
            "username": user,
            "password": pkcs1_encrypt_hex((self.source.password or "") + secret, found.group(1)),
            "hardware": _dig(rand, "hwinfo") or "",
            "certinfo": "",
            "secret": secret,
        }
        resp = self._http.post("/vapi/extjs/access/ticket", data=form)
        body = self._json(resp, "ticket")
        self._csrf = _dig(body, "CSRFPreventionToken") or ""
        if not self._csrf:
            raise SangforError("Sangfor did not accept the username or password")

    def get(self, path: str, what: str, **params):
        return self._json(self._http.get(path, params=params, headers=self._headers()), what)

    def post(self, path: str, what: str, **data):
        return self._json(self._http.post(path, data=data, headers=self._headers()), what)

    def vms(self) -> list[dict]:
        body = self.get(
            "/vapi/extjs/cluster/vms", "vms", group_type="group", sort_type="", desc=1, nostatus=0
        )
        return [vm for vm in _walk(body) if "vmid" in vm and vm.get("name")]

    def vm_config(self, vmid) -> dict:
        body = self.get(f"/vapi/extjs/cluster/vm/{vmid}/config", "vm_config")
        data = body.get("data", body) if isinstance(body, dict) else {}
        return data if isinstance(data, dict) else {}

    def export_ova(self, vm: SourceVM, target: Path, progress=None, timeout_s: float = 6 * 3600):
        """Ask Sangfor to export the VM as OVA, download it, remove the export there."""
        vmid = vm.ref
        self.post(
            "/vapi/extjs/cluster/vm/vm_export",
            "vm_export",
            vmid=vmid,
            type="ova",
            vmw_vm_version="8",
        )
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                body = self.get(
                    "/vapi/extjs/cluster/vm/vm_export_process",
                    "export_process",
                    vmid=vmid,
                    type="ova",
                )
                done, percent, failed = _export_state(body)
                if failed:
                    raise SangforError(f"Sangfor could not export {vm.name}: {failed}")
                if progress and percent is not None:
                    progress(f"Sangfor is exporting {vm.name} to OVA", 2 + int(percent * 0.3))
                if done:
                    break
                if time.monotonic() > deadline:
                    raise SangforError("Sangfor did not finish the export in time")
                self._sleep(5)
            params = {"vmname": vm.name, "exportType": "ova", "vmid": vmid}
            with self._http.stream(
                "GET", "/download", params=params, headers=self._headers()
            ) as resp:
                if resp.status_code >= 400:
                    raise SangforError(f"Sangfor refused the download ({resp.status_code})")
                total = int(resp.headers.get("content-length") or 0)
                got = 0
                with open(target, "wb") as out:
                    for chunk in resp.iter_bytes(4 << 20):
                        out.write(chunk)
                        got += len(chunk)
                        if progress and total:
                            progress(f"Downloading {vm.name}.ova", 32 + int(10 * got / total))
            if target.stat().st_size < 10240:
                raise SangforError("Sangfor sent no OVA file (the export may have expired)")
            return target
        finally:
            try:
                self.post(
                    "/vapi/extjs/cluster/vm/vm_file_remove", "vm_file_remove", vmid=vmid, type="ova"
                )
            except Exception:
                pass


def _dig(value, key):
    for item in _walk(value):
        if key in item:
            return item[key]
    return None


def _walk(value):
    """Every dict inside a JSON answer, depth first (group trees, data wrappers)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def _export_state(body) -> tuple[bool, float | None, str]:
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return False, None, ""
    percent = None
    for key in ("percent", "progress", "process", "rate"):
        if isinstance(data.get(key), (int, float)) or str(data.get(key, "")).rstrip("%").isdigit():
            percent = float(str(data[key]).rstrip("%"))
            break
    status = str(data.get("status", data.get("state", ""))).lower()
    failed = data.get("error") or (
        data.get("message") if status in ("fail", "failed", "error") else ""
    )
    done = status in ("finish", "finished", "done", "success", "complete", "completed") or (
        percent is not None and percent >= 100
    )
    return done, percent, str(failed or "")


def parse_vm(vm: dict, config: dict | None = None) -> SourceVM:
    cfg = {**vm, **(config or {})}
    cores = int(cfg.get("cores") or cfg.get("cpu") or cfg.get("vcpus") or 1)
    sockets = int(cfg.get("sockets") or 1)
    memory = cfg.get("memory") or cfg.get("mem") or 1024
    memory_mb = int(float(memory) / (1024 * 1024)) if float(memory) > 1 << 22 else int(memory)
    disks = []
    for key, value in cfg.items():
        if re.fullmatch(r"(ide|sata|scsi|virtio)\d+", str(key)) and "cdrom" not in str(value):
            size = re.search(r"size=(\d+(?:\.\d+)?)([KMGT])", str(value))
            unit = {"K": 1 / 1024**2, "M": 1 / 1024, "G": 1, "T": 1024}
            disks.append(int(float(size.group(1)) * unit[size.group(2)]) if size else 0)
    nics = [k for k in cfg if re.fullmatch(r"net\d+", str(k))]
    uefi = str(cfg.get("bios", "")).lower() in ("ovmf", "uefi") or bool(cfg.get("efidisk0"))
    status = str(cfg.get("status", "")).lower()
    return SourceVM(
        ref=str(vm["vmid"]),
        name=str(vm.get("name")),
        vcpus=max(1, cores * sockets),
        memory_mb=max(128, memory_mb),
        firmware=Firmware.UEFI if uefi else Firmware.BIOS,
        disks_gb=disks,
        nics=max(1, len(nics)),
        state="running" if status in ("running", "1") else "stopped",
        guest_os=str(cfg.get("ostype") or cfg.get("os_type") or ""),
        notes="Sangfor HCI",
    )


def list_sangfor_vms(source: Source, client: Sangfor | None = None) -> list[SourceVM]:
    client = client or Sangfor(source)
    client.login()
    found = []
    for vm in client.vms():
        try:
            config = client.vm_config(vm["vmid"])
        except Exception:
            config = {}
        found.append(parse_vm(vm, config))
    return sorted(found, key=lambda v: v.name.lower())


def export_to_ova(source: Source, vm: SourceVM, out_dir: Path, progress, client=None) -> Path:
    client = client or Sangfor(source)
    client.login()
    return client.export_ova(vm, out_dir / f"sangfor-{vm.ref}.ova", progress)


__all__ = ["GIB", "Sangfor", "SangforError", "export_to_ova", "list_sangfor_vms", "parse_vm"]
