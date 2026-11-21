"""Storage workspace boundaries and telemetry rendering without a live cluster."""

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).parents[1] / "ibhics/manager/static"


class StorageTree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.elements = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parents = [a.get("id") for _, a in self.stack if a.get("id")]
        if attrs.get("id"):
            self.elements[attrs["id"]] = (attrs, parents)
        if tag not in {"input", "link", "meta", "br", "hr", "img", "use", "path"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break


def test_storage_primary_workspaces_are_siblings():
    tree = StorageTree()
    tree.feed((STATIC / "index.html").read_text())
    for name in ("virtual", "local", "iso"):
        attrs, parents = tree.elements[f"storage-panel-{name}"]
        assert attrs["role"] == "tabpanel"
        assert attrs["aria-labelledby"] == f"storage-tab-{name}"
        assert "page-storage" in parents
        assert not any(p.startswith("storage-panel-") for p in parents)
        assert ("hidden" in attrs) == (name != "virtual")
    for element in ("isos", "iso-dest"):
        assert "storage-panel-iso" in tree.elements[element][1]
        assert "storage-panel-virtual" not in tree.elements[element][1]
    assert "storage-panel-local" in tree.elements["pools"][1]
    for element in ("vs-prepare", "vs-create", "vs-tabs", "virtual-storage-status"):
        assert "storage-panel-virtual" in tree.elements[element][1]


def test_storage_telemetry_uses_reported_values():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for Storage rendering checks")
    source = (STATIC / "virtual-storage.js").read_text()
    components = source.split("  const reported =", 1)[1].split("  async function render()", 1)[0]
    code = (
        r"""
const assert = require('node:assert/strict');
const escapes = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => escapes[c]);
const num = String, nodeName = String, isAdmin = () => false;
const activeTab = 'overview', replicationView = 'panels';
const cacheControls = () => '';
"""
        + "const reported ="
        + components
    )
    code += r"""
const resource = () => ({name:'virtualstorage',devices:[{'disk-state':'UpToDate',quorum:true}],
connections:[{'connection-state':'Connected',peer_devices:[{'percent-in-sync':99.5}]}]});
const data = {phase:'ready',
created:{spec:{name:'virtualstorage',virtual_ip:'10.0.0.1',size_gib:100}},
nodes:[{name:'one',role:'compute',healthy:true,resources:[resource()]},
{name:'two',role:'compute',healthy:true,resources:[resource()]}],
cache:[{node:'one',mode:'writeback',dirty_bytes:0},{node:'two',dirty_bytes:12*1024**2}]};
const pool = {name:'virtualstorage',capacity_gb:100,allocation_gb:25,available_gb:75,
members:[{node:'one'}]};
assert(replicationConnected(data));
data.nodes[0].healthy = false; assert(!replicationConnected(data));
data.nodes[0].healthy = true; data.phase = 'degraded'; assert(!replicationConnected(data));
data.phase = 'ready'; data.nodes[0].resources[0].connections[0]['connection-state'] = 'Connecting';
assert(!replicationConnected(data));
data.nodes[0].resources[0] = resource();
data.nodes[0].resources[0].devices[0]['disk-state'] = 'Inconsistent';
assert(!replicationConnected(data));
data.nodes[0].resources[0] = resource();
const html = workspace(data,pool,false,true);
assert(html.includes('25%')); assert(html.includes('99.5%'));
assert(html.includes('0 MiB')); assert(html.includes('12 MiB'));
assert(!html.includes('ISO library')); assert(!html.includes('Local &amp; external'));
assert(!html.includes('<b>Not reported</b>')); assert(!html.includes('NaN'));
assert(!workspace({nodes:[],cache:[]},null,false,false).includes('<b>Not reported</b>'));
assert.equal(metric('Missing',null),''); assert.equal(metric('Missing','Not reported'),'');
assert(metric('Zero','0').includes('<b>0</b>'));
assert(topologyScene(data,pool).includes('25.0% used'));
assert(!topologyScene(data,null).includes('NaN'));
"""
    result = subprocess.run(["node", "-e", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
