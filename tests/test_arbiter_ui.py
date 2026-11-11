"""Arbiter web console: the pairing key only after a root sign-in."""

from fastapi.testclient import TestClient

from ibhics.arbiter_ui import create_app

SECRET = "fixture-pairing-key"


def make():
    app = create_app(
        verifier=lambda pw: pw == "correct-test-password",
        status_reader=lambda: {"pairing_key": SECRET, "role": "witness"},
    )
    return TestClient(app, base_url="https://arbiter.test")


def test_key_requires_login_and_is_not_in_html():
    c = make()
    assert c.get("/api/status").status_code == 401
    assert SECRET not in c.get("/").text


def test_login_cookie_and_logout():
    c = make()
    r = c.post("/api/login", json={"username": "root", "password": "correct-test-password"})
    assert r.status_code == 200
    header = r.headers["set-cookie"].lower()
    assert "secure" in header and "httponly" in header and "samesite=strict" in header
    assert c.get("/api/status").json()["pairing_key"] == SECRET
    assert c.get("/api/status").headers["cache-control"] == "no-store"
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/status").status_code == 401


def test_wrong_username_or_password_cannot_read_key():
    c = make()
    for user, pw in [("admin", "correct-test-password"), ("root", "bad-password")]:
        assert c.post("/api/login", json={"username": user, "password": pw}).status_code == 401
    assert c.get("/api/status").status_code == 401


def test_repeated_failed_login_is_limited():
    c = make()
    for _ in range(5):
        c.post("/api/login", json={"username": "root", "password": "wrong"})
    r = c.post("/api/login", json={"username": "root", "password": "correct-test-password"})
    assert r.status_code == 401 and "too many" in r.json()["detail"]


def test_static_files_are_packaged():
    c = make()
    assert c.get("/app.js").status_code == 200 and c.get("/style.css").status_code == 200


def test_certificate_made_once_with_private_key_mode(tmp_path):
    import shutil

    import pytest

    from ibhics.arbiter_ui.__main__ import ensure_certificate

    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    cert, key = tmp_path / "tls/arbiter.crt", tmp_path / "tls/arbiter.key"
    ensure_certificate(cert, key)
    assert key.stat().st_mode & 0o777 == 0o600 and b"CERTIFICATE" in cert.read_bytes()
    before = cert.read_bytes()
    ensure_certificate(cert, key)
    assert cert.read_bytes() == before
