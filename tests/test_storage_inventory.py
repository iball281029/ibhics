from copy import deepcopy

from ibhics.hypervisor.storage_inventory import parse_inventory


def disk(path, children=None, **extra):
    return {
        "name": path.removeprefix("/dev/"),
        "path": path,
        "type": "disk",
        "size": 100 * 1024**3,
        "serial": path,
        "rota": True,
        "children": children or [],
        **extra,
    }


def lv(path, mount=None):
    return {
        "name": path,
        "path": path,
        "type": "lvm",
        "fstype": "xfs",
        "mountpoints": [mount] if mount else [],
    }


def test_os_disk_and_swap_never_selectable():
    data = {
        "blockdevices": [
            disk("/dev/sda", [lv("/dev/mapper/sys-root", "/")]),
            disk("/dev/sdb", [lv("/dev/mapper/sys-swap", "[SWAP]")]),
            disk("/dev/sdc"),
        ]
    }
    result = parse_inventory(data, [], [])
    assert [d["selectable"] for d in result["disks"]] == [False, False, True]


def test_reclaim_pool_requires_all_raid_and_cache_members():
    data_lv = lv("/dev/mapper/ibhc--hybrid01-data", "/var/lib/ibhics/pools/hybrid01")
    raid = {"path": "/dev/md127", "name": "md127", "type": "raid1", "children": [data_lv]}
    data = {
        "blockdevices": [
            disk("/dev/sda", [raid]),
            disk("/dev/sdb", [deepcopy(raid)]),
            disk("/dev/sdc", [deepcopy(data_lv)], rota=False),
        ]
    }
    pools = [{"name": "hybrid01", "target": "/var/lib/ibhics/pools/hybrid01"}]
    result = parse_inventory(data, pools, [])
    assert all(d["selectable"] for d in result["disks"])
    assert all(d["required_disks"] == ["/dev/sda", "/dev/sdb", "/dev/sdc"] for d in result["disks"])
    assert all(d["reclaim_pools"] == ["hybrid01"] for d in result["disks"])
    # Any live, persistent, snapshot or backing-store reference blocks the group.
    used = parse_inventory(
        data, pools, [{"owner": "npm-vm", "path": "/var/lib/ibhics/pools/hybrid01/npm-vm.qcow2"}]
    )
    assert not any(d["selectable"] for d in used["disks"])


def test_unknown_mount_and_shared_system_vg_block_entire_group():
    shared = lv("/dev/mapper/shared-data")
    data = {
        "blockdevices": [
            disk("/dev/a", [shared, lv("/dev/mapper/shared-root", "/")]),
            disk("/dev/b", [deepcopy(shared)]),
            disk("/dev/c", [lv("/dev/mapper/other-data", "/mnt/other")]),
        ]
    }
    assert not any(d["selectable"] for d in parse_inventory(data, [], [])["disks"])


def test_fingerprint_changes_with_disk_identity_or_reference():
    data = {"blockdevices": [disk("/dev/sda")]}
    first = parse_inventory(data, [], [])
    data["blockdevices"][0]["serial"] = "replaced-disk"
    assert first["fingerprint"] != parse_inventory(data, [], [])["fingerprint"]


def test_foreign_vg_with_separate_lvs_still_requires_all_pvs():
    data = {
        "blockdevices": [
            disk("/dev/sda", [lv("/dev/mapper/ceph-data")]),
            disk("/dev/sdb", [lv("/dev/mapper/ceph-db")]),
        ]
    }
    physical = {
        "report": [
            {
                "pv": [
                    {"pv_name": "/dev/sda", "vg_name": "ceph"},
                    {"pv_name": "/dev/sdb", "vg_name": "ceph"},
                ]
            }
        ]
    }
    rows = parse_inventory(data, [], [], physical)["disks"]
    assert all(d["required_disks"] == ["/dev/sda", "/dev/sdb"] for d in rows)
    physical["report"][0]["pv"].append({"pv_name": "/dev/missing", "vg_name": "ceph"})
    assert not any(d["selectable"] for d in parse_inventory(data, [], [], physical)["disks"])


def test_file_path_prefix_does_not_confuse_two_pool_names():
    mount = "/var/lib/ibhics/pools/data"
    data = {"blockdevices": [disk("/dev/sda", [lv("/dev/mapper/data", mount)])]}
    pool = [{"name": "data", "target": mount}]
    refs = [{"owner": "other", "path": mount + "-other/vm.qcow2"}]
    assert parse_inventory(data, pool, refs)["disks"][0]["selectable"]
