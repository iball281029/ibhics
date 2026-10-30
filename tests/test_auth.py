"""Sign-in, roles, sessions and the audit log."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.auth import Auth, AuthError, Role, hash_password, verify_password

ADMIN_PW = "first-Admin-pw"


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    auth = Auth(state_path=tmp_path / "users.json", audit_path=tmp_path / "audit.log", clock=clock)
    auth.bootstrap(ADMIN_PW)
    client = TestClient(create_app(FakeDriver(demo_console=False), auth=auth))
    return client, auth, clock, tmp_path


def sign_in(client, name, password):
    return client.post("/api/v1/auth/login", json={"username": name, "password": password})


def admin(client, auth):
    """Sign in as admin and replace the initial password, as the first sign-in requires."""
    assert sign_in(client, "admin", ADMIN_PW).status_code == 200
    resp = client.post(
        "/api/v1/auth/password",
        json={"current_password": ADMIN_PW, "new_password": "Admin-pw-2"},
    )
    assert resp.status_code == 204, resp.text


def add_user(client, name, role, password="Temp-pw-123"):
    resp = client.post("/api/v1/users", json={"name": name, "role": role, "password": password})
    assert resp.status_code == 201, resp.text


def as_user(env, name, role):
    """A fresh browser signed in as a new user who has already set their own password."""
    client, auth, *_ = env
    other = TestClient(client.app)
    auth.add_user(name, Role(role), "Temp-pw-123")
    assert sign_in(other, name, "Temp-pw-123").status_code == 200
    other.post(
        "/api/v1/auth/password",
        json={"current_password": "Temp-pw-123", "new_password": f"{name}-own-pw"},
    )
    return other


def test_password_hashing():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "md5$abc")


def test_everything_but_health_and_login_needs_a_session(env):
    client, *_ = env
    assert client.get("/api/v1/health").json()["auth"] is True
    assert client.get("/api/v1/vms").status_code == 401
    assert client.post("/api/v1/vms", json={"name": "x"}).status_code == 401
    assert client.get("/").status_code == 200  # the page itself, which then asks to sign in


def test_initial_password_must_be_replaced_first(env):
    client, *_ = env
    resp = sign_in(client, "admin", ADMIN_PW)
    assert resp.status_code == 200
    assert resp.json()["user"]["must_change_password"] is True
    assert "httponly" in resp.headers["set-cookie"].lower()
    assert "samesite=strict" in resp.headers["set-cookie"].lower()

    blocked = client.get("/api/v1/vms")
    assert blocked.status_code == 403 and "initial password" in blocked.json()["detail"]
    assert client.get("/api/v1/auth/me").json()["name"] == "admin"

    weak = client.post(
        "/api/v1/auth/password", json={"current_password": ADMIN_PW, "new_password": "short"}
    )
    assert weak.status_code == 422
    wrong = client.post(
        "/api/v1/auth/password",
        json={"current_password": "nope", "new_password": "Long-enough-1"},
    )
    assert wrong.status_code == 403

    admin_ok = client.post(
        "/api/v1/auth/password",
        json={"current_password": ADMIN_PW, "new_password": "Long-enough-1"},
    )
    assert admin_ok.status_code == 204
    assert client.get("/api/v1/vms").status_code == 200


def test_wrong_password_and_lockout(env):
    client, _, clock, _ = env
    for _ in range(5):
        resp = sign_in(client, "admin", "guess")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "wrong username or password"
    assert sign_in(client, "ghost", "guess").json()["detail"] == "wrong username or password"
    locked = sign_in(client, "admin", ADMIN_PW)
    assert locked.status_code == 401 and "too many" in locked.json()["detail"]
    clock.now += 301
    assert sign_in(client, "admin", ADMIN_PW).status_code == 200


def test_bearer_token_for_scripts(env):
    client, *_ = env
    admin(client, None)
    token = sign_in(client, "admin", "Admin-pw-2").json()["token"]
    script = TestClient(client.app)
    assert script.get("/api/v1/vms").status_code == 401
    ok = script.get("/api/v1/vms", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


def test_roles(env):
    client, auth, *_ = env
    admin(client, auth)
    viewer = as_user(env, "vera", "viewer")
    operator = as_user(env, "otto", "operator")

    assert viewer.get("/api/v1/vms").status_code == 200
    resp = viewer.post("/api/v1/vms", json={"name": "v1", "disks": [{"size_gb": 1}]})
    assert resp.status_code == 403 and "operator" in resp.json()["detail"]

    resp = operator.post("/api/v1/vms", json={"name": "v1", "disks": [{"size_gb": 1}]})
    assert resp.status_code == 201
    uuid = resp.json()["uuid"]
    assert operator.post(f"/api/v1/vms/{uuid}/start").status_code == 200
    pool = {"name": "p2", "type": "dir"}
    node = client.get("/api/v1/vms").json()[0]["node"]
    assert operator.post(f"/api/v1/nodes/{node}/pools", json=pool).status_code == 403
    assert operator.get("/api/v1/users").status_code == 403
    assert operator.get("/api/v1/audit").status_code == 403

    assert client.post(f"/api/v1/nodes/{node}/pools", json=pool).status_code == 201


def test_user_management(env):
    client, auth, *_ = env
    admin(client, auth)
    add_user(client, "otto", "operator")
    names = {u["name"]: u for u in client.get("/api/v1/users").json()}
    assert names["otto"]["must_change_password"] is True
    assert "password_hash" not in names["otto"]

    bad = client.post("/api/v1/users", json={"name": "Bad Name", "role": "viewer", "password": "x"})
    assert bad.status_code == 422
    dup = client.post(
        "/api/v1/users", json={"name": "otto", "role": "viewer", "password": "Another-pw-1"}
    )
    assert dup.status_code == 422

    otto = TestClient(client.app)
    assert sign_in(otto, "otto", "Temp-pw-123").status_code == 200
    # disabling ends otto's session at once
    assert client.patch("/api/v1/users/otto", json={"enabled": False}).status_code == 200
    assert otto.get("/api/v1/auth/me").status_code == 401
    assert sign_in(otto, "otto", "Temp-pw-123").status_code == 401

    # the last admin cannot lock themselves out
    me = client.patch("/api/v1/users/admin", json={"role": "viewer"})
    assert me.status_code == 422
    assert client.delete("/api/v1/users/admin").status_code == 422
    assert client.delete("/api/v1/users/otto").status_code == 204
    assert client.delete("/api/v1/users/otto").status_code == 404


def test_session_idle_timeout_and_logout(env):
    client, _, clock, _ = env
    admin(client, None)
    clock.now += 29 * 60
    assert client.get("/api/v1/vms").status_code == 200  # activity keeps it alive
    clock.now += 31 * 60
    resp = client.get("/api/v1/vms")
    assert resp.status_code == 401 and "sign in again" in resp.json()["detail"]

    assert sign_in(client, "admin", "Admin-pw-2").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/vms").status_code == 401


def test_audit_log(env):
    client, auth, _, tmp_path = env
    sign_in(client, "admin", "nope")
    admin(client, auth)
    client.post("/api/v1/vms", json={"name": "a1", "disks": [{"size_gb": 1}]})

    entries = client.get("/api/v1/audit").json()
    actions = [(e["user"], e["action"], e["target"], e["result"]) for e in reversed(entries)]
    assert actions[0] == ("admin", "LOGIN", "failed", 401)
    assert ("admin", "LOGIN", "ok", 200) in actions
    assert ("admin", "POST", "/api/v1/auth/password", 204) in actions
    assert ("admin", "POST", "/api/v1/vms", 201) in actions
    assert (tmp_path / "audit.log").read_text().count("\n") == len(entries)


def test_users_survive_a_restart(env):
    client, auth, _, tmp_path = env
    admin(client, auth)
    add_user(client, "otto", "operator")
    again = Auth(state_path=tmp_path / "users.json")
    again.load()
    assert {u.name for u in again.users()} == {"admin", "otto"}
    assert again.bootstrap("ignored") is None  # users exist: no new admin
    assert (tmp_path / "users.json").stat().st_mode & 0o077 == 0
    with pytest.raises(AuthError):
        again.authenticate(sign_in(client, "admin", "Admin-pw-2").json()["token"])


def test_bootstrap_generates_a_password(tmp_path):
    auth = Auth(state_path=tmp_path / "u.json")
    password = auth.bootstrap()
    assert password and len(password) >= 12
    assert auth.login("admin", password).user == "admin"


def test_console_needs_operator_and_same_origin(env):
    client, auth, *_ = env
    admin(client, auth)
    uuid = client.post("/api/v1/vms", json={"name": "c1", "disks": [{"size_gb": 1}]}).json()["uuid"]
    client.post(f"/api/v1/vms/{uuid}/start")
    viewer = as_user(env, "vera", "viewer")

    def reason(c, headers=None):
        with c.websocket_connect(f"/api/v1/vms/{uuid}/console", headers=headers or {}) as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_bytes()
        return closed.value.reason

    assert "sign in" in reason(TestClient(client.app))
    assert "operator" in reason(viewer)
    assert "another site" in reason(client, {"origin": "https://evil.example"})
    # admin from our own page gets past auth; the fake VM simply has no display
    assert "no VNC display" in reason(client, {"origin": "http://testserver"})
