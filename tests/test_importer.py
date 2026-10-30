"""ibHiCS Migrate: source parsing, virt-v2v invocation, and imports through the API."""

import io
import subprocess
import tarfile
from concurrent.futures import Executor, Future
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_cluster import TOKEN, Agents, Clock, join

from ibhics.cluster import Cluster, NodeRole
from ibhics.hypervisor import Firmware
from ibhics.hypervisor.fake import FakeDriver
from ibhics.importer import (
    ConversionFailed,
    Converted,
    FakeConverter,
    NodeImport,
    Source,
    SourceType,
    SourceUnavailable,
    SourceVM,
    V2VConverter,
    list_vms,
)
from ibhics.importer.sources import parse_ovf, parse_vmware_xml
from ibhics.manager import create_app
from ibhics.manager.imports import ImportManager

ESX_XML = """<domain type='vmware'>
  <name>win2019-db</name>
  <memory unit='KiB'>8388608</memory>
  <vcpu placement='static'>4</vcpu>
  <os firmware='efi'><type arch='x86_64'>hvm</type></os>
  <devices>
    <disk type='file' device='disk'><source file='[ds1] win2019-db/win2019-db.vmdk'/></disk>
    <disk type='file' device='disk'><source file='[ds1] win2019-db/win2019-db_1.vmdk'/></disk>
    <disk type='file' device='cdrom'><source file='[ds1] iso/tools.iso'/></disk>
    <interface type='bridge'><source bridge='VM Network'/></interface>
  </devices>
</domain>"""

OVF = """<?xml version="1.0"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
  xmlns:vmw="http://www.vmware.com/schema/ovf">
  <DiskSection>
    <Disk ovf:diskId="vmdisk1" ovf:capacity="40" ovf:capacityAllocationUnits="byte * 2^30"/>
    <Disk ovf:diskId="vmdisk2" ovf:capacity="10737418240"/>
  </DiskSection>
  <VirtualSystem ovf:id="web-01">
    <Name>web-01</Name>
    <OperatingSystemSection ovf:id="96"><Description>Ubuntu Linux (64-bit)</Description>
    </OperatingSystemSection>
    <VirtualHardwareSection>
      <Item><rasd:ResourceType>3</rasd:ResourceType><rasd:VirtualQuantity>2</rasd:VirtualQuantity></Item>
      <Item><rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits>
        <rasd:ResourceType>4</rasd:ResourceType><rasd:VirtualQuantity>4096</rasd:VirtualQuantity></Item>
      <Item><rasd:ResourceType>10</rasd:ResourceType></Item>
      <Item><rasd:ResourceType>10</rasd:ResourceType></Item>
      <vmw:Config vmw:key="firmware" vmw:value="efi"/>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>"""


def make_ova(path: Path, ovf: str = OVF) -> Path:
    with tarfile.open(path, "w") as tar:
        data = ovf.encode()
        info = tarfile.TarInfo("web-01.ovf")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return path


VMWARE = Source("esx1", SourceType.VMWARE, host="10.0.0.50", username="root", password="pw")


# Sources


def test_source_validation_and_uris():
    VMWARE.validate()
    assert VMWARE.libvirt_uri == "esx://root@10.0.0.50/?no_verify=1"
    vc = Source(
        "vc",
        SourceType.VMWARE,
        host="vcenter.local",
        username="administrator@vsphere.local",
        password="x",
        path="/DC1/host/Cluster1/esx1.local",
        verify_tls=True,
    )
    vc.validate()
    assert vc.libvirt_uri == (
        "vpx://administrator%40vsphere.local@vcenter.local/DC1/host/Cluster1/esx1.local"
    )
    assert vc.masked()["password"] == "********"
    with pytest.raises(ValueError, match="username and password"):
        Source("x", SourceType.VMWARE, host="h").validate()
    with pytest.raises(ValueError, match="absolute folder"):
        Source("x", SourceType.OVA, path="exports").validate()
    with pytest.raises(ValueError, match="vCenter path"):
        Source("x", SourceType.VMWARE, host="h", username="u", password="p", path="a;rm").validate()


def test_parse_vmware_xml():
    vm = parse_vmware_xml(ESX_XML)
    assert (vm.name, vm.vcpus, vm.memory_mb) == ("win2019-db", 4, 8192)
    assert vm.firmware == Firmware.UEFI
    assert vm.disks_gb == [0, 0]  # cdrom ignored; sizes unknown until converted
    assert vm.nics == 1


