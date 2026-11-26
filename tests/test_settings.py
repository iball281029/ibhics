"""Settings workflows with real OpenSSL Ed25519 and fake node execution."""

import base64
import hashlib
import json
import shutil
import subprocess
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.auth import Auth, AuthError, Forbidden, Role
from ibhics.manager.permissions import category
from ibhics.manager.settings import Settings
from ibhics.manager.totp import totp
from ibhics.signatures import Ed25519Verifier

NOW = 1_800_000_000.0
TODAY = date(2027, 1, 15)
PASSWORD = "Settings-password-42"
SIGNING = None
VERIFIER = None


@pytest.fixture(scope="session", autouse=True)
def signing_keys(tmp_path_factory):
    global SIGNING, VERIFIER
    if not shutil.which("openssl"):
        pytest.skip("OpenSSL CLI is not installed; real Ed25519 signing unavailable")
    root = tmp_path_factory.mktemp("ed25519")
    SIGNING = root
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(root / "key.pem")],
        check=True,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        [
            "openssl",
            "pkey",
            "-in",
            str(root / "key.pem"),
            "-pubout",
            "-out",
            str(root / "release-public.pem"),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    VERIFIER = Ed25519Verifier(root)


def sign(payload):
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    data, signature = SIGNING / "data", SIGNING / "sig"
    data.write_bytes(message)
    subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(SIGNING / "key.pem"),
            "-rawin",
            "-in",
            str(data),
            "-out",
            str(signature),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return {"payload": payload, "signature": base64.b64encode(signature.read_bytes()).decode()}


def verify(message, signature):
    return VERIFIER(message, signature)


def license_file(**changes):
    return sign(
        {
            "customer": "Test Customer",
            "cluster_id": "cluster-test",
            "nodes": 3,
            "update_until": "2027-02-01",
            "support_until": "2027-02-15",
            **changes,
        }
    )


def manifest(package=b"fake wheel", **changes):
    return sign(
        {
            "filename": "ibhics-1.4.0-py3-none-any.whl",
            "version": "1.4.0",
            "released": "2027-01-01",
            "notes": "Security fixes",
            "sha256": hashlib.sha256(package).hexdigest(),
            "url": "https://updates.invalid/package.whl",
            **changes,
        }
    )


@pytest.fixture
def env(tmp_path):
    auth = Auth(tmp_path / "users.json", tmp_path / "audit.jsonl", clock=lambda: NOW)
    auth.bootstrap(PASSWORD, must_change=False)
    settings = Settings(
        tmp_path / "settings.json",
        cluster_id=lambda: "cluster-test",
        node_count=lambda: 3,
        verifier=verify,
        today=lambda: TODAY,
    )
    app = create_app(FakeDriver(demo_console=False), auth=auth, settings=settings)
    client = TestClient(app)
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
        ).status_code
        == 200
    )
    return client, auth, settings


def browser(env, role, name="testuser"):
    client, auth, _ = env
    auth.add_user(name, role, PASSWORD)
    auth.user(name).must_change_password = False
    result = TestClient(client.app)
    assert (
        result.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).status_code
        == 200
    )
    return result


def test_subscription_none_valid_invalid_and_persistence(env):
    client, _, settings = env
    assert settings.subscription()["update"]["status"] == "None"
    assert client.post("/api/v1/settings/subscription", json=license_file()).status_code == 200
    state = settings.subscription()
    assert state["update"] == {"status": "Active", "until": "2027-02-01", "days": 17}
    assert state["support"]["status"] == "Active"
    bad = license_file()
    bad["payload"]["nodes"] = 20
    assert client.post("/api/v1/settings/subscription", json=bad).status_code == 422
    for changes in (
        {"cluster_id": "other"},
        {"nodes": 2},
        {"support_until": "invalid"},
        {"customer": ""},
    ):
        assert (
            client.post("/api/v1/settings/subscription", json=license_file(**changes)).status_code
            == 422
        )
    again = Settings(
        settings.path,
        cluster_id=lambda: "cluster-test",
        node_count=lambda: 3,
        verifier=verify,
        today=lambda: TODAY,
    )
    assert again.subscription() == state
    assert settings.path.stat().st_mode & 0o077 == 0


def test_separate_expiry_and_expiry_day(env):
    _, _, settings = env
    settings.upload_license(license_file(update_until="2027-01-14", support_until="2027-01-15"))
    assert settings.subscription()["update"]["status"] == "Expired"
    assert settings.subscription()["support"]["status"] == "Active"
    settings.upload_license(license_file(support_until="2027-01-14"))
    assert settings.subscription()["support"]["status"] == "Expired"


def test_default_build_refuses_signed_uploads_without_dependency(env):
    client, _, settings = env
    settings.verifier = None
    assert client.post("/api/v1/settings/subscription", json=license_file()).status_code == 422
    assert (
        client.post(
            "/api/v1/settings/updates/stage",
            json={"envelope": manifest(), "package": base64.b64encode(b"fake wheel").decode()},
        ).status_code
        == 422
    )
    assert settings.subscription()["support"]["status"] == "None"
    assert client.post("/api/v1/settings/updates/install").status_code == 422
    assert not settings.data["jobs"]


def test_support_403_without_support_even_admin(env):
    client, _, settings = env
    assert client.post("/api/v1/settings/support/cases", json={}).status_code == 403
    settings.upload_license(license_file(support_until="2027-01-14"))
    assert (
        client.post(
            "/api/v1/settings/support/cases", json={"subject": "Help", "details": "VM"}
        ).status_code
        == 403
    )
    assert settings.data["cases"] == []


