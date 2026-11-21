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
// only WebGL and DOM APIs. This catches crowded labels in the actual single floor,
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
    closest() { return null; }
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
    const vms=Array.from({length:vmCount},(_,i)=>({uuid:'vm-'+i,name:['win-app','linux-db','worker'][i%3]+'-'+i,state:i%5===4?'stopped':'running',os_type:['windows','linux','other'][i%3],node:i%2?'node-a':'node-b',nics:i===0?[{network:networks[0].name,mac:'52:54:00:00:00:01'},{network:networks[1].name,mac:'52:54:00:00:00:02'}]:[{network:networks[i%vlanCount].name}],ips:['10.0.'+(i%vlanCount)+'.'+(i+10)]}));
    vms[0].name='db-production-service-123456789';vms[0].ips.push('10.0.11.160');
    const data={key:'test',networks,vms,stats:Object.fromEntries(vms.map(v=>[v.uuid,{net_rx_bps:150000,net_tx_bps:30000}])),groups:{members:{}},rules:[{src:'any',dst:'default',action:'deny',enabled:true}]};
    const doc={querySelector:$,createElement:()=>new Element(),createElementNS:()=>new Element(),createTextNode:text=>({textContent:text}),addEventListener(){},hidden:false};
    const liveVMs=structuredClone(vms);
    const window={NetworkCore:core,NetworkUI:{getData:()=>data,selectVM:id=>{window.selected=id;},
      stageNic:(uuid,index,network)=>{data.vms=core.stageNicMove(data.vms,uuid,index,network,data.networks);data.pending=[uuid];},
      openRule:(src,dst)=>{window.ruleDraft={src,dst};}
    }};
    const storage=new Map(),localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)};
    let pending=0;
    const motion={matches:true,addEventListener(){}};
    const api=new Function('THREE','OrbitControls','window','document','matchMedia','ResizeObserver','MutationObserver','devicePixelRatio','localStorage','requestAnimationFrame','cancelAnimationFrame',source+'\nreturn {draw,layout,setView,finishDrag,selectVM,up,move,down, snapshot:()=>({drag,nodes,labels,beams,particles,renderer,camera,controls,scans,lamps,zones,fitPoints}),setDrag:d=>drag=d};')({...THREE,WebGLRenderer:Renderer},Controls,window,doc,()=>motion,class {observe(){}},class {observe(){}},2,localStorage,()=>++pending,()=>{pending=0;});
    api.draw(1);
    let snapshot=api.snapshot();
    assert.equal(snapshot.nodes.filter(n=>!n.userData.ghost).length,vmCount);
    assert.equal(snapshot.nodes.filter(n=>n.userData.ghost).length,1,'secondary NIC has a ghost');
    assert.equal(snapshot.renderer.ratio,1.5);
    assert.equal(snapshot.controls.autoRotate,false);
    const primary=snapshot.labels.find(l=>l.uuid===vms[0].uuid&&l.kind==='vm');
    assert.equal(Array.from(primary.el.children[0].textContent).length,24);
    assert.equal(primary.el.children[1].textContent,vms[0].ips[0]);
    assert(primary.el.title.includes(vms[0].name)&&primary.el.title.includes(vms[0].ips[1]),'tooltip preserves full details');
    assert(snapshot.labels.some(l=>l.el.textContent==='Trunk switch (uplink)'));
    assert(snapshot.labels.some(l=>l.el.textContent==='Internet / external router'));
    const at=snapshot.nodes[0].position.clone().add(new THREE.Vector3(0,1.5,0)).project(snapshot.camera),event={button:0,clientX:(at.x+1)*1114/2,clientY:(1-at.y)*700/2,stopImmediatePropagation(){this.stopped=true;}};
    api.down(event);assert.equal(api.snapshot().drag,null,'view mode cannot start a VM drag');assert(event.stopped,'VM inspection cannot orbit the scene');
    api.move(event);assert.equal(snapshot.renderer.domElement.style.cursor,'');
    const guide=$('#nw-three').querySelector('.nw-edit-guide');assert(guide.hidden);
    assert.equal($('.nw-view-tools').children.filter(c=>c.className==='nw-tool-group').length,2);
    assert(snapshot.camera.isOrthographicCamera);
    const assertFit=()=>{
      const snap=api.snapshot(),w=$('#nw-three').clientWidth,h=$('#nw-three').clientHeight;
      const ps=snap.fitPoints.map(p=>p.clone().project(snap.camera));
      ps.forEach(v=>assert(Math.abs(v.x)<=.94&&Math.abs(v.y)<=.94,'all projected meshes fit'));
      const xs=ps.map(p=>(p.x+1)*w/2),ys=ps.map(p=>(1-p.y)*h/2);
      for(const l of snap.labels.filter(l=>!l.el.hidden)){const x=parseFloat(l.el.style.left),y=parseFloat(l.el.style.top);xs.push(x,x+parseFloat(l.el.style.width));ys.push(y,y+(l.kind==='vm'?38:26));}
      const left=Math.min(...xs),right=Math.max(...xs),top=Math.min(...ys),bottom=Math.max(...ys);
      assert(Math.max((right-left)/w,(bottom-top)/h)>=.85,'scene uses at least 85% of the limiting dimension');
      assert(Math.max((right-left)/w,(bottom-top)/h)<=.90,'meshes and labels stay near the 88% fit target');
      assert(Math.abs((left+right)/2-w/2)<w*.03&&Math.abs((top+bottom)/2-h/2)<h*.03,'combined projection is centered');
    };
    assertFit();

    const vmLabels=snapshot.labels.filter(l=>l.kind==='vm');
    assert.equal(vmLabels.filter(l=>!l.el.hidden).length,vmCount,`${vlanCount} VLAN / ${vmCount} VM labels visible`);
    const boxes=snapshot.labels.filter(l=>!l.el.hidden).map(l=>({x:parseFloat(l.el.style.left),y:parseFloat(l.el.style.top),w:parseFloat(l.el.style.width),h:l.kind==='vm'?38:26}));
    boxes.forEach((b,i)=>{assert(b.x>=0&&b.y>=0&&b.x+b.w<=1114&&b.y+b.h<=700);boxes.slice(i+1).forEach(c=>assert(!core.overlaps(b,c)));});
    snapshot.beams.forEach(b=>b.curve.getPoints(32).forEach(p=>assert([p.x,p.y,p.z].every(Number.isFinite))));
    assert(snapshot.particles.length>0);
    const beforeIdle=pending;api.draw(1.1);assert.equal(pending,beforeIdle,'reduced motion does not enqueue a continuous render loop');
    api.setView('top');api.draw(2);assert.equal(snapshot.controls.enableRotate,false);assertFit();
    api.setView('3d');api.draw(3);assert.equal(snapshot.controls.enableRotate,true);assertFit();
    $('#nw-three').clientWidth=760;$('#nw-three').clientHeight=560;api.draw(3.1);assertFit();
    $('#nw-three').clientWidth=1114;$('#nw-three').clientHeight=700;api.draw(3.2);assertFit();
    $('#nw-stopped').checked=false;$('#nw-stopped').onchange();api.draw(4);snapshot=api.snapshot();assert.equal(snapshot.nodes.filter(n=>!n.userData.ghost).length,vms.filter(v=>v.state==='running').length);
    const cameraBefore=api.snapshot().camera.position.clone(),targetBefore=api.snapshot().controls.target.clone();
    motion.matches=false;const fixedNodes=api.snapshot().nodes.map(n=>n.position.clone()),fixedLamps=api.snapshot().lamps.map(n=>n.visible);
    api.draw(1000);api.draw(2000);
    assert(api.snapshot().camera.position.equals(cameraBefore));assert(api.snapshot().controls.target.equals(targetBefore));
    api.snapshot().nodes.forEach((n,i)=>assert(n.position.equals(fixedNodes[i])));assert.deepEqual(api.snapshot().lamps.map(n=>n.visible),fixedLamps);
    data.stats={};window.Network3D.update();api.draw(5);assert(api.snapshot().camera.position.equals(cameraBefore),'stats refresh preserves the view');assert.equal(api.snapshot().particles.length,0,'unknown traffic stays still');
    api.selectVM(vms[0].uuid);assert.equal(window.selected,vms[0].uuid);
    const node=api.snapshot().nodes[0],original=node.position.clone();node.position.x+=.5;api.setDrag({node,moved:true,original});api.finishDrag();
    assert.equal(JSON.parse(storage.get('test:hologram')).positions[node.userData.locationKey].x,node.position.x-node.userData.origin.x);
    // Exercise production drop handlers. A single-NIC move stages only that NIC;
    // no API is used, and the unchanged inventory remains the conflict baseline.
    data.editing=true;window.Network3D.update();api.draw(6);
    snapshot=api.snapshot();assert.equal(guide.hidden,false,'edit instructions appear only in edit mode');
    guide.children[1].onclick();assert(guide.hidden,'edit instructions can be dismissed');
    assert.equal(snapshot.labels.filter(l=>l.kind==='shield').length,1,'one firewall shield');
    assert(snapshot.nodes.every(n=>n.position.y===.76),'all VMs stand on one floor');
    const movedNode=snapshot.nodes.find(n=>n.userData.vm.nics.length===1),oldVM=structuredClone(movedNode.userData.vm);
    const other=snapshot.zones.findIndex((z,i)=>networks[i].name!==movedNode.userData.net),zone=snapshot.zones[other];
    const start=movedNode.position.clone();movedNode.position.set(zone.x,.76,zone.z);
    api.setDrag({node:movedNode,moved:true,original:start});api.up({});api.draw(7);
    assert.equal(data.vms.find(v=>v.uuid===oldVM.uuid).nics[0].network,networks[other].name);
    assert.equal(liveVMs.find(v=>v.uuid===oldVM.uuid).nics[0].network,oldVM.nics[0].network,'live state untouched');
    assert(api.snapshot().labels.find(l=>l.uuid===oldVM.uuid).el.classes.has('pending'));
    assert(api.snapshot().beams.some(b=>b.line?.material.isLineDashedMaterial),'staged cables dashed');
    // Shift-drag to a VM opens the shared rule popup with the exact endpoints.
    snapshot=api.snapshot();const sourceNode=snapshot.nodes[0],targetNode=snapshot.nodes.find(n=>n.userData.vm.uuid!==sourceNode.userData.vm.uuid);
    const world=targetNode.position.clone().add(new THREE.Vector3(0,1.5,0)),screen=world.project(snapshot.camera);
    api.setDrag({node:sourceNode,moved:true,rule:true,original:sourceNode.position.clone()});
    api.up({clientX:(screen.x+1)*1114/2,clientY:(1-screen.y)*700/2});
    assert.deepEqual(window.ruleDraft,{src:'vm:'+sourceNode.userData.vm.name,dst:'vm:'+targetNode.userData.vm.name});
    // A multi-NIC drop must ask which adapter before mutating the draft.
    snapshot=api.snapshot();const multiNode=snapshot.nodes.find(n=>n.userData.vm.nics.length===2),multiBefore=structuredClone(multiNode.userData.vm.nics);
    const otherZone=snapshot.zones.findIndex((z,i)=>networks[i].name!==multiNode.userData.net),drop=snapshot.zones[otherZone];
    const multiStart=multiNode.position.clone();multiNode.position.set(drop.x,.76,drop.z);
    api.setDrag({node:multiNode,moved:true,original:multiStart});api.up({});
    assert.deepEqual(data.vms.find(v=>v.uuid===multiNode.userData.vm.uuid).nics,multiBefore);
    const choice=$('#nw-vm-panel').children.find(c=>c.className==='nw-nic-choice');assert(choice,'NIC chooser remains visible after drop');
    choice.children[1].onclick();
    const afterMulti=data.vms.find(v=>v.uuid===multiNode.userData.vm.uuid).nics;
    assert.equal(afterMulti[0].network,networks[otherZone].name);assert.equal(afterMulti[0].mac,multiBefore[0].mac);assert.deepEqual(afterMulti[1],multiBefore[1]);
    // Cancelling a gesture never changes the staged adapter or stored position.
    const cancelled=api.snapshot().nodes[0],before=cancelled.position.clone(),draftBefore=JSON.stringify(data.vms);
    cancelled.position.x+=10;api.setDrag({node:cancelled,moved:true,original:before});api.finishDrag(false);
    assert(cancelled.position.equals(before));assert.equal(JSON.stringify(data.vms),draftBefore);
    data.networks.push({name:'empty',vlan:99});window.Network3D.update();api.draw(8);
    const emptyLabel=api.snapshot().labels.find(l=>l.kind==='empty'&&l.el.textContent==='No VMs');assert(emptyLabel);assert.equal(emptyLabel.el.textContent,'No VMs');assert.equal(emptyLabel.el.hidden,false);
    const emptyZone=api.snapshot().zones.at(-1);assert.equal(emptyLabel.position.x,emptyZone.x);assert.equal(emptyLabel.position.z,emptyZone.z);
    doc.hidden=true;const renders=api.snapshot().renderer.renders;api.draw(6);assert.equal(api.snapshot().renderer.renders,renders);
  }
  for(const vlanCount of [0,1,2,3,4,7]) {
    const networks=Array.from({length:vlanCount},(_,i)=>({name:'net-'+i}));
    const vms=Array.from({length:20},(_,i)=>({uuid:String(i),state:'running',nics:[{network:'net-'+(i%Math.max(1,vlanCount))}]}));
    const layers=core.dioramaLayout(networks,vms);
    assert.equal(layers.length,vlanCount);
    layers.forEach(l=>l.slots.forEach(p=>assert.deepEqual(core.clampSlot(p,l),{x:p.x,z:p.z})));
    assert(layers.every(l=>l.y===0),'all VLAN counts use a single floor');
    layers.forEach((l,i)=>layers.slice(i+1).forEach(q=>assert(Math.abs(l.x-q.x)>=(l.w+q.w)/2 || Math.abs(l.z-q.z)>=(l.d+q.d)/2,'zones do not overlap')));
    const slots=layers.flatMap(l=>l.slots.map(p=>({x:l.x+p.x,z:l.z+p.z})));
    slots.forEach((p,i)=>slots.slice(i+1).forEach(q=>assert(Math.hypot(p.x-q.x,p.z-q.z)>=5.3,'enlarged VMs do not overlap')));
  }
  const multi=[{uuid:'multi',name:'multi',state:'running',nics:[{network:'a',mac:'first'},{network:'b',mac:'second'}]}],nets=[{name:'a'},{name:'b'}];
  const multiSlots=core.dioramaLayout(nets,multi).flatMap(z=>z.slots);
  assert.equal(multiSlots.filter(s=>!s.ghost).length,1,'one primary model');assert.equal(multiSlots.filter(s=>s.ghost).length,1,'one secondary ghost');
  const duplicate=[{...multi[0],nics:[...multi[0].nics,{network:'b'}]}];assert.equal(core.dioramaLayout(nets,duplicate)[1].slots.length,1,'duplicate NICs in a VLAN share one ghost');
  const staged=core.stageNicMove(multi,'multi',1,'a',nets);
  assert.deepEqual(staged[0].nics,[{network:'a',mac:'first'},{network:'a',mac:'second'}]);
  assert.equal(multi[0].nics[1].network,'b');
  assert.throws(()=>core.stageNicMove(multi,'multi',2,'a',nets));
  assert.throws(()=>core.stageNicMove(multi,'multi',0,'missing',nets));
  assert.deepEqual(core.draftRuleFromDrag(multi[0],'b'),{src:'vm:multi',dst:'b'});
  assert.deepEqual(core.draftRuleFromDrag(multi[0],'any'),{src:'vm:multi',dst:'any'});
  assert.equal(core.draftRuleFromDrag(multi[0],'vm:multi'),null);
  console.log('Production diorama: 2 VLAN / 5 VM and 3 VLAN / 20 VM single-floor geometry, labels, staged single/multi-NIC drops, policy drag, views, traffic, persistence and idle checks passed');
}
checkDiorama().catch(e=>{console.error(e);process.exitCode=1;});