def test_vmware_guest_os_names():
    from ibhics.importer.sources import guest_os_name

    assert guest_os_name("ubuntu-64") == "Ubuntu Linux (64-bit)"
    assert guest_os_name("rhel9_64Guest") == "Red Hat Enterprise Linux 9 (64-bit)"
    assert guest_os_name("windows9-64") == "Windows 10 (64-bit)"
    assert guest_os_name("someNewOs") == "someNewOs"


def test_parse_ovf_and_ova(tmp_path):
    vm = parse_ovf(OVF)
    assert (vm.name, vm.vcpus, vm.memory_mb, vm.nics) == ("web-01", 2, 4096, 2)
    assert vm.disks_gb == [40, 10]
    assert vm.firmware == Firmware.UEFI
    assert vm.guest_os == "Ubuntu Linux (64-bit)"
    ova = make_ova(tmp_path / "web-01.ova")
    assert list_vms(Source("o", SourceType.OVA, path=str(tmp_path)))[0].ref == str(ova)


def test_list_vmware_vms():
    class Dom:
        def __init__(self, xml, active):
            self.xml, self.active = xml, active

        def XMLDesc(self, _f):
            return self.xml

        def isActive(self):
            return self.active

    class Conn:
        closed = False

        def listAllDomains(self):
            return [Dom(ESX_XML, True), Dom(ESX_XML.replace("win2019-db", "app"), False)]

        def close(self):
            Conn.closed = True

    files = {
        "/folder/win2019-db/win2019-db.vmdk": 'RW 83886080 VMFS "win2019-db-flat.vmdk"',
        "/folder/win2019-db/win2019-db_1.vmdk": 'RW 2097152 VMFS "x-flat.vmdk"\nRW 1 VMFS',
        "/folder/win2019-db/win2019-db.vmx": 'displayName = "x"\nguestOS = "windows2019srv-64"',
    }
    urls = []

    def fetch(_source, url):
        urls.append(url)
        path = url.split("//", 1)[1].split("/", 1)[1].split("?")[0]
        if "/" + path not in files:
            raise OSError("404")
        return files["/" + path]

    vms = list_vms(VMWARE, opener=lambda _s: Conn(), fetch=fetch)
    assert [(v.name, v.state) for v in vms] == [("app", "stopped"), ("win2019-db", "running")]
    assert Conn.closed
    db = vms[1]
    assert db.disks_gb == [40, 2] and db.guest_os == "Windows Server 2019 (64-bit)"
    assert "dcPath=ha-datacenter&dsName=ds1" in urls[0]
    assert vms[0].disks_gb == [0, 0] and vms[0].guest_os == ""  # files missing: blanks


def test_list_disk_images(tmp_path):
    (tmp_path / "hyperv dc01.vhdx").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("skip me")

    def qemu_img(cmd, **_kw):
        assert cmd[:3] == ["qemu-img", "info", "--output=json"]
        return subprocess.CompletedProcess(cmd, 0, stdout='{"virtual-size": 64424509440}')

    [vm] = list_vms(Source("d", SourceType.DISK, path=str(tmp_path)), run=qemu_img)
    assert vm.name == "hyperv-dc01" and vm.disks_gb == [60]
    with pytest.raises(SourceUnavailable, match="mounted"):
        list_vms(Source("d", SourceType.DISK, path=str(tmp_path / "missing")))


# Conversion


class FakeV2V:
    """Stands in for virt-v2v: records the command and writes what -o local would."""

    def __init__(self, fail=None, efi=False):
        self.cmds, self.fail, self.efi = [], fail, efi
        self.password_seen = None

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        assert kw["env"]["LIBGUESTFS_BACKEND"] == "direct"
        if "-ip" in cmd:
            self.password_seen = Path(cmd[cmd.index("-ip") + 1]).read_text()
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"virt-v2v: {self.fail}")
        out, name = Path(cmd[cmd.index("-os") + 1]), cmd[cmd.index("-on") + 1]
        (out / f"{name}-sda").write_bytes(b"boot")
        (out / f"{name}-sdb").write_bytes(b"data")
        loader = "<loader type='pflash'/>" if self.efi else ""
        (out / f"{name}.xml").write_text(f"<domain><os>{loader}</os></domain>")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_v2v_from_vmware(tmp_path):
    run = FakeV2V(efi=True)
    vm = parse_vmware_xml(ESX_XML)
    out = V2VConverter(run, log_dir=tmp_path).convert(VMWARE, vm, "db", tmp_path)
    cmd = run.cmds[0]
    assert cmd[:3] == ["virt-v2v", "-ic", "esx://root@10.0.0.50/?no_verify=1"]
    assert cmd[-9:] == [
        "win2019-db",
        "-o",
        "local",
        "-os",
        str(tmp_path),
        "-of",
        "raw",
        "-on",
        "db",
    ]
    assert run.password_seen == "pw"
    assert not list(tmp_path.glob("v2v-*"))  # the password file is gone
    assert [p.name for p in out.images] == ["db-sda", "db-sdb"]
    assert out.firmware == Firmware.UEFI


