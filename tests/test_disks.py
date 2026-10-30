"""Local disks offered for a new LVM pool."""

import json

from ibhics.hypervisor.disks import parse_lsblk

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
