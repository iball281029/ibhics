"""Persisted two-person approval; replay only through a private ASGI scope."""

import json
import secrets
import threading
import time
from datetime import UTC, datetime

import httpx

from .auth import Forbidden
from .events import publish
from .settings import private_json


class PendingApproval(Exception):
    def __init__(self, item):
        self.item = item


def risky(method, route, body, auth, params):
    if method == "DELETE" and route.startswith(
        (
            "/api/v1/vms/",
            "/api/v1/recycle-bin/",
            "/api/v1/backup/targets/",
            "/api/v1/users/",
            "/api/v1/backup/policies/",
        )
    ):
        return True
    if route in ("/api/v1/settings/updates/install", "/api/v1/settings/updates/rollback"):
        return method == "POST"
    if route == "/api/v1/security/2fa":
        return method == "PUT" and body.get("require_2fa") is False
    if route == "/api/v1/security/approvals":
        return method == "PUT" and body.get("enabled") is False
    if route == "/api/v1/users/{name}" and method == "PATCH":
        user = auth.user(params["name"])
        return user.role == "admin" and (
            body.get("role", "admin") != "admin" or body.get("enabled") is False
        )
    # Conservatively gate all repository/policy changes, including reduced locks.
    return (
        method in ("PUT", "PATCH", "POST")
        and route.startswith(
            (
                "/api/v1/backup/targets",
                "/api/v1/backup/policies",
            )
        )
        and not route.endswith(("/run", "/restore", "/verify", "/test"))
    )


def redacted(value):
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if any(
                word in key.lower()
                for word in ("password", "secret", "token", "credential", "private", "access_key")
            )
            else redacted(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redacted(child) for child in value]
    return value


class Approvals:
    def __init__(self, auth, clock=time.time):
        self.auth, self.clock = auth, clock
        self.path = auth._state_path.with_name("approvals.json") if auth._state_path else None
        self.lock = threading.RLock()
        self.data = {"enabled": False, "requests": []}
        if self.path and self.path.exists():
            self.data.update(json.loads(self.path.read_text()))
        for item in self.data["requests"]:
            if item["state"] == "executing":
                item.update(state="interrupted", detail="Inspect action before retrying")

    def save(self):
        if self.path:
            private_json(self.path, self.data)

    def configure(self, enabled):
        with self.lock:
            if enabled and sum(u.enabled and u.role == "admin" for u in self.auth.users()) < 2:
                raise ValueError("two enabled admins are required")
            self.data["enabled"] = enabled
            self.save()

    def expire(self):
        for item in self.data["requests"]:
            if item["state"] == "pending" and self.clock() >= item["expires"]:
                item["state"] = "expired"
        self.save()

    def view(self, user):
        with self.lock:
            self.expire()
            return {
                "enabled": self.data["enabled"],
                "requests": [
                    {
                        **{k: v for k, v in item.items() if k != "body"},
                        "changes": redacted(item["body"]),
                    }
                    for item in self.data["requests"]
                    if user.role == "admin" or item["requester"] == user.name
                ],
            }

    def create(self, user, request, body, context=None):
        with self.lock:
            self.expire()
            if sum(i["state"] == "pending" for i in self.data["requests"]) >= 1000:
                raise ValueError("too many pending approvals")
            item = {
                "id": secrets.token_hex(16),
                "state": "pending",
                "requester": user.name,
                "approver": "",
                "method": request.method,
                "path": request.url.path,
                "context": context,
                "query": request.url.query,
                "body": body,
                "expires": self.clock() + 86400,
                "created": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            }
            self.data["requests"].append(item)
            publish(
                self,
                "approval.pending",
                f"Approval requested by {user.name}",
                f"approval:{item['id']}",
            )
            self.save()
            return {
                "approval_request_id": item["id"],
                "state": "pending",
                "detail": "รออนุมัติจาก admin อีกคน",
            }

    def execution_user(self, scope):
        item = scope.get("ibhics.approval")
        if item is None:
            return None
        if not any(i is item for i in self.data["requests"]) or item["state"] != "executing":
            raise Forbidden("invalid approval execution")
        user = self.auth.user(item["requester"])
        if not user.enabled:
            raise Forbidden("requester is disabled")
        return user

    async def decide(self, identifier, actor, approve, app):
        if actor.role != "admin" or not actor.enabled:
            raise Forbidden("admin approval required")
        with self.lock:
            self.expire()
            item = next((i for i in self.data["requests"] if i["id"] == identifier), None)
            if item is None or item["state"] != "pending":
                raise ValueError("approval is missing, expired or already decided")
            if actor.name == item["requester"]:
                raise Forbidden("you cannot approve your own request")
            item.update(approver=actor.name, state="executing" if approve else "rejected")
            self.save()
        self.auth.record(
            actor.name,
            "APPROVE" if approve else "REJECT",
            f"{identifier} requester={item['requester']}",
            200,
            "",
        )
        if not approve:
            return {"state": "rejected"}

        async def replay(scope, receive, send):
            scope["ibhics.approval"] = item
            await app(scope, receive, send)

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=replay), base_url="http://approval.internal"
            ) as client:
                response = await client.request(
                    item["method"], item["path"], params=item["query"], json=item["body"]
                )
            item.update(
                state="executed" if response.is_success else "failed", result=response.status_code
            )
        except Exception:
            item.update(state="failed", result=500)
        finally:
            with self.lock:
                self.save()
        self.auth.record(
            item["requester"],
            "APPROVED_EXECUTION",
            f"{item['path']} approver={actor.name} approval={identifier}",
            item["result"],
            "",
        )
        return {k: item[k] for k in ("id", "state", "result")}
