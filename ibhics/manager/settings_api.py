"""Settings endpoints, installed on the existing authenticated router."""

import base64

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field

from .auth import Forbidden
from .permissions import BUILTIN, CATEGORIES
from .settings import MAX_PACKAGE


class Credentials(BaseModel):
    password: str = Field(default="", max_length=256, repr=False)
    code: str = Field(default="", max_length=64, repr=False)


class RoleIn(BaseModel):
    permissions: list[str] = Field(max_length=9)


class ApprovalConfig(BaseModel):
    enabled: bool


class ApprovalDecision(BaseModel):
    approve: bool


class SecurityIn(BaseModel):
    require_2fa: bool


class SettingsConfig(BaseModel):
    support_endpoint: str = Field(default="", max_length=2048)
    channels: dict[str, str]
    channel: str = "stable"
    auto_install: bool = False


def install(api, settings, auth):
    def signed_in(request):
        user = getattr(request.state, "user", None)
        if auth is None or user is None:
            raise Forbidden("sign-in must be enabled")
        return user

    def admin(request):
        user = signed_in(request)
        if user.role != "admin":
            raise Forbidden("this needs the admin role")
        return user

    @api.get("/approvals")
    def approval_list(request: Request):
        return request.app.state.approvals.view(signed_in(request))

    @api.put("/security/approvals")
    def approval_config(body: ApprovalConfig, request: Request):
        actor = admin(request)
        approvals = request.app.state.approvals
        approvals.configure(body.enabled)
        return approvals.view(actor)

    @api.post("/approvals/{identifier}/decision")
    async def approval_decision(identifier: str, body: ApprovalDecision, request: Request):
        actor = admin(request)
        return await request.app.state.approvals.decide(
            identifier, actor, body.approve, request.app
        )

    @api.get("/roles")
    def roles():
        return {
            "categories": CATEGORIES,
            "roles": {
                **{k: sorted(v) for k, v in BUILTIN.items()},
                **(auth.custom_roles if auth else {}),
            },
        }

    @api.put("/roles/{name}")
    def define_role(name: str, body: RoleIn, request: Request):
        admin(request)
        auth.define_role(name, body.permissions)
        return roles()

    @api.get("/auth/2fa")
    def status(request: Request):
        user = signed_in(request)
        return {
            "enabled": bool(user.totp_secret),
            # Required for this user: by the cluster-wide setting or by an admin for them.
            "required": auth.require_2fa or bool(getattr(user, "require_totp", False)),
            "required_for_all": auth.require_2fa,
            "recovery_remaining": len(user.recovery_hashes),
        }

    @api.post("/auth/2fa/setup")
    def setup(body: Credentials, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        user = signed_in(request)
        return auth.setup_2fa(user.name, body.password)

    @api.post("/auth/2fa/confirm")
    def confirm(body: Credentials, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        user = signed_in(request)
        return {"recovery_codes": auth.confirm_2fa(user.name, body.code)}

    @api.post("/auth/2fa/disable")
    def disable(body: Credentials, request: Request):
        user = signed_in(request)
        auth.disable_2fa(user.name, body.password, body.code)
        return {"enabled": False}

    @api.put("/security/2fa")
    def enforce(body: SecurityIn, request: Request):
        admin(request)
        auth.enforce_2fa(body.require_2fa)
        return {"required": auth.require_2fa}

    @api.post("/users/{name}/2fa/reset")
    def reset(name: str, body: Credentials, request: Request):
        actor = admin(request)
        if actor.name == name:
            raise Forbidden("use your own 2FA settings to disable 2FA")
        with auth._lock:
            recent = auth._reauth(actor, body.password)
            if actor.totp_secret and not auth._second_factor(actor, body.code):
                recent.append(auth._clock())
                raise Forbidden("invalid authentication code")
            auth.reset_2fa(name)
        return {"reset": True}

    @api.get("/notifications")
    def notification_list(request: Request):
        return request.app.state.notifications.view()

    @api.post("/notifications/read")
    async def notification_read(request: Request):
        body = await limited_json_body(request, 65536)
        identifiers = body.get("ids", [])
        if not isinstance(identifiers, list) or any(not isinstance(i, str) for i in identifiers):
            raise ValueError("Supply notification ids")
        return request.app.state.notifications.read(identifiers)

    @api.get("/settings/notifications")
    def notification_config(request: Request, response: Response):
        admin(request)
        response.headers["Cache-Control"] = "no-store"
        return request.app.state.notifications.config()

    @api.put("/settings/notifications")
    async def notification_save(request: Request):
        admin(request)
        return request.app.state.notifications.configure(await limited_json_body(request, 16384))

    @api.post("/settings/notifications/test")
    async def notification_test(request: Request):
        import asyncio

        admin(request)
        future = request.app.state.notifications.test()
        return await asyncio.wrap_future(future)

    @api.post("/settings/notifications/test-line")
    async def notification_test_line(request: Request):
        import asyncio

        admin(request)
        return await asyncio.wrap_future(request.app.state.notifications.test_line())

    @api.get("/settings")
    def view(request: Request):
        result = settings.view()
        user = getattr(request.state, "user", None)
        if auth is not None and (user is None or user.role != "admin"):
            for key in ("cases", "support_endpoint", "channels"):
                result.pop(key, None)
        return result

    @api.put("/settings/config")
    def config(body: SettingsConfig, request: Request):
        admin(request)
        settings.configure(body.model_dump())
        return settings.view()

    @api.post("/settings/subscription")
    async def license_upload(request: Request):
        admin(request)
        return settings.upload_license(await limited_json_body(request, 128 * 1024))

    @api.delete("/settings/subscription")
    def license_remove(request: Request):
        admin(request)
        return settings.remove_license()

    @api.post("/settings/support/cases", status_code=201)
    async def case(request: Request):
        # Reject entitlement before accepting a diagnostic body.
        actor = signed_in(request)
        if settings.subscription()["support"]["status"] != "Active":
            raise Forbidden("An active Support subscription is required to open a case")
        body = await limited_json_body(request, 6 * 1024 * 1024)
        import asyncio

        return await asyncio.to_thread(settings.open_case, actor.name, body)

    @api.post("/settings/updates/stage")
    async def stage(request: Request):
        signed_in(request)
        body = await limited_json_body(request, MAX_PACKAGE * 4 // 3 + 65536)
        try:
            package = base64.b64decode(body.get("package", ""), validate=True)
        except (ValueError, TypeError):
            raise ValueError("invalid package encoding") from None
        return settings.stage(body.get("envelope", {}), package)

    @api.post("/settings/updates/check")
    def check(request: Request):
        signed_in(request)
        settings.check()
        return settings.view()

    @api.post("/settings/updates/install", status_code=202)
    def install_update(request: Request):
        signed_in(request)
        return settings.start_job()

    @api.post("/settings/updates/rollback", status_code=202)
    def rollback(request: Request):
        signed_in(request)
        return settings.start_job(rollback=True)


async def limited_json_body(request, limit):
    import json

    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, "upload exceeds size limit")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("upload must contain valid JSON") from None
    if not isinstance(body, dict):
        raise ValueError("upload must be a JSON object")
    return body
