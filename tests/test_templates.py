"""VM templates: convert, deploy independent disk copies, guest customization."""

import json
import types
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from test_migration import Env, Inline

from ibhics.hypervisor import CloneSpec, Customization, DiskSpec, InvalidState, VMExists, VMSpec
from ibhics.hypervisor import libvirt_driver as lv
from ibhics.hypervisor.customize import build_iso, cloud_init_files, hostname_for, seed_files
from ibhics.hypervisor.domain_xml import build_domain_xml, build_volume_xml, is_template
from ibhics.hypervisor.fake import FakeDriver
from ibhics.hypervisor.serialize import clone_from_json, clone_to_json, spec_from_json, spec_to_json
from ibhics.manager import create_app
from ibhics.manager.deploys import DeployManager

# ---------- guest customization ----------


def test_cloud_init_seed_creates_user_and_hostname():
    c = Customization(username="ops", password="s3cret", ssh_keys=["ssh-ed25519 AAAA ops"])
    files = cloud_init_files(c, "web_01", "iid-1")
    assert files["meta-data"] == "instance-id: iid-1\nlocal-hostname: web-01\n"
    assert files["user-data"].startswith("#cloud-config\n")
    config = json.loads(files["user-data"].split("\n", 1)[1])  # JSON is YAML
    assert config["hostname"] == "web-01"
    user = config["users"][1]
    assert user["name"] == "ops" and user["ssh_authorized_keys"] == ["ssh-ed25519 AAAA ops"]
    assert config["chpasswd"]["users"] == [{"name": "ops", "password": "s3cret", "type": "text"}]
    assert config["ssh_pwauth"] is True


def test_cloud_init_own_user_data_is_used_as_is():
    files = cloud_init_files(Customization(user_data="#cloud-config\nruncmd: [ls]\n"), "a", "i")
    assert files["user-data"] == "#cloud-config\nruncmd: [ls]\n"


def test_windows_unattend_sets_name_and_passwords():
    c = Customization(os="windows", password="P<ss", username="ops", timezone="UTC")
    files = seed_files(c, "app-server-0001-long", "i")
    assert set(files) == {"Autounattend.xml", "Unattend.xml"}
    root = ET.fromstring(files["Unattend.xml"].encode())
    ns = {"u": "urn:schemas-microsoft-com:unattend"}
    assert root.find(".//u:ComputerName", ns).text == "app-server-0001"  # 15 characters
    values = [v.text for v in root.findall(".//u:Value", ns)]
    assert values == ["P<ss", "P<ss"]  # Administrator, and the local admin account
    assert root.find(".//u:LocalAccount/u:Name", ns).text == "ops"


def test_hostname_defaults_to_vm_name():
    assert hostname_for(Customization(), "db.prod") == "db-prod"
    assert hostname_for(Customization(hostname="x1"), "db") == "x1"


@pytest.mark.parametrize(
    "bad",
    [
        Customization(os="mac"),
        Customization(hostname="bad_name"),
        Customization(os="windows", hostname="a" * 16),
        Customization(username="Root!"),
        Customization(ssh_keys=["a\nb"]),
    ],
)
def test_customization_is_validated(bad):
    with pytest.raises(ValueError):
        bad.validate()


def test_build_iso_uses_xorriso(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda t: "/usr/bin/xorriso" if t == "xorriso" else None)
    calls = []

    def run(cmd):
        calls.append(cmd)
        out = cmd[cmd.index("-o") + 1]
        names = sorted(p.name for p in __import__("pathlib").Path(cmd[-1]).iterdir())
        assert names == ["meta-data", "user-data"]
        open(out, "w").close()

    out = build_iso({"meta-data": "", "user-data": ""}, tmp_path / "seeds" / "a.iso", run=run)
    assert calls[0][:3] == ["/usr/bin/xorriso", "-as", "mkisofs"]
    assert calls[0][calls[0].index("-V") + 1] == "cidata"
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_build_iso_without_tool_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _t: None)
    with pytest.raises(InvalidState, match="xorriso"):
        build_iso({"a": ""}, tmp_path / "a.iso")


# ---------- XML and JSON ----------


