"""P2V: an agent sends a physical machine's disks; they become an importable VM."""

import gzip
import os
import shutil
import subprocess
from concurrent.futures import Executor, Future
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ibhics.cluster import Cluster
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app
from ibhics.manager.imports import ImportManager
from ibhics.manager.p2v import AGENT, P2VError, P2VSessions, validate_inventory

MIB = 1 << 20


class Inline(Executor):
    def submit(self, fn, *args, **kwargs):
        f = Future()
        fn(*args, **kwargs)
        f.set_result(None)
        return f


@pytest.fixture
def env(tmp_path):
    driver = FakeDriver(demo_console=False)
    driver.files_root = str(tmp_path / "pools")
    cluster = Cluster.standalone("node1", driver)
    imports = ImportManager(cluster, executor=Inline())
    p2v = P2VSessions(tmp_path / "p2v.json")
    client = TestClient(create_app(cluster, imports=imports, p2v=p2v, p2v_pin="PIN="))
    return client, driver, p2v


def inventory(size=3 * MIB):
    return {
        "hostname": "srv01",
        "cpus": 4,
        "memory_mb": 8192,
        "firmware": "uefi",
        "os": "Ubuntu 24.04",
        "macs": ["AA:BB:CC:00:11:22", "bad"],
        "disks": [{"name": "sda", "size_bytes": size}],
    }


@pytest.mark.parametrize("os_name", ["Ubuntu 24.04", "Windows Server 2022"])
def test_session_upload_import_and_cleanup(env, os_name):
    client, driver, p2v = env
    opened = client.post("/api/v1/p2v/sessions", json={"pool": "default"}).json()
    sid = opened["session"]["id"]
    token = opened["command"].split("--token '")[1].split("'")[0]
    assert "--pinnedpubkey 'sha256//PIN='" in opened["command"]
    h = {"X-P2V-Token": token}
    assert (
        client.post(
            f"/api/v1/p2v/upload/{sid}/inventory",
            json=inventory(),
            headers={"X-P2V-Token": "wrong"},
        ).status_code
        == 403
    )
    machine = client.post(
        f"/api/v1/p2v/upload/{sid}/inventory", json={**inventory(), "os": os_name}, headers=h
    ).json()
    assert machine["macs"] == ["aa:bb:cc:00:11:22"]  # invalid entries dropped
    data = b"\x00" * MIB + b"boot".ljust(MIB, b"x") + b"\x00" * MIB
    resp = client.put(
        f"/api/v1/p2v/upload/{sid}/disks/0",
        content=gzip.compress(data),
        headers={**h, "Content-Encoding": "gzip"},
    )
    assert resp.status_code == 200 and resp.json()["bytes"] == len(data)
    raw = Path(p2v.get(sid).folder) / "disk0.raw"
    assert raw.read_bytes() == data
    if os.uname().sysname == "Linux":  # APFS on a Mac may allocate holes anyway
        assert raw.stat().st_blocks * 512 < len(data)  # zero megabytes stayed holes
    assert client.post(f"/api/v1/p2v/upload/{sid}/finish", headers=h).json()["state"] == "ready"
    # the token is spent once the upload is finished
    assert client.post(f"/api/v1/p2v/upload/{sid}/finish", headers=h).status_code == 403
    [vm] = client.get("/api/v1/migrate/sources/physical/vms").json()
    assert vm["name"] == "srv01" and vm["firmware"] == "uefi" and vm["memory_mb"] == 8192
    job = client.post("/api/v1/migrate/jobs", json={"source": "physical", "vm": sid}).json()
    assert job["state"] == "done", job
    assert [v.spec.name for v in driver.list_vms()] == ["srv01"]
    assert not raw.parent.exists()  # raw copies removed after the import
    assert client.get("/api/v1/p2v/sessions").json() == []


def test_short_or_oversized_disk_is_refused(env):
    client, _, p2v = env
    opened = client.post("/api/v1/p2v/sessions", json={"pool": "default"}).json()
    sid = opened["session"]["id"]
    h = {"X-P2V-Token": opened["command"].split("--token '")[1].split("'")[0]}
    client.post(f"/api/v1/p2v/upload/{sid}/inventory", json=inventory(2 * MIB), headers=h)
    short = client.put(f"/api/v1/p2v/upload/{sid}/disks/0", content=b"x" * MIB, headers=h)
    assert short.status_code == 422 and "stopped at" in short.json()["detail"]
    big = client.put(f"/api/v1/p2v/upload/{sid}/disks/0", content=b"x" * 3 * MIB, headers=h)
    assert big.status_code == 422
    assert client.post(f"/api/v1/p2v/upload/{sid}/finish", headers=h).status_code == 422
    assert not list(Path(p2v.get(sid).folder).glob("*.raw"))


