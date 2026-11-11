"""Source diagnostics: every connector is fake; no source or external system is contacted."""

import json
import socket
import ssl
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ibhics.cluster import Cluster
from ibhics.hypervisor.fake import FakeDriver
from ibhics.importer import Source, SourceType
from ibhics.importer.diagnostics import test_source as diagnose
from ibhics.manager import create_app
from ibhics.manager.imports import ImportManager

SOURCE = Source(
    "esx01", SourceType.VMWARE, host="esx.example.test", username="root", password="private-secret"
)
NODE = "192.0.2.10"


class Probes:
    def __init__(self, fail=None, error=None):
        self.calls = []
        self.fail = fail
        self.error = error or TimeoutError()

    def call(self, step, result):
        self.calls.append(step)
        if self.fail == step:
            raise self.error
        return result

    def test(self, source=SOURCE, **options):
        return diagnose(
            source,
            connect=lambda h, p, t: self.call(f"TCP {p}", NODE),
            resolve=lambda h, p: self.call("DNS", [(None, None, None, None, ("192.0.2.20", 0))]),
            tls=lambda s, t: self.call("HTTPS/TLS", "TLSv1.3 SHA256 fake"),
            login=lambda s, t: self.call("Login", "Authenticated"),
            inventory=lambda s, t: self.call("List VMs", 3),
            path_check=lambda s, t: self.call("Path", "Readable"),
            node_ip=NODE,
            **options,
        )


@pytest.mark.parametrize(
    "step,error,hint",
    [
        ("DNS", socket.gaierror("invalid"), "DNS server"),
        ("TCP 443", TimeoutError(), "routing/firewall"),
        ("TCP 443", ConnectionRefusedError(), "Port closed"),
        ("HTTPS/TLS", ssl.SSLError("bad cert"), "certificate trust"),
        ("Login", RuntimeError("401 private-secret"), "Wrong user/password"),
        ("List VMs", RuntimeError("no permissions private-secret"), "read permissions"),
        ("TCP 22", ConnectionRefusedError(), "Port closed"),
    ],
)
def test_connection_stops_at_first_failure(step, error, hint):
    probes = Probes(step, error)
    result = probes.test()
    assert probes.calls[-1] == step
    assert [c["step"] for c in result["checks"]] == probes.calls
    assert all(c["ok"] for c in result["checks"][:-1])
    failed = result["checks"][-1]
    assert failed["ok"] is False
    assert hint in failed["hint"]
    assert NODE in failed["detail"]
    assert "private-secret" not in json.dumps(result)
    if isinstance(error, TimeoutError):
        assert f"{NODE} → {SOURCE.host}:443" in failed["hint"]


def test_vmware_ok_and_nfc_warning():
    result = Probes().test()
    assert [c["step"] for c in result["checks"]] == [
        "DNS",
        "TCP 443",
        "HTTPS/TLS",
        "Login",
        "List VMs",
        "TCP 22",
        "TCP 902",
    ]
    assert all(c["ok"] for c in result["checks"])
    assert "3 VMs found" in result["checks"][4]["detail"]
    warned = Probes("TCP 902").test()
    assert warned["checks"][-1]["ok"] is False
    assert "Warning only (NFC)" in warned["checks"][-1]["hint"]
    assert all(c["ok"] for c in warned["checks"][:-1])


def test_numeric_host_skips_dns_and_https_transport_skips_ssh():
    result = Probes().test(replace(SOURCE, host="192.0.2.20", transport="https"))
    assert [c["step"] for c in result["checks"]] == [
        "TCP 443",
        "HTTPS/TLS",
        "Login",
        "List VMs",
        "TCP 902",
    ]


@pytest.mark.parametrize("step", [None, "DNS", "TCP 22", "Login", "List VMs"])
def test_kvm_read_only_probe_order(step):
    probes = Probes(step)
    result = probes.test(replace(SOURCE, type=SourceType.KVM))
    expected = ["DNS", "TCP 22", "Login", "List VMs"]
    assert (
        probes.calls == expected[: expected.index(step) + 1] if step else probes.calls == expected
    )
    assert result["checks"][-1]["ok"] is (step is None)


