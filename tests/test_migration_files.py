"""LibvirtDriver: UEFI variables and seed CDs that must follow a migrating VM."""

import types

import pytest

from ibhics.hypervisor import InvalidState
from ibhics.hypervisor import libvirt_driver as lv


def xml(nvram="", cdrom=""):
    os_el = f"<os><nvram format='qcow2'>{nvram}</nvram></os>" if nvram else "<os/>"
    cd = f"<disk type='file' device='cdrom'><source file='{cdrom}'/></disk>" if cdrom else ""
    return f"<domain><name>vm</name>{os_el}<devices>{cd}</devices></domain>"


class Domain:
    def __init__(self, desc):
        self.desc = desc

    def XMLDesc(self, _flags):
        return self.desc


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lv, "libvirt", types.SimpleNamespace(libvirtError=Exception, VIR_DOMAIN_XML_INACTIVE=2)
    )
    nvram, seeds = tmp_path / "nvram", tmp_path / "seeds"
    nvram.mkdir()

    def make(*domains):
        d = object.__new__(lv.LibvirtDriver)
        d.nvram_dir, d.seed_dir = str(nvram), str(seeds)
        doms = [Domain(x) for x in domains]
        d._conn = types.SimpleNamespace(
            listAllDomains=lambda: doms, lookupByUUIDString=lambda _u: doms[0]
        )
        return d

    return make, nvram, seeds


def test_files_to_send_are_nvram_and_seed_only(make, tmp_path):
    make, nvram, seeds = make
    seeds.mkdir()
    (nvram / "vm_VARS.qcow2").write_bytes(b"vars")
    (seeds / "vm-seed.iso").write_bytes(b"seed")
    other = tmp_path / "pool-setup.iso"
    other.write_bytes(b"iso")
    d = make(xml(str(nvram / "vm_VARS.qcow2"), str(seeds / "vm-seed.iso")))
    assert d.migration_files("u") == {
        str(nvram / "vm_VARS.qcow2"): b"vars",
        str(seeds / "vm-seed.iso"): b"seed",
    }
    # an ISO in a storage pool is not staged; never-started UEFI has no file yet
    assert make(xml(str(nvram / "missing.qcow2"), str(other))).migration_files("u") == {}


def test_stage_writes_files_and_creates_the_seed_folder(make):
    make, nvram, seeds = make
    d = make()
    files = {str(nvram / "vm_VARS.qcow2"): b"vars", str(seeds / "vm-seed.iso"): b"seed"}
    assert sorted(d.stage_migration_files(files)) == sorted(files)
    assert (nvram / "vm_VARS.qcow2").read_bytes() == b"vars"
    assert (nvram / "vm_VARS.qcow2").stat().st_mode & 0o777 == 0o600
    assert (seeds / "vm-seed.iso").read_bytes() == b"seed"
    assert not [p for p in nvram.iterdir() if p.name.startswith(".stage-")]


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "{n}/../x", "{n}/sub/x", "{n}/.hidden", "relative"]
)
def test_stage_refuses_paths_outside_nvram_and_seed(make, path):
    make, nvram, _ = make
    with pytest.raises(ValueError):
        make().stage_migration_files({path.format(n=nvram): b"x"})


def test_stage_refuses_a_file_another_vm_uses(make):
    make, nvram, _ = make
    path = str(nvram / "vm_VARS.qcow2")
    (nvram / "vm_VARS.qcow2").write_bytes(b"theirs")
    with pytest.raises(InvalidState):
        make(xml(path)).stage_migration_files({path: b"mine"})
    assert (nvram / "vm_VARS.qcow2").read_bytes() == b"theirs"


def test_remove_keeps_files_in_use(make):
    make, nvram, _ = make
    used, stale = nvram / "used_VARS.qcow2", nvram / "stale_VARS.qcow2"
    used.write_bytes(b"u")
    stale.write_bytes(b"s")
    d = make(xml(str(used)))
    assert d.remove_migration_files([str(used), str(stale), "/etc/passwd"]) == [str(stale)]
    assert used.exists() and not stale.exists()


def test_define_vm_drops_a_cd_file_this_node_lacks(make, tmp_path, monkeypatch):
    make, nvram, seeds = make
    d = make()
    defined = []

    class Err(Exception):
        pass

    def lookup(_u):
        raise Err()

    monkeypatch.setattr(lv.libvirt, "libvirtError", Err, raising=False)
    d._conn.lookupByUUIDString = lookup
    d._conn.defineXML = lambda x: defined.append(x) or "dom"
    d._info = lambda dom: dom
    present = tmp_path / "present.iso"
    present.write_bytes(b"x")
    desc = (
        "<domain><name>vm</name><uuid>u1</uuid><devices>"
        f"<disk type='file' device='cdrom'><source file='{seeds}/gone.iso'/></disk>"
        f"<disk type='file' device='cdrom'><source file='{present}'/></disk>"
        "</devices></domain>"
    )
    assert d.define_vm(desc) == "dom"
    assert "gone.iso" not in defined[0] and str(present) in defined[0]
