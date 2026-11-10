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
    assert review["maximum_gib"] == 87
    assert review["copies"] == 2
    assert review["nodes"][0]["reclaim_pools"] == ["hybrid01"]


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
