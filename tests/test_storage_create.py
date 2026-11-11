from copy import deepcopy

import pytest
from pydantic import ValidationError

from ibhics.storage_create import StorageCreateSpec, review_disks


def fixture():
    spec = StorageCreateSpec(
        name="hybrid01",
        arbiter="arbiter-01",
        virtual_ip="172.16.12.106",
        size_gib=80,
        nodes=[
            {"node": name, "fingerprint": "a" * 64, "data": ["/dev/sda"], "cache": ["/dev/sdb"]}
            for name in ("node1", "node2")
        ],
    )
    disks = [
        {
            "path": path,
            "selectable": True,
            "size_gib": 100,
            "reasons": [],
            "required_disks": [path],
            "reclaim_pools": ["hybrid01"],
            "media": media,
        }
        for path, media in (("/dev/sda", "hdd"), ("/dev/sdb", "ssd"))
    ]
    inventories = {
        name: {"fingerprint": "a" * 64, "disks": deepcopy(disks)} for name in ("node1", "node2")
    }
    return spec, inventories


def test_review_reports_disks_and_reclaimed_pools():
    spec, inventories = fixture()
    review = review_disks(spec, inventories)
    assert review["maximum_gib"] == 86
    assert review["copies"] == 2
    assert review["policy"] == "RF2"
    assert review["nodes"][0]["reclaim_pools"] == ["hybrid01"]


def test_rf2_uses_all_data_disk_capacity_without_a_second_local_mirror():
    spec, inventories = fixture()
    for selected in spec.nodes:
        selected.data = ["/dev/sda", "/dev/sdb"]
        selected.cache = ["/dev/sdc"]
        inventories[selected.node]["disks"] = [
            {
                "path": p,
                "selectable": True,
                "size_gib": size,
                "media": media,
                "required_disks": [p],
                "reclaim_pools": [],
                "reasons": [],
            }
            for p, size, media in (
                ("/dev/sda", 3726, "hdd"),
                ("/dev/sdb", 3726, "hdd"),
                ("/dev/sdc", 894.3, "ssd"),
            )
        ]
    spec.size_gib = 6703
    reviewed = review_disks(spec, inventories)
    capacity = reviewed["capacity"]
    assert capacity["raw_data_gib"] == 14904
    assert capacity["after_rf2_gib"] == 7452
    assert capacity["headroom_gib"] == pytest.approx(745.2)
    assert capacity["metadata_and_service_gib"] == 3.25
    assert capacity["maximum_gib"] == 6703
    assert capacity["cache_gib"] == pytest.approx(1788.6)
    assert capacity["unmatched_gib"] == 0


def test_old_layout_option_and_rf3_cannot_be_submitted():
    spec, _ = fixture()
    body = spec.model_dump()
    body["nodes"][0]["layout"] = "mirror"
    with pytest.raises(ValidationError):
        StorageCreateSpec(**body)
    body = spec.model_dump() | {"replicas": 3}
    with pytest.raises(ValidationError):
        StorageCreateSpec(**body)


def test_smaller_node_limits_two_copy_capacity():
    spec, inventories = fixture()
    inventories["node2"]["disks"][0]["size_gib"] = 90
    spec.size_gib = 70
    capacity = review_disks(spec, inventories)["capacity"]
    assert capacity["raw_data_gib"] == 190
    assert capacity["after_rf2_gib"] == 90
    assert capacity["unmatched_gib"] == 5


@pytest.mark.parametrize("change", ["identity", "protected", "shared", "capacity", "cache"])
def test_review_refuses_changed_or_unsafe_selection(change):
    spec, inventories = fixture()
    node = inventories["node1"]
    if change == "identity":
        node["fingerprint"] = "b" * 64
    elif change == "protected":
        node["disks"][0].update(selectable=False, reasons=["Protected mount: /"])
    elif change == "shared":
        node["disks"][0]["required_disks"].append("/dev/sdc")
    elif change == "capacity":
        node["disks"][0]["size_gib"] = 10
    else:
        node["disks"][1]["media"] = "hdd"
    with pytest.raises(ValueError):
        review_disks(spec, inventories)


@pytest.mark.parametrize("path", ["/dev/sda1", "/dev/../sda", "/dev/mapper/system", "/tmp/disk"])
def test_only_whole_local_disk_paths_are_accepted(path):
    spec, _ = fixture()
    data = spec.model_dump()
    data["nodes"][0]["data"] = [path]
    with pytest.raises(ValidationError):
        StorageCreateSpec(**data)
