"""Company-hosted AI gateway. Provider credentials never reach customer installations."""

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import Field, ValidationError

from ..manager.settings import Settings, cluster_key
from ..manager.settings_api import limited_json_body
from .common import ChatIn, prompt_messages, redact


class GatewayIn(ChatIn):
    cluster_id: str = Field(min_length=1, max_length=100)
    license: dict
    snapshot: dict = Field(default_factory=dict)


class Usage:
    """Atomic, persistent per-installation request budget, including failed attempts."""

    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage "
                "(tenant TEXT, day TEXT, n INTEGER, PRIMARY KEY(tenant,day))"
            )
        os.chmod(path, 0o600)

    def reserve(self, tenant, limit):
        day = datetime.now(UTC).date().isoformat()
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT n FROM usage WHERE tenant=? AND day=?", (tenant, day)
            ).fetchone()
            used = row[0] if row else 0
            if used >= limit:
                raise HTTPException(429, "Daily AI allowance reached. Contact Support.")
            db.execute(
                "INSERT INTO usage VALUES (?,?,?) "
                "ON CONFLICT(tenant,day) DO UPDATE SET n=excluded.n",
                (tenant, day, used + 1),
            )
        return {"used": used + 1, "limit": limit, "resets": "00:00 UTC"}


class ClaudeProvider:
    def __init__(self, key, model, transport=None):
        self.key, self.model, self.transport = key, model, transport

    def answer(self, body):
        if not self.key or not self.model:
            raise HTTPException(503, "AI service is not provisioned. Contact Support.")
        system, messages = prompt_messages(body, body.snapshot if body.include_context else {})
        try:
            with httpx.Client(
                timeout=45, transport=self.transport, follow_redirects=False
            ) as client:
                response = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"},
                    json={
                        "model": self.model,
                        "max_tokens": 1200,
                        "system": system,
                        "messages": messages,
                    },
                )
                response.raise_for_status()
                data = response.json()
            text = "\n".join(c["text"] for c in data["content"] if c.get("type") == "text")
            if not text.strip():
                raise ValueError("No text response")
            return redact(text[:12000])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(
                503, "AI service is temporarily unavailable. Please retry later."
            ) from exc


def create_gateway(profiles, usage_path, provider, verifier=None):
    app = FastAPI(title="ibHiCS Support AI gateway", docs_url=None, redoc_url=None)
    usage = Usage(usage_path)

    @app.post("/v1/chat")
    async def chat(request: Request):
        try:
            body = GatewayIn.model_validate(await limited_json_body(request, 40000))
        except ValidationError as exc:
            raise HTTPException(422, "Invalid chat request") from exc
        tenant = cluster_key(body.cluster_id)
        profile = profiles.get(tenant)
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        digest = hashlib.sha256(token.encode()).hexdigest()
        if (
            not profile
            or not profile.get("enabled", False)
            or not token
            or not hmac.compare_digest(digest, profile.get("token_sha256", ""))
        ):
            raise HTTPException(403, "AI service is not enrolled for this installation")
        settings = Settings(
            cluster_id=lambda: tenant, node_count=lambda: profile["nodes"], verifier=verifier
        )
        settings.data["license"] = body.license
        if settings.subscription()["support"]["status"] != "Active":
            raise HTTPException(403, "An active signed Support subscription is required")
        quota = await asyncio.to_thread(
            usage.reserve, tenant, min(1000, max(1, int(profile["daily_requests"])))
        )
        reply = await asyncio.to_thread(provider.answer, body)
        return {"reply": reply, "allowance": quota}

    return app


def main():
    """Provision trusted profiles and keys on the company's gateway host only."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--usage", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8891)
    parser.add_argument("--trust-dir", type=Path, default=None)
    args = parser.parse_args()
    profiles = json.loads(args.profiles.read_text())
    provider = ClaudeProvider(
        os.environ.get("ANTHROPIC_API_KEY", ""), os.environ.get("IBHICS_AI_MODEL", "")
    )
    from ..signatures import Ed25519Verifier

    uvicorn.run(
        create_gateway(profiles, args.usage, provider, Ed25519Verifier(args.trust_dir)),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
