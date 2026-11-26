"""Customer-side assistant. Signed Support is checked for every interaction."""

import asyncio
import os
import threading
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError

from .. import __version__
from ..ai.common import KNOWLEDGE, ChatIn, redact
from .auth import Forbidden
from .settings import https_url, private_json
from .settings_api import limited_json_body


class GatewayClient:
    def __init__(self, url="", token="", transport=None):
        self.url, self.token, self.transport = url, token, transport

    def configured(self):
        try:
            https_url(self.url)
            return bool(self.token)
        except ValueError:
            parts = urlsplit(self.url)
            # SSH reverse forwards terminate on loopback; public gateways require HTTPS.
            return bool(
                self.token
                and parts.scheme == "http"
                and parts.hostname in ("127.0.0.1", "::1")
                and not parts.username
                and not parts.password
                and not parts.fragment
            )

    def chat(self, payload):
        if not self.configured():
            raise HTTPException(503, "Contact Support to provision the AI service.")
        try:
            with httpx.Client(
                timeout=55, transport=self.transport, follow_redirects=False
            ) as client:
                response = client.post(
                    self.url, headers={"Authorization": f"Bearer {self.token}"}, json=payload
                )
                if response.status_code == 429:
                    raise HTTPException(429, "AI allowance reached. Contact Support.")
                if response.status_code == 403:
                    raise HTTPException(
                        403, "AI access was declined. Contact Support to check your subscription."
                    )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result.get("reply"), str) or not result["reply"].strip():
                    raise ValueError("Invalid AI reply")
                return {
                    "reply": redact(result["reply"][:12000]),
                    "allowance": result.get("allowance", {}),
                }
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise HTTPException(
                503, "AI service is unavailable. Check connectivity or contact Support."
            ) from exc


class Assistant:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or GatewayClient(
            os.environ.get("IBHICS_AI_GATEWAY_URL", ""),
            os.environ.get("IBHICS_AI_GATEWAY_TOKEN", ""),
        )
        self.path = settings.path.with_name("assistant.json") if settings.path else None
        self.enabled = False
        self.lock = threading.Lock()

    def status(self):
        import json

        # Refresh small shared state so an Active Manager handover sees the last setting.
        with self.lock:
            if self.path and self.path.exists():
                try:
                    self.enabled = json.loads(self.path.read_text()).get("enabled") is True
                except (ValueError, OSError):
                    self.enabled = False
            licensed = self.settings.subscription()["support"]["status"] == "Active"
            configured = self.client.configured()
            reason = (
                "An active Support subscription is required."
                if not licensed
                else "Contact Support to provision the AI service."
                if not configured
                else "Ask your administrator to enable AI Assistant."
                if not self.enabled
                else "Ready. Answers are guidance; verify before changing your system."
            )
            return {
                "licensed": licensed,
                "configured": configured,
                "enabled": self.enabled,
                "available": licensed and configured and self.enabled,
                "reason": reason,
                "reason_code": (
                    "no_support"
                    if not licensed
                    else "not_provisioned"
                    if not configured
                    else "disabled"
                    if not self.enabled
                    else "ready"
                ),
                "support_until": self.settings.subscription()["support"]["until"],
                "context_notice": (
                    "Messages use an external AI service. Optional summaries include "
                    "resource names "
                    "and job status. Do not send secrets. History is cleared on reload or New chat."
                ),
            }

    def require(self):
        status = self.status()
        if not status["available"]:
            raise HTTPException(403 if not status["licensed"] else 409, status["reason"])

    def configure(self, enabled):
        status = self.status()
        if enabled and (not status["licensed"] or not status["configured"]):
            raise HTTPException(409, status["reason"])
        with self.lock:
            if self.path:
                private_json(self.path, {"enabled": enabled})
            self.enabled = enabled
        return self.status()

    def ask(self, body, snapshot):
        self.require()
        payload = body.model_dump()
        payload.update(
            cluster_id=self.settings.cluster_id(),
            license=self.settings.data["license"],
            snapshot=snapshot,
        )
        payload["message"] = redact(payload["message"])
        payload["history"] = [
            {**turn, "content": redact(turn["content"])} for turn in payload["history"]
        ]

        def clean(value):
            if isinstance(value, str):
                return redact(value)
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value

        payload["snapshot"] = clean(snapshot)
        result = self.client.chat(payload)
        result["references"] = [{"id": k, "title": k.capitalize()} for k in KNOWLEDGE]
        return result


class ConfigIn(BaseModel):
    enabled: bool


def install(api, app, settings, auth, cluster, backups, assistant=None, migrations=None):
    assistant = assistant or Assistant(settings)
    app.state.assistant = assistant
    from .assistant_operations import OperationIn, Operations, ReviewIn, parse_request

    operations = Operations(cluster, auth, assistant, backups, migrations=migrations)
    app.state.assistant_operations = operations

    def actor(request):
        user = getattr(request.state, "user", None)
        if auth is None or user is None:
            raise Forbidden("Sign in to use AI Assistant")
        return user

    def enabled_actor(request: Request):
        user = actor(request)
        assistant.require()
        return user

    enabled_user = Depends(enabled_actor)

    def context(request):
        user = actor(request)
        assistant.require()
        permissions = auth.permissions(user)
        result = {"version": __version__}
        if "cluster" in permissions:
            result["nodes"] = [
                {"name": m.name[:80], "role": str(m.role), "healthy": cluster.is_healthy(m)}
                for m in cluster.members()
            ][:32]
        if "backup" in permissions:
            result["recent_backup_jobs"] = [
                {"kind": j.kind, "resource": j.vm[:80], "state": j.state, "percent": j.percent}
                for j in backups.jobs()[:10]
            ]
        if "vms" in permissions:
            vms = cluster.all_vms()
            result["virtual_machines"] = {
                "total": len(vms),
                "running": sum(vm.state.value == "running" for _, vm in vms),
            }
        return result

    @api.get("/assistant/status")
    def status(request: Request, response: Response):
        actor(request)
        response.headers["Cache-Control"] = "no-store"
        return assistant.status()

    @api.put("/assistant/config")
    def configure(body: ConfigIn, request: Request):
        if actor(request).role != "admin":
            raise Forbidden("Only an administrator can enable AI Assistant")
        return assistant.configure(body.enabled)

    @api.get("/assistant/context")
    def preview_context(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return context(request)

    @api.post("/assistant/chat")
    async def chat(request: Request, response: Response):
        user = actor(request)
        assistant.require()
        response.headers["Cache-Control"] = "no-store"
        try:
            body = ChatIn.model_validate(await limited_json_body(request, 32000))
        except ValidationError as exc:
            raise HTTPException(422, "Invalid chat message or history") from exc
        try:
            operation = parse_request(body.message, body.language)
        except ValidationError as exc:
            raise HTTPException(422, "Check the VLAN ID; valid IDs are 1 to 4094") from exc
        if operation:
            return await asyncio.to_thread(operations.plan, operation, user)
        snapshot = await asyncio.to_thread(context, request) if body.include_context else {}
        return await asyncio.to_thread(assistant.ask, body, snapshot)

    @api.get("/assistant/operations")
    def operation_inventory(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return operations.inventory(actor(request))

    @api.post("/assistant/plan")
    def operation_plan(body: OperationIn, response: Response, user=enabled_user):
        response.headers["Cache-Control"] = "no-store"
        return operations.plan(body, user)

    @api.post("/assistant/review")
    def operation_review(body: ReviewIn, response: Response, user=enabled_user):
        response.headers["Cache-Control"] = "no-store"
        return operations.review(body.token, user)
