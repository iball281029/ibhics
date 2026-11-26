"""Assistant plans cannot bypass core permissions, confirmation or approval rules."""

import pytest
import test_ai_assistant
from test_ai_assistant import link
from test_auth import as_user
from test_settings import license_file

from ibhics.manager.auth import Role

env = test_ai_assistant.env
signing_keys = test_ai_assistant.signing_keys


@pytest.fixture
def ready(env, tmp_path):
    client, auth, settings, provider = link(env, tmp_path)
    assert client.put("/api/v1/assistant/config", json={"enabled": True}).status_code == 200
    return client, auth, settings, provider


def test_vlan_request_is_only_a_plan_until_explicit_core_api_call(ready):
    client, _, _, provider = ready
    r = client.post(
        "/api/v1/assistant/chat",
        json={"message": "ช่วยสร้าง network VLAN123 ให้หน่อย", "language": "th"},
    )
    assert r.status_code == 200, r.text
    plan = r.json()["proposal"]
    assert plan["body"] == {"name": "vlan123", "vlan": 123}
    assert len(client.get("/api/v1/networks").json()) == 1
    assert provider.calls == []
    reviewed = client.post("/api/v1/assistant/review", json={"token": plan["token"]})
    assert reviewed.status_code == 200
    # Review is also read only. The user-confirmed request uses the real Network API.
    assert len(client.get("/api/v1/networks").json()) == 1
    r = client.post("/api/v1" + reviewed.json()["path"], json=reviewed.json()["body"])
    assert r.status_code == 201
    assert any(n["vlan"] == 123 for n in client.get("/api/v1/networks").json())
    assert client.post("/api/v1/assistant/review", json={"token": plan["token"]}).status_code == 409


def test_readonly_viewer_cannot_prepare_mutations(ready):
    client, auth, _, _ = ready
    other = as_user((client, auth), "viewer-actions", Role.VIEWER)
    assert other.get("/api/v1/assistant/operations").status_code == 200
    assert (
        other.post("/api/v1/assistant/plan", json={"kind": "create_vlan", "vlan": 123}).status_code
        == 403
    )
    assert (
        other.post(
            "/api/v1/assistant/plan", json={"kind": "start_vm", "target": "unknown"}
        ).status_code
        == 403
    )
    assert (
        other.post("/api/v1/assistant/chat", json={"message": "check system status"}).status_code
        == 200
    )


def test_signed_plan_rejects_tampering_foreign_user_expiry_and_license_loss(ready):
    client, auth, settings, _ = ready
    ops = client.app.state.assistant_operations
    ops.clock = lambda: 1000
    plan = client.post("/api/v1/assistant/plan", json={"kind": "create_vlan", "vlan": 123}).json()[
        "proposal"
    ]
    token = plan["token"]
    assert client.post("/api/v1/assistant/review", json={"token": token + "x"}).status_code == 403
    other = as_user((client, auth), "other-actions-admin", Role.ADMIN)
    assert other.post("/api/v1/assistant/review", json={"token": token}).status_code == 403
    ops.clock = lambda: 1301
    assert client.post("/api/v1/assistant/review", json={"token": token}).status_code == 409
    ops.clock = lambda: 1001
    settings.upload_license(license_file(support_until="2020-01-01"))
    assert client.post("/api/v1/assistant/review", json={"token": token}).status_code == 403


def test_vm_plan_uses_uuid_force_false_and_rechecks_state(ready):
    client, _, _, _ = ready
    vm = client.post("/api/v1/vms", json={"name": "action-vm"}).json()
    r = client.post("/api/v1/assistant/chat", json={"message": "start VM action-vm"})
    assert r.status_code == 200, r.text
    plan = r.json()["proposal"]
    assert plan["path"] == f"/vms/{vm['uuid']}/start"
    assert client.get(f"/api/v1/vms/{vm['uuid']}").json()["state"] == "stopped"
    assert client.post("/api/v1" + plan["path"], json={}).status_code == 200
    assert client.post("/api/v1/assistant/review", json={"token": plan["token"]}).status_code == 409
    stop = client.post(
        "/api/v1/assistant/plan", json={"kind": "shutdown_vm", "target": vm["uuid"]}
    ).json()["proposal"]
    assert stop["body"] == {"force": False}
    assert client.get(f"/api/v1/vms/{vm['uuid']}").json()["state"] == "running"
    status = client.post(
        "/api/v1/assistant/chat", json={"message": "check status VM action-vm"}
    ).json()
    assert "Hypervisor: running" in status["reply"]


def test_custom_vm_role_can_plan_vm_but_not_vlan(ready):
    client, auth, _, _ = ready
    vm = client.post("/api/v1/vms", json={"name": "role-vm"}).json()
    auth.define_role("vm-actions", ["vms"])
    from fastapi.testclient import TestClient

    auth.add_user("vm-actions-user", "vm-actions", "Temp-pw-123")
    other = TestClient(client.app)
    assert (
        other.post(
            "/api/v1/auth/login", json={"username": "vm-actions-user", "password": "Temp-pw-123"}
        ).status_code
        == 200
    )
    other.post(
        "/api/v1/auth/password",
        json={"current_password": "Temp-pw-123", "new_password": "vm-actions-own-pw"},
    )
    assert (
        other.post(
            "/api/v1/assistant/plan", json={"kind": "start_vm", "target": vm["uuid"]}
        ).status_code
        == 200
    )
    assert (
        other.post("/api/v1/assistant/plan", json={"kind": "create_vlan", "vlan": 123}).status_code
        == 403
    )


def test_unsupported_or_negative_requests_cannot_generate_actions(ready):
    client, _, _, _ = ready
    for kind in ("delete_vm", "shell", "migrate_vm"):
        assert client.post("/api/v1/assistant/plan", json={"kind": kind}).status_code == 422
    from ibhics.manager.assistant_operations import parse_request

    assert parse_request("Do not create VLAN 123", "en") is None
    assert parse_request("อย่าสร้าง VLAN123", "th") is None
    assert (
        client.post("/api/v1/assistant/chat", json={"message": "create VLAN 4095"}).status_code
        == 422
    )


def test_normal_api_checks_confirmed_plan_body_entitlement_and_repeat(ready):
    client, _, settings, _ = ready
    plan = client.post("/api/v1/assistant/plan", json={"kind": "create_vlan", "vlan": 123}).json()[
        "proposal"
    ]
    headers = {"X-IBHICS-Assistant-Plan": plan["token"]}
    assert (
        client.post(
            "/api/v1/networks", json={"name": "wrong", "vlan": 124}, headers=headers
        ).status_code
        == 409
    )
    settings.upload_license(license_file(support_until="2020-01-01"))
    assert client.post("/api/v1/networks", json=plan["body"], headers=headers).status_code == 403
    settings.upload_license(license_file())
    assert client.post("/api/v1/networks", json=plan["body"], headers=headers).status_code == 201
    vm = client.post("/api/v1/vms", json={"name": "duplicate-restart"}).json()
    client.post(f"/api/v1/vms/{vm['uuid']}/start", json={})
    plan = client.post(
        "/api/v1/assistant/plan", json={"kind": "restart_vm", "target": vm["uuid"]}
    ).json()["proposal"]
    headers = {"X-IBHICS-Assistant-Plan": plan["token"]}
    assert (
        client.post("/api/v1" + plan["path"], json=plan["body"], headers=headers).status_code == 200
    )
    assert (
        client.post("/api/v1" + plan["path"], json=plan["body"], headers=headers).status_code == 409
    )