def test_v2v_inputs_and_failure(tmp_path):
    run = FakeV2V()
    ova = SourceVM(ref="/mnt/exports/web.ova", name="web")
    V2VConverter(run, log_dir=tmp_path).convert(
        Source("o", SourceType.OVA, path="/mnt/exports"), ova, "web", tmp_path
    )
    assert run.cmds[0][:4] == ["virt-v2v", "-i", "ova", "/mnt/exports/web.ova"]
    disk = SourceVM(ref="/mnt/hv/dc01.vhdx", name="dc01")
    V2VConverter(run, log_dir=tmp_path).convert(
        Source("d", SourceType.DISK, path="/mnt/hv"), disk, "dc01", tmp_path
    )
    assert run.cmds[1][:4] == ["virt-v2v", "-i", "disk", "/mnt/hv/dc01.vhdx"]

    with pytest.raises(ConversionFailed, match="inspection could not detect the source guest"):
        V2VConverter(
            FakeV2V(fail="inspection could not detect the source guest"), log_dir=tmp_path
        ).convert(VMWARE, parse_vmware_xml(ESX_XML), "x", tmp_path / "f")


def test_node_import_creates_vm_after_conversion(tmp_path):
    driver = FakeDriver()
    vm = parse_ovf(OVF)
    vm.ref = "/exports/web-01.ova"
    info = NodeImport(driver, FakeConverter(), str(tmp_path)).run(
        Source("o", SourceType.OVA, path="/exports"), vm, "web-01", vcpus=3, start=True
    )
    assert info.spec.vcpus == 3 and info.spec.memory_mb == 4096
    assert info.spec.firmware == Firmware.UEFI
    assert len(info.spec.disks) == 2 and len(info.spec.nics) == 2
    assert info.state == "running"
    assert driver.read_disk(info.uuid, 0).startswith(b"ova:/exports/web-01.ova:0")
    assert list(tmp_path.iterdir()) == []  # scratch cleaned


def test_node_import_removes_vm_when_copy_fails(tmp_path):
    class Broken(FakeConverter):
        def convert(self, source, vm, name, out_dir):
            done = super().convert(source, vm, name, out_dir)
            done.images[1].write_bytes(b"x" * (10 * 1024 * 1024))  # bigger than the fake disk
            return Converted(done.images, done.firmware)

    driver = FakeDriver()
    vm = SourceVM(ref="/x.ova", name="x", disks_gb=[1, 1])
    with pytest.raises(ValueError):
        NodeImport(driver, Broken(), str(tmp_path)).run(VMWARE, vm, "x")
    assert driver.list_vms() == []


# Through the manager and an agent


class Inline(Executor):
    def submit(self, fn, *args, **kwargs):
        future = Future()
        fn(*args, **kwargs)
        future.set_result(None)
        return future


