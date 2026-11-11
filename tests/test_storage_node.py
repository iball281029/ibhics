import json
from types import SimpleNamespace

from test_storage_create import fixture

from ibhics.storage_node import NodeProvisionIn, provision


def test_large_ssd_cache_provision_fits_lvm_chunk_limit(tmp_path, monkeypatch):
    spec, inventories = fixture()
    current = inventories["node1"]
    for disk in current["disks"]:
        disk["reclaim_pools"] = []
    current["disks"][1]["size_gib"] = 894.3
    monkeypatch.setattr("ibhics.storage_node.inventory", lambda *a, **kw: current)
    calls = []
    cache_bytes = 912181428224  # Actual cache LV at the reported production failure.

    def run(args, **kwargs):
        calls.append(args)
        output = ""
        if args[0] == "lsblk":
            output = json.dumps(
                {
                    "blockdevices": [
                        {"name": p, "path": p, "type": "disk", "fstype": None, "mountpoints": []}
                        for p in ("/dev/sda", "/dev/sdb")
                    ]
                }
            )
        elif args[0] == "pvs":
            output = json.dumps({"report": [{"pv": []}]})
        elif args[0] == "lvs":
            output = str(cache_bytes)
        elif args[0] == "lvconvert" and "--cachevol" in args:
            chunk = int(args[args.index("--chunksize") + 1][:-1]) * 1024
            assert cache_bytes // chunk <= 1_000_000
            assert chunk == 1024**2
        elif args[0] == "lvcreate":
            # Reused disks can retain signatures inside newly allocated LV extents.
            # The operator already confirmed erasure; never stop for an invisible prompt.
            assert "-y" in args
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    result = provision(
        None,
        NodeProvisionIn(name=spec.name, selection=spec.nodes[0], size_gib=80, confirm_wipe=True),
        node="node1",
        root=tmp_path,
        run=run,
    )
    assert result["cache_mode"] == "writethrough"
    assert len([c for c in calls if c[0] == "lvconvert"]) == 2
    creates = [c for c in calls if c[0] == "lvcreate"]
    data_lv, meta_lv = creates[0], creates[1]
    # thin data on the HDD only, thin metadata on the SSD only (before the cache takes it)
    assert "thin" in data_lv and set(data_lv) & set(spec.nodes[0].data)
    assert not set(data_lv) & set(spec.nodes[0].cache)
    assert "thin_meta" in meta_lv and set(meta_lv[-len(spec.nodes[0].cache) :]) == set(
        spec.nodes[0].cache
    )
    pool = next(c for c in calls if c[0] == "lvconvert" and "thin-pool" in c)
    assert pool[pool.index("--poolmetadata") + 1].endswith("/thin_meta")
    assert calls.index(pool) < calls.index(creates[2])  # cache LV comes after