@pytest.mark.parametrize("kind", [SourceType.OVA, SourceType.DISK, SourceType.UPLOAD])
@pytest.mark.parametrize("error", [None, FileNotFoundError(), PermissionError()])
def test_local_path_probes(kind, error):
    probes = Probes("Path" if error else None, error)
    result = probes.test(Source("folder", kind, path="/fake/exports"))
    assert probes.calls == ["Path"]
    assert result["checks"][0]["ok"] is (error is None)
    if error:
        assert "Mount" in result["checks"][0]["hint"]


def test_real_path_checker_reads_only(tmp_path):
    source = Source("exports", SourceType.OVA, path=str(tmp_path))
    assert diagnose(source, node_ip=NODE)["checks"][0]["ok"]
    missing = replace(source, path=str(tmp_path / "missing"))
    assert not diagnose(missing, node_ip=NODE)["checks"][0]["ok"]
    assert not (tmp_path / "missing").exists()


def test_timeout_bounds_even_a_stuck_injected_connector():
    def slow(*args):
        time.sleep(1.0)

    started = time.monotonic()
    result = diagnose(SOURCE, resolve=slow, node_ip=NODE, timeout=0.01, budget=0.02)
    assert time.monotonic() - started < 0.5  # well before the stuck call returns
    assert result["checks"][0]["step"] == "DNS"
    assert not result["checks"][0]["ok"]


def test_redacts_success_details_and_hints():
    result = diagnose(
        replace(SOURCE, host="192.0.2.20"),
        connect=lambda *a: NODE,
        tls=lambda *a: "private-secret",
        login=lambda *a: (_ for _ in ()).throw(RuntimeError("401 private-secret")),
        node_ip=NODE,
    )
    assert "private-secret" not in json.dumps(result)
    assert "[redacted]" in result["checks"][1]["detail"]


def make_client(tmp_path, probes=None):
    cluster = Cluster.standalone("fake-node", FakeDriver())
    manager = ImportManager(
        cluster, tester=(probes or Probes()).test, upload_dir=tmp_path / "uploads"
    )
    app = create_app(cluster, imports=manager, iso_dir=tmp_path / "iso")
    return TestClient(app), manager


def test_api_unsaved_and_saved_source_tests_keep_password_private(tmp_path):
    client, manager = make_client(tmp_path, Probes("Login", RuntimeError("401 private-secret")))
    body = SOURCE.to_dict()
    unsaved = client.post("/api/v1/migrate/sources/test", json=body)
    assert unsaved.status_code == 200
    assert manager.sources() == [manager.uploads_source, manager.physical_source]
    assert not (tmp_path / "uploads").exists()
    assert client.post("/api/v1/migrate/sources", json=body).status_code == 201
    saved = client.post(f"/api/v1/migrate/sources/{SOURCE.name}/test")
    assert saved.status_code == 200
    assert saved.json() == unsaved.json()
    assert saved.json()["checks"][-1]["step"] == "Login"
    for response in (saved, unsaved):
        assert "private-secret" not in response.text
        assert 'password":' not in response.text
    assert manager.source(SOURCE.name).password == "private-secret"
    assert client.post("/api/v1/migrate/sources/missing/test").status_code == 404
    assert client.post("/api/v1/migrate/sources/uploads/test").status_code == 200
    assert not (tmp_path / "uploads").exists()


@pytest.mark.parametrize(
    "body",
    [
        {"password": "private-secret"},
        {**SOURCE.to_dict(), "password": "private-secret" * 50},
        ["private-secret"],
    ],
)
def test_api_validation_does_not_echo_password(tmp_path, body):
    client, _ = make_client(tmp_path)
    response = client.post("/api/v1/migrate/sources/test", json=body)
    assert response.status_code == 422
    assert "private-secret" not in response.text


def test_api_success(tmp_path):
    client, manager = make_client(tmp_path)
    response = client.post("/api/v1/migrate/sources/test", json=SOURCE.to_dict())
    assert response.status_code == 200
    assert all(c["ok"] for c in response.json()["checks"])
    manager.add_source(SOURCE)
    response = client.post(f"/api/v1/migrate/sources/{SOURCE.name}/test")
    assert response.status_code == 200
    assert all(c["ok"] for c in response.json()["checks"])


