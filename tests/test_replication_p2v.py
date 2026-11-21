"""Push delta protocol: authentication, integrity and atomic multi-disk commit."""

import hashlib
import json
from pathlib import Path

import pytest

from ibhics.manager.p2v import P2VError, P2VSessions, source_vm
from ibhics.replication.p2v import session_store
from ibhics.replication.store import CHUNK
from tests.test_p2v import env as p2v_env
from tests.test_p2v import inventory

env = p2v_env


def payload(disks):
    machine = inventory(len(disks[0]))
    machine["disks"] = [
        {"name": f"disk{i}", "size_bytes": len(data)} for i, data in enumerate(disks)
    ]
    return {
        "machine": machine,
        "hashes": [
            [hashlib.sha256(data[n : n + CHUNK]).hexdigest() for n in range(0, len(data), CHUNK)]
            for data in disks
        ],
    }


def start(env, disks):
    client, _, sessions = env
    opened = client.post("/api/v1/p2v/sessions", json={"pool": "default"}).json()
    sid = opened["session"]["id"]
    headers = {"X-P2V-Token": opened["agent_token"]}
    base = f"/api/v1/p2v/upload/{sid}/delta"
    return client, sessions, sid, headers, base


def upload(client, headers, base, plan, disks):
    for i, chunks in enumerate(plan["needed"]):
        for n in chunks:
            response = client.put(
                f"{base}/{plan['round']}/disks/{i}/chunks/{n}",
                content=disks[i][n * CHUNK : (n + 1) * CHUNK],
                headers=headers,
            )
            assert response.status_code == 200, response.text


def test_delta_two_rounds_atomic_multi_disk_and_zero_chunk(env):
    disks = [b"a" * CHUNK + b"b" * (1 << 20), bytes(1 << 20)]
    client, sessions, sid, h, base = start(env, disks)
    data = payload(disks)
    assert client.post(base + "/begin", json=data).status_code == 403
    plan = client.post(base + "/begin", json=data, headers=h).json()
    assert plan["needed"] == [[0, 1], []] and plan["chunk_size"] == CHUNK
    commit = f"{base}/{plan['round']}/commit"
    assert client.post(commit, headers=h).status_code == 422
    store = session_store(sessions.get(sid).folder, data["machine"])
    assert store.current() == {}
    upload(client, h, base, plan, disks)
    first = client.post(commit, headers=h).json()
    assert first["change_tracking"] == "hash-delta"
    assert [Path(p).read_bytes() for p in source_vm(sessions.get(sid)).disk_paths] == disks
    assert client.post(commit, headers=h).status_code == 422
    disks[0] = b"a" * CHUNK + b"c" * (1 << 20)
    data = payload(disks)
    plan = client.post(base + "/begin", json=data, headers=h).json()
    assert plan["needed"] == [[1], []]
    # Before commit the entire prior generation stays authoritative.
    assert store.current()["generation"] == first["generation"]
    upload(client, h, base, plan, disks)
    second = client.post(f"{base}/{plan['round']}/commit", headers=h).json()
    assert second["generation"] != first["generation"]
    assert second["bytes_transferred"] == 1 << 20
    assert [Path(p).read_bytes() for p in source_vm(sessions.get(sid)).disk_paths] == disks
    plan = client.post(base + "/begin", json=data, headers=h).json()
    assert plan["needed"] == [[], []]
    assert client.post(f"{base}/{plan['round']}/commit", headers=h).json()["bytes_transferred"] == 0
    # Standard one-shot upload endpoints still reject spent tokens.
    assert client.post(base.rsplit("/delta", 1)[0] + "/finish", headers=h).status_code == 403
    restarted = P2VSessions(sessions._path)
    assert restarted.authorize(sid, h["X-P2V-Token"], replicate=True).state == "ready"
    assert h["X-P2V-Token"] not in sessions._path.read_text()