def test_domain_xml_carries_template():
    spec = VMSpec(name="t", template=True, disks=[DiskSpec(size_gb=1)])
    assert is_template(ET.fromstring(build_domain_xml(spec)))
    clone = VMSpec(name="c", disks=[DiskSpec(size_gb=1)])
    assert not is_template(ET.fromstring(build_domain_xml(clone)))
    assert lv._spec_from_xml(build_domain_xml(spec)).template


@pytest.mark.parametrize("backend", ["file", "block"])
def test_volume_has_no_backing_store(backend):
    vol = ET.fromstring(build_volume_xml("c", 0, DiskSpec(size_gb=20, backend=backend)))
    assert vol.find("backingStore") is None


def test_json_round_trips():
    spec = VMSpec(name="a", template=True)
    back = spec_from_json(spec_to_json(spec))
    assert back.template
    assert "linked_to" not in spec_to_json(back)
    clone = CloneSpec(name="b", customization=Customization(hostname="h"))
    assert clone_from_json(clone_to_json(clone)) == clone


# ---------- fake driver ----------


@pytest.fixture
def fake():
    d = FakeDriver(demo_console=False)
    vm = d.create_vm(
        VMSpec(name="ubuntu", vcpus=2, memory_mb=2048, disks=[DiskSpec(size_gb=1)], iso="/i.iso")
    )
    d.write_disk(vm.uuid, 0, 0, b"golden")
    return d, vm.uuid


def test_template_cannot_power_on(fake):
    d, uuid = fake
    assert d.set_template(uuid, True).spec.template
    with pytest.raises(InvalidState, match="template"):
        d.start_vm(uuid)
    d.set_template(uuid, False)
    d.start_vm(uuid)
    with pytest.raises(InvalidState, match="shut"):
        d.set_template(uuid, True)


def test_full_clone_copies_disks_and_hardware(fake):
    d, uuid = fake
    vm = d.clone_vm(uuid, CloneSpec(name="ubuntu-2"))
    assert (vm.spec.vcpus, vm.spec.memory_mb, vm.spec.boot) == (2, 2048, ["hd"])
    assert vm.spec.iso is None
    assert "linked_to" not in spec_to_json(vm.spec)
    assert d.read_disk(vm.uuid, 0).startswith(b"golden")
    assert vm.spec.nics == [] or vm.spec.nics[0].mac != d.get_vm(uuid).spec.nics[0].mac
    with pytest.raises(VMExists):
        d.clone_vm(uuid, CloneSpec(name="ubuntu-2"))


def test_template_clone_is_independent_and_template_can_be_removed(fake):
    d, uuid = fake
    d.set_template(uuid, True)
    clone = d.clone_vm(uuid, CloneSpec(name="copy1"))
    d.write_disk(clone.uuid, 0, 0, b"edited")
    assert d.read_disk(uuid, 0).startswith(b"golden")
    d.set_template(uuid, False)
    d.set_template(uuid, True)
    d.delete_vm(uuid)
    assert d.read_disk(clone.uuid, 0).startswith(b"edited")
    d.start_vm(clone.uuid)


def test_customized_clone_gets_seed_cd(fake):
    d, uuid = fake
    vm = d.clone_vm(uuid, CloneSpec(name="web1", customization=Customization(username="ops")))
    assert vm.spec.iso == "/var/lib/ibhics/seeds/web1-seed.iso"
    assert "instance-id: " + vm.uuid in d.seeds["web1"]["meta-data"]
    d.delete_vm(vm.uuid)
    assert "web1" not in d.seeds


def test_running_vm_is_not_cloned(fake):
    d, uuid = fake
    d.start_vm(uuid)
    with pytest.raises(InvalidState, match="shut"):
        d.clone_vm(uuid, CloneSpec(name="x"))


# ---------- libvirt driver ----------


class LibvirtError(Exception):
    pass


GIB = 1024**3
SOURCE_XML = build_domain_xml(
    VMSpec(name="tpl", template=True, disks=[DiskSpec(size_gb=20, pool="default")])
)


class Vol:
    def __init__(self, path):
        self._path = path
        self.deleted = False

    def path(self):
        return self._path

    def info(self):
        return (0, 20 * GIB, GIB)

    def delete(self, _flags):
        self.deleted = True