def test_case_local_delivery_failure_and_attachment(env):
    client, _, settings = env
    settings.upload_license(license_file())
    body = {
        "subject": "Need help",
        "details": "Diagnostic details",
        "severity": "high",
        "diagnostic": base64.b64encode(b"diagnostics").decode(),
    }
    response = client.post("/api/v1/settings/support/cases", json=body)
    assert response.status_code == 201
    assert response.json()["delivery"] == "local"
    assert "diagnostic" not in response.json()
    settings.data["support_endpoint"] = "https://support.invalid/cases"
    sent = []
    settings.send = lambda endpoint, case: sent.append((endpoint, case))
    assert client.post("/api/v1/settings/support/cases", json=body).json()["delivery"] == "sent"
    assert sent[0][1]["diagnostic"] == body["diagnostic"]
    settings.send = lambda *_: (_ for _ in ()).throw(RuntimeError("remote failure"))
    assert (
        "delivery failed"
        in client.post("/api/v1/settings/support/cases", json=body).json()["delivery"]
    )
    assert len(settings.data["cases"]) == 3
    body["diagnostic"] = "not base64!"
    assert client.post("/api/v1/settings/support/cases", json=body).status_code == 422


def test_update_signature_checksum_and_entitlement(env):
    client, _, settings = env
    package = b"fake wheel"
    body = {"envelope": manifest(), "package": base64.b64encode(package).decode()}
    assert client.post("/api/v1/settings/updates/stage", json=body).status_code == 403
    settings.upload_license(license_file())
    bad = manifest()
    bad["payload"]["version"] = "tampered"
    assert (
        client.post("/api/v1/settings/updates/stage", json={**body, "envelope": bad}).status_code
        == 422
    )
    assert (
        client.post("/api/v1/settings/updates/stage", json={**body, "package": "eA=="}).status_code
        == 422
    )
    future = manifest(released="2027-02-02")
    assert (
        client.post("/api/v1/settings/updates/stage", json={**body, "envelope": future}).status_code
        == 403
    )
    assert client.post("/api/v1/settings/updates/stage", json=body).status_code == 200
    # Expiry does not stop an entitled historical release from being manually installed.
    settings.upload_license(license_file(update_until="2027-01-14"))
    assert client.post("/api/v1/settings/updates/stage", json=body).status_code == 200
    settings.verifier = lambda *_: False
    assert settings.license() is None


class FakeUpdater:
    def __init__(self, fail=""):
        self.calls = []
        self.fail = fail
        self.versions = {"active": "old", "standby": "old"}

    def nodes(self):
        return ["active", "standby"]

    def active(self):
        return "active"

    def prepare(self, node, package, manifest):
        self.calls.append(("prepare", node))
        assert package == b"fake wheel"

    def install(self, node):
        self.calls.append(("install", node))
        self.versions[node] = "new"

    def healthy(self, node):
        return node != self.fail or self.versions[node] == "old"

    def rollback(self, node):
        self.calls.append(("rollback", node))
        self.versions[node] = "old"


def wait_job(settings):
    deadline = time.monotonic() + 3
    while settings.data["jobs"][-1]["state"] in ("running", "queued"):
        assert time.monotonic() < deadline
        time.sleep(0.005)
    # Synchronize the final durable write before test fixture cleanup.
    with settings.lock:
        return settings.data["jobs"][-1]


@pytest.mark.parametrize("failed", ["", "standby", "active"])
def test_rolling_order_and_health_rollback(env, failed):
    client, _, settings = env
    settings.updater = updater = FakeUpdater(failed)
    settings.upload_license(license_file())
    settings.stage(manifest(), b"fake wheel")
    assert client.post("/api/v1/settings/updates/install").status_code == 202
    job = wait_job(settings)
    assert updater.calls[:2] == [("prepare", "standby"), ("prepare", "active")]
    assert updater.calls[2] == ("install", "standby")
    if failed:
        assert job["state"] == "failed"
        assert job["detail"] == "Rolled back changed nodes"
        assert set(updater.versions.values()) == {"old"}
    else:
        assert job["state"] == "done"
        assert updater.calls[3] == ("install", "active")
        assert client.post("/api/v1/settings/updates/rollback").status_code == 202
        assert wait_job(settings)["state"] == "done"
        assert set(updater.versions.values()) == {"old"}


def test_auto_check_daily_download_wait_and_expiry(env):
    _, _, settings = env
    settings.upload_license(license_file())
    settings.configure(
        {
            "channels": {"stable": "https://updates.invalid/stable.json", "beta": ""},
            "channel": "stable",
            "support_endpoint": "",
            "auto_install": False,
        }
    )
    calls = []

    def fetch(url):
        calls.append(url)
        return b"fake wheel" if url.endswith(".whl") else json.dumps(manifest()).encode()

    settings.fetch = fetch
    settings.check(daily=True)
    assert len(calls) == 2
    assert settings.view()["staged"]["version"] == "1.4.0"
    assert settings.data["jobs"] == []
    settings.check(daily=True)
    assert len(calls) == 2
    settings.upload_license(license_file(update_until="2027-01-14"))
    with pytest.raises(ValueError):
        settings.check()
    assert len(calls) == 3  # no package download after subscription expiry


def test_install_rechecks_staged_signature(env):
    client, _, settings = env
    settings.updater = FakeUpdater()
    settings.upload_license(license_file())
    settings.stage(manifest(), b"fake wheel")
    settings.data["staged"]["envelope"]["payload"]["version"] = "tamper"
    assert client.post("/api/v1/settings/updates/install").status_code == 422
    assert not settings.updater.calls