def test_delta_corrupt_oversized_and_superseded_rounds(env):
    disks = [b"a" * (1 << 20)]
    client, _, _, h, base = start(env, disks)
    data = payload(disks)
    for bad in ({**data, "hashes": [["bad"]]}, {**data, "hashes": []}):
        assert client.post(base + "/begin", json=bad, headers=h).status_code == 422
    plan = client.post(base + "/begin", json=data, headers=h).json()
    path = f"{base}/{plan['round']}/disks/0/chunks/0"
    assert client.put(path, content=b"x" * (1 << 20), headers=h).status_code == 422
    assert client.put(path, content=b"x" * (CHUNK + 1), headers=h).status_code == 413
    assert client.put(path, content=disks[0], headers={"X-P2V-Token": "wrong"}).status_code == 403
    plan2 = client.post(base + "/begin", json=data, headers=h).json()
    assert client.put(path, content=disks[0], headers=h).status_code == 422
    assert client.post(f"{base}/{plan['round']}/commit", headers=h).status_code == 422
    upload(client, h, base, plan2, disks)
    assert client.post(f"{base}/{plan2['round']}/commit", headers=h).status_code == 200


def test_delta_failed_store_commit_preserves_baseline(env, monkeypatch):
    from ibhics.replication import store as module

    disks = [b"a" * (1 << 20)]
    client, sessions, sid, h, base = start(env, disks)
    plan = client.post(base + "/begin", json=payload(disks), headers=h).json()
    upload(client, h, base, plan, disks)
    client.post(f"{base}/{plan['round']}/commit", headers=h)
    store = session_store(sessions.get(sid).folder, payload(disks)["machine"])
    baseline = store.current()
    disks[0] = b"b" * (1 << 20)
    plan = client.post(base + "/begin", json=payload(disks), headers=h).json()
    upload(client, h, base, plan, disks)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(module, "durable_json", fail)
    with pytest.raises(OSError):
        sessions.delta_commit(sessions.get(sid), plan["round"])
    assert store.current() == baseline
    assert Path(baseline["disks"][0]["path"]).read_bytes() == b"a" * (1 << 20)


def test_delta_ttl_and_restart_without_completed_round(tmp_path):
    now = [0]
    sessions = P2VSessions(tmp_path / "sessions.json", clock=lambda: now[0])
    session, token = sessions.open("default", tmp_path)
    sessions.delta_begin(session, payload([bytes(1 << 20)]))
    restarted = P2VSessions(sessions._path, clock=lambda: now[0])
    with pytest.raises(P2VError, match="begin again"):
        restarted.delta_commit(restarted.get(session.id), "old")
    now[0] = 25 * 3600
    with pytest.raises(PermissionError, match="expired"):
        sessions.authorize(session.id, token, replicate=True)


def test_delta_manifest_recovers_after_session_save_crash(tmp_path, monkeypatch):
    sessions = P2VSessions(tmp_path / "sessions.json")
    session, _ = sessions.open("default", tmp_path)
    plan = sessions.delta_begin(session, payload([bytes(1 << 20)]))

    def fail():
        raise OSError("save failed")

    monkeypatch.setattr(sessions, "_save", fail)
    with pytest.raises(OSError):
        sessions.delta_commit(session, plan["round"])
    restarted = P2VSessions(sessions._path)
    recovered = restarted.get(session.id)
    assert recovered.state == "ready"
    assert Path(source_vm(recovered).disk_paths[0]).read_bytes() == bytes(1 << 20)
    assert (
        json.loads(session_store(session.folder, recovered.machine).current()["token"])["hostname"]
        == "srv01"
    )


def test_delta_import_claim_blocks_push_and_refreshes_generation(env):
    disks = [b"a" * (1 << 20)]
    client, sessions, sid, h, base = start(env, disks)
    plan = client.post(base + "/begin", json=payload(disks), headers=h).json()
    upload(client, h, base, plan, disks)
    with pytest.raises(P2VError, match="finish"):
        sessions.claim_import(sid)
    client.post(f"{base}/{plan['round']}/commit", headers=h)
    vm = sessions.claim_import(sid)
    assert Path(vm.disk_paths[0]).read_bytes() == disks[0]
    assert sessions.get(sid).state == "importing"
    assert client.post(base + "/begin", json=payload(disks), headers=h).status_code == 403
    sessions.mark(sid, "ready")
    assert client.post(base + "/begin", json=payload(disks), headers=h).status_code == 200


@pytest.mark.parametrize("machine", [{"disks": [1]}, None, {"hostname": "a", "cpus": None}])
def test_delta_bad_inventory_is_client_error(env, machine):
    client, _, _, h, base = start(env, [])
    assert (
        client.post(base + "/begin", json={"machine": machine, "hashes": []}, headers=h).status_code
        == 422
    )
