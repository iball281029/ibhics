"""Exercise dashboard calculations and actual SVG rendering in Node, without a browser."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_home_allocation_and_live_history():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for dashboard JavaScript checks")
    source = (Path(__file__).parents[1] / "ibhics/manager/static/app.js").read_text()
    static = Path(__file__).parents[1] / "ibhics/manager/static"
    helpers = "\n".join((static / f).read_text() for f in ("history-time.js", "storage-view.js"))
    home = source.split("// ---------- home ----------", 1)[1].split(
        "// ---------- compute ----------", 1
    )[0]
    code = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const $ = (id) => {
  if (!elements.has(id)) elements.set(id, {addEventListener() {}});
  return elements.get(id);
};
const escapes = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => escapes[c]);
const gbText = (n) => `${n} MB`;
const pct = (a,b) => b>0 ? Math.min(100,Math.round(a/b*100)) : 0;
const num = String, avatar = () => '', bar = () => '', ago = () => '';
const TOPOLOGY = {}, JOB_KINDS = {}, JOB_STATE = {}, enc = encodeURIComponent;
const nodeLabel = (n) => n;
const state = {cluster:{members:[{name:'node1',role:'compute',healthy:true}],quorum:true},
hosts:{node1:{cpus:8,memory_mb:8000}},
summaries:{node1:{cpu_percent:25,memory_used_mb:2000,memory_mb:8000,threads:8}},
vms:[{name:'live',node:'node1',state:'running',vcpus:10,memory_mb:10000,disks:[{size_gb:100}]},
{name:'off',node:'node1',state:'stopped',vcpus:20,memory_mb:20000,disks:[{size_gb:50}]}],
templates:[{disks:[{size_gb:50}]}],pools:[{capacity_gb:160,allocation_gb:40}],jobs:[],moves:[],imports:[],deploys:[]};
const computeMembers = () => state.cluster.members;
"""
    code += home
    code += r"""
assert.equal(totals().vcpus,10);
assert.equal(totals().vmem,10000);
assert.equal(totals().vdisk,200); // stopped VMs and templates still reserve disk capacity
assert.equal(allocationPct(10,8),125);
assert.equal(allocationPct(1,0),null);
assert.equal(percentText(null),'–');
renderHome();
const html = $('#usage').innerHTML;
assert.equal((html.match(/overallocated"/g)||[]).length,3);
assert.equal(html.split('<strong>125%</strong>').length-1,3);
assert.equal((html.match(/Overallocated by 25%/g)||[]).length,3);
assert.equal((html.match(/class="resource-chart"/g)||[]).length,3);
assert(!html.includes('allocation-track'));
assert(!html.includes('chart-ring'));
assert.equal(html.split('100%</text>').length-1,3);
assert.equal(html.split('50%</text>').length-1,3);
assert.equal(html.split('class="resource-chart"').length-1,3);
assert(html.split('time-tick').length-1 >= 3); // clock-time axis, not "min ago"
assert(html.includes('Idle: <b>75%</b>'));
assert(html.includes('Available: <b>75%</b>'));
assert(html.includes('No recorded data in this period'));
assert(!html.includes('NaN'));
assert(!allocationMeter('CPU',8,8,'').includes('overallocated'));
assert(allocationMeter('CPU',8.01,8,'').includes('overallocated'));
assert(allocationMeter('CPU',1,0,'').includes('Capacity unavailable'));
const chart=usageChart('cpu','CPU',50,'');
assert(!chart.includes('NaN'));
assert(!usageChart('memory','Memory',null,'').includes('Available: <b>100%'));
assert(chart.includes('gradientUnits="userSpaceOnUse"')); // flat lines need gradient space
state.hosts={};state.summaries={};state.pools=[];
renderHome();
assert(!$('#usage').innerHTML.includes('NaN'));
assert($('#usage').innerHTML.includes('No data'));
"""
    result = subprocess.run(["node", "-e", helpers + "\n" + code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