def test_rbac_roles_custom_permissions_and_last_admin(env):
    client, auth, settings = env
    operator = browser(env, "operator", "operator1")
    viewer = browser(env, "viewer", "viewer1")
    for path in (
        "/settings/subscription",
        "/settings/updates/stage",
        "/settings/updates/install",
        "/settings/updates/rollback",
        "/settings/updates/check",
        "/users",
    ):
        assert operator.post("/api/v1" + path, json={}).status_code == 403
    assert viewer.post("/api/v1/vms", json={}).status_code == 403
    for method, path in (
        ("put", "/edge/rules"),
        ("post", "/backup/targets"),
        ("post", "/migrate/sources"),
        ("post", "/networks"),
    ):
        # Invalid inputs are validated only after permissions pass.
        assert getattr(operator, method)("/api/v1" + path, json={}).status_code != 403
        assert getattr(viewer, method)("/api/v1" + path, json={}).status_code == 403
    assert client.put("/api/v1/roles/netops", json={"permissions": ["network"]}).status_code == 200
    custom = browser(env, "netops", "networkuser")
    assert custom.put("/api/v1/edge/rules", json={}).status_code != 403
    assert custom.post("/api/v1/vms", json={}).status_code == 403
    assert custom.put("/api/v1/roles/netops", json={"permissions": ["users"]}).status_code == 403
    assert custom.get("/api/v1/settings").status_code == 200
    assert "channels" not in custom.get("/api/v1/settings").json()
    assert client.patch("/api/v1/users/admin", json={"role": "viewer"}).status_code == 422
    assert client.patch("/api/v1/users/admin", json={"enabled": False}).status_code == 422
    auth.define_role("usermgr", ["users"])
    manager = browser(env, "usermgr", "usermgr1")
    assert manager.patch("/api/v1/users/admin", json={"password": PASSWORD}).status_code == 403
    assert (
        manager.post(
            "/api/v1/users", json={"name": "evil", "password": PASSWORD, "role": "admin"}
        ).status_code
        == 403
    )
    settings.upload_license(license_file())
    assert viewer.post("/api/v1/settings/support/cases", json={}).status_code == 403


@pytest.mark.parametrize(
    "timestamp,sha1,sha256,sha512",
    [
        (59, "94287082", "46119246", "90693936"),
        (1111111109, "07081804", "68084774", "25091201"),
        (1111111111, "14050471", "67062674", "99943326"),
        (1234567890, "89005924", "91819424", "93441116"),
        (2000000000, "69279037", "90698825", "38618901"),
        (20000000000, "65353130", "77737706", "47863826"),
    ],
)
def test_rfc6238_vectors(timestamp, sha1, sha256, sha512):
    for algorithm, length, expected in (
        ("sha1", 20, sha1),
        ("sha256", 32, sha256),
        ("sha512", 64, sha512),
    ):
        key = (b"1234567890" * 7)[:length]
        secret = base64.b32encode(key).decode()
        assert totp(secret, timestamp, digits=8, algorithm=algorithm) == expected


def test_totp_enrollment_login_replay_recovery_and_persistence(env):
    client, auth, _ = env
    response = client.post("/api/v1/auth/2fa/setup", json={"password": PASSWORD})
    assert response.status_code == 200
    secret = response.json()["secret"]
    assert response.headers["cache-control"] == "no-store"
    assert secret not in json.dumps(client.get("/api/v1/auth/me").json())
    assert client.post("/api/v1/auth/2fa/confirm", json={"code": "bad"}).status_code == 403
    result = client.post("/api/v1/auth/2fa/confirm", json={"code": totp(secret, NOW)})
    codes = result.json()["recovery_codes"]
    assert len(codes) == len(set(codes)) == 10
    for code in codes:
        assert code not in auth._state_path.read_text()
    assert client.get("/api/v1/auth/me").status_code == 401  # old sessions revoked
    with pytest.raises(AuthError):
        auth.login("admin", PASSWORD)
    with pytest.raises(AuthError):
        auth.login("admin", PASSWORD, totp(secret, NOW))  # enrollment code cannot be replayed
    session = auth.login("admin", PASSWORD, codes[0])
    assert auth.authenticate(session.token).name == "admin"
    with pytest.raises(AuthError):
        auth.login("admin", PASSWORD, codes[0])
    again = Auth(auth._state_path, clock=lambda: NOW + 30)
    again.load()
    assert again.user("admin").totp_secret == secret
    again.login("admin", PASSWORD, totp(secret, NOW + 30))
    with pytest.raises(AuthError):
        again.login("admin", PASSWORD, totp(secret, NOW + 30))
    assert secret not in auth._audit_path.read_text()
    assert not any(code in auth._audit_path.read_text() for code in codes)


def test_enforcement_restricts_password_only_sessions_and_allows_enrollment(env):
    client, auth, _ = env
    viewer = browser(env, "viewer", "viewer1")
    assert client.put("/api/v1/security/2fa", json={"require_2fa": True}).status_code == 200
    assert viewer.get("/api/v1/vms").status_code == 403
    assert viewer.get("/api/v1/auth/me").status_code == 200
    assert viewer.get("/api/v1/auth/2fa").json()["required"] is True
    assert viewer.post("/api/v1/auth/2fa/setup", json={"password": PASSWORD}).status_code == 200
    auth.user("viewer1").totp_secret = base64.b32encode(b"test-test-test-test!!").decode()
    with pytest.raises(Forbidden):
        auth.disable_2fa("viewer1", PASSWORD, totp(auth.user("viewer1").totp_secret, NOW))


def test_admin_reset_only_other_users_with_reauth(env):
    client, auth, _ = env
    auth.add_user("other", Role.VIEWER, PASSWORD)
    auth.user("other").totp_secret = "secret"
    assert (
        client.post("/api/v1/users/admin/2fa/reset", json={"password": PASSWORD}).status_code == 403
    )
    assert (
        client.post("/api/v1/users/other/2fa/reset", json={"password": "wrong"}).status_code == 403
    )
    assert (
        client.post("/api/v1/users/other/2fa/reset", json={"password": PASSWORD}).status_code == 200
    )
    assert not auth.user("other").totp_secret


def test_sensitive_validation_does_not_echo_secrets(env):
    client, _, _ = env
    secret = "private-secret" * 30
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": secret})
    assert response.status_code == 422
    assert secret not in response.text


