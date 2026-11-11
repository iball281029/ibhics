"""Nutanix AHV source over a stand-in Prism v3 API."""

import json

import httpx
import pytest

from ibhics.hypervisor.base import Firmware
from ibhics.importer import Source, SourceType
from ibhics.importer.nutanix import NutanixError, Prism, list_nutanix_vms, pull_disks

VM = {
    "metadata": {"uuid": "vm-1"},
    "spec": {
        "name": "app01",
        "description": "web",
        "resources": {
            "num_sockets": 2,
            "num_vcpus_per_socket": 2,
            "memory_size_mib": 8192,
            "power_state": "ON",
            "boot_config": {"boot_type": "UEFI"},
            "disk_list": [
                {
                    "uuid": "d-1",
                    "disk_size_bytes": 40 << 30,
                    "device_properties": {"device_type": "DISK"},
                },
                {"uuid": "cd", "device_properties": {"device_type": "CDROM"}},
            ],
            "nic_list": [
                {"mac_address": "50:6b:8d:00:00:01", "subnet_reference": {"name": "VM-Net"}}
            ],
        },
    },
}


class FakePrism:
    def __init__(self, fail_task=False, password="pw"):
        self.images, self.deleted, self.fail_task, self.password = {}, [], fail_task, password

    def handler(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth != httpx.BasicAuth("admin", self.password)._auth_header:
            return httpx.Response(401)
        path = request.url.path
        if path.endswith("/vms/list"):
            return httpx.Response(200, json={"entities": [VM], "metadata": {"total_matches": 1}})
        if path.endswith("/images") and request.method == "POST":
            body = json.loads(request.content)
            assert body["spec"]["resources"]["data_source_reference"] == {
                "kind": "vm_disk",
                "uuid": "d-1",
            }
            self.images["img-1"] = b"QFI\xfb" + b"\0" * 1020
            return httpx.Response(
                202,
                json={
                    "metadata": {"uuid": "img-1"},
                    "status": {"execution_context": {"task_uuid": "t-1"}},
                },
            )
        if "/tasks/" in path:
            return httpx.Response(
                200,
                json={
                    "status": "FAILED" if self.fail_task else "SUCCEEDED",
                    "error_detail": "no space",
                },
            )
        if path.endswith("/images/img-1/file"):
            return httpx.Response(200, content=self.images["img-1"])
        if path.endswith("/images/img-1") and request.method == "DELETE":
            self.deleted.append("img-1")
            return httpx.Response(202, json={})
        return httpx.Response(404)


def client(fake, password="pw"):
    return httpx.Client(
        base_url="https://prism:9440",
        auth=("admin", password),
        transport=httpx.MockTransport(fake.handler),
    )


SOURCE = Source("ntnx", SourceType.NUTANIX, host="10.0.0.53", username="admin", password="pw")


def test_list_maps_vm_fields():
    [vm] = list_nutanix_vms(SOURCE, Prism(SOURCE, http=client(FakePrism())))
    assert (vm.ref, vm.name, vm.vcpus, vm.memory_mb) == ("vm-1", "app01", 4, 8192)
    assert vm.firmware == Firmware.UEFI and vm.disks_gb == [40] and vm.disk_paths == ["d-1"]
    assert vm.networks == ["VM-Net"] and vm.state == "running"


def test_pull_copies_through_a_temporary_image_and_deletes_it(tmp_path):
    fake = FakePrism()
    prism = Prism(SOURCE, http=client(fake), sleep=lambda _s: None)
    [vm] = list_nutanix_vms(SOURCE, prism)
    steps = []
    disks = pull_disks(SOURCE, vm, tmp_path, lambda s, p: steps.append(s), prism=prism)
    assert disks == [(str(tmp_path / "nutanix-disk0"), "qcow2")]
    assert fake.deleted == ["img-1"] and steps


def test_failed_export_still_deletes_the_image(tmp_path):
    fake = FakePrism(fail_task=True)
    prism = Prism(SOURCE, http=client(fake), sleep=lambda _s: None)
    [vm] = list_nutanix_vms(SOURCE, prism)
    with pytest.raises(NutanixError, match="no space"):
        pull_disks(SOURCE, vm, tmp_path, lambda s, p: None, prism=prism)
    assert fake.deleted == ["img-1"]


def test_wrong_password_is_reported_plainly():
    prism = Prism(SOURCE, http=client(FakePrism(), password="bad"))
    with pytest.raises(NutanixError, match="username or password"):
        prism.vms()


def test_source_validation():
    with pytest.raises(ValueError):
        Source("n", SourceType.NUTANIX, host="10.0.0.53").validate()
    SOURCE.validate()


def test_api_adds_a_nutanix_source_with_masked_password():
    from fastapi.testclient import TestClient

    from ibhics.hypervisor.fake import FakeDriver
    from ibhics.manager import create_app

    c = TestClient(create_app(FakeDriver(demo_console=False)))
    body = {
        "name": "ntnx",
        "type": "nutanix",
        "host": "10.0.0.53",
        "username": "admin",
        "password": "secret-pw",
    }
    resp = c.post("/api/v1/migrate/sources", json=body)
    assert resp.status_code == 201, resp.text
    assert "secret-pw" not in resp.text and resp.json()["type"] == "nutanix"
