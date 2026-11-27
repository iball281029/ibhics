"""AI license encryption, offline issuance and shared Manager credential lifecycle."""

import importlib.util
import json
import stat
import subprocess
from datetime import date
from pathlib import Path

import pytest
import test_settings
from fastapi import HTTPException

from ibhics.ai.codex_preview import AI_PASS_EXPIRED, CodexLocalPreview
from ibhics.ai.common import ChatIn
from ibhics.ai.license_auth import MAX_AUTH, open_sealed, seal
from ibhics.manager.assistant import Assistant, GatewayClient
from ibhics.manager.settings import Settings

signing_keys = test_settings.signing_keys
env = test_settings.env
AUTH = b'{"tokens":{"access_token":"fake-access","refresh_token":"fake-refresh"}}'


def settings_at(tmp_path):
    return Settings(
        tmp_path / "settings.json",
        cluster_id=lambda: "cluster-test",
        verifier=test_settings.verify,
        today=lambda: date(2027, 1, 15),
    )


def envelope(**changes):
    payload = {
        "customer": "Example",
        "cluster_id": "cluster-test",
        "nodes": 3,
        "support_until": "2028-01-01",
        "ai": {
            "account": "vendor-1",
            "daily_limit": 20,
            "auth": seal(AUTH, "cluster-test"),
            "issued": "2027-01-15",
        },
    }
    payload.update(changes)
    return test_settings.sign(payload)


def test_ai_pass_roundtrip_cluster_normalization_and_limit():
    blob = seal(AUTH, "IBHX-1234-ABCD")
    assert open_sealed(blob, "1234abcd") == AUTH
    with pytest.raises(ValueError, match="authentication failed"):
        open_sealed(blob, "other-cluster")
    with pytest.raises(ValueError, match="64 KiB"):
        seal(b"x" * (MAX_AUTH + 1), "cluster")
    maximum = b"x" * MAX_AUTH
    assert open_sealed(seal(maximum, "c"), "c") == maximum


@pytest.mark.parametrize("part", [1, 2, 3])
def test_ai_pass_tampering_rejected_before_decryption(monkeypatch, part):
    import ibhics.ai.license_auth as auth

    pieces = seal(AUTH, "cluster-test").split(".")
    pieces[part] = ("A" if pieces[part][0] != "A" else "B") + pieces[part][1:]
    monkeypatch.setattr(auth, "_crypt", lambda *a: pytest.fail("Must verify MAC first"))
    with pytest.raises(ValueError):
        open_sealed(".".join(pieces), "cluster-test")


def test_ai_upload_permissions_remove_and_legacy_replacement(tmp_path):
    settings = settings_at(tmp_path)
    standby = settings_at(tmp_path)
    settings.upload_license(envelope())
    home = tmp_path / "ai-codex"
    assert (home / "auth.json").read_bytes() == AUTH
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "auth.json").stat().st_mode) == 0o600
    assert standby.license()["ai"]["account"] == "vendor-1"
    legacy = envelope()
    del legacy["payload"]["ai"]
    settings.upload_license(test_settings.sign(legacy["payload"]))
    assert settings.license() and "ai" not in settings.license() and not home.exists()
    settings.upload_license(envelope())
    settings.remove_license()
    assert not home.exists() and standby.license() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"account": "../bad"},
        {"daily_limit": True},
        {"daily_limit": 51},
        {"auth": "bad"},
        {"auth": None},
    ],
)
def test_ai_license_rejects_bad_metadata(tmp_path, changes):
    settings = settings_at(tmp_path)
    valid = envelope()
    valid["payload"]["ai"].update(changes)
    with pytest.raises(ValueError):
        settings.upload_license(test_settings.sign(valid["payload"]))
    assert settings.license() is None


def test_ai_upload_rejects_wrong_cluster_pass_preserving_existing_auth(tmp_path):
    settings = settings_at(tmp_path)
    settings.upload_license(envelope())
    bad = envelope()
    bad["payload"]["ai"]["auth"] = seal(AUTH, "another-cluster")
    with pytest.raises(ValueError, match="AI pass authentication failed"):
        settings.upload_license(test_settings.sign(bad["payload"]))
    assert settings.license()["ai"]["auth"] != bad["payload"]["ai"]["auth"]
    assert (tmp_path / "ai-codex" / "auth.json").read_bytes() == AUTH