def test_every_authenticated_route_has_a_valid_category(env):
    client, _, _ = env
    paths = client.app.openapi()["paths"]
    assert len(paths) > 100
    for path in paths:
        assert category(path) in {
            "vms",
            "storage",
            "network",
            "backup",
            "replication",
            "cluster",
            "updates",
            "users",
            "subscription",
        }


def test_viewer_and_empty_custom_role_cannot_mutate_any_endpoint(env):
    """Exercise the actual dependency on every registered non-public mutation route."""
    import re

    client, auth, _ = env
    auth.define_role("readonly", [])
    clients = [browser(env, "viewer", "allviewer"), browser(env, "readonly", "allcustom")]
    count = 0
    for path, operations in client.app.openapi()["paths"].items():
        if not path.startswith("/api/v1/") or path.startswith(
            ("/api/v1/auth/", "/api/v1/p2v/upload/", "/api/v1/notifications/read")
        ):
            continue
        for operation in operations:
            method = operation.upper()
            if method not in {"POST", "PATCH", "PUT", "DELETE"}:
                continue
            for readonly in clients:
                url = re.sub(r"\{[^}]+\}", "test", path)
                response = readonly.request(method, url, json={})
                assert response.status_code == 403, (method, path, response.text)
            count += 1
    assert count > 100


def test_custom_role_persists_and_role_reduction_revokes_sessions(env):
    client, auth, _ = env
    auth.define_role("netops", ["network"])
    custom = browser(env, "netops", "customuser")
    assert custom.get("/api/v1/auth/me").json()["permissions"] == ["network"]
    auth.define_role("netops", [])
    assert custom.get("/api/v1/auth/me").status_code == 401
    again = Auth(auth._state_path)
    again.load()
    assert again.user("customuser").role == "netops"
    assert again.permissions(again.user("customuser")) == set()


def test_2fa_confirmation_rate_limit_and_expiry(env):
    client, auth, _ = env
    secret = auth.setup_2fa("admin", PASSWORD)["secret"]
    for _ in range(5):
        with pytest.raises(Forbidden):
            auth.confirm_2fa("admin", "not-a-code")
    with pytest.raises(Forbidden, match="too many"):
        auth.confirm_2fa("admin", totp(secret, NOW))
    auth._clock = lambda: NOW + 601
    with pytest.raises(Forbidden, match="expired"):
        auth.confirm_2fa("admin", totp(secret, NOW + 601))
    assert not auth.user("admin").totp_secret


def test_no_auth_settings_security_endpoints_fail_closed():
    client = TestClient(create_app(FakeDriver(demo_console=False)))
    for path in ("setup", "confirm", "disable"):
        assert client.post(f"/api/v1/auth/2fa/{path}", json={}).status_code == 403
    assert client.post("/api/v1/settings/subscription", json={}).status_code == 403


def test_auto_skips_installed_release_before_downloading(env):
    _, _, settings = env
    settings.upload_license(license_file())
    settings.data["channels"]["stable"] = "https://updates.invalid/stable.json"
    calls = []

    def fetch(url):
        calls.append(url)
        return json.dumps(manifest(version=settings.current_version)).encode()

    settings.fetch = fetch
    settings.check()
    assert len(calls) == 1
    assert settings.data["staged"] is None


def test_failed_rollback_and_interrupted_restart_block_new_jobs(env):
    client, _, settings = env
    settings.updater = updater = FakeUpdater()
    settings.upload_license(license_file())
    settings.stage(manifest(), b"fake wheel")
    client.post("/api/v1/settings/updates/install")
    assert wait_job(settings)["state"] == "done"
    updater.rollback = lambda node: (_ for _ in ()).throw(RuntimeError("rollback failed"))
    client.post("/api/v1/settings/updates/rollback")
    assert wait_job(settings)["state"] == "interrupted"
    assert client.post("/api/v1/settings/updates/install").status_code == 422
    settings.data["jobs"][-1]["state"] = "running"
    settings.save()
    again = Settings(settings.path, verifier=verify, updater=FakeUpdater())
    assert again.data["jobs"][-1]["state"] == "interrupted"
    with pytest.raises(ValueError, match="reconciliation"):
        again.start_job(rollback=True)


def test_auto_install_requires_opt_in_and_uses_job(env):
    _, _, settings = env
    settings.upload_license(license_file())
    settings.updater = FakeUpdater()
    settings.configure(
        {
            "channels": {"stable": "https://updates.invalid/stable.json", "beta": ""},
            "channel": "stable",
            "support_endpoint": "",
            "auto_install": True,
        }
    )
    settings.fetch = lambda url: (
        b"fake wheel" if url.endswith(".whl") else json.dumps(manifest()).encode()
    )
    settings.check()
    assert wait_job(settings)["state"] == "done"


def test_case_configuration_status_is_truthful_for_custom_support_role(env):
    _, auth, settings = env
    auth.define_role("supportagent", ["subscription"])
    custom = browser(env, "supportagent", "supportuser")
    settings.data["support_endpoint"] = "https://support.invalid/cases"
    result = custom.get("/api/v1/settings").json()
    assert result["support_delivery_configured"] is True
    assert "support_endpoint" not in result


def test_prepare_failure_does_not_install_and_requires_reconciliation(env):
    client, _, settings = env
    settings.upload_license(license_file())
    settings.updater = updater = FakeUpdater()
    settings.stage(manifest(), b"fake wheel")
    updater.prepare = lambda *_: (_ for _ in ()).throw(RuntimeError("prepare failed"))
    assert client.post("/api/v1/settings/updates/install").status_code == 202
    assert wait_job(settings)["state"] == "interrupted"
    assert updater.calls == []


def test_wheel_replacement_is_blocked_during_an_update(env):
    _, _, settings = env
    settings.upload_license(license_file())
    settings.data["jobs"].append({"state": "running"})
    with pytest.raises(ValueError, match="while an update"):
        settings.stage(manifest(), b"fake wheel")


