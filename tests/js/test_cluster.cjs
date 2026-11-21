const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ibhics/manager/static/app.js', 'utf8');
const context = vm.createContext({
  NetworkCore: require('../../ibhics/manager/static/network-core.js'),
  state: {hosts: {n1: {cpus: 8, memory_mb: 8192}}, summaries: {n1: {cpu_percent: 35, threads: 8, memory_used_mb: 2048, memory_mb: 8192}}, vms: [{node: 'n1'}]},
  esc: s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]),
  enc: encodeURIComponent, gbText: mb => `${mb} MB`,
  computeMembers: () => [{name: 'n1'}, {name: 'n2'}], nodeLabel: name => name,
});
vm.runInContext(source.slice(source.indexOf('function ipmiText('), source.indexOf('function renderCluster()')), context);
const status = value => context.clusterHAStatus(value);
assert.equal(status(null), '—');
assert.equal(status({}), '—');
assert.equal(status({available: true}), '—');
assert.equal(status({available: false, vms: []}), 'Unavailable on this manager');
assert.equal(status({available: true, vms: []}), 'On · 0 protected VMs');
assert.equal(status({available: true, vms: [{uuid:'one'}, {uuid:'two'}]}), 'On · 2 protected VMs');
const compute = {name:'n1', role:'compute', healthy:true, local:true};
const witness = {name:'witness', role:'witness', healthy:false};
const render = (members, admin=true) => context.nodeRows({members}, admin);
let html = render([compute, witness]);
assert.match(html, /35% used/);
assert.match(html, /25% used/);
assert.match(html, /data-evacuate="n1"/);
assert.match(html, /data-node="witness"/);
assert.doesNotMatch(html, /data-node="n1"/);
assert.match(html, /Quorum witness/);
assert.match(html, /Offline/);
assert.doesNotMatch(render([compute], false), /data-(node-label|maint|evacuate|node)=/);
html = render([{...compute, healthy:false}]);
assert.doesNotMatch(html, /35% used|25% used|<meter/);
assert.match(html, /8 cores/);
assert.match(html, /live usage unavailable/);
assert.doesNotMatch(render([{...compute, name:'<script>'}]), /<script>/);
assert.match(context.clusterMetric('CPU','x','capacity',120), /value="100"/);
console.log('Cluster HA, live metrics, offline fallback, escaping and action checks passed');
// Exercise topology and onboarding with the same renderer used by the page.
const elements = new Map();
Object.assign(context, {
  $: selector => { if (!elements.has(selector)) elements.set(selector, {}); return elements.get(selector); },
  isAdmin: () => true, location: {hostname: 'localhost'},
  TOPOLOGY: {'two-node-witness': '2 nodes + witness'}, loadHardwareBadge: () => {},
});
vm.runInContext(source.slice(source.indexOf('function renderCluster()'), source.indexOf('$("#page-cluster").addEventListener')), context);
for (const count of [1,2,3,4,7]) {
  context.state.cluster = {name:'Production',cluster_id:'12345678-abcd',quorum:true,members:Array.from({length:count},(_,i)=>({...compute,name:`n${i}`,healthy:i!==1}))};
  context.state.clusterHA = {available:true,vms:[{uuid:'one'}]};
  context.renderCluster();
  const hero = elements.get('#cluster-hero').innerHTML;
  assert.equal((hero.match(/class="cl-map-node /g)||[]).length,count);
  assert.equal((hero.match(/class="cl-link /g)||[]).length,count);
  assert.match(elements.get('#cluster-overview').innerHTML,/<b>On<\/b><span>1 VM protected/);
  assert.match(elements.get('#cluster-members').innerHTML,/Add a node/);
}
context.state.cluster = {quorum:false,members:[compute]};
context.renderCluster();
assert.equal(elements.get('#cluster-empty').hidden,false);
assert.equal(elements.get('#cluster-hero').hidden,true);
assert.equal(elements.get('#cluster-page-add').hidden,true);
assert.equal(elements.get('#cluster-page-create').hidden,false);
console.log('Cluster topology sizes and standalone onboarding checks passed');