def test_assistant_ai_autoenable_admin_disable_gateway_and_status_privacy(tmp_path, monkeypatch):
    monkeypatch.setattr("ibhics.ai.codex_preview.shutil.which", lambda name: "/stub/codex")
    settings = settings_at(tmp_path)
    assistant = Assistant(settings, GatewayClient("https://ai.example", "gateway-token"))
    assert not assistant.status()["enabled"]
    settings.upload_license(envelope())
    status = assistant.status()
    assert status["available"] and isinstance(assistant.provider, CodexLocalPreview)
    assert assistant.provider.codex_home == tmp_path / "ai-codex"
    assert assistant.provider.daily_limit == 20
    assert "fake-access" not in json.dumps(status) and '"auth"' not in json.dumps(status)
    assert status["ai"] == {"account": "vendor-1", "daily_limit": 20, "state": "ready"}
    calls = []
    monkeypatch.setattr(assistant.provider, "chat", lambda p: calls.append(p) or {"reply": "ok"})
    assistant.ask(ChatIn(message="help"), {})
    assert "license" not in calls[0] and "fake-access" not in json.dumps(calls)
    assistant.configure(False)
    assert not Assistant(settings).status()["enabled"]
    legacy = envelope()["payload"]
    del legacy["ai"]
    settings.upload_license(test_settings.sign(legacy))
    assistant.status()
    assert assistant.provider is assistant.client


def test_ai_auth_failure_has_specific_status_and_isolated_home(tmp_path, monkeypatch):
    calls = []

    class Process:
        returncode = 1

        def __init__(self, command, **kwargs):
            calls.append(kwargs)

        def communicate(self, *args, **kwargs):
            return None, "401 refresh_token expired fake-secret-never-expose"

    settings = settings_at(tmp_path)
    settings.upload_license(envelope())
    payload = settings.license()
    monkeypatch.setattr("ibhics.ai.codex_preview.shutil.which", lambda name: "/stub/codex")
    monkeypatch.setenv("CODEX_HOME", "/must-not-use-operator-home")
    monkeypatch.setattr(subprocess, "Popen", Process)
    # License verification must keep using the real OpenSSL subprocess.
    monkeypatch.setattr(settings, "license", lambda: payload)
    assistant = Assistant(settings)
    with pytest.raises(HTTPException) as error:
        assistant.ask(ChatIn(message="help"), {})
    assert error.value.detail == AI_PASS_EXPIRED
    assert calls[0]["env"]["CODEX_HOME"] == str(tmp_path / "ai-codex")
    status = assistant.status()
    assert status["reason_code"] == "ai_pass_expired" and not status["available"]
    assert "fake-secret" not in json.dumps(status)


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "license_gen", Path(__file__).resolve().parents[1] / "tools" / "license-gen.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_license_gen_signs_payload_and_journals_without_tokens(tmp_path, monkeypatch):
    gen = load_generator()
    root = tmp_path / "accounts"
    account = root / "vendor"
    account.mkdir(parents=True)
    (account / "auth.json").write_bytes(AUTH)
    monkeypatch.setattr(gen, "logged_in", lambda path: path == account)
    output = tmp_path / "license.json"
    gen.main(
        [
            "--accounts-dir",
            str(root),
            "gen",
            "--key",
            str(test_settings.SIGNING / "key.pem"),
            "--customer",
            "Customer",
            "--cluster-id",
            "cluster-test",
            "--nodes",
            "3",
            "--support-until",
            "2028-01-01",
            "--ai-account",
            "vendor",
            "-o",
            str(output),
        ]
    )
    signed = json.loads(output.read_text())
    settings = settings_at(tmp_path)
    payload = settings.verify(signed)
    assert payload["ai"]["daily_limit"] == 20 and payload["ai"]["account"] == "vendor"
    assert open_sealed(payload["ai"]["auth"], "cluster-test") == AUTH
    journal = root.parent / "issued.jsonl"
    assert "fake-access" not in journal.read_text() and "auth" not in journal.read_text()
    assert json.loads(journal.read_text())["customer"] == "Customer"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


@pytest.mark.parametrize("operation", ["replace", "legacy", "remove"])
def test_ai_credentials_restored_when_settings_save_fails(tmp_path, monkeypatch, operation):
    settings = settings_at(tmp_path)
    settings.upload_license(envelope())
    old = settings.data["license"]
    auth = tmp_path / "ai-codex" / "auth.json"
    replacement = envelope()["payload"]
    if operation == "legacy":
        del replacement["ai"]
    elif operation == "replace":
        replacement["ai"]["auth"] = seal(
            b'{"tokens":{"access_token":"new-fake-access"}}', "cluster-test"
        )
    signed = test_settings.sign(replacement)

    def fail_save():
        raise OSError("simulated disk error")

    monkeypatch.setattr(settings, "save", fail_save)
    with pytest.raises((ValueError, OSError), match="simulated disk error"):
        settings.remove_license() if operation == "remove" else settings.upload_license(signed)
    assert auth.read_bytes() == AUTH
    assert settings.data["license"] == old
    assert not list(tmp_path.glob(".ai-codex-*"))


