"""Disk targets and offline resizing, with no hypervisor or guest tools required."""

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_import_relay import TOKEN, VM, add_shared
from test_import_relay import env as relay_env  # noqa: F401

from ibhics.cluster.remote import AgentClient, RemoteImport
from ibhics.hypervisor import InvalidState
from ibhics.hypervisor.fake import FakeDriver
from ibhics.importer import NodeImport, Source, SourceType, SourceVM
from ibhics.importer.resize import ResizeError, minimum_gb, resize_image
from ibhics.importer.sources import GIB

env = relay_env


class Runner:
    def __init__(self, kind="ext4", used=2 * GIB, fail=None, lvm=False, extra=""):
        self.kind, self.used, self.fail, self.lvm, self.extra = kind, used, fail, lvm, extra
        self.calls = []
        self.environments = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs.get("input")))
        self.environments.append(kwargs.get("env", {}))
        if args[0] == self.fail:
            raise subprocess.CalledProcessError(1, args, stderr="secret password")
        output = ""
        device = "/dev/vg/root" if self.lvm else "/dev/sda1"
        if args[0] == "guestfish" and "--ro" in args:
            output = "vg_extent_size: 4194304\n"
        elif args[0] == "virt-filesystems":
            output = (
                "Name,Type,VFS,Label,Size,Parent\n"
                f"{device},filesystem,{self.kind},,{10 * GIB},-\n"
                f"/dev/sda1,partition,-,,{10 * GIB},/dev/sda\n" + self.extra
            )
            if self.lvm:
                output += f"/dev/sda1,pv,-,,{10 * GIB},/dev/vg\n{device},lv,-,,{10 * GIB},/dev/vg\n"
        elif args[0] == "virt-df":
            output = (
                "VirtualMachine,Filesystem,1K-blocks,Used,Available,Use%\n"
                f"vm,{device},10485760,{self.used // 1024},1,20%\n"
            )
        elif args[:2] == ["qemu-img", "create"]:
            with open(args[-2], "wb") as stream:
                stream.truncate(int(args[-1]))
        elif args[:2] == ["qemu-img", "resize"]:
            with open(args[-2], "r+b") as stream:
                stream.truncate(int(args[-1]))
        elif args[:2] == ["qemu-img", "convert"]:
            Path(args[-1]).write_bytes(b"working")
        return SimpleNamespace(stdout=output)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "disk.raw"
    with path.open("wb") as stream:
        stream.write(b"original")
        stream.truncate(10 * GIB)
    return path


@pytest.mark.parametrize("kind", ["ntfs", "ext2", "ext3", "ext4"])
def test_shrink_supported_filesystem(image, kind):
    runner = Runner(kind)
    resize_image(image, 6, runner)
    for environment in runner.environments:
        for variable in ("LIBGUESTFS_CACHEDIR", "LIBGUESTFS_TMPDIR", "TMPDIR"):
            assert Path(environment[variable]) == image.parent.resolve() / ".libguestfs-cache"
    assert image.stat().st_size == 6 * GIB
    script = next(
        script for args, script in runner.calls if args[0] == "guestfish" and "--rw" in args
    )
    assert ("ntfsresize-size" if kind == "ntfs" else "resize2fs-size") in script
    command = next(args for args, _ in runner.calls if args[0] == "virt-resize")
    assert command[command.index("--shrink") + 1] == "/dev/sda1"


def test_lvm_shrink_prepares_lv_and_pv(image):
    runner = Runner(lvm=True)
    resize_image(image, 6, runner)
    script = next(
        script for args, script in runner.calls if args[0] == "guestfish" and "--rw" in args
    )
    assert script.index("resize2fs-size") < script.index("lvresize") < script.index("pvresize-size")


@pytest.mark.parametrize("kind", ["xfs", "btrfs", "refs", "BitLocker", "unknown"])
def test_unsupported_shrink_preserves_original(image, kind):
    runner = Runner(kind)
    with pytest.raises(ResizeError, match="unsupported"):
        resize_image(image, 6, runner)
    assert image.stat().st_size == 10 * GIB
    with image.open("rb") as stream:
        assert stream.read(8) == b"original"
    assert not any(args[0] == "guestfish" for args, _ in runner.calls)


def test_minimum_and_low_target(image):
    assert minimum_gb(2 * GIB) == 3
    assert minimum_gb(11 * GIB) == 13
    with pytest.raises(ResizeError, match="below minimum 9 GiB"):
        resize_image(image, 6, Runner(used=8 * GIB))
    assert image.stat().st_size == 10 * GIB


def test_failed_shrink_preserves_original_and_hides_output(image):
    with pytest.raises(ResizeError, match="original retained") as error:
        resize_image(image, 6, Runner(fail="virt-resize"))
    assert "secret" not in str(error.value)
    assert image.stat().st_size == 10 * GIB


def test_grow_uses_qemu_img(image):
    runner = Runner()
    resize_image(image, 12, runner)
    assert image.stat().st_size == 12 * GIB
    assert runner.calls[0][0][:4] == ["qemu-img", "resize", "-f", "raw"]
    assert len(runner.calls) == 1


