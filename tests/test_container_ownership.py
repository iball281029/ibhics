"""Ownership is enforced in node inventory and actions, independent of UI deduplication."""

import json
from pathlib import Path

import pytest

from ibhics.hypervisor import InvalidState
from ibhics.hypervisor.containers import ContainerNotFound, ContainerSpec, LxcContainers


def nodes(tmp_path, monkeypatch):
    states = {"node1": {}, "node2": {}}
    calls = []

    def make(node):
        def run(argv):
            calls.append((node, argv))
            name = argv[argv.index("-n") + 1] if "-n" in argv else ""
            if argv[0] == "lxc-create":
                home = Path(argv[argv.index("-P") + 1]) / name
                home.mkdir()
                (home / "config").write_text(
                    "lxc.include = /usr/share/lxc/config/debian.common.conf\n"
                )
                (home / "rootdev").write_bytes(b"root filesystem contents")
            if argv[0] == "lxc-start":
                states[node][name] = "RUNNING"
            if argv[0] == "lxc-stop":
                states[node][name] = "STOPPED"
            if argv[0] == "lxc-info":
                return 0, "State: " + states[node].get(name, "STOPPED") + "\n"
            return 0, ""

        backend = LxcContainers(
            lambda pool: str(tmp_path / pool),
            lambda name: "br0",
            run,
            lambda: ["pool"],
            node_name=node,
        )
        # LXC/CRIU are simulated here; exercise real holders in test_container_ha.
        monkeypatch.setattr(backend, "_runlock_acquire", lambda *args: None)
        monkeypatch.setattr(backend, "_runlock_release", lambda *args: None)
        monkeypatch.setattr(backend, "_wait_running", lambda *args: None)
        monkeypatch.setattr(backend, "_apply_network", lambda *args: None)
        return backend

    return make("node1"), make("node2"), states, calls


def test_create_has_one_persistent_owner_and_peer_cannot_start_or_delete(tmp_path, monkeypatch):
    first, second, states, calls = nodes(tmp_path, monkeypatch)
    first.create(ContainerSpec("app", pool="pool"))
    owner = tmp_path / "pool/containers/app/.ibhics-owner.json"
    assert json.loads(owner.read_text()) == {"node": "node1"}
    assert len(first.list()) == 1 and second.list() == []
    before = len(calls)
    for operation in [lambda: second.action("app", "start"), lambda: second.delete("app")]:
        with pytest.raises(ContainerNotFound):
            operation()
    assert len(calls) == before
    # New backend instances read the same durable authority after a service restart.
    restarted = LxcContainers(
        lambda pool: str(tmp_path / pool),
        lambda name: "br0",
        first._run,
        lambda: ["pool"],
        node_name="node1",
    )
    assert restarted.get("app").owner_node == "node1"
    with pytest.raises(InvalidState):
        second.create(ContainerSpec("app", pool="pool"))
    assert (owner.parent / "rootfs.img").read_bytes() == b"root filesystem contents"


def test_creation_reservation_is_shared_between_node_instances(tmp_path, monkeypatch):
    first, second, _, _ = nodes(tmp_path, monkeypatch)
    base = tmp_path / "pool/containers"
    base.mkdir(parents=True)
    with first._creation_lock(base, "same"):
        with pytest.raises(InvalidState, match="already in progress"):
            second.create(ContainerSpec("same", pool="pool"))
    assert not (base / "same").exists()
    second.create(ContainerSpec("same", pool="pool"))
    assert len(second.list()) == 1 and first.list() == []


def test_owner_cannot_be_overwritten_and_corruption_fails_closed(tmp_path, monkeypatch):
    first, second, _, _ = nodes(tmp_path, monkeypatch)
    first.create(ContainerSpec("app", pool="pool"))
    home = tmp_path / "pool/containers/app"
    with pytest.raises(InvalidState, match="another node"):
        second._claim_owner(home)
    assert json.loads((home / ".ibhics-owner.json").read_text())["node"] == "node1"
    (home / ".ibhics-owner.json").write_text("{bad")
    with pytest.raises(InvalidState, match="metadata"):
        first.list()
    assert (home / "rootfs.img").exists()


def test_unowned_legacy_container_is_claimed_by_running_node_or_explicit_assignment(
    tmp_path, monkeypatch
):
    first, second, states, _ = nodes(tmp_path, monkeypatch)
    home = tmp_path / "pool/containers/legacy"
    home.mkdir(parents=True)
    (home / "ibhics.json").write_text(
        json.dumps(ContainerSpec("legacy", pool="pool").public() | {"password": ""})
    )
    with pytest.raises(InvalidState, match="no registered owner"):
        second.action("legacy", "start")
    states["node1"]["legacy"] = "RUNNING"
    assert first.list()[0].owner_node == "node1"
    assert second.list() == []


def test_nfs_negative_owner_lookup_uses_persisted_settings_authority(tmp_path, monkeypatch):
    first, second, _, _ = nodes(tmp_path, monkeypatch)
    first.create(ContainerSpec("app", pool="pool"))
    meta = tmp_path / "pool/containers/app/ibhics.json"
    assert json.loads(meta.read_text())["owner_node"] == "node1"
    original = Path.read_text

    def negative_owner_lookup(path, *args, **kwargs):
        if path.name == ".ibhics-owner.json":
            raise FileNotFoundError("NFS cached a negative lookup")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", negative_owner_lookup)
    assert second.list() == []
    with pytest.raises(ContainerNotFound):
        second.delete("app")
