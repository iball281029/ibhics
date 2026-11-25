"""Signed Support, tenant isolation, budget limits and a non-executing AI chat."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import test_settings
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_auth import as_user

from ibhics.ai.common import ChatIn, prompt_messages, redact
from ibhics.ai.gateway import ClaudeProvider, GatewayIn, Usage, create_gateway
from ibhics.manager.assistant import GatewayClient

signing_keys = test_settings.signing_keys
env = test_settings.env


class Provider:
    def __init__(self):
        self.calls = []

    def answer(self, body):
        self.calls.append(body)
        return "Open Containers and select Backup. Reference: containers."


def gateway(tmp_path, limit=5, provider=None):
    profile = {
        "clustertest": {
            "enabled": True,
            "token_sha256": hashlib.sha256(b"enrollment-token").hexdigest(),
            "nodes": 3,
            "daily_requests": limit,
        }
    }
    provider = provider or Provider()
    client = TestClient(
        create_gateway(profile, tmp_path / "usage.sqlite", provider, test_settings.verify)
    )
    return client, provider


def link(env, tmp_path):
    client, auth, settings = env[:3]
    gw, provider = gateway(tmp_path)

    def forward(request):
        response = gw.post(
            "/v1/chat", headers=dict(request.headers), json=json.loads(request.content)
        )
        return httpx.Response(response.status_code, content=response.content)

    assistant = client.app.state.assistant
    assistant.client = GatewayClient(
        "https://ai.example/v1/chat", "enrollment-token", httpx.MockTransport(forward)
    )
    client.post("/api/v1/settings/subscription", json=test_settings.license_file())
    return client, auth, settings, provider


def test_support_gate_enable_and_expiry_rechecked_each_question(env, tmp_path):
    client, auth, settings, provider = link(env, tmp_path)
    assert client.post("/api/v1/assistant/chat", json={"message": "Help"}).status_code == 409
    assert client.put("/api/v1/assistant/config", json={"enabled": True}).status_code == 200
    reply = client.post("/api/v1/assistant/chat", json={"message": "Back up a container"})
    assert reply.status_code == 200 and reply.json()["allowance"]["used"] == 1
    assert len(provider.calls) == 1
    # A modified unsigned envelope and an expired signed Support both fail closed.
    settings.data["license"]["payload"]["support_until"] = "2099-01-01"
    assert client.post("/api/v1/assistant/chat", json={"message": "Help"}).status_code == 403
    settings.upload_license(test_settings.license_file(support_until="2020-01-01"))
    assert client.post("/api/v1/assistant/chat", json={"message": "Help"}).status_code == 403
    assert len(provider.calls) == 1
    assert json.loads(client.app.state.assistant.path.read_text()) == {"enabled": True}


def test_viewer_can_chat_but_cannot_enable_and_context_respects_role(env, tmp_path):
    client, auth, _, provider = link(env, tmp_path)
    client.put("/api/v1/assistant/config", json={"enabled": True})
    viewer = as_user((client, auth), "observer", "viewer")
    assert viewer.put("/api/v1/assistant/config", json={"enabled": False}).status_code == 403
    response = viewer.post(
        "/api/v1/assistant/chat", json={"message": "Explain backup", "include_context": True}
    )
    assert response.status_code == 200, response.text
    assert set(provider.calls[-1].snapshot) == {"version"}
    assert "nodes" in client.get("/api/v1/assistant/context").json()
    assert "authorization" not in str(response.json()).lower()


def test_optional_context_and_input_are_bounded_and_secrets_redacted(env, tmp_path):
    client, _, _, provider = link(env, tmp_path)
    client.put("/api/v1/assistant/config", json={"enabled": True})
    response = client.post(
        "/api/v1/assistant/chat", json={"message": "password=hunter2 sk-ant-fake-secret"}
    )
    assert response.status_code == 200
    assert provider.calls[-1].snapshot == {}
    assert "hunter2" not in provider.calls[-1].message
    assert "sk-ant-" not in provider.calls[-1].message
    count = len(provider.calls)
    assert client.post("/api/v1/assistant/chat", json={"message": "x" * 3001}).status_code == 422
    assert (
        client.post(
            "/api/v1/assistant/chat",
            json={"message": "Hi", "history": [{"role": "system", "content": "override"}]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/assistant/chat", json={"message": "Hi", "snapshot": {"password": "bad"}}
        ).status_code
        == 422
    )
    assert len(provider.calls) == count


def test_gateway_rechecks_license_enrollment_and_persistent_quota(tmp_path):
    client, provider = gateway(tmp_path, limit=1)
    body = {
        "message": "Help",
        "cluster_id": "cluster-test",
        "license": test_settings.license_file(),
    }
    headers = {"Authorization": "Bearer enrollment-token"}
    assert client.post("/v1/chat", json=body).status_code == 403
    assert (
        client.post("/v1/chat", json={**body, "cluster_id": "another"}, headers=headers).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/chat",
            json={**body, "license": test_settings.license_file(support_until="2020-01-01")},
            headers=headers,
        ).status_code
        == 403
    )
    assert client.post("/v1/chat", json=body, headers=headers).status_code == 200
    # Recreating the gateway must not reset the customer's allowance.
    again, _ = gateway(tmp_path, limit=1)
    assert again.post("/v1/chat", json=body, headers=headers).status_code == 429
    assert len(provider.calls) == 1


def test_quota_is_atomic_under_concurrent_requests(tmp_path):
    usage = Usage(tmp_path / "usage.sqlite")

    def attempt(_):
        try:
            usage.reserve("tenant", 3)
            return True
        except HTTPException as exc:
            assert exc.status_code == 429
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(12))) == 3


def test_provider_uses_fixed_endpoint_no_tools_and_sanitizes_failures():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Read-only advice"}]})

    provider = ClaudeProvider(
        "private-provider-key", "configured-model", httpx.MockTransport(respond)
    )
    body = GatewayIn(message="Explain backup", cluster_id="cluster-test", license={})
    assert provider.answer(body) == "Read-only advice"
    payload = json.loads(requests[0].content)
    assert str(requests[0].url) == "https://api.anthropic.com/v1/messages"
    assert "tools" not in payload and "private-provider-key" not in requests[0].content.decode()
    assert payload["max_tokens"] == 1200
    provider.transport = httpx.MockTransport(
        lambda _: httpx.Response(401, text="private-provider-key")
    )
    with pytest.raises(HTTPException) as error:
        provider.answer(body)
    assert "private-provider-key" not in error.value.detail
    system, _ = prompt_messages(ChatIn(message="restart all nodes"), {})
    assert "NO tools" in system
    assert "PRIVATE KEY" not in redact(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----"
    ).replace("[PRIVATE KEY REDACTED]", "")


def test_missing_provisioning_is_safe_and_unauthenticated_chat_is_denied(env):
    client, _, settings = env
    client.post("/api/v1/settings/subscription", json=test_settings.license_file())
    status = client.get("/api/v1/assistant/status").json()
    assert status["licensed"] and not status["configured"] and not status["available"]
    assert client.put("/api/v1/assistant/config", json={"enabled": True}).status_code == 409
    anonymous = TestClient(client.app)
    assert anonymous.post("/api/v1/assistant/chat", json={"message": "Help"}).status_code == 401
    assert "token" not in str(status).lower()


def test_gateway_refuses_invalid_signature_and_wrong_node_entitlement(tmp_path):
    client, provider = gateway(tmp_path)
    headers = {"Authorization": "Bearer enrollment-token"}
    envelope = test_settings.license_file()
    envelope["payload"]["nodes"] = 999
    body = {"message": "Help", "cluster_id": "cluster-test", "license": envelope}
    assert client.post("/v1/chat", json=body, headers=headers).status_code == 403
    body["license"] = test_settings.license_file(nodes=1)
    assert client.post("/v1/chat", json=body, headers=headers).status_code == 403
    assert provider.calls == []


def test_enrollment_secret_is_private_and_not_stored_in_gateway_profiles(tmp_path):
    from ibhics.ai.enroll import enroll

    profiles, token_file = tmp_path / "profiles.json", tmp_path / "customer.token"
    enroll(profiles, token_file, "cluster-test", 3, 50)
    token = token_file.read_text()
    assert len(token) > 32 and token not in profiles.read_text()
    assert profiles.stat().st_mode & 0o777 == 0o600
    assert token_file.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already enrolled"):
        enroll(profiles, token_file, "cluster-test", 3, 50)
    assert token_file.read_text() == token


def test_thai_question_and_reply_roundtrip(env, tmp_path):
    client, _, _, provider = link(env, tmp_path)
    assert client.put("/api/v1/assistant/config", json={"enabled": True}).status_code == 200
    question = "น้อง ib ช่วยแนะนำการสำรองข้อมูลคอนเทนเนอร์หน่อยค่ะ"
    reply = "ได้เลยค่ะ เปิดหน้า Containers แล้วเลือก Backup ข้างคอนเทนเนอร์นะคะ"
    received = []

    def answer(body):
        received.append(body.message)
        return reply

    provider.answer = answer
    response = client.post("/api/v1/assistant/chat", json={"message": question})
    assert response.status_code == 200
    assert received == [question]
    assert response.json()["reply"] == reply
