"""Sangfor HCI source against a stand-in of its console API."""

import shutil
import subprocess
from urllib.parse import parse_qs

import httpx
import pytest

from ibhics.hypervisor.base import Firmware
from ibhics.importer import Source, SourceType
from ibhics.importer.sangfor import (
    Sangfor,
    SangforError,
    list_sangfor_vms,
    parse_vm,
    pkcs1_encrypt_hex,
)

SOURCE = Source("sf", SourceType.SANGFOR, host="10.0.0.98", username="admin", password="pw")


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_password_encryption_matches_pkcs1_v15(tmp_path):
    key = tmp_path / "k.pem"
    subprocess.run(["openssl", "genrsa", "-out", key, "1024"], check=True, capture_output=True)
    mod = (
        subprocess.run(
            ["openssl", "rsa", "-in", key, "-noout", "-modulus"],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.split("=")[1]
        .strip()
    )
    cipher = bytes.fromhex(pkcs1_encrypt_hex("pw" + "s3cr3t", mod))
    (tmp_path / "c.bin").write_bytes(cipher)
    plain = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-decrypt",
            "-inkey",
            key,
            "-in",
            tmp_path / "c.bin",
            "-pkeyopt",
            "rsa_padding_mode:pkcs1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert plain == b"pws3cr3t"


class FakeSangfor:
    MOD = "C7" + "A1" * 127  # odd-looking but valid hex modulus for the flow test

    def __init__(self, good_password=True, export_fails=False):
        self.calls, self.good, self.export_fails, self.polls = [], good_password, export_fails, 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        if path == "/login":
            return httpx.Response(
                200, text=f'<script>VMP.data={{RSAModulus : "{self.MOD}"}}</script>'
            )
        if path == "/vapi/json/access/srand":
            return httpx.Response(200, json={"data": {"hwinfo": ""}})
        if path == "/vapi/json/access/secret":
            return httpx.Response(200, json={"data": {"secret": "abc"}})
        if path == "/vapi/extjs/access/ticket":
            form = parse_qs(req.content.decode())
            assert form["username"] == ["admin"] and len(form["password"][0]) > 100
            if not self.good:
                return httpx.Response(200, json={"success": 0, "message": "wrong password"})
            return httpx.Response(200, json={"success": 1, "data": {"CSRFPreventionToken": "T"}})
        assert req.headers.get("CSRFPreventionToken") == "T"
        if path == "/vapi/extjs/cluster/vms":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "Group",
                            "children": [
                                {"vmid": 101, "name": "web01", "status": "running"},
                                {"vmid": 102, "name": "db01", "status": "stopped"},
                            ],
                        }
                    ]
                },
            )
        if path.endswith("/config"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "cores": 2,
                        "sockets": 2,
                        "memory": 4096,
                        "bios": "ovmf",
                        "ide0": "local:101/vm-disk-1.qcow2,size=40G",
                        "ide2": "none,media=cdrom",
                        "net0": "virtio=fe:fc:fe:00:00:01,bridge=vs1",
                    }
                },
            )
        if path.endswith("/vm_export"):
            return httpx.Response(200, json={"success": 1})
        if path.endswith("/vm_export_process"):
            self.polls += 1
            if self.export_fails:
                return httpx.Response(200, json={"data": {"status": "failed", "error": "busy"}})
            return httpx.Response(200, json={"data": {"percent": min(100, 50 * self.polls)}})
        if path == "/download":
            return httpx.Response(200, content=b"OVA" * 5000)
        if path.endswith("/vm_file_remove"):
            return httpx.Response(200, json={"success": 1})
        return httpx.Response(404)


def client(fake):
    http = httpx.Client(base_url="https://10.0.0.98", transport=httpx.MockTransport(fake.handler))
    return Sangfor(SOURCE, http=http, sleep=lambda _s: None)


def test_lists_vms_from_the_group_tree_with_their_config():
    vms = list_sangfor_vms(SOURCE, client(FakeSangfor()))
    assert [v.name for v in vms] == ["db01", "web01"]
    web = next(v for v in vms if v.name == "web01")
    assert web.ref == "101" and web.vcpus == 4 and web.memory_mb == 4096
    assert web.firmware == Firmware.UEFI and web.disks_gb == [40] and web.nics == 1


def test_wrong_password_is_reported():
    with pytest.raises(SangforError, match="wrong password"):
        client(FakeSangfor(good_password=False)).login()


def test_export_downloads_ova_and_removes_it_on_sangfor(tmp_path):
    fake = FakeSangfor()
    c = client(fake)
    c.login()
    vm = parse_vm({"vmid": 101, "name": "web01"})
    out = c.export_ova(vm, tmp_path / "web01.ova", progress=lambda *a: None)
    assert out.read_bytes() == b"OVA" * 5000
    assert ("POST", "/vapi/extjs/cluster/vm/vm_file_remove") in fake.calls


def test_failed_export_still_cleans_up(tmp_path):
    fake = FakeSangfor(export_fails=True)
    c = client(fake)
    c.login()
    with pytest.raises(SangforError, match="busy"):
        c.export_ova(parse_vm({"vmid": 101, "name": "web01"}), tmp_path / "x.ova")
    assert ("POST", "/vapi/extjs/cluster/vm/vm_file_remove") in fake.calls


def test_api_accepts_a_sangfor_source():
    from fastapi.testclient import TestClient

    from ibhics.hypervisor.fake import FakeDriver
    from ibhics.manager import create_app

    c = TestClient(create_app(FakeDriver(demo_console=False)))
    body = {
        "name": "sf",
        "type": "sangfor",
        "host": "10.0.0.98",
        "username": "admin",
        "password": "secret-pw",
    }
    resp = c.post("/api/v1/migrate/sources", json=body)
    assert resp.status_code == 201 and "secret-pw" not in resp.text
