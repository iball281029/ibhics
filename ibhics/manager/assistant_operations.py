"""Bounded assistant plans. Mutations still use the ordinary authenticated APIs.

Model text is never treated as an executable command or an API request. Plans are
prepared locally, bound to a user/cluster, and rechecked before the browser asks
the existing API to perform the operation (including its audit/approval checks).
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import Forbidden


class OperationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["create_vlan", "start_vm", "shutdown_vm", "restart_vm", "status"]
    target: str = Field(default="", max_length=128)
    vlan: int | None = Field(default=None, ge=1, le=4094)
    name: str = Field(default="", max_length=63, pattern=r"^[a-zA-Z0-9_.-]*$")
    language: Literal["en", "th"] = "en"


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=4096)


def parse_request(text, language):
    """Recognise a small explicit command vocabulary, never execute model output."""
    if re.search(r"\b(?:not|don't|never)\b|อย่า|ไม่ต้อง", text, re.I):
        return None
    if re.search(r"\b(?:create|add)\b|สร้าง|เพิ่ม", text, re.I):
        match = re.search(r"vlan\s*\)?\s*[:=]?\s*([0-9]{1,5})(?![0-9])", text, re.I)
        if match:
            return OperationIn(kind="create_vlan", vlan=int(match[1]), language=language)
    match = re.search(r"(?:vm|วีเอ็ม)\s*[\"']?([a-zA-Z0-9ก-๙_.-]+)", text, re.I)
    target = match[1] if match else ""
    if re.search(r"status|สถานะ", text, re.I):
        return OperationIn(kind="status", target=target, language=language)
    if not target:
        return None
    for kind, pattern in (
        ("restart_vm", r"\brestart\b|\breboot\b|รีสตาร์[ตท]"),
        ("shutdown_vm", r"\bshutdown\b|shut\s+down|power\s+off|ปิด"),
        ("start_vm", r"\bstart\b|power\s+on|เปิด"),
    ):
        if re.search(pattern, text, re.I):
            return OperationIn(kind=kind, target=target, language=language)
    return None


class Operations:
    def __init__(self, cluster, auth, assistant, backups, clock=time.time, migrations=None):
        self.cluster, self.auth, self.assistant, self.backups = cluster, auth, assistant, backups
        self.clock = clock
        self.migrations = migrations
        self._submitted = {}
        self._submit_lock = threading.Lock()
        self._key = secrets.token_bytes(32)
        if assistant.path:
            path = assistant.path.with_name("assistant-plans.key")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(self._key)
            self._key = path.read_bytes()
            if len(self._key) != 32 or path.stat().st_mode & 0o077:
                raise RuntimeError("Invalid assistant plan signing key")

    def authorize(self, user, kind):
        self.assistant.require()
        if kind == "create_vlan" and str(user.role) != "admin":
            raise Forbidden("Creating a VLAN requires the admin role")
        if kind.endswith("_vm") and "vms" not in self.auth.permissions(user):
            raise Forbidden("VM operations require permission to manage VMs")

    def inventory(self, user):
        self.authorize(user, "status")
        return {
            "vms": [
                {"uuid": vm.uuid, "name": vm.spec.name, "node": member.name, "state": str(vm.state)}
                for member, vm in self.cluster.all_vms()
                if not vm.spec.template and not vm.spec.recycled
            ],
            "can_create_vlan": str(user.role) == "admin",
            "can_manage_vms": "vms" in self.auth.permissions(user),
        }

    def find_vm(self, target):
        matches = [
            (m, v) for m, v in self.cluster.all_vms() if v.uuid == target or v.spec.name == target
        ]
        if len(matches) != 1:
            raise HTTPException(409, "Choose one VM by its exact name or UUID")
        return matches[0]

    def describe(self, operation, user):
        self.authorize(user, operation.kind)
        th = operation.language == "th"
        if operation.kind == "status":
            if operation.target:
                member, vm = self.find_vm(operation.target)
                guest = member.driver.guest_info(vm.uuid)
                reply = (
                    f"VM: {vm.spec.name}\nNode: {member.name}\n"
                    f"Hypervisor: {vm.state}\nOS: {guest.os_name or 'Unknown'}\n"
                    f"Guest Agent: {'Responding' if guest.agent else 'Not responding'}\n"
                    f"Storage I/O guard: {vm.storage_error_protection}"
                )
                if not guest.agent:
                    reply += "\n" + (
                        "ยังยืนยันไม่ได้ว่า OS พร้อมใช้งาน"
                        if th
                        else "OS readiness is not verified by the running state alone."
                    )
            else:
                self.cluster.refresh()
                rows = self.cluster.all_vms()
                reply = ("สถานะล่าสุด" if th else "Current status") + ":\n"
                reply += "\n".join(
                    f"{m.name}: {'Online' if self.cluster.is_healthy(m) else 'Unreachable'}"
                    for m in self.cluster.members()
                )
                reply += "\nQuorum: " + ("OK" if self.cluster.has_quorum() else "Not available")
                reply += "\n" + "\n".join(
                    f"VM {v.spec.name}: {v.state} ({m.name})"
                    for m, v in rows
                    if not v.spec.template and not v.spec.recycled
                )
                reply += "\n" + (
                    "ระบุชื่อ VM เพื่อตรวจ OS และ Guest Agent เพิ่มเติม"
                    if th
                    else "Specify a VM name to check OS and Guest Agent readiness."
                )
            return {"reply": reply, "checked_at": self.clock()}
        self.cluster.require_quorum()
        if operation.kind == "create_vlan":
            if operation.vlan is None:
                raise HTTPException(422, "A VLAN ID from 1 to 4094 is required")
            name = operation.name or f"vlan{operation.vlan}"
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", name):
                raise HTTPException(422, "Invalid network name")
            members = self.cluster.compute_members()
            if not members:
                raise HTTPException(409, "No compute nodes are available")
            facts = []
            for member in members:
                self.cluster.require_in_service(member)
                networks = member.driver.list_networks()
                if any(n.name == name or n.vlan == operation.vlan for n in networks):
                    raise HTTPException(
                        409, "The network name or VLAN already exists; inspect Networks"
                    )
                if not any(n.name == "default" and n.bridge == "br0" for n in networks):
                    raise HTTPException(409, f"{member.name} has no supported br0 LAN network")
                facts.append({"node": member.name, "networks": [asdict(n) for n in networks]})
            return {
                "reply": (
                    "ตรวจค่าแล้ว นี่คือการเพิ่ม Virtual switch (VLAN) ใน "
                    "Network → Virtual switches กรุณาตรวจรายละเอียดก่อนยืนยัน"
                )
                if th
                else (
                    "Preflight complete for Network → Virtual switches → "
                    "+ Add virtual switch (VLAN). Review before confirming."
                ),
                "operation": operation.model_dump(),
                "path": "/networks",
                "body": {"name": name, "vlan": operation.vlan},
                "summary": f"Add virtual switch (VLAN) · {name} · VLAN {operation.vlan}",
                "details": [
                    "Menu: Network → Virtual switches → + Add virtual switch (VLAN)",
                    f"Network: {name} · VLAN: {operation.vlan}",
                    "Nodes: " + ", ".join(m.name for m in members),
                    f"Bridge (automatic): ibr{operation.vlan}",
                    (
                        "สร้างเครือข่ายให้ VM เลือกใช้ ไม่ได้ตั้งค่าสวิตช์ภายนอกหรือย้าย VM เข้าเครือข่ายนี้ "
                        "กรุณาตรวจว่าพอร์ต trunk อนุญาต VLAN นี้แล้ว"
                        if th
                        else "Creates a VM network. It does not configure the physical switch "
                        "or move VM NICs. "
                        "Confirm the upstream trunk allows this VLAN."
                    ),
                ],
                "verification": {
                    "kind": "network",
                    "name": name,
                    "vlan": operation.vlan,
                    "nodes": [m.name for m in members],
                },
                "facts": facts,
            }
        member, vm = self.find_vm(operation.target)
        if vm.spec.template or vm.spec.recycled:
            raise HTTPException(409, "Templates and recycled VMs cannot be operated here")
        if self.migrations and any(
            j.uuid == vm.uuid and j.state in ("queued", "running") for j in self.migrations.jobs()
        ):
            raise HTTPException(409, "A VM migration is active; wait for it to finish")
        if any(
            j.vm == vm.spec.name and j.state in ("queued", "running", "retry_wait")
            for j in self.backups.jobs()
        ):
            raise HTTPException(409, "A VM job is active; wait for it to finish")
        if operation.kind == "start_vm":
            self.cluster.require_in_service(member)
            if str(vm.state) not in ("stopped", "paused"):
                raise HTTPException(409, "The VM is already running")
            action = "start"
        else:
            if str(vm.state) != "running":
                raise HTTPException(409, "The VM must be running for this operation")
            if operation.kind == "restart_vm" and vm.restart_needed:
                raise HTTPException(
                    409, "Pending hardware changes require a planned power cycle from the VM page"
                )
            action = "stop" if operation.kind == "shutdown_vm" else "restart"
        return {
            "reply": "กรุณายืนยันคำสั่งสำหรับ VM นี้" if th else "Review and confirm the VM operation.",
            "operation": {**operation.model_dump(), "target": vm.uuid},
            "path": f"/vms/{vm.uuid}/{action}",
            "body": {} if action == "start" else {"force": False},
            "summary": f"{operation.kind.replace('_', ' ')} · {vm.spec.name}",
            "details": [
                f"VM: {vm.spec.name}",
                f"Node: {member.name}",
                f"Current state: {vm.state}",
                "Shutdown/restart interrupts the guest service. Forced power off is not used.",
            ],
            "verification": {"kind": "vm", "uuid": vm.uuid, "name": vm.spec.name, "action": action},
            "facts": {
                "node": member.name,
                "uuid": vm.uuid,
                "name": vm.spec.name,
                "state": str(vm.state),
                "restart_needed": vm.restart_needed,
                "vcpus": vm.spec.vcpus,
                "memory_mb": vm.spec.memory_mb,
            },
        }

    @staticmethod
    def fingerprint(plan):
        return hashlib.sha256(
            json.dumps(
                [plan["path"], plan["body"], plan["facts"]], sort_keys=True, default=str
            ).encode()
        ).hexdigest()

    def plan(self, operation, user):
        plan = self.describe(operation, user)
        if "operation" not in plan:
            return plan
        payload = {
            "owner": user.name,
            "cluster": self.cluster.cluster_id,
            "expires": self.clock() + 300,
            "operation": plan["operation"],
            "fingerprint": self.fingerprint(plan),
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
        signature = hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()
        plan.pop("facts")
        return {
            "reply": plan.pop("reply"),
            "proposal": {**plan, "token": encoded + "." + signature},
        }

    def review(self, token, user):
        try:
            encoded, signature = token.rsplit(".", 1)
            expected = hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(base64.urlsafe_b64decode(encoded))
            if payload["owner"] != user.name or payload["cluster"] != self.cluster.cluster_id:
                raise ValueError
            if payload["expires"] <= self.clock():
                raise HTTPException(409, "The plan expired. Ask Nong ib to prepare it again")
            operation = OperationIn.model_validate(payload["operation"])
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(403, "Invalid assistant plan") from exc
        fresh = self.describe(operation, user)
        if self.fingerprint(fresh) != payload["fingerprint"]:
            raise HTTPException(409, "The system changed. Prepare a new plan before continuing")
        fresh.pop("facts")
        return fresh

    def submitted(self, token):
        """Reject duplicate in-flight submissions; the browser never auto-retries."""
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._submit_lock:
            now = self.clock()
            self._submitted = {k: expiry for k, expiry in self._submitted.items() if expiry > now}
            if key in self._submitted:
                raise HTTPException(
                    409, "This plan was already submitted. Check the current status"
                )
            self._submitted[key] = now + 300