def test_static_migrate_controls():
    static = Path(__file__).parents[1] / "ibhics" / "manager" / "static"
    html = (static / "index.html").read_text()
    assert 'id="test-source-form">Test connection</button>' in html
    assert 'id="migrate-selected"' in html
    assert 'id="import-pick-vms"' in html
    assert 'id="source-error"' in html
    js = (static / "app.js").read_text()
    assert "data-source-test=" in js
    assert "Run connection test" in js
    assert "steps: 4," in js[js.index("const importWiz") :]


def test_network_mapping_preserves_nic_order_and_duplicate_networks():
    """Run the actual pure JS mapper without a browser or network."""
    static = Path(__file__).parents[1] / "ibhics" / "manager" / "static" / "app.js"
    js = static.read_text()
    code = js[js.index("function importNetworkKeys(vm)") : js.index("function renderImportNets()")]
    code += """
const assert = require('node:assert/strict');
assert.deepEqual(importNetworkKeys({nics:3, networks:['LAN','WAN','LAN']}), ['LAN','WAN','LAN']);
assert.deepEqual(importNetworkKeys({nics:2, networks:[]}),
  ['Adapter 1 (unspecified)','Adapter 2 (unspecified)']);
const mapping = new Map([['LAN','switch1'],['WAN','switch2']]);
const mapped = importNetworkKeys({nics:3,networks:['WAN','LAN','LAN']}).map(k=>mapping.get(k));
assert.deepEqual(mapped, ['switch2','switch1','switch1']);
"""
    subprocess.run(["node", "-e", code], check=True, capture_output=True, text=True)


def test_default_vmware_login_reports_401_without_parsing_secret_body(monkeypatch):
    from ibhics.importer import diagnostics
    from ibhics.importer.esxi_api import EsxiApiError

    monkeypatch.setattr(
        "ibhics.importer.esxi_api.https_transport",
        lambda source, timeout: lambda body, cookie: (401, "private-secret", None),
    )
    with pytest.raises(EsxiApiError, match="401") as exc:
        diagnostics._login(SOURCE, 1)
    assert "private-secret" not in str(exc.value)


def test_default_ssh_probe_never_sets_up_keys_or_services(monkeypatch):
    from ibhics.importer import diagnostics

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert "private-secret" not in repr(argv)
        assert "StrictHostKeyChecking=yes" in argv
        assert "authorized_keys" not in repr(argv)
        assert kwargs["timeout"] == 2
        askpass = Path(kwargs["env"]["SSH_ASKPASS"])
        assert askpass.exists()
        return subprocess.CompletedProcess(argv, 0, "Authenticated", "")

    monkeypatch.setattr(diagnostics.subprocess, "run", run)
    assert diagnostics._login(replace(SOURCE, type=SourceType.KVM), 2)
    assert len(calls) == 1
    assert calls[0][0][-1] == "true"
    assert not Path(calls[0][1]["env"]["SSH_ASKPASS"]).exists()


def test_default_libvirt_inventory_is_killable_and_password_not_in_argv(monkeypatch):
    from ibhics.importer import diagnostics

    def run(argv, **kwargs):
        assert "private-secret" not in repr(argv)
        assert json.loads(kwargs["input"])["password"] == "private-secret"
        assert kwargs["timeout"] == 2
        assert "listAllDomains" in argv[-1]
        return subprocess.CompletedProcess(argv, 0, "5\n", "")

    monkeypatch.setattr(diagnostics.subprocess, "run", run)
    assert diagnostics._inventory(SOURCE, 2) == 5


def test_empty_dns_result_fails_before_connect():
    result = diagnose(SOURCE, resolve=lambda *args: [], node_ip=NODE)
    assert [c["step"] for c in result["checks"]] == ["DNS"]
    assert not result["checks"][0]["ok"]


def test_kvm_timeout_hint_uses_ssh_port():
    result = Probes("Login").test(replace(SOURCE, type=SourceType.KVM))
    assert f"{NODE} → {SOURCE.host}:22" in result["checks"][-1]["hint"]