@pytest.fixture
def env(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    make_ova(exports / "web-01.ova")
    agents = Agents()
    node2 = FakeDriver(memory_mb=262144)
    agents.add("http://node2:8007", "node2", NodeRole.COMPUTE, node2)
    agents.add("http://witness:8007", "witness", NodeRole.WITNESS)
    cluster = Cluster(token=TOKEN, client_factory=agents.client, clock=Clock())
    cluster.add_local("node1", FakeDriver(memory_mb=65536))
    imports = ImportManager(cluster, state_path=tmp_path / "migrate.json", executor=Inline())
    client = TestClient(create_app(cluster, imports=imports))
    join(client, "node2", "compute", "http://node2:8007")
    join(client, "witness", "witness", "http://witness:8007")
    return client, imports, exports, node2, tmp_path


def test_import_through_api(env):
    client, imports, exports, node2, tmp_path = env
    resp = client.post(
        "/api/v1/migrate/sources", json={"name": "nas", "type": "ova", "path": str(exports)}
    )
    assert resp.status_code == 201, resp.text
    [vm] = client.get("/api/v1/migrate/sources/nas/vms").json()
    assert vm["name"] == "web-01" and vm["disks_gb"] == [40, 10]

    resp = client.post(
        "/api/v1/migrate/jobs", json={"source": "nas", "vm": vm["ref"], "node": "node2"}
    )
    assert resp.status_code == 202, resp.text
    [job] = client.get("/api/v1/migrate/jobs").json()
    assert job["state"] == "done", job
    created = client.get(f"/api/v1/vms/{job['result']}").json()
    assert created["node"] == "node2" and created["name"] == "web-01"
    assert node2.read_disk(job["result"], 1).startswith(b"ova:")

    again = client.post("/api/v1/migrate/jobs", json={"source": "nas", "vm": vm["ref"]})
    assert again.status_code == 409 and "choose another name" in again.json()["detail"]
    renamed = client.post(
        "/api/v1/migrate/jobs",
        json={"source": "nas", "vm": vm["ref"], "name": "web-02", "start": True},
    )
    assert renamed.json()["node"] == "node2"  # most free memory
    missing = client.post("/api/v1/migrate/jobs", json={"source": "nas", "vm": "/nope.ova"})
    assert missing.status_code == 404


def test_vmware_source_keeps_password_private(env):
    client, imports, _, _, tmp_path = env
    body = {"name": "esx", "type": "vmware", "host": "10.0.0.50", "username": "root"}
    assert client.post("/api/v1/migrate/sources", json=body).status_code == 422
    body["password"] = "VMware1!"
    assert client.post("/api/v1/migrate/sources", json=body).status_code == 201
    [listed] = client.get("/api/v1/migrate/sources").json()
    assert listed["settings"]["password"] == "********"
    state = tmp_path / "migrate.json"
    assert state.stat().st_mode & 0o077 == 0
    again = ImportManager(imports._cluster, state_path=state)
    again.load()
    assert again.source("esx").password == "VMware1!"
    assert client.delete("/api/v1/migrate/sources/esx").status_code == 204
    assert client.get("/api/v1/migrate/sources/esx/vms").status_code == 404


def test_unreachable_folder_is_reported(env):
    client, *_ = env
    client.post("/api/v1/migrate/sources", json={"name": "gone", "type": "disk", "path": "/nope"})
    resp = client.get("/api/v1/migrate/sources/gone/vms")
    assert resp.status_code == 502 and "mounted" in resp.json()["detail"]


def test_unreachable_vmware_host_fails_fast():
    import socket
    import time

    from ibhics.importer.sources import SourceUnavailable, check_reachable

    with socket.socket() as srv:  # a port that is bound but never accepts: refused at once
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
    start = time.monotonic()
    with pytest.raises(SourceUnavailable, match=f"cannot reach 127.0.0.1 on port {port}"):
        check_reachable("127.0.0.1", port, timeout=2)
    assert time.monotonic() - start < 3


def test_v2v_error_is_the_real_reason():
    from ibhics.importer.convert import v2v_error

    out = """[   0.0] Setting up the source
virt-v2v: error: internal error: invalid argument: libvirt domain 'Zabbix' is
running or paused.  It must be shut down in order to perform virt-v2v
conversion

If reporting bugs, run virt-v2v with debugging enabled and include the
complete output:

  virt-v2v -v -x [...]
"""
    assert v2v_error(out, 1) == (
        "internal error: invalid argument: libvirt domain 'Zabbix' is running or paused."
        "  It must be shut down in order to perform virt-v2v conversion"
    )
    assert v2v_error("something broke\n  virt-v2v -v -x [...]\n", 1) == "something broke"
    assert v2v_error("", 3) == "virt-v2v exited 3"


def test_import_refuses_vm_bigger_than_the_pool():
    from types import SimpleNamespace

    from ibhics.manager.imports import NotEnoughSpace, check_space

    pool = SimpleNamespace(name="default", available_gb=186.4)
    member = SimpleNamespace(name="node1", driver=SimpleNamespace(list_pools=lambda: [pool]))
    vm = SourceVM(ref="Zabbix", name="Zabbix", disks_gb=[300])
    with pytest.raises(
        NotEnoughSpace, match="300 GB of disks, but storage pool 'default' on node1 has only 186 GB"
    ):
        check_space(member, "default", vm)
    check_space(member, "default", SourceVM(ref="a", name="a", disks_gb=[100, 50]))
    check_space(member, "default", SourceVM(ref="b", name="b", disks_gb=[0]))  # unknown size