@pytest.mark.parametrize(
    "bad",
    [
        {"hostname": "a b"},
        {"cpus": 0},
        {"firmware": "coreboot"},
        {"disks": []},
        {"disks": [{"name": "../etc", "size_bytes": 1 << 30}]},
    ],
)
def test_inventory_validation(bad):
    with pytest.raises(P2VError):
        validate_inventory({**inventory(), **bad})


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_agent_script_parses_and_refuses_missing_options():
    assert subprocess.run(["bash", "-n", str(AGENT)]).returncode == 0
    run = subprocess.run(["bash", str(AGENT)], capture_output=True, text=True)
    assert run.returncode == 2 and "usage" in run.stderr


def test_p2v_windows_agent_download(env, tmp_path, monkeypatch):
    import json

    from ibhics.manager import p2v as module

    exe = tmp_path / "agent.exe"
    exe.write_bytes(b"MZ-fake-executable")
    monkeypatch.setattr(module, "WINDOWS_AGENT", exe)
    client, _, sessions = env
    opened = client.post("/api/v1/p2v/sessions", json={"pool": "default"}).json()
    sid = opened["session"]["id"]
    token = opened["agent_token"]
    assert token == opened["command"].split("--token '")[1].split("'")[0]
    url = opened["windows_agent_url"]
    assert url == f"/api/v1/p2v/sessions/{sid}/agent.exe"
    assert client.get(url).status_code == 403
    assert client.get(url, headers={"X-P2V-Token": "wrong"}).status_code == 403
    response = client.get(url, headers={"X-P2V-Token": token})
    assert response.status_code == 200
    assert response.headers["content-disposition"] == f'attachment; filename="ibhics-p2v-{sid}.exe"'
    assert response.headers["cache-control"] == "no-store"
    payload, trailer = response.content.rsplit(b"\n#IBHICS-P2V#", 1)
    assert payload == exe.read_bytes()
    assert trailer.endswith(b"#END#")
    assert json.loads(trailer[:-5]) == {
        "server": "http://testserver",
        "session": sid,
        "token": token,
        "pin": "PIN=",
    }
    # Downloading does not rotate/spend the Linux command's token.
    assert sessions.authorize(sid, token).state == "waiting"
    saved = sessions._path.read_text()
    assert token not in saved
    assert token not in client.get("/api/v1/p2v/sessions").text
    sessions.mark(sid, "ready")
    assert client.get(url, headers={"X-P2V-Token": token}).status_code == 403


def test_p2v_windows_agent_missing(env, tmp_path, monkeypatch):
    from ibhics.manager import p2v as module

    monkeypatch.setattr(module, "WINDOWS_AGENT", tmp_path / "missing.exe")
    client, _, _ = env
    opened = client.post("/api/v1/p2v/sessions", json={"pool": "default"}).json()
    response = client.get(
        opened["windows_agent_url"], headers={"X-P2V-Token": opened["agent_token"]}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Windows agent is not included in this build"


def test_p2v_windows_download_requires_login(tmp_path, monkeypatch):
    from ibhics.manager import p2v as module
    from ibhics.manager.auth import Auth

    exe = tmp_path / "agent.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(module, "WINDOWS_AGENT", exe)
    auth = Auth(state_path=tmp_path / "users.json")
    auth.bootstrap("Initial-password-123")
    sessions = P2VSessions()
    session, token = sessions.open("default", tmp_path)
    client = TestClient(create_app(FakeDriver(demo_console=False), auth=auth, p2v=sessions))
    url = f"/api/v1/p2v/sessions/{session.id}/agent.exe"
    assert client.get(url, headers={"X-P2V-Token": token}).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "Initial-password-123"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "Initial-password-123",
                "new_password": "Updated-password-123",
            },
        ).status_code
        == 204
    )
    assert client.get(url, headers={"X-P2V-Token": token}).status_code == 200


@pytest.mark.parametrize("firmware", ["bios", "uefi"])
def test_p2v_windows_source_preserves_os_and_firmware(env, firmware):
    from ibhics.manager.p2v import source_vm

    _, _, sessions = env
    session, _ = sessions.open("default", Path(env[1].files_root))
    sessions.inventory(
        session,
        {
            **inventory(),
            "os": "Windows Server 2022 21H2 (build 20348)",
            "firmware": firmware,
            "disks": [{"name": "PhysicalDrive0", "size_bytes": 3 * MIB}],
        },
    )
    vm = source_vm(session)
    assert vm.guest_os == "Windows Server 2022 21H2 (build 20348)"
    assert vm.firmware.value == firmware
    assert vm.disk_paths == [str(Path(session.folder) / "disk0.raw")]