def test_wizard_validation_and_partial_job_retry_with_mock_dom():
    """Exercise actual wizard handlers in Node; this is not a browser integration test."""
    static = Path(__file__).parents[1] / "ibhics" / "manager" / "static" / "app.js"
    js = static.read_text()
    start = js.index("const importForm =")
    end = js.index("// ---------- settings ----------", start)
    prefix = r"""
const assert = require('node:assert/strict');
const noop = () => {};
const elements = {node: {value:'node1',addEventListener:noop},
                  pool: {value:'default',addEventListener:noop}};
const boxes = new Map();
const $ = (key) => {
  if (key==='#import-form') return {elements};
  if (!boxes.has(key)) boxes.set(key,{textContent:'',innerHTML:'',hidden:false,
                                    addEventListener:noop});
  return boxes.get(key);
};
const $$ = () => [];
const handlers = {};
const wizard = (id, options) => ({step:4,check:options.check,show:options.show});
const state = {pools:[{node:'node1',name:'default',active:true,type:'dir',available_gb:100}],
               vms:[],imports:[],networks:[],sources:[]};
const esc = s => String(s);
let refreshes=0, tab=null;
const refresh=async () => {refreshes++};
const selectTab=(id, value) => {tab=value};
const toast=noop;
let rejectSecond=true, requests=[];
const post=async (url, body) => {
  requests.push(body);
  if (body.vm==='vm2' && rejectSecond) {rejectSecond=false;throw new Error('mock rejection')}
  return {id:body.vm};
};
"""
    suffix = r"""
(async () => {
  const vm1={ref:'vm1',name:'web1',vcpus:2,memory_mb:2048,state:'stopped',
             disks_gb:[20],nics:3,networks:['WAN','LAN','LAN'],guest_os:'Linux'};
  const vm2={ref:'vm2',name:'db1',vcpus:4,memory_mb:4096,state:'stopped',
             disks_gb:[40],nics:1,networks:['LAN'],guest_os:'Windows 11'};
  Object.assign(importBatch,{source:'esx01',vms:[vm1,vm2],picked:new Set(['vm1','vm2'])});
  importBatch.networks=new Map([['LAN','vs-lan'],['WAN','vs-wan']]);
  importWiz.check(4);
  assert.match($('#import-capacity').textContent,/60 GB total disks/);
  vm1.state='running';assert.throws(()=>importWiz.check(1),/Power off/);vm1.state='stopped';
  state.pools[0].available_gb=50;assert.throws(()=>importWiz.check(2),/Insufficient space/);
  state.pools[0].available_gb=100;state.pools[0].type='lvm';
  assert.throws(()=>importWiz.check(2),/Insufficient space/);state.pools[0].type='dir';
  importBatch.names.set('vm1','db1');assert.throws(()=>importWiz.check(2),/distinct/);
  importBatch.names.set('vm1','bad name');assert.throws(()=>importWiz.check(2),/VM names/);
  importBatch.names.set('vm1','existing');state.vms=[{name:'existing'}];
  assert.throws(()=>importWiz.check(2),/already exists/);state.vms=[];
  importBatch.names.set('vm1','renamed');
  importBatch.resources.set('vm1',{vcpus:8,memory_mb:8192});
  const form=new Map([['node','node1'],['pool','default'],['clear_hibernation','on']]);
  await assert.rejects(handlers['import-dialog'](form),/mock rejection/);
  assert.deepEqual([...importBatch.submitted],['vm1']);
  assert.match($('#import-submit-status').textContent,/1 of 2 jobs created/);
  await handlers['import-dialog'](form);
  assert.equal(requests.length,3);
  assert.deepEqual(requests.map(r=>r.vm),['vm1','vm2','vm2']);
  assert.deepEqual(requests[0].network,['vs-wan','vs-lan','vs-lan']);
  assert.deepEqual(requests[2].network,['vs-lan']);
  assert.equal(requests[0].clear_hibernation,false);
  assert.equal(requests[2].clear_hibernation,true);
  assert.equal(requests[0].vcpus,8);assert.equal(requests[0].memory_mb,8192);
  assert.equal(refreshes,1);assert.equal(tab,'imports');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    subprocess.run(["node", "-e", prefix + js[start:end] + suffix], check=True, capture_output=True)


def test_testing_an_unsaved_source_needs_admin():
    from ibhics.manager.app import API, required_role
    from ibhics.manager.auth import Role

    assert required_role("POST", f"{API}/migrate/sources/test") == Role.ADMIN
    assert required_role("POST", f"{API}/migrate/sources/{{name}}/test") == Role.OPERATOR