// Run the production adapter with local transaction mocks: Save must use the
// existing 2D editor, and policy gestures must open the existing draft dialog.
async function checkSharedEditor() {
  const fs=require('node:fs'),source=fs.readFileSync(require.resolve('../../ibhics/manager/static/app.js'),'utf8');
  const adapter=source.slice(source.indexOf('window.NetworkUI={'),source.indexOf('// Terminal (v0.8)'));
  const live={networks:[{name:'a'},{name:'b'}],vms:[{uuid:'one',name:'one',nics:[{network:'a',mac:'identity'}]}],guests:{},stats:{},groups:{members:{}}};
  const ntopo={edit:null},nw={draft:null},calls=[];
  let allowed=true;
  const window={Network3D:{update:()=>calls.push('update')}};
  const elements={'#nw-apply':{click:()=>calls.push('safe apply button')}};
  const topoStartEdit=()=>{if(allowed)ntopo.edit={base:structuredClone(live),vms:structuredClone(live.vms),networks:structuredClone(live.networks)};};
  const topoSaveEdit=async()=>{calls.push('2D Save');live.vms=structuredClone(ntopo.edit.vms);ntopo.edit=null;};
  const topoPlan=(_base,draft)=>draft.vms.filter((v,i)=>JSON.stringify(v.nics)!==JSON.stringify(live.vms[i].nics)).map(v=>({type:'patch',uuid:v.uuid}));
  new Function('window','state','ntopo','nw','NetworkCore','topoData','nwRules','topoLayoutKey','canOperate','topoPlan','topoStartEdit','topoSaveEdit','topoEndEdit','drawTopology','nwOpenRule','$',adapter)(
    window,live,ntopo,nw,core,()=>ntopo.edit||live,()=>[],()=> 'test',()=>allowed,topoPlan,topoStartEdit,topoSaveEdit,()=>{ntopo.edit=null;},()=>calls.push('draw'),(src,dst)=>{nw.draft={src,dst};},s=>elements[s]);
  const ui=window.NetworkUI;
  ui.beginEdit();ui.stageNic('one',0,'b');
  assert.equal(live.vms[0].nics[0].network,'a');
  assert.equal(ui.getData().vms[0].nics[0].network,'b');assert.deepEqual(ui.getData().pending,['one']);
  ui.discardEdit();assert.equal(ui.getData().vms[0].nics[0].network,'a');
  ui.beginEdit();ui.stageNic('one',0,'b');await ui.saveEdit();
  assert.equal(live.vms[0].nics[0].network,'b');assert(calls.includes('2D Save'));
  ui.openRule('vm:one','any');assert.deepEqual(nw.draft,{src:'vm:one',dst:'any'});
  ui.applyRules();assert(calls.includes('safe apply button'));
  ui.beginEdit();allowed=false;ui.stageNic('one',0,'a');assert.equal(ui.getData().vms[0].nics[0].network,'b','viewers cannot stage');
  allowed=true;ntopo.edit.saving=true;ui.stageNic('one',0,'a');ui.discardEdit();assert(ntopo.edit,'saving draft cannot be discarded');
  assert.equal(ui.getData().vms[0].nics[0].network,'b');
  console.log('Shared 2D NIC draft: staging, discard, Save delegation, permissions and policy safe-apply routing passed');
}
checkSharedEditor().catch(e=>{console.error(e);process.exitCode=1;});
