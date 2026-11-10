const assert = require('node:assert/strict');
const {pools} = require('../../ibhics/manager/static/storage-view.js');
const local = node => ({node, name:'default', type:'dir', source:'/same/local/path', active:true, capacity_gb:20});
const shared = node => ({node, name:'virtualstorage', type:'nfs', source:'172.16.12.106:/shared', active:true, capacity_gb:6703});
const rows=[local('one'),local('two'),{...shared('one'),active:false},shared('two')];
const view=pools(rows);
assert.equal(view.length,3);
assert.equal(view.filter(p=>p.shared).length,1);
assert.equal(view[2].members.length,2);
assert.equal(view[2].node,'two'); // Browse through an active mount.
assert.equal(view.reduce((sum,p)=>sum+p.capacity_gb,0),6743);
assert.equal(rows.length,4); // Keep per-node mounts available for VM placement.
assert.equal(pools([...rows,{...shared('two'),source:'172.16.12.106:/other'}]).length,4);
console.log('Shared export grouping and capacity checks passed');
