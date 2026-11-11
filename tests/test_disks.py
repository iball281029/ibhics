"""Local disks offered for a new LVM pool."""

import json

import pytest

from ibhics.hypervisor.disks import clear_plan, lv_vg, parse_lsblk

G = 1024**3


def dev(name, size, type="disk", fstype=None, mounts=(), children=(), **kw):
    return {
        "name": name,
        "path": f"/dev/{name}",
        "size": size * G,
        "type": type,
        "fstype": fstype,
        "mountpoints": list(mounts) or [None],
        "ro": False,
        "children": list(children) or None,
        **kw,
    }


def test_only_disks_the_node_does_not_use():
    system = dev(
        "nvme0n1",
        480,
        children=[
            dev("nvme0n1p1", 1, "part", "vfat", ["/boot/efi"]),
            dev(
                "nvme0n1p3",
                478,
                "part",
                "LVM2_member",
                children=[dev("ibhics-root", 30, "lvm", "ext4", ["/"])],
            ),
        ],
    )
    raid = dev("sda", 12509, model="RAID 930-8i  ", tran="sas", serial="600d")
    old_esxi = dev(
        "sdb",
        894,
        children=[dev("sdb1", 4, "part", "vfat"), dev("sdb7", 880, "part", "VMFS_volume_member")],
    )
    pool = dev("sdc", 2000, fstype="LVM2_member", children=[dev("ibhics_r-vm", 40, "lvm")])
    usb_stick = dev("sdd", 0.5, tran="usb")
    mpath = [
        dev(
            n,
            1000,
            children=[dev("mpatha", 1000, "mpath", path="/dev/mapper/mpatha")],
            fstype="mpath_member",
        )
        for n in ("sde", "sdf")
    ]
    for m in mpath:
        m["children"][0]["path"] = "/dev/mapper/mpatha"
    text = json.dumps({"blockdevices": [system, raid, old_esxi, pool, usb_stick, *mpath]})

    disks = parse_lsblk(text)
    assert [d.path for d in disks] == ["/dev/sda", "/dev/sdb", "/dev/mapper/mpatha"]
    sda, sdb, lun = disks
    assert (sda.size_gb, sda.model, sda.transport, sda.empty) == (
        12509.0,
        "RAID 930-8i",
        "sas",
        True,
    )
    assert not sdb.empty and sdb.contents == "2 partitions (VMFS_volume_member, vfat)"
    assert lun.empty and lun.size_gb == 1000.0


def ceph_disk(name, vg="ceph--a1b2", lv="osd--block--c3d4", rota=False):
    return dev(
        name,
        894,
        fstype="LVM2_member",
        rota=rota,
        children=[dev(f"{vg}-{lv}", 894, "lvm", path=f"/dev/mapper/{vg}-{lv}")],
    )


def test_old_ceph_osd_disks_are_offered_and_ssd_hdd_told_apart():
    text = json.dumps(
        {
            "blockdevices": [
                ceph_disk("sdb"),
                dev("sdc", 4000, rota=True, tran="sata"),
                dev("nvme1n1", 1600, rota="1", tran="nvme"),  # older lsblk: "1"/"0"
            ]
        }
    )
    sdb, sdc, nvme = parse_lsblk(text)
    assert (sdb.path, sdb.empty, sdb.contents, sdb.media) == (
        "/dev/sdb",
        False,
        "Ceph OSD (LVM)",
        "ssd",
    )
    assert (sdc.empty, sdc.media) == (True, "hdd")
    assert nvme.media == "ssd"


def test_lv_vg_reads_device_mapper_names():
    assert lv_vg("ceph--a1b2-osd--block--c3d4") == "ceph-a1b2"
    assert lv_vg("/dev/mapper/ibhics-root") == "ibhics"
    assert lv_vg("ibhics_raid-vm1") == "ibhics_raid"


def test_clearing_shared_raid_pv_removes_it_only_once():
    raid = dev("md127", 100, "raid1", fstype="LVM2_member")
    blk = json.dumps(
        {"blockdevices": [dev("sda", 100, children=[raid]), dev("sdb", 100, children=[raid])]}
    )
    plan = clear_plan(blk, pvs(("/dev/md127", "old-data")), ["/dev/sda", "/dev/sdb"])
    assert plan.count(["pvremove", "-ff", "-y", "/dev/md127"]) == 1


def pvs(*rows):
    return json.dumps({"report": [{"pv": [{"pv_name": p, "vg_name": v} for p, v in rows]}]})


def test_clearing_removes_old_volume_groups_then_wipes():
    blk = json.dumps({"blockdevices": [ceph_disk("sdb"), dev("sdc", 4000)]})
    plan = clear_plan(blk, pvs(("/dev/sdb", "ceph-a1b2"), ("/dev/nvme0n1p3", "ibhics")),
                      ["/dev/sdb", "/dev/sdc"])  # fmt: skip
    assert plan == [
        ["vgchange", "-an", "ceph-a1b2"],
        ["vgremove", "-ff", "-y", "ceph-a1b2"],
        ["pvremove", "-ff", "-y", "/dev/sdb"],
        ["wipefs", "-a", "/dev/sdb"],
    ]  # sdc is empty: nothing to do


def test_clearing_wipes_partitions_of_an_old_system():
    old = dev(
        "sdb", 894, children=[dev("sdb1", 4, "part", "vfat"), dev("sdb2", 880, "part", "xfs")]
    )
    plan = clear_plan(json.dumps({"blockdevices": [old]}), "{}", ["/dev/sdb"])
    assert plan == [
        ["wipefs", "-a", "/dev/sdb1"],
        ["wipefs", "-a", "/dev/sdb2"],
        ["wipefs", "-a", "/dev/sdb"],
    ]


def test_clearing_refuses_what_it_should_not_touch():
    mounted = dev("sdb", 100, children=[dev("sdb1", 100, "part", "ext4", ["/data"])])
    pool = dev("sdc", 100, fstype="LVM2_member", children=[dev("ibhics_raid-vm1", 40, "lvm")])
    span = ceph_disk("sdd")
    blk = json.dumps({"blockdevices": [mounted, pool, span, dev("sde", 100)]})
    for device, rows, message in [
        ("/dev/sdb", (), "in use"),
        ("/dev/sdc", (("/dev/sdc", "ibhics_raid"),), "in use"),
        ("/dev/sdd", (("/dev/sdd", "ceph-a1b2"), ("/dev/sdx", "ceph-a1b2")), "also uses /dev/sdx"),
        ("/dev/sdq", (), "not a disk"),
    ]:
        with pytest.raises(ValueError, match=message):
            clear_plan(blk, pvs(*rows), [device])
    # An idle PV of an ibHiCS pool (no LVs active) is still refused through pvs.
    idle = json.dumps({"blockdevices": [dev("sdf", 100, fstype="LVM2_member")]})
    with pytest.raises(ValueError, match="belongs to ibHiCS"):
        clear_plan(idle, pvs(("/dev/sdf", "ibhics_old")), ["/dev/sdf"])
