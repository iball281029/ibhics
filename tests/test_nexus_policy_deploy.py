"""Deployment must reject local drift and restore precisely the files it changes."""

import base64
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "backup_deploy", Path(__file__).parents[1] / "ops/deploy/nexus_policy.py"
)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_drift_blocks_update(tmp_path):
    (tmp_path / "module.py").write_bytes(b"local edit")
    with pytest.raises(RuntimeError, match="differ"):
        deploy.check_files(tmp_path, {"module.py": [deploy.digest(b"reviewed")]})
    assert (tmp_path / "module.py").read_bytes() == b"local edit"


def test_symlink_blocks_update(tmp_path):
    (tmp_path / "real").write_bytes(b"safe")
    (tmp_path / "module.py").symlink_to(tmp_path / "real")
    with pytest.raises(RuntimeError, match="symlink"):
        deploy.check_files(tmp_path, {"module.py": [deploy.digest(b"safe")]})


def test_update_and_rollback_preserve_unrelated_files(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    backup = tmp_path / "backup"
    (backup / "files").mkdir(parents=True)
    (package / "old.py").write_bytes(b"old")
    (backup / "files/old.py").write_bytes(b"old")
    (package / "unrelated").write_bytes(b"keep")
    (package / "__pycache__").mkdir()
    (package / "__pycache__/old.pyc").write_bytes(b"stale")
    files = {
        name: base64.b64encode(data).decode()
        for name, data in [("old.py", b"new"), ("added.py", b"added")]
    }
    deploy.apply_files(package, files)
    assert (package / "old.py").read_bytes() == b"new"
    assert (package / "added.py").exists()
    assert not (package / "__pycache__").exists()
    deploy.rollback_code(package, backup, files)
    assert (package / "old.py").read_bytes() == b"old"
    assert not (package / "added.py").exists()
    assert (package / "unrelated").read_bytes() == b"keep"


@pytest.mark.parametrize("health_passes", [True, False])
def test_transaction_rolls_back_on_unhealthy_services(tmp_path, monkeypatch, health_passes):
    import json
    import sys
    import types

    root = tmp_path / "opt"
    package = root / "venv/lib/ibhics"
    package.mkdir(parents=True)
    state = tmp_path / "backup.json"
    state.write_text('{"policies": []}')
    (package / "module.py").write_bytes(b"old")
    stage = root / "backup-staging/stage"
    stage.mkdir(parents=True)
    (stage / "stage-receipt.json").write_text(
        json.dumps({"source_sha256": "verified", "state": "passed"})
    )
    calls = []

    def run(*args):
        calls.append(args)
        return str(package) if args[0].endswith("/python") and "pathlib" in args[-1] else ""

    def health(*args):
        assert (root / ".backup-upgrade-in-progress").exists()
        if not health_passes:
            raise RuntimeError("unhealthy")

    monkeypatch.setattr(deploy, "ROOT", root)
    monkeypatch.setattr(deploy, "VENV", root / "venv")
    monkeypatch.setattr(deploy.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deploy.sys, "platform", "linux")
    monkeypatch.setattr(deploy, "run", run)
    monkeypatch.setattr(deploy, "active", lambda _: True)
    monkeypatch.setattr(deploy, "environment", lambda _: {"IBHICS_BACKUP_STATE_FILE": str(state)})
    monkeypatch.setattr(deploy, "check_idle", lambda: None)
    monkeypatch.setattr(deploy, "check_health", health)
    monkeypatch.setitem(sys.modules, "nbd", types.ModuleType("nbd"))
    monkeypatch.setitem(sys.modules, "zstandard", types.ModuleType("zstandard"))
    monkeypatch.setattr(
        deploy.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0)
    )
    payload = {
        "expected": {"module.py": [deploy.digest(b"old")]},
        "stage_sha": "verified",
        "files": {"module.py": base64.b64encode(b"new").decode()},
        "new_hashes": {"module.py": deploy.digest(b"new")},
    }
    if health_passes:
        deploy.main(payload)
        assert (package / "module.py").read_bytes() == b"new"
        assert (root / "nexus-policy-release.json").exists()
    else:
        with pytest.raises(RuntimeError, match="unhealthy"):
            deploy.main(payload)
        assert (package / "module.py").read_bytes() == b"old"
        assert not (root / "nexus-policy-release.json").exists()
    assert state.read_text() == '{"policies": []}'
    assert not (root / ".backup-upgrade-in-progress").exists()
    assert not any("libvirt" in str(call) for call in calls)


def test_ha_standby_is_valid_but_generic_503_is_rejected():
    env = {"IBHICS_SHARED_STATE": "/shared", "IBHICS_NODE_NAME": "test-node"}
    body = {"status": "standby", "node": "test-node", "active_node": "node2"}
    assert deploy.health_body_ok("ibhics-manager", 503, body, env)
    assert not deploy.health_body_ok("ibhics-manager", 503, {"status": "unavailable"}, env)
    assert not deploy.health_body_ok("ibhics-manager", 503, body, {})
    assert not deploy.health_body_ok(
        "ibhics-manager", 503, {**body, "active_node": "test-node"}, env
    )
    assert not deploy.health_body_ok("ibhics-manager", 503, {**body, "node": "wrong"}, env)
    assert not deploy.health_body_ok("ibhics-agent", 503, body, env)


def test_active_manager_and_agent_require_expected_version():
    body = {"status": "ok", "version": "1.3.0b2+vstore3.27"}
    assert deploy.health_body_ok("ibhics-manager", 200, body, {})
    assert deploy.health_body_ok("ibhics-agent", 200, {"version": body["version"]}, {})
    assert not deploy.health_body_ok("ibhics-manager", 503, body, {})
    assert not deploy.health_body_ok("ibhics-manager", 200, {**body, "version": "old"}, {})


def test_health_check_reads_ha_body_from_http_503(monkeypatch, capsys):
    import io
    import json
    import types
    import urllib.error

    body = {
        "status": "standby",
        "node": "test-node",
        "active_node": "node2",
        "active_url": "https://172.16.12.103:8888",
    }

    def open_response(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 503, "Standby", {}, io.BytesIO(json.dumps(body).encode())
        )

    monkeypatch.setattr(
        deploy.urllib.request,
        "build_opener",
        lambda *args: types.SimpleNamespace(open=open_response),
    )
    monkeypatch.setattr(deploy, "active", lambda _: True)
    deploy.check_health(
        ["ibhics-manager"],
        {"ibhics-manager": {"IBHICS_SHARED_STATE": "/shared", "IBHICS_NODE_NAME": "test-node"}},
    )
    assert "ACTIVE_MANAGER_URL: https://172.16.12.103:8888" in capsys.readouterr().out
