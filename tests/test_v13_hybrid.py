"""1.3 hybrid pools: HDDs (or md RAID) for data, an SSD cache (LVM dm-cache) in front."""

import pytest
from fastapi.testclient import TestClient

from ibhics.hypervisor import hybrid
from ibhics.hypervisor.base import InvalidState, PoolSpec, PoolType
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app


class Lvm:
    def __init__(self, cached=True):
        self.calls, self.cached = [], cached
        self.cache_bytes = 200 * 2**30

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[0] == "lvs" and "lv_size" in cmd:
            return f"  {self.cache_bytes}\n"
        if cmd[0] == "lvs":
            if not self.cached:
                return "  ;0;0;0;0;0;0;0;0\n"
            return "  writethrough;1000;250;10;900;100;50;50;1048576\n"
        if cmd[0] == "pvs":
            return (
                "  /dev/sdb 4000000\n  /dev/nvme0n1 0\n" if "pv_used" in cmd[-3] else "/dev/sdb\n"
            )
        return ""


def test_build_makes_vg_data_and_cache():
    run = Lvm()
    dev = hybrid.build(run, "hy1", ["/dev/md0"], ["/dev/nvme0n1"], "writethrough", 200)
    assert dev == "/dev/ibhc-hy1/data" and hybrid.vg_of(dev) == "ibhc-hy1"
    cmds = [" ".join(c) for c in run.calls]
    assert "vgcreate -y ibhc-hy1 /dev/md0 /dev/nvme0n1" in cmds
    assert "lvcreate -y -n data -l 100%PVS ibhc-hy1 /dev/md0" in cmds
    assert "lvcreate -y -n cache -L 200G ibhc-hy1 /dev/nvme0n1" in cmds
    want = "--cachevol cache --cachemode writethrough --chunksize 256k ibhc-hy1/data"
    assert any(want in c for c in cmds)
    assert hybrid.vg_of("/dev/sdb") is None


def test_stats_hit_ratio_and_dirty():
    st = hybrid.stats(Lvm(), "ibhc-x")
    assert st["mode"] == "writethrough" and st["size_gb"] == pytest.approx(1000 / 1024, 0.1)
    assert st["used_pct"] == 25.0 and st["read_hit_pct"] == 90.0 and st["write_hit_pct"] == 50.0
    assert st["hit_pct"] == pytest.approx(86.4, 0.01)
    assert hybrid.stats(Lvm(cached=False), "ibhc-x") is None


def test_remove_add_and_mode():
    run = Lvm()
    hybrid.remove_cache(run, "ibhc-x")
    cmds = [" ".join(c) for c in run.calls]
    assert "lvconvert -y --uncache ibhc-x/data" in cmds
    assert "vgreduce ibhc-x /dev/nvme0n1" in cmds and "pvremove -y /dev/nvme0n1" in cmds
    with pytest.raises(InvalidState):
        hybrid.add_cache(run, "ibhc-x", ["/dev/sdc"], "writeback", None)  # has one already
    hybrid.add_cache(Lvm(cached=False), "ibhc-x", ["/dev/sdc"], "writeback", None)
    with pytest.raises(InvalidState):
        hybrid.set_mode(Lvm(cached=False), "ibhc-x", "writeback")
    run = Lvm()
    hybrid.set_mode(run, "ibhc-x", "writeback")
    assert run.calls[-1] == ["lvchange", "-y", "--cachemode", "writeback", "ibhc-x/data"]


def test_spec_validation():
    ok = PoolSpec("h", PoolType.DISK, devices=["/dev/sda", "/dev/sdb"],
                  cache_devices=["/dev/nvme0n1"], confirm_wipe=True)  # fmt: skip
    ok.validate()  # several data disks are fine with a cache
    for bad in (
        {"cache_devices": ["/dev/sda"]},
        {"cache_mode": "fast"},
        {"cache_gb": 0},
    ):
        with pytest.raises(ValueError):
            PoolSpec("h", PoolType.DISK, devices=["/dev/sda"], confirm_wipe=True,
                     **{"cache_devices": ["/dev/nvme0n1"], **bad}).validate()  # fmt: skip
    with pytest.raises(ValueError):
        PoolSpec("h", PoolType.LVM, devices=["/dev/sda"], cache_devices=["/dev/sdb"],
                 confirm_wipe=True).validate()  # fmt: skip


def test_api_hybrid_pool_and_cache():
    client = TestClient(create_app(FakeDriver()))
    node = client.get("/api/v1/pools").json()[0]["node"]
    body = {"name": "hy", "type": "disk", "devices": ["/dev/sdb"], "cache_devices": ["/dev/sdc"],
            "cache_mode": "writethrough", "confirm_wipe": True}  # fmt: skip
    p = client.post(f"/api/v1/nodes/{node}/pools", json=body).json()
    assert p["hybrid"] and p["cache"]["mode"] == "writethrough"
    url = f"/api/v1/nodes/{node}/pools/hy/cache"
    assert client.post(url, json={"action": "mode", "mode": "writeback"}).json()["cache"]["mode"] \
        == "writeback"  # fmt: skip
    assert client.post(url, json={"action": "remove"}).json()["cache"] is None
    assert client.post(url, json={"action": "add", "devices": ["/dev/sdc"]}).status_code == 422
    r = client.post(url, json={"action": "add", "devices": ["/dev/sdc"], "confirm_wipe": True})
    assert r.json()["cache"]
    assert client.post(f"/api/v1/nodes/{node}/pools/default/cache",
                       json={"action": "remove"}).status_code == 409  # fmt: skip
    assert "pool-cache" in {j["kind"] for j in client.get("/api/v1/backup/jobs").json()}


def test_chunk_size_fits_big_ssds():
    run = Lvm()
    assert hybrid.chunk_size(run, "ibhc-x") == "256k"  # 200 GiB: 64k would be 3.3M chunks
    run.cache_bytes = 894 * 10**9  # the 894 GB SSD that lvconvert refused with 64k chunks
    size = int(hybrid.chunk_size(run, "ibhc-x")[:-1]) * 1024
    assert run.cache_bytes // size <= 1_000_000 and size == 1024 * 1024
    run.cache_bytes = 10 * 2**30
    assert hybrid.chunk_size(run, "ibhc-x") == "64k"
    run = Lvm()
    hybrid.build(run, "hy1", ["/dev/md0"], ["/dev/sdc"], "writethrough", None)
    assert ["--chunksize", "256k"] == run.calls[-1][-3:-1]
