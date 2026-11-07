"""0.8.0b3: Thai VM names, and ISO files named .ISO."""

from fastapi.testclient import TestClient

from ibhics.hypervisor import DiskSpec
from ibhics.hypervisor.customize import Customization, hostname_for
from ibhics.hypervisor.domain_xml import file_name, volume_name
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app


def test_thai_vm_name_gets_ascii_files():
    assert file_name("web-01") == "web-01"
    thai = file_name("เครื่องบัญชี-01")
    assert thai.startswith("vm-") and thai.isascii() and thai == file_name("เครื่องบัญชี-01")
    assert volume_name("เครื่องบัญชี-01", 0, DiskSpec(size_gb=1)) == f"{thai}-disk0.qcow2"
    assert hostname_for(Customization(os="linux"), "เครื่อง-web01").isascii()


def test_thai_vm_via_api_and_iso_in_capitals():
    client = TestClient(create_app(FakeDriver(demo_console=False)))
    vm = client.post("/api/v1/vms", json={"name": "เครื่องบัญชี-01", "disks": [{"size_gb": 1}]})
    assert vm.status_code == 201, vm.text
    assert vm.json()["name"] == "เครื่องบัญชี-01"
    assert client.post("/api/v1/vms", json={"name": "มี ช่องว่าง"}).status_code == 422
    uuid = vm.json()["uuid"]
    cd = client.post(f"/api/v1/vms/{uuid}/cdrom", json={"iso": "/var/lib/ibhics/iso/WIN2022.ISO"})
    assert cd.status_code != 422, cd.text


def test_legacy_windows_gets_sata_disks_and_e1000():
    import xml.etree.ElementTree as ET

    from ibhics.hypervisor import NicSpec, VMSpec
    from ibhics.hypervisor.domain_xml import build_domain_xml

    spec = VMSpec(
        name="w2k8",
        disks=[DiskSpec(size_gb=1, bus="sata"), DiskSpec(size_gb=1, bus="sata")],
        nics=[NicSpec(model="e1000")],
    )
    root = ET.fromstring(build_domain_xml(spec))
    devs = [t.get("dev") for t in root.findall("devices/disk[@device='disk']/target")]
    assert devs == ["sdc", "sdd"]  # sda and sdb are the CD drives
    assert root.find("devices/interface/model").get("type") == "e1000"
    client = TestClient(create_app(FakeDriver(demo_console=False)))
    body = {"name": "w7", "disks": [{"size_gb": 1, "bus": "sata"}], "nics": [{"model": "e1000"}]}
    assert client.post("/api/v1/vms", json=body).status_code == 201
    assert (
        client.post(
            "/api/v1/vms", json={**body, "name": "w12", "nics": [{"model": "e1000e"}]}
        ).status_code
        == 201
    )
    assert client.post("/api/v1/vms", json={**body, "nics": [{"model": "rtl"}]}).status_code == 422


def test_windows_up_to_2012_r2_install_on_sata_and_intel_nic():
    """Setup of Server 2012 R2 refused the bundled virtio drivers (unsigned for it)."""
    import json
    import re
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = Path("ibhics/manager/static/app.js").read_text()
    code = "\n".join(
        re.search(rf"^function {fn}\(.*?^}}\n", js, re.S | re.M).group(0)
        for fn in ("osProfile", "virtioFolder")
    )
    cases = [
        "windows server 2012 r2",
        "windows server 2012",
        "windows 8.1",
        "windows server 2008 r2",
        "windows 7",
        "windows server 2016",
        "windows server 2022",
        "windows 10",
    ]
    out = subprocess.run(
        ["node", "-e", code + f"console.log(JSON.stringify({json.dumps(cases)}.map("
         "(v) => osProfile('windows', v))))"],  # fmt: skip
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    got = dict(zip(cases, json.loads(out), strict=True))
    for v in cases[:5]:
        assert got[v]["legacy"], v
    assert got["windows server 2012 r2"]["nic"] == "e1000e"
    for v in cases[5:]:
        assert not got[v]["legacy"] and got[v]["nic"] == "virtio", v
