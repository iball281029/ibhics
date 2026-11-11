"""Authenticated local status/pairing UI for the Arbiter witness."""

import ctypes
import hmac
import socket
import subprocess
import threading
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..manager.auth import _DUMMY_HASH, Auth, AuthError, Role, User, hash_password
from ..storage import readiness

HERE = Path(__file__).parent / "static"
COOKIE = "arbiter_session"
CRYPT_LOCK = threading.Lock()


def verify_root(password: str) -> bool:
    """Verify the current Linux root hash without copying it into UI user storage."""
    line = next(
        (s for s in Path("/etc/shadow").read_text().splitlines() if s.startswith("root:")), ""
    )
    fields = line.split(":")
    stored = fields[1] if len(fields) > 1 else ""
    if not stored or stored.startswith(("!", "*")):
        return False
    crypt = ctypes.CDLL("libcrypt.so.1").crypt
    crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    crypt.restype = ctypes.c_char_p
    with CRYPT_LOCK:
        candidate = crypt(password.encode(), stored.encode())
        return bool(candidate and hmac.compare_digest(candidate, stored.encode()))


class RootAuth(Auth):
    def __init__(self, verifier):
        super().__init__(idle_timeout=900, max_age=3600)
        self.verifier = verifier
        self._users["root"] = User("root", Role.ADMIN, _DUMMY_HASH)

    def login(self, name: str, password: str):
        with self._lock:
            now = self._clock()
            recent = [t for t in self._failures.get("root", []) if now - t < self._lockout]
            if len(recent) >= self._max_failures:
                raise AuthError("too many failed sign-ins; try again in a few minutes")
            valid = name == "root" and self.verifier(password)
            self._users["root"].password_hash = hash_password(password) if valid else _DUMMY_HASH
            return super().login("root", password)


class Login(BaseModel):
    username: str = Field(max_length=32)
    password: str = Field(min_length=1, max_length=1024)


def node_status() -> dict:
    values = {}
    for line in Path("/etc/ibhics/ibhics.env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    running = (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", "ibhics-agent"], timeout=4, check=False
        ).returncode
        == 0
    )
    address = values.get("IBHICS_AGENT_HOST", "")
    return {
        "name": values.get("IBHICS_NODE_NAME", socket.gethostname()),
        "role": values.get("IBHICS_ROLE", "witness"),
        "address": address,
        "agent_url": f"http://{address}:8007",
        "agent_running": running,
        "pairing_key": values.get("IBHICS_CLUSTER_TOKEN", ""),
        "replication": readiness(),
    }


def create_app(verifier=verify_root, status_reader=node_status) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    auth = RootAuth(verifier)

    @app.middleware("http")
    async def secure_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response

    def signed_in(request: Request):
        try:
            return auth.authenticate(request.cookies.get(COOKIE))
        except AuthError as exc:
            raise HTTPException(401, "Sign in first") from exc

    @app.get("/")
    def index():
        return FileResponse(HERE / "index.html")

    @app.get("/app.js")
    def javascript():
        return FileResponse(HERE / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    def css():
        return FileResponse(HERE / "style.css", media_type="text/css")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "arbiter-ui"}

    @app.post("/api/login")
    def login(body: Login, response: Response):
        try:
            session = auth.login(body.username, body.password)
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        response.set_cookie(
            COOKIE, session.token, secure=True, httponly=True, samesite="strict", max_age=3600
        )
        return {"username": "root"}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, user: Annotated[User, Depends(signed_in)]):
        auth.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/status")
    def status(user: Annotated[User, Depends(signed_in)]):
        return status_reader()

    return app