def test_signed_manifest_with_invalid_release_date_is_422(env):
    client, _, settings = env
    settings.upload_license(license_file())
    response = client.post(
        "/api/v1/settings/updates/stage",
        json={
            "envelope": manifest(released=None),
            "package": base64.b64encode(b"fake wheel").decode(),
        },
    )
    assert response.status_code == 422
    assert settings.data["staged"] is None


def test_invalid_user_edit_cannot_partially_change_permissions(env):
    client, auth, _ = env
    auth.add_user("other", "viewer", PASSWORD)
    response = client.patch("/api/v1/users/other", json={"role": "admin", "password": "short"})
    assert response.status_code == 422
    assert auth.user("other").role == "viewer"
    assert auth.permissions(auth.user("other")) == set()


def test_custom_updates_role_can_stage_and_install_but_cannot_upload_license(env):
    _, auth, settings = env
    auth.define_role("updatemgr", ["updates"])
    custom = browser(env, "updatemgr", "updateuser")
    settings.updater = FakeUpdater()
    settings.upload_license(license_file())
    assert custom.post("/api/v1/settings/subscription", json=license_file()).status_code == 403
    response = custom.post(
        "/api/v1/settings/updates/stage",
        json={
            "envelope": manifest(),
            "package": base64.b64encode(b"fake wheel").decode(),
        },
    )
    assert response.status_code == 200
    assert custom.post("/api/v1/settings/updates/install").status_code == 202
    assert wait_job(settings)["state"] == "done"


def test_ed25519_default_override_wrong_signature_and_no_openssl(env, monkeypatch):
    _, _, settings = env
    public = (SIGNING / "release-public.pem").read_bytes()
    (settings.path.parent / "release-public.pem").write_bytes(public)
    default = Settings(settings.path, cluster_id=lambda: "cluster-test", node_count=lambda: 3)
    assert default.verify(license_file())["customer"] == "Test Customer"
    bad = license_file()
    bad["signature"] = base64.b64encode(b"x" * 64).decode()
    with pytest.raises(ValueError, match="invalid signed"):
        default.verify(bad)
    bad = license_file()
    bad["payload"]["customer"] = "modified"
    with pytest.raises(ValueError):
        default.verify(bad)
    envelope = license_file()
    monkeypatch.setattr(
        "ibhics.signatures.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("openssl missing")),
    )
    with pytest.raises(ValueError):
        default.verify(envelope)


def test_ed25519_temp_permissions_cleanup_timeout(monkeypatch):
    import os
    from pathlib import Path

    import ibhics.signatures as signatures

    envelope = license_file()
    message = json.dumps(envelope["payload"], sort_keys=True, separators=(",", ":")).encode()
    real_run = subprocess.run
    temporary = []

    def inspect(args, **kwargs):
        assert isinstance(args, list) and not kwargs.get("shell")
        assert kwargs["timeout"] == 10
        paths = [Path(args[args.index(flag) + 1]) for flag in ("-inkey", "-in", "-sigfile")]
        assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in paths)
        temporary.extend(paths)
        return real_run(args, **kwargs)

    monkeypatch.setattr(signatures.subprocess, "run", inspect)
    assert VERIFIER(message, base64.b64decode(envelope["signature"]))
    assert all(not path.exists() for path in temporary)
    monkeypatch.setattr(
        signatures.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("openssl", 10)),
    )
    assert not VERIFIER(message, base64.b64decode(envelope["signature"]))


def test_release_signing_tool(tmp_path):
    import sys
    from pathlib import Path

    payload, output = tmp_path / "payload.json", tmp_path / "signed.json"
    payload.write_text(json.dumps(license_file()["payload"]))
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "tools/sign-release.py"),
            str(SIGNING / "key.pem"),
            str(payload),
            str(output),
        ],
        check=True,
        timeout=10,
    )
    assert Settings(verifier=verify).verify(json.loads(output.read_text()))["nodes"] == 3


def enable_approvals(env):
    client, _, _ = env
    other = browser(env, "admin", "secondadmin")
    assert client.put("/api/v1/security/approvals", json={"enabled": True}).status_code == 200
    return other


def test_approval_default_requires_two_admins_self_reject_expiry_and_persistence(env):
    client, auth, _ = env
    approvals = client.app.state.approvals
    assert not client.get("/api/v1/approvals").json()["enabled"]
    assert client.put("/api/v1/security/approvals", json={"enabled": True}).status_code == 422
    other = enable_approvals(env)
    auth.add_user("victim", "viewer", PASSWORD)
    response = client.delete("/api/v1/users/victim")
    assert response.status_code == 202
    identifier = response.json()["approval_request_id"]
    assert auth.user("victim")
    path = f"/api/v1/approvals/{identifier}/decision"
    assert client.post(path, json={"approve": True}).status_code == 403
    assert other.post(path, json={"approve": False}).json()["state"] == "rejected"
    assert auth.user("victim")
    assert other.post(path, json={"approve": True}).status_code == 422
    response = client.delete("/api/v1/users/victim")
    identifier = response.json()["approval_request_id"]
    from ibhics.manager.approvals import Approvals

    loaded = Approvals(auth)
    assert loaded.view(auth.user("admin"))["requests"][-1]["id"] == identifier
    loaded.clock = lambda: time.time() + 86401
    assert loaded.view(auth.user("admin"))["requests"][-1]["state"] == "expired"
    approvals.clock = loaded.clock
    assert (
        other.post(f"/api/v1/approvals/{identifier}/decision", json={"approve": True}).status_code
        == 422
    )


