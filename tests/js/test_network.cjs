const assert = require('node:assert/strict');
const core = require('../../ibhics/manager/static/network-core.js');
const inventory = [{uuid:'a',name:'web',nics:[{network:'native'}],ips:['10.0.1.2']},{uuid:'b',name:'db',nics:[{network:'data'},{network:'native'}],ips:['10.0.2.8']}];
const rules = [{name:'sql',action:'allow',src:'group:apps',dst:'vm:db',proto:'tcp',ports:'5432,6000-6010',enabled:true},{name:'block-data',action:'deny',src:'any',dst:'data',proto:'any',enabled:true}];
const flow={src:'web',dst:'db',proto:'tcp',port:5432};
assert.equal(core.simulate(rules,flow,inventory,{a:'apps'}).rule,'sql');
assert.equal(core.simulate(rules,{...flow,port:6005},inventory,{a:'apps'}).allowed,true);
assert.equal(core.simulate(rules,{...flow,port:6011},inventory,{a:'apps'}).allowed,false);
assert.equal(core.simulate(rules,flow,inventory,{}).index,2);
assert.equal(core.simulate([{...rules[0],enabled:false},rules[1]],flow,inventory,{a:'apps'}).allowed,false);
assert.equal(core.simulate([{...rules[0],ports:'',port:22}],{...flow,port:22},inventory,{a:'apps'}).allowed,true);
assert.equal(core.simulate([rules[1],rules[0]],flow,inventory,{a:'apps'}).allowed,false);
assert.equal(core.simulate([],flow,inventory,{}).allowed,true);
assert.equal(core.simulate([{...rules[1],dst:'10.0.2.0/24'}],{...flow,dst:'10.0.2.8'},inventory,{}).allowed,false);
// An IP endpoint is not implicitly resolved to a VM, exactly as in the backend.
assert.equal(core.simulate([rules[0]],{...flow,dst:'10.0.2.8'},inventory,{a:'apps'}).rule,'');
assert.throws(()=>core.simulate([], {...flow,src:'missing'},inventory,{}),/No VM/);
assert.equal(core.ipNumber('010.0.0.1'),null);
assert.equal(core.inCIDR('255.255.255.255','0.0.0.0/0'),true);
assert.equal(core.inCIDR('10.0.0.3','10.0.0.3/32'),true);
assert.equal(core.inCIDR('10.0.0.4','10.0.0.3/32'),false);
assert.equal(core.inCIDR('10.0.0.4','10.0.0.3/33'),false);
assert.throws(()=>core.ports('80,90-20'));
assert.throws(()=>core.ports('65536'));
for (const count of [5,20]) {
  const points=Array.from({length:count},(_,i)=>({id:i,x:160+(i%5)*145,y:180+Math.floor(i/5)*90,w:130,h:26}));
  const obstacles=points.map(p=>({x:p.x-8,y:p.y-8,w:16,h:16}));
  const placed=core.placeLabels(points,1100,650,obstacles).filter(p=>p.box);
  assert.equal(placed.length,count);
  placed.forEach((p,i)=>{assert(!obstacles.some(o=>core.overlaps(p.box,o)));placed.slice(i+1).forEach(q=>assert(!core.overlaps(p.box,q.box)));});
}
const crowded=core.placeLabels(Array.from({length:20},()=>({x:100,y:100,w:160,h:26})),250,200);
assert(crowded.some(p=>!p.box));
console.log('Network simulation and 5–20 VM label collision checks passed');