@pytest.mark.parametrize("node", ["node1", "node2"])
def test_api_disk_targets_local_and_remote(env, monkeypatch, node):
    client, imports, _, _ = env
    imports._discover = lambda source: [replace(VM, disks_gb=[10, 20])]
    add_shared(client)
    seen = []

    def resize(path, size):
        seen.append(size)

    monkeypatch.setattr("ibhics.importer.node.resize_image", resize)
    targets = [{"pool": "default", "size_gb": 12}, {"pool": "shared", "size_gb": 6}]
    response = client.post(
        "/api/v1/migrate/jobs",
        json={
            "source": "pve",
            "vm": "101",
            "node": node,
            "disks": targets,
        },
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["state"] == "done", job
    assert job["disks"] == targets
    assert seen == [12, 6]
    vm = client.get(f"/api/v1/vms/{job['result']}").json()
    assert vm["disks"][0]["pool"] == "default"
    assert vm["disks"][1]["pool"] == "shared"
    assert vm["disks"][1]["size_gb"] == 6
    assert vm["disks"][0]["size_gb"] == 12


def test_api_targets_relay_requires_all_pools_shared(env):
    client, _, _, node2 = env
    add_shared(client)
    node2.unreachable = {"10.9.9.9"}
    response = client.post(
        "/api/v1/migrate/jobs",
        json={
            "source": "pve",
            "vm": "101",
            "node": "node2",
            "pool": "shared",
            "disks": [{"pool": "default"}],
        },
    )
    assert response.status_code == 409
    response = client.post(
        "/api/v1/migrate/jobs",
        json={
            "source": "pve",
            "vm": "101",
            "node": "node2",
            "pool": "shared",
            "disks": [{"pool": "shared"}],
        },
    )
    assert response.status_code == 202
    assert response.json()["state"] == "done", response.text
    assert response.json()["via"] == "node1"


@pytest.mark.parametrize(
    "disks,code", [([], 422), ([{"size_gb": 0}], 422), ([{"pool": "missing"}], 404)]
)
def test_invalid_disk_targets(env, disks, code):
    client, *_ = env
    response = client.post(
        "/api/v1/migrate/jobs",
        json={
            "source": "pve",
            "vm": "101",
            "disks": disks,
        },
    )
    assert response.status_code == code, response.text


def test_old_agent_rejects_disk_overrides_before_conversion():
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(404)

    http = httpx.Client(base_url="http://old:8007", transport=httpx.MockTransport(respond))
    remote = RemoteImport(AgentClient("http://old:8007", TOKEN, http=http))
    with pytest.raises(InvalidState, match="does not support disk targets; upgrade"):
        remote.run(Source("s", SourceType.KVM, host="host"), VM, "vm", disks=[{"size_gb": 12}])
    assert calls == ["/agent/v1/imports/capabilities"]


def test_shrink_fails_before_vm_creation_and_cleans_scratch(tmp_path, monkeypatch):
    driver = FakeDriver()

    def fail(*args):
        raise ResizeError("XFS unsupported")

    monkeypatch.setattr("ibhics.importer.node.resize_image", fail)
    importer = NodeImport(driver, scratch_dir=str(tmp_path))
    with pytest.raises(ResizeError, match="XFS unsupported"):
        importer.run(
            Source("s", SourceType.OVA, path="/source"),
            SourceVM(ref="vm", name="vm", disks_gb=[10]),
            "vm",
            disks=[{"size_gb": 5}],
        )
    assert not driver.list_vms()
    assert not list(tmp_path.glob("import-vm-*"))


def test_old_agent_accepts_default_disk_targets_without_capability_query():
    import json

    from ibhics.hypervisor import VMSpec
    from ibhics.manager.schemas import VMOut

    driver = FakeDriver(demo_console=False)
    info = driver.create_vm(VMSpec(name="vm"))
    calls = []

    def respond(request):
        calls.append(request.url.path)
        assert "disks" not in json.loads(request.content)
        return httpx.Response(200, json=VMOut.from_info(info).model_dump(mode="json"))

    http = httpx.Client(base_url="http://old:8007", transport=httpx.MockTransport(respond))
    remote = RemoteImport(AgentClient("http://old:8007", TOKEN, http=http))
    result = remote.run(
        Source("s", SourceType.KVM, host="host"),
        VM,
        "vm",
        disks=[{"pool": "default", "size_gb": 10}],
    )
    assert result.uuid == info.uuid
    assert calls == ["/agent/v1/imports/run"]


def test_small_raw_partition_is_kept(image):
    # A Windows MSR has no filesystem and is copied unchanged.
    resize_image(
        image, 6, Runner("ntfs", extra=f"/dev/sda2,partition,-,,{16 * 1024**2},/dev/sda\n")
    )
    assert image.stat().st_size == 6 * GIB


def test_large_unknown_partition_is_refused(image):
    runner = Runner("ntfs", extra=f"/dev/sda2,partition,-,,{4 * GIB},/dev/sda\n")
    with pytest.raises(ResizeError, match="encrypted"):
        resize_image(image, 6, runner)
    assert image.stat().st_size == 10 * GIB