def test_ai_license_api_accepts_full_auth_size_without_disclosing_it(env):
    client, _, settings = env[:3]
    data = {"tokens": {"access_token": "x" * (MAX_AUTH - 40)}}
    auth = json.dumps(data).encode()
    assert len(auth) <= MAX_AUTH
    signed = test_settings.license_file()
    signed["payload"]["ai"] = {
        "account": "vendor",
        "daily_limit": 20,
        "auth": seal(auth, settings.cluster_id()),
    }
    response = client.post(
        "/api/v1/settings/subscription", json=test_settings.sign(signed["payload"])
    )
    assert response.status_code == 200, response.text
    assert "auth" not in response.json()
    status = client.get("/api/v1/assistant/status").json()
    assert status["ai"]["account"] == "vendor" and "auth" not in json.dumps(status)


@pytest.mark.parametrize("force", [False, True])
def test_license_gen_five_license_confirmation(tmp_path, monkeypatch, force):
    gen = load_generator()
    root = tmp_path / "accounts"
    account = root / "vendor"
    account.mkdir(parents=True)
    (account / "auth.json").write_bytes(AUTH)
    (root.parent / "issued.jsonl").write_text(
        "\n".join(json.dumps({"account": "vendor"}) for _ in range(5)) + "\n"
    )
    monkeypatch.setattr(gen, "logged_in", lambda path: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    args = [
        "--accounts-dir",
        str(root),
        "gen",
        "--key",
        str(test_settings.SIGNING / "key.pem"),
        "--customer",
        "Customer",
        "--cluster-id",
        "cluster-test",
        "--nodes",
        "3",
        "--support-until",
        "2028-01-01",
        "--ai-account",
        "vendor",
        "-o",
        str(tmp_path / "license.json"),
    ]
    if force:
        gen.main([*args, "--force"])
        assert (tmp_path / "license.json").exists()
    else:
        with pytest.raises(SystemExit) as error:
            gen.main(args)
        assert error.value.code == 1 and not (tmp_path / "license.json").exists()


def test_license_gen_add_account_and_list_use_isolated_home(tmp_path, monkeypatch, capsys):
    gen = load_generator()
    root = tmp_path / "accounts"
    calls = []

    def run(command, **options):
        calls.append((command, options))
        if command == ["codex", "login"]:
            (Path(options["env"]["CODEX_HOME"]) / "auth.json").write_bytes(AUTH)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    gen.main(["--accounts-dir", str(root), "add-account", "vendor"])
    assert stat.S_IMODE((root / "vendor").stat().st_mode) == 0o700
    gen.main(["--accounts-dir", str(root), "accounts"])
    assert calls[0][0] == ["codex", "login"]
    assert calls[1][0] == ["codex", "login", "status"]
    assert all(call[1]["env"]["CODEX_HOME"] == str(root / "vendor") for call in calls)
    assert "vendor: logged in; 0 licenses" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        gen.main(["--accounts-dir", str(root), "add-account", "../bad"])
    assert len(calls) == 2


def test_assistant_ai_missing_auth_does_not_fall_back_to_gateway(tmp_path):
    settings = settings_at(tmp_path)
    settings.upload_license(envelope())
    (tmp_path / "ai-codex" / "auth.json").unlink()
    assistant = Assistant(settings, GatewayClient("https://example.invalid", "gateway-token"))
    status = assistant.status()
    assert not status["configured"] and not status["available"] and assistant.provider is None


def test_assistant_license_provider_snapshot_survives_concurrent_status_change(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("ibhics.ai.codex_preview.shutil.which", lambda name: "/stub/codex")
    settings = settings_at(tmp_path)
    settings.upload_license(envelope())
    assistant = Assistant(settings, GatewayClient("https://example.invalid", "gateway-token"))
    assistant.status()
    local = assistant.provider
    calls = []
    monkeypatch.setattr(local, "chat", lambda body: calls.append(body) or {"reply": "ok"})
    monkeypatch.setattr(assistant.client, "chat", lambda body: pytest.fail("Must stay local"))
    original_dump = ChatIn.model_dump

    def changed_status(body, *args, **kwargs):
        assistant.provider = assistant.client
        return original_dump(body, *args, **kwargs)

    monkeypatch.setattr(ChatIn, "model_dump", changed_status)
    assistant.ask(ChatIn(message="help"), {})
    assert len(calls) == 1 and "license" not in calls[0]


def test_ai_license_still_requires_active_support(tmp_path, monkeypatch):
    monkeypatch.setattr("ibhics.ai.codex_preview.shutil.which", lambda name: "/stub/codex")
    settings = settings_at(tmp_path)
    settings.upload_license(envelope(support_until="2020-01-01"))
    assistant = Assistant(settings)
    status = assistant.status()
    assert status["enabled"] and status["reason_code"] == "no_support"
    assert not status["available"]
    with pytest.raises(HTTPException) as error:
        assistant.ask(ChatIn(message="help"), {})
    assert error.value.status_code == 403
