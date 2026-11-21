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
const ring = topologyScene(data,pool);
assert(ring.includes('25.0% used'));
assert(ring.includes('stroke-dasharray="25 100"'));
assert.equal((ring.match(/data-ring-index=/g) || []).length, 6);
assert(ring.includes('href="#storage/virtual/mirror"'));
assert(ring.includes('href="#storage/virtual/cache"'));
assert(ring.includes('href="#storage/virtual/capacity"'));
assert(html.includes('id="vs-panel-mirror"'));
assert(!html.includes('id="vs-panel-replication"'));
data.nodes[0].healthy = false;
assert(topologyScene(data,pool).includes('vs-tone-alert'));
assert(!topologyScene(data,pool).includes('vs-scene vs-linked'));
data.cache[0].error = 'Unavailable';
assert(topologyScene(data,pool).includes('Check cache'));
data.cache = [{dirty_bytes:1}];
assert(topologyScene(data,pool).includes('vs-tone-warning'));
const missing = topologyScene({nodes:[],cache:[]},null);
assert(missing.includes('class="vs-missing">—'));
assert(!missing.includes('NaN'));
assert(!missing.includes('Online'));
pool.display_name = '<img src=x onerror=alert(1)>';
assert(!topologyScene(data,pool).includes('<img'));
pool.allocation_gb=200;
assert(topologyScene(data,pool).includes('100.0% used'));
assert(!topologyScene(data,null).includes('NaN'));
"""
    result = subprocess.run(["node", "-e", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_virtual_storage_mirror_tab_keeps_migrate_replication():
    html = (STATIC / "index.html").read_text()
    tree = StorageTree()
    tree.feed(html)
    attrs, _ = tree.elements["vs-tab-mirror"]
    assert attrs["aria-controls"] == "vs-panel-mirror"
    assert attrs["data-vs-tab"] == "mirror"
    assert "vs-tab-replication" not in html
    assert '<button data-tab="replications">Replication</button>' in html
    assert '<script defer src="storage-scene.js"></script>' in html
    assert '<script type="module" src="fabric3d.js"></script>' in html
    scene = (STATIC / "storage-scene.js").read_text()
    assert "from 'three'" not in scene
    assert "canvas" not in scene


def test_storage_ring_motion_and_navigation():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for ring interaction checks")
    code = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = {}, visibility = {}, mediaListeners = {};
const media = {matches:false,addEventListener:(k,v)=>mediaListeners[k]=v,removeEventListener(){}};
let pageActive = true, hidden = false, observerCallback, frames = new Map(), serial = 0;
const classes = new Set();
const node = {dataset:{ringIndex:'0'},setAttribute(k,v){this[k]=v;}};
const host = {isConnected:true,classList:{toggle(k,v){v ? classes.add(k) : classes.delete(k);}},
  closest:()=>hidden ? {} : null, querySelectorAll:()=>[node],
  querySelector:()=>({getBoundingClientRect:()=>({left:0,top:0,width:840,height:760})}),
  addEventListener:(k,v)=>listeners[k]=v,removeEventListener(){},
  setPointerCapture(){},hasPointerCapture:()=>false,releasePointerCapture(){}};
const page = {classList:{contains:()=>pageActive}};
const document = {hidden:false,getElementById:()=>page,
  addEventListener:(k,v)=>visibility[k]=v,removeEventListener(){}};
const window = {};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window, document,
  matchMedia:()=>media,MutationObserver:class {
    constructor(fn){observerCallback=fn;}observe(){}disconnect(){}},
  requestAnimationFrame:fn=>{frames.set(++serial,fn);return serial;},
  cancelAnimationFrame:id=>frames.delete(id)});
function step(now) {const jobs=[...frames.values()];frames.clear();jobs.forEach(fn=>fn(now));}
window.StorageScene.mount(host);
assert.equal(frames.size,1);step(100);const initial=node.transform;step(140);
assert.notEqual(node.transform,initial);assert(!node.transform.includes('rotate'));
listeners.pointerenter();assert.equal(frames.size,0);assert(classes.has('vs-motion-paused'));
const target = {closest:()=>null};
listeners.pointerdown({pointerId:1,button:0,isPrimary:true,clientX:664,clientY:364,target});
listeners.pointermove({pointerId:1,clientX:420,clientY:608});
assert.notEqual(node.transform,initial);
listeners.pointerup({pointerId:1});
let prevented=false;
listeners.click({detail:1,preventDefault(){prevented=true;},stopPropagation(){}});
assert(prevented); // Drag must not navigate to the icon underneath the pointer.
prevented=false;listeners.click({detail:1,preventDefault(){prevented=true;}});assert(!prevented);
listeners.pointerleave();assert.equal(frames.size,1);
document.hidden=true;visibility.visibilitychange();assert.equal(frames.size,0);
document.hidden=false;visibility.visibilitychange();assert.equal(frames.size,1);
hidden=true;observerCallback();assert.equal(frames.size,0);
hidden=false;pageActive=false;observerCallback();assert.equal(frames.size,0);
pageActive=true;observerCallback();assert.equal(frames.size,1);
media.matches=true;mediaListeners.change();assert.equal(frames.size,0);
const still=node.transform;
listeners.pointerdown({pointerId:2,button:0,clientX:664,clientY:364,target});
listeners.pointermove({pointerId:2,clientX:420,clientY:608});assert.equal(node.transform,still);
media.matches=false;mediaListeners.change();assert.equal(frames.size,1);
window.StorageScene.mount(null);assert.equal(frames.size,0);
"""
    result = subprocess.run(
        ["node", "-e", code, str(STATIC / "storage-scene.js")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
