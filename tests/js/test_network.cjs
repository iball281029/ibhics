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

// Execute the production scene with real Three.js geometry/projection, replacing
// only WebGL and DOM APIs. This catches crowded labels in the actual terrace,
// rather than checking an unrelated grid of screen coordinates.
async function checkDiorama() {
  const THREE = await import('../../ibhics/manager/static/vendor/three/three.module.min.js');
  const fs = require('node:fs');
  const source = fs.readFileSync(require.resolve('../../ibhics/manager/static/network3d.js'), 'utf8').replace(/^import .*;$/gm, '');
  class Element {
    constructor() { this.children=[]; this.style={}; this.hidden=false; this.checked=true; this.clientWidth=1114; this.clientHeight=700; this.width=1114; this.height=700; this.textContent=''; this.events={}; this.attributes={}; this.classes=new Set(['active']); this.classList={contains:c=>this.classes.has(c),add:c=>this.classes.add(c),remove:c=>this.classes.delete(c),toggle:c=>this.classes.has(c)?(this.classes.delete(c),false):(this.classes.add(c),true)}; }
    append(...items) { this.children.push(...items); }
    prepend(...items) { this.children.unshift(...items); }
    replaceChildren(...items) { this.children=items; }
    setAttribute(k,v) { this.attributes[k]=v; }
    addEventListener(k,v) { this.events[k]=v; }
    querySelector(s) { return this.children.find(c=>c.className===s.slice(1)) || new Element(); }
    getContext() { return {createRadialGradient:()=>({addColorStop(){}}),fillRect(){}}; }
    getBoundingClientRect() { return {left:0,top:0,width:this.clientWidth,height:this.clientHeight}; }
    setPointerCapture() {}
  }
  class Renderer {
    constructor() { this.domElement=new Element(); this.ratio=1; this.renders=0; }
    setPixelRatio(r) {this.ratio=r;}
    getPixelRatio() {return this.ratio;}
    setSize(w,h) {this.domElement.width=w*this.ratio;this.domElement.height=h*this.ratio;}
    render(scene,camera) {scene.updateMatrixWorld();camera.updateMatrixWorld();this.renders++;}
  }
  class Controls {
    constructor(camera) {this.camera=camera;this.target=new THREE.Vector3();this.enabled=true;}
    addEventListener() {}
    update() {this.camera.lookAt(this.target);this.camera.updateMatrixWorld();}
  }
  for (const [vlanCount,vmCount] of [[2,5],[3,20]]) {
    const elements=new Map(), $=s=>{if(!elements.has(s))elements.set(s,new Element());return elements.get(s);};
    $('#topo-edit-tools').hidden=true;
    const networks=Array.from({length:vlanCount},(_,i)=>({name:i?'vlan-'+i:'default',vlan:i*10+1,nodes:['node-a','node-b']}));
    const vms=Array.from({length:vmCount},(_,i)=>({uuid:'vm-'+i,name:['win-app','linux-db','worker'][i%3]+'-'+i,state:i%5===4?'stopped':'running',os_type:['windows','linux','other'][i%3],node:i%2?'node-a':'node-b',nics:[{network:networks[i%vlanCount].name}],ips:['10.0.'+(i%vlanCount)+'.'+(i+10)]}));
    const data={key:'test',networks,vms,stats:Object.fromEntries(vms.map(v=>[v.uuid,{net_rx_bps:150000,net_tx_bps:30000}])),groups:{members:{}},rules:[{src:'any',dst:'default',action:'deny',enabled:true}]};
    const doc={querySelector:$,createElement:()=>new Element(),createElementNS:()=>new Element(),addEventListener(){},hidden:false};
    const window={NetworkCore:core,NetworkUI:{getData:()=>data,selectVM:id=>{window.selected=id;}}};
    const storage=new Map(),localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)};
    let pending=0;
    const api=new Function('THREE','OrbitControls','window','document','matchMedia','ResizeObserver','MutationObserver','devicePixelRatio','localStorage','requestAnimationFrame','cancelAnimationFrame',source+'\nreturn {draw,layout,setView,finishDrag,selectVM, snapshot:()=>({nodes,labels,beams,particles,renderer,camera,controls,scans,lamps}),setDrag:d=>drag=d};')({...THREE,WebGLRenderer:Renderer},Controls,window,doc,()=>({matches:true,addEventListener(){}}),class {observe(){}},class {observe(){}},2,localStorage,()=>++pending,()=>{pending=0;});
    api.draw(1);
    let snapshot=api.snapshot();
    assert.equal(snapshot.nodes.length,vmCount);
    assert.equal(snapshot.renderer.ratio,1.5);
    assert(snapshot.camera.isOrthographicCamera);
    const vmLabels=snapshot.labels.filter(l=>l.kind==='vm');
    assert.equal(vmLabels.filter(l=>!l.el.hidden).length,vmCount,`${vlanCount} VLAN / ${vmCount} VM labels visible`);
    const boxes=snapshot.labels.filter(l=>!l.el.hidden).map(l=>({x:parseFloat(l.el.style.left),y:parseFloat(l.el.style.top),w:parseFloat(l.el.style.width),h:l.kind==='vm'?38:26}));
    boxes.forEach((b,i)=>{assert(b.x>=0&&b.y>=0&&b.x+b.w<=1114&&b.y+b.h<=700);boxes.slice(i+1).forEach(c=>assert(!core.overlaps(b,c)));});
    snapshot.beams.forEach(b=>b.curve.getPoints(32).forEach(p=>assert([p.x,p.y,p.z].every(Number.isFinite))));
    assert(snapshot.particles.length>0);
    assert.equal(pending,1,'reduced motion does not enqueue a continuous render loop');
    api.setView('top');api.draw(2);assert.equal(snapshot.controls.enableRotate,false);
    api.setView('3d');api.draw(3);assert.equal(snapshot.controls.enableRotate,true);
    $('#nw-stopped').checked=false;$('#nw-stopped').onchange();api.draw(4);snapshot=api.snapshot();assert.equal(snapshot.nodes.length,vms.filter(v=>v.state==='running').length);
    data.stats={};window.Network3D.update();api.draw(5);assert.equal(api.snapshot().particles.length,0,'unknown traffic stays still');
    api.selectVM(vms[0].uuid);assert.equal(window.selected,vms[0].uuid);
    const node=api.snapshot().nodes[0],original=node.position.clone();node.position.x+=.5;api.setDrag({node,moved:true,original});api.finishDrag();
    assert.equal(JSON.parse(storage.get('test:hologram')).positions[node.userData.locationKey].x,node.position.x-node.userData.origin.x);
    doc.hidden=true;const renders=api.snapshot().renderer.renders;api.draw(6);assert.equal(api.snapshot().renderer.renders,renders);
  }
  for(const vlanCount of [0,1,2,3,4,7]) {
    const networks=Array.from({length:vlanCount},(_,i)=>({name:'net-'+i}));
    const vms=Array.from({length:20},(_,i)=>({uuid:String(i),state:'running',nics:[{network:'net-'+(i%Math.max(1,vlanCount))}]}));
    const layers=core.dioramaLayout(networks,vms);
    assert.equal(layers.length,vlanCount);
    layers.forEach(l=>l.slots.forEach(p=>assert.deepEqual(core.clampSlot(p,l),{x:p.x,z:p.z})));
    if(vlanCount>3)assert(layers.every(l=>l.y===0),'large fabrics use side-by-side platforms');
  }
  console.log('Production diorama: 2 VLAN / 5 VM and 3 VLAN / 20 VM geometry, labels, views, traffic, persistence and idle checks passed');
}
checkDiorama().catch(e=>{console.error(e);process.exitCode=1;});