def test_approval_executes_as_requester_audits_both_and_rechecks_permissions(env):
    client, auth, _ = env
    other = enable_approvals(env)
    auth.add_user("victim", "viewer", PASSWORD)
    identifier = client.delete("/api/v1/users/victim").json()["approval_request_id"]
    result = other.post(f"/api/v1/approvals/{identifier}/decision", json={"approve": True})
    assert result.json()["state"] == "executed", result.text
    assert result.json()["result"] == 204
    assert "victim" not in [u.name for u in auth.users()]
    audit = auth._audit_path.read_text()
    assert "approver=secondadmin" in audit and "requester=admin" in audit
    assert "APPROVED_EXECUTION" in audit
    operator = browser(env, "operator", "requester")
    identifier = operator.delete("/api/v1/vms/nonexistent").json()["approval_request_id"]
    auth.update_user("requester", role="viewer")
    result = other.post(f"/api/v1/approvals/{identifier}/decision", json={"approve": True})
    assert result.json() == {"id": identifier, "state": "failed", "result": 403}
    assert not operator.get("/api/v1/approvals", headers={"X-Approval": identifier}).is_success


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("delete", "/vms/test", {}),
        ("delete", "/recycle-bin/test", {}),
        ("delete", "/vms/test/snapshots/point", {}),
        ("delete", "/backup/targets/repository", {}),
        ("delete", "/backup/targets/repository/backups/point", {}),
        ("post", "/backup/targets", {"immutability_days": 0}),
        ("post", "/settings/updates/install", {}),
        ("post", "/settings/updates/rollback", {}),
        ("patch", "/users/secondadmin", {"role": "viewer"}),
        ("patch", "/users/secondadmin", {"enabled": False}),
        ("put", "/security/2fa", {"require_2fa": False}),
        ("put", "/security/approvals", {"enabled": False}),
    ],
)
def test_risky_routes_return_pending_instead_of_performing_action(env, method, path, body):
    client, _, _ = env
    enable_approvals(env)
    response = getattr(client, method)(
        "/api/v1" + path, **({"json": body} if method != "delete" else {})
    )
    assert response.status_code == 202, response.text
    assert response.json()["approval_request_id"]


def test_approval_install_and_rollback_create_jobs_only_after_approval(env):
    client, _, settings = env
    other = enable_approvals(env)
    settings.upload_license(license_file())
    settings.updater = FakeUpdater()
    settings.stage(manifest(), b"fake wheel")
    for operation in ("install", "rollback"):
        count = len(settings.data["jobs"])
        identifier = client.post("/api/v1/settings/updates/" + operation).json()[
            "approval_request_id"
        ]
        assert len(settings.data["jobs"]) == count
        assert (
            other.post(f"/api/v1/approvals/{identifier}/decision", json={"approve": True}).json()[
                "state"
            ]
            == "executed"
        )
        assert wait_job(settings)["state"] == "done"


def test_automatic_install_waits_when_two_person_policy_enabled(env):
    _, _, settings = env
    enable_approvals(env)
    settings.upload_license(license_file())
    settings.updater = FakeUpdater()
    settings.data.update(
        auto_install=True, channels={"stable": "https://updates.invalid/index", "beta": ""}
    )
    settings.fetch = lambda url: (
        b"fake wheel" if url.endswith(".whl") else json.dumps(manifest()).encode()
    )
    settings.check()
    assert settings.data["staged"] and not settings.data["jobs"]


def test_old_users_state_reads_and_new_state_accepts_legacy_user_schema(tmp_path):
    from dataclasses import dataclass

    @dataclass
    class LegacyUser:
        name: str
        role: Role
        password_hash: str
        enabled: bool = True
        must_change_password: bool = False
        created: str = ""
        last_login: str | None = None

    path = tmp_path / "users.json"
    auth = Auth(path)
    auth.bootstrap(PASSWORD, must_change=False)
    auth.define_role("netonly", ["network"])
    auth.add_user("custom", "netonly", PASSWORD)
    secret = auth.setup_2fa("admin", PASSWORD)["secret"]
    for entry in json.loads(path.read_text())["users"]:
        entry["role"] = Role(entry["role"])
        LegacyUser(**entry)  # same constructor and enum used by the previous release
    restored = Auth(path)
    restored.load()
    assert restored.user("admin").totp_pending == secret
    assert restored.user("custom").role == "netonly"
    assert restored.permissions(restored.user("custom")) == {"network"}
    old = tmp_path / "old.json"
    old.write_text(
        json.dumps({"users": [{"name": "legacy", "role": "admin", "password_hash": "hash"}]})
    )
    restored = Auth(old)
    restored.load()
    assert restored.user("legacy").totp_secret == ""
    assert not restored.require_2fa


@pytest.mark.parametrize(
    "outcome", ["success", "health-fail", "pip-fail", "tamper", "rollback-fail"]
)
def test_node_updater_app_only_fake_runner_and_rollback(tmp_path, outcome):
    from types import SimpleNamespace

    from ibhics.updater import NodeApplicationUpdater

    (tmp_path / "release-public.pem").write_bytes((SIGNING / "release-public.pem").read_bytes())
    calls = []

    def runner(args):
        assert isinstance(args, list)
        calls.append(args)
        if "pip" in args and outcome == "pip-fail":
            raise RuntimeError("pip failed")
        return SimpleNamespace(stdout=b"not-found", returncode=1 if args[0] == "pgrep" else 0)

    updater = NodeApplicationUpdater(
        tmp_path,
        venv=tmp_path / "venv",
        runner=runner,
        guard=tmp_path / "replication.lock",
        health=lambda version: (
            outcome not in ("health-fail", "rollback-fail")
            or (outcome == "health-fail" and version != "1.4.0")
        ),
    )
    assert updater.prepare(manifest(), b"fake wheel")["state"] == "prepared"
    assert len(calls) == 1 and calls[0][:2] == ["cp", "-a"]
    if outcome == "tamper":
        from pathlib import Path

        Path(updater.read()["wheel"]).write_bytes(b"altered")
    assert updater.start()["state"] == "queued"
    assert calls[-1][0] == "systemd-run"  # worker survives stopping its own parent service
    updater.execute()
    data = updater.read()
    if outcome == "success":
        assert data["state"] == "done"
        install = next(c for c in calls if "pip" in c)
        assert install[1:7] == [
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
        ]
        updater.start(rollback=True)
        updater.execute()
        assert updater.read()["state"] == "done"
        assert any(c[:2] == ["mv", str(tmp_path / "venv")] for c in calls)
    elif outcome == "tamper":
        assert data["state"] == "failed"
        assert not any(c[:2] == ["systemctl", "stop"] for c in calls)
    elif outcome == "rollback-fail":
        assert data["state"] == "interrupted"
    else:
        assert data["state"] == "failed" and data["detail"].startswith("Rolled back")
    assert all(
        c[0] in {"cp", "mv", "systemd-run", "pgrep", "systemctl", str(tmp_path / "venv/bin/python")}
        for c in calls
    )
    for args in calls:
        assert not {"reboot", "virsh", "libvirtd", "zpool", "mkfs", "mount", "umount"} & set(args)