class StoragePool:
    def __init__(self, kind="dir"):
        self.kind = kind
        self.created = []

    def XMLDesc(self, _f):
        return f"<pool type='{self.kind}'/>"

    def storageVolLookupByName(self, name):
        return Vol(f"/pool/{name}")

    def createXML(self, xml, _flags):
        self.created.append(xml)
        return Vol("/pool/" + ET.fromstring(xml).findtext("name"))


class Dom:
    def __init__(self, xml, uuid="src", active=False):
        self.xml, self.uuid, self.active = xml, uuid, active

    def XMLDesc(self, _f):
        return self.xml

    def isActive(self):
        return self.active

    def state(self):
        return (5, 0)

    def UUIDString(self):
        return self.uuid

    def name(self):
        return ET.fromstring(self.xml).findtext("name")

    def autostart(self):
        return 0

    def setAutostart(self, _v):
        pass


class Conn:
    def __init__(self, pool):
        self.pool = pool
        self.doms = {"src": Dom(SOURCE_XML)}

    def lookupByName(self, name):
        for d in self.doms.values():
            if d.name() == name:
                return d
        raise LibvirtError(name)

    def lookupByUUIDString(self, uuid):
        return self.doms[uuid]

    def storagePoolLookupByName(self, _n):
        return self.pool

    def storageVolLookupByPath(self, path):
        return Vol(path)

    def defineXML(self, xml):
        uuid = ET.fromstring(xml).findtext("uuid") or "src"
        self.doms[uuid] = Dom(xml, uuid)
        return self.doms[uuid]

    def listAllDomains(self):
        return list(self.doms.values())


@pytest.fixture
def lvd(monkeypatch, tmp_path):
    monkeypatch.setattr(
        lv,
        "libvirt",
        types.SimpleNamespace(
            libvirtError=LibvirtError,
            VIR_DOMAIN_XML_INACTIVE=2,
            VIR_DOMAIN_RUNNING=1,
            VIR_DOMAIN_PAUSED=3,
            VIR_DOMAIN_SHUTDOWN=4,
            VIR_DOMAIN_SHUTOFF=5,
            VIR_DOMAIN_CRASHED=6,
        ),
    )

    def make(kind="dir"):
        d = object.__new__(lv.LibvirtDriver)
        d._conn = Conn(StoragePool(kind))
        d.commands = []
        d.run = d.commands.append
        d.seed_dir = str(tmp_path / "seeds")
        return d

    return make


def test_libvirt_full_clone_copies_into_file_pool(lvd):
    d = lvd()
    vm = d.clone_vm("src", CloneSpec(name="web1"))
    vol = ET.fromstring(d._conn.pool.created[0])
    assert vol.findtext("name") == "web1-disk0.qcow2"
    assert vol.find("backingStore") is None
    assert d.commands == [
        ["qemu-img", "convert", "-n", "-f", "qcow2", "-O", "qcow2"]
        + ["/pool/tpl-disk0.qcow2", "/pool/web1-disk0.qcow2"]
    ]
    assert "linked_to" not in spec_to_json(vm.spec)
    assert not d.set_template("src", False).spec.template


def test_libvirt_full_clone_copies_into_block_pool(lvd):
    d = lvd(kind="logical")  # the template's disk is qcow2; the target pool is LVM
    d.clone_vm("src", CloneSpec(name="db1"))
    assert d.commands == [
        ["qemu-img", "convert", "-n", "-f", "qcow2", "-O", "raw"]
        + ["/pool/tpl-disk0.qcow2", "/pool/db1-disk0"]
    ]


def test_libvirt_template_does_not_start(lvd):
    d = lvd()
    with pytest.raises(InvalidState, match="template"):
        d.start_vm("src")


# ---------- through the manager, with a remote node ----------


@pytest.fixture
def env(tmp_path):
    env = Env(tmp_path)
    deploys = DeployManager(env.cluster, executor=Inline())
    env.client = TestClient(create_app(env.cluster, migrations=env.migrations, deploys=deploys))
    return env