def test_agent_updater_token_signature_checksum_limits(tmp_path):
    from types import SimpleNamespace

    from ibhics.agent.app import create_agent_app
    from ibhics.cluster.topology import NodeRole
    from ibhics.updater import NodeApplicationUpdater

    (tmp_path / "release-public.pem").write_bytes((SIGNING / "release-public.pem").read_bytes())
    calls = []

    def runner(args):
        calls.append(args)
        return SimpleNamespace(stdout=b"", returncode=1 if args[0] == "pgrep" else 0)

    updater = NodeApplicationUpdater(tmp_path, runner=runner)
    client = TestClient(
        create_agent_app(
            "node",
            NodeRole.WITNESS,
            "node-token",
            application_updater=updater,
            storage_root=tmp_path / "storage",
        )
    )
    path = "/agent/v1/application-update"
    body = {"envelope": manifest(), "package": base64.b64encode(b"fake wheel").decode()}
    assert client.post(path + "/prepare", json=body).status_code == 401
    assert not calls
    client.headers["X-Ibhics-Token"] = "node-token"
    invalid = manifest()
    invalid["payload"]["sha256"] = "invalid"
    assert client.post(path + "/prepare", json={**body, "envelope": invalid}).status_code == 422
    assert client.post(path + "/prepare", json={**body, "package": "eA=="}).status_code == 422
    assert not calls
    assert client.post(path + "/prepare", json=body).status_code == 200
    assert client.post(path + "/install").status_code == 202
    assert client.get(path).json()["state"] == "queued"
    assert client.post(path + "/prepare", json=body).status_code == 422
    assert client.post(path + "/rollback").status_code == 422


def test_production_cluster_adapter_order_and_durable_worker(env, monkeypatch):
    import ibhics.updater as production

    client, _, settings = env
    (settings.path.parent / "release-public.pem").write_bytes(
        (SIGNING / "release-public.pem").read_bytes()
    )
    inventory = {
        "active": {"address": "http://active:8007", "token": "x"},
        "standby": {"address": "http://standby:8007", "token": "x"},
    }
    adapter = production.ClusterUpdater(inventory, "active")
    settings.updater = adapter
    settings.upload_license(license_file())
    settings.stage(manifest(), b"fake wheel")
    launched = []
    monkeypatch.setattr(production, "launch", lambda *args: launched.append(args))
    result = client.post("/api/v1/settings/updates/install")
    assert result.status_code == 202
    identifier = result.json()["id"]
    assert settings.data["jobs"][-1]["external"]
    assert launched[0][0] == "ibhics-cluster-update-" + identifier
    calls = []
    operation_ids = {}

    def request(self, node, method, path, **kwargs):
        calls.append((node, method, path))
        if path == "/prepare":
            assert kwargs["json"]["envelope"]["signature"]
        if path == "/install":
            operation_ids[node] = kwargs["json"]["operation_id"]
        return {"id": node, "state": "done", "operation_id": operation_ids.get(node)}

    monkeypatch.setattr(production.ClusterUpdater, "request", request)
    monkeypatch.setattr(production.time, "sleep", lambda _: None)
    config = settings.path.parent / "update-jobs" / (identifier + ".config.json")
    production.cluster_worker(config, identifier)
    settings.refresh_jobs()
    assert settings.data["jobs"][-1]["state"] == "done"
    assert settings.data["previous_available"]
    assert [(n, p) for n, m, p in calls if m == "POST"] == [
        ("standby", "/prepare"),
        ("active", "/prepare"),
        ("standby", "/install"),
        ("active", "/install"),
    ]
    assert not config.exists()
    settings.save()
    restored = Settings(settings.path, verifier=verify, updater=adapter)
    assert restored.view()["jobs"][-1]["state"] == "done"


def test_approval_non_object_and_oversized_delete_cannot_bypass(env):
    client, auth, _ = env
    enable_approvals(env)
    auth.add_user("victim", "viewer", PASSWORD)
    assert client.request("DELETE", "/api/v1/users/victim", json=[]).status_code == 422
    assert client.request("DELETE", "/api/v1/users/victim", content=b"x" * 65537).status_code == 422
    assert auth.user("victim")


def test_approvals_redact_nested_credentials_and_limit_visibility(env):
    client, _, _ = env
    enable_approvals(env)
    viewer = browser(env, "viewer", "privateviewer")
    response = client.post(
        "/api/v1/backup/targets",
        json={"name": "repo", "settings": {"password": "never-show", "immutability_days": 0}},
    )
    assert response.status_code == 202
    view = client.get("/api/v1/approvals").json()
    assert "never-show" not in json.dumps(view)
    assert view["requests"][-1]["changes"]["settings"]["password"] == "[redacted]"
    assert viewer.get("/api/v1/approvals").json()["requests"] == []
    identifier = response.json()["approval_request_id"]
    assert (
        viewer.post(f"/api/v1/approvals/{identifier}/decision", json={"approve": True}).status_code
        == 403
    )


def test_node_update_replication_interlock_prevents_service_changes(tmp_path):
    import fcntl
    from types import SimpleNamespace

    from ibhics.updater import NodeApplicationUpdater

    (tmp_path / "release-public.pem").write_bytes((SIGNING / "release-public.pem").read_bytes())
    calls = []

    def runner(args):
        calls.append(args)
        return SimpleNamespace(stdout=b"", returncode=1 if args[0] == "pgrep" else 0)

    guard = tmp_path / "replication.lock"
    updater = NodeApplicationUpdater(tmp_path, runner=runner, guard=guard)
    updater.prepare(manifest(), b"fake wheel")
    updater.start()
    with guard.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        updater.execute()
    assert updater.read()["state"] == "failed"
    assert not any(c[0] == "systemctl" for c in calls)


def test_node_update_submission_is_idempotent_and_rejects_arbitrary_ids(tmp_path):
    from types import SimpleNamespace

    from ibhics.updater import NodeApplicationUpdater

    (tmp_path / "release-public.pem").write_bytes((SIGNING / "release-public.pem").read_bytes())
    calls = []

    def runner(args):
        calls.append(args)
        return SimpleNamespace(stdout=b"", returncode=0)

    updater = NodeApplicationUpdater(tmp_path, runner=runner)
    updater.prepare(manifest(), b"fake wheel")
    with pytest.raises(ValueError):
        updater.start(operation_id="unsafe; command")
    identifier = "a" * 32
    first = updater.start(operation_id=identifier)
    assert updater.start(operation_id=identifier) == first
    assert len([c for c in calls if c[0] == "systemd-run"]) == 1


def test_cluster_adapter_retries_interrupted_response_and_legacy_rollback(monkeypatch):
    from ibhics.updater import ClusterUpdater

    adapter = ClusterUpdater({"node": {"address": "http://unused", "token": "x"}}, "node")
    tokens = []

    def request(node, method, path, **kwargs):
        if method == "POST":
            tokens.append(kwargs["json"]["operation_id"])
            if len(tokens) == 1:
                raise ConnectionError("response lost during service restart")
        return {"state": "done", "operation_id": tokens[-1]}

    monkeypatch.setattr(adapter, "request", request)
    monkeypatch.setattr("ibhics.updater.time.sleep", lambda _: None)
    adapter.install("node")
    assert len(tokens) == 2 and tokens[0] == tokens[1]
    monkeypatch.setattr(
        adapter,
        "request",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("old wheel has no updater endpoint")),
    )
    monkeypatch.setattr(adapter, "legacy_health", lambda node: True)
    adapter.rollback("node")
    assert adapter.healthy("node")


def test_update_approval_is_bound_to_the_reviewed_manifest(env):
    client, _, settings = env
    other = enable_approvals(env)
    settings.upload_license(license_file())
    settings.updater = FakeUpdater()
    settings.stage(manifest(), b"fake wheel")
    identifier = client.post("/api/v1/settings/updates/install").json()["approval_request_id"]
    review = client.get("/api/v1/approvals").json()["requests"][-1]
    assert review["context"]["version"] == "1.4.0"
    settings.stage(manifest(version="1.5.0"), b"fake wheel")
    result = other.post(f"/api/v1/approvals/{identifier}/decision", json={"approve": True})
    assert result.json()["state"] == "failed" and result.json()["result"] == 403
    assert settings.data["jobs"] == []


@pytest.mark.parametrize(
    "agent_version,manager_version,expected",
    [
        ("new", "new", True),
        ("old", "new", False),
        ("new", "old", False),
    ],
)
def test_node_health_requires_expected_application_versions(
    tmp_path, monkeypatch, agent_version, manager_version, expected
):
    import httpx

    from ibhics.updater import NodeApplicationUpdater

    class LocalHealthClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            assert "127.0.0.1" in url
            version = agent_version if "/agent/" in url else manager_version
            return httpx.Response(
                200, request=httpx.Request("GET", url), json={"version": version, "status": "ok"}
            )

    monkeypatch.setattr("ibhics.updater.httpx.Client", LocalHealthClient)
    monkeypatch.setattr("ibhics.updater.time.sleep", lambda _: None)
    monkeypatch.setenv("IBHICS_AGENT_HOST", "127.0.0.1")
    assert NodeApplicationUpdater(tmp_path).health("new") is expected


def test_admin_can_require_and_turn_off_2fa_per_user(tmp_path):
    from ibhics.manager.auth import Auth

    auth = Auth(tmp_path / "users.json")
    auth.bootstrap("Adm1n-password!", must_change=False)
    auth.add_user("bob", "operator", "Bob-password-1!")
    assert auth.update_user("bob", require_totp=True).require_totp is True
    again = Auth(tmp_path / "users.json")
    again.load()
    assert again.user("bob").require_totp is True
    assert auth.update_user("bob", require_totp=False).require_totp is False


def test_remove_license_preserves_other_settings_and_persists(env):
    client, _, settings = env
    settings.upload_license(license_file())
    original_channels = dict(settings.data["channels"])
    response = client.delete("/api/v1/settings/subscription")
    assert response.status_code == 200
    assert response.json()["support"]["status"] == "None"
    assert response.json()["update"]["status"] == "None"
    assert settings.data["channels"] == original_channels
    assert json.loads(settings.path.read_text())["license"] is None
    assert client.get("/api/v1/assistant/status").json()["licensed"] is False
    assert client.delete("/api/v1/settings/subscription").status_code == 200
    assert client.post("/api/v1/settings/subscription", json=license_file()).status_code == 200
    assert settings.subscription()["support"]["status"] == "Active"


def test_remove_license_requires_admin(env):
    from test_auth import as_user

    from ibhics.manager.auth import Role

    client, auth, settings = env
    settings.upload_license(license_file())
    operator = as_user(env, "remove-license-operator", Role.OPERATOR)
    assert operator.delete("/api/v1/settings/subscription").status_code == 403
    assert settings.subscription()["support"]["status"] == "Active"