def test_deploy_from_template_on_remote_node(env):
    uuid = env.create_vm("tpl", node="node2", start=False)
    api = env.client
    assert api.post(f"/api/v1/vms/{uuid}/template", json={"template": True}).json()["template"]
    assert api.post(f"/api/v1/vms/{uuid}/start").status_code == 409

    body = {
        "name": "web1",
        "power_on": True,
        "customization": {"hostname": "web1", "username": "ops", "password": "pw"},
    }
    resp = api.post(f"/api/v1/vms/{uuid}/clone", json=body)
    assert resp.status_code == 202, resp.text
    job = api.get("/api/v1/deploys").json()[0]
    assert job["state"] == "done", job
    assert (job["source"], job["node"], job["customized"]) == (
        "tpl",
        "node2",
        True,
    )
    vm = api.get(f"/api/v1/vms/{job['result']}").json()
    assert vm["node"] == "node2" and vm["state"] == "running"
    assert "linked_to" not in vm and "linked" not in job
    assert "ops" in env.node2.seeds["web1"]["user-data"]

    again = api.post(f"/api/v1/vms/{uuid}/clone", json={"name": "web1"})
    assert again.status_code == 409
    assert api.delete(f"/api/v1/vms/{uuid}").status_code == 204
    assert api.get(f"/api/v1/vms/{job['result']}").json()["state"] == "running"


def test_deploy_failure_is_recorded(env):
    uuid = env.create_vm("plain", start=False)
    resp = env.client.post(f"/api/v1/vms/{uuid}/clone", json={"name": "x", "pool": "nope"})
    assert resp.status_code == 202
    job = env.client.get("/api/v1/deploys").json()[0]
    assert job["state"] == "failed" and "nope" in job["error"]


def test_deployed_vm_joins_template_group(env):
    uuid = env.create_vm("tpl", start=False)
    group = env.client.post("/api/v1/vm-groups", json={"name": "Linux"}).json()["id"]
    env.client.put("/api/v1/vm-groups/members", json={"vms": [uuid], "group": group})
    env.client.post(f"/api/v1/vms/{uuid}/clone", json={"name": "c1"})
    new = env.client.get("/api/v1/deploys").json()[0]["result"]
    assert env.client.get("/api/v1/vm-groups").json()["members"][new] == group


@pytest.mark.parametrize("template", [False, True])
@pytest.mark.parametrize("linked", [True, "true", 1])
def test_clone_and_deploy_reject_linked_before_starting_job(env, template, linked):
    uuid = env.create_vm("source", start=False)
    if template:
        env.client.post(f"/api/v1/vms/{uuid}/template", json={"template": True})
    resp = env.client.post(f"/api/v1/vms/{uuid}/clone", json={"name": "copy1", "linked": linked})
    assert resp.status_code == 422
    assert "linked clones are not supported; VMs are deployed as full copies" in resp.text
    assert env.client.get("/api/v1/deploys").json() == []
    assert len(env.client.get("/api/v1/vms").json()) == 1


def test_clone_accepts_legacy_false_without_advertising_field(env):
    uuid = env.create_vm("source", start=False)
    resp = env.client.post(f"/api/v1/vms/{uuid}/clone", json={"name": "copy1", "linked": False})
    assert resp.status_code == 202
    assert resp.json()["state"] == "done"
    schemas = env.client.get("/openapi.json").json()["components"]["schemas"]
    assert "linked" not in schemas["CloneIn"]["properties"]
    assert "linked" not in schemas["DeployOut"]["properties"]
    assert "linked_to" not in schemas["VMOut"]["properties"]


@pytest.mark.parametrize("template", [False, True])
def test_agent_rejects_linked_clone(fake, template):
    from ibhics.agent import create_agent_app
    from ibhics.cluster import NodeRole

    driver, uuid = fake
    driver.set_template(uuid, template)
    client = TestClient(create_agent_app("node", NodeRole.COMPUTE, "test-token", driver))
    resp = client.post(
        f"/agent/v1/vms/{uuid}/clone",
        json={"name": "copy1", "linked": True},
        headers={"X-Ibhics-Token": "test-token"},
    )
    assert resp.status_code == 422
    assert "linked clones are not supported; VMs are deployed as full copies" in resp.text
    assert len(driver.list_vms()) == 1
