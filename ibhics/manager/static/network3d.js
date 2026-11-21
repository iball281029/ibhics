/* Single floor VLAN hologram. All dependencies are vendored; no network services. */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
const $ = s => document.querySelector(s), core = window.NetworkCore;
const host = $('#nw-three'), motion = matchMedia('(prefers-reduced-motion: reduce)');
let selectedVM=null, editMode=false, zones=[], zoneNetworks=[], rulePreview=null;
let lastLabelLayout=0, lamps=[], scans=[], equipment=[], fitAspect=0, fitPoints=[], editHintDismissed=false;
const bounds=new THREE.Box3(), unitBox=new THREE.BoxGeometry(1,1,1), unitSphere=new THREE.SphereGeometry(1,12,8), edgeCache=new Map(), materialCache=new Set(), fills=new Map();
const glowCanvas=document.createElement('canvas');glowCanvas.width=glowCanvas.height=64;
const ctx=glowCanvas.getContext('2d'), gradient=ctx.createRadialGradient(32,32,0,32,32,32);
gradient.addColorStop(0,'#ffffffbb');gradient.addColorStop(.25,'#ffffff33');gradient.addColorStop(1,'#ffffff00');ctx.fillStyle=gradient;ctx.fillRect(0,0,64,64);
const glowTexture=new THREE.CanvasTexture(glowCanvas);
let renderer, scene, camera, controls, frame = 0, nodes = [], labels = [], beams = [], particles = [], key = '', prefs = {}, mode = '3d', drag = null, hover = null;
const ray = new THREE.Raycaster(), pointer = new THREE.Vector2(), plane = new THREE.Plane(), intersection = new THREE.Vector3();
const overlay = document.createElement('div'); overlay.className = 'nw-labels';
const leaders = document.createElementNS('http://www.w3.org/2000/svg','svg'); leaders.classList.add('nw-leaders');
host.append(leaders, overlay);
const palette = ['#3ef0ff','#b49bff','#ffcb66','#6effbd','#ff8fd8'];
function readPrefs(k) {try{return JSON.parse(localStorage.getItem(k+':hologram') || '{}');}catch{return {};}}
function savePrefs() {try{localStorage.setItem(key+':hologram',JSON.stringify(prefs));}catch{/* Storage may be disabled. Arrangement still works for this session. */}}
function active() {return !!renderer && !document.hidden && $('#page-network').classList.contains('active') && $('#tab-net-topology').classList.contains('active') && !$('#nw-three-wrap').hidden;}
function ensure() {
  if(renderer)return true;
  try {
    renderer=new THREE.WebGLRenderer({antialias:true,alpha:true});renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));host.prepend(renderer.domElement);
    scene=new THREE.Scene();camera=new THREE.OrthographicCamera(-20,20,20,-20,.1,1000);
    controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=false;controls.autoRotate=false;controls.minZoom=.25;controls.maxZoom=5;
    controls.addEventListener('change',wake);
    renderer.domElement.addEventListener('pointerdown',down,true);
    renderer.domElement.addEventListener('pointermove',move);
    renderer.domElement.addEventListener('pointerup',up);
    renderer.domElement.addEventListener('pointercancel',()=>{finishDrag(false);update();wake();});
    renderer.domElement.addEventListener('lostpointercapture',()=>{if(drag){finishDrag(false);update();wake();}});
    new ResizeObserver(()=>{fit();wake();}).observe(host);
    const button=document.createElement('button');button.className='nw-panel-toggle';button.textContent='Hide details';button.setAttribute('aria-expanded','true');
    button.onclick=()=>{const closed=$('#nw-three-wrap').classList.toggle('nw-panel-closed');button.textContent=closed?'Show details':'Hide details';button.setAttribute('aria-expanded',String(!closed));};host.append(button);editToolbar();return true;
  }catch(e){host.querySelector('.nw-scene-hint').textContent='3D unavailable. Use the 2D NIC editor to view and manage connections.';return false;}
}
function fill(color,opacity){const id=color+':'+opacity;if(!fills.has(id)){const m=new THREE.MeshBasicMaterial({color,transparent:opacity<1,opacity,depthWrite:opacity>=1});fills.set(id,m);materialCache.add(m);}return fills.get(id);}
function modelPart(parent,geometry,color,x,y,z,sx=1,sy=1,sz=1,opacity=.38){
  const mesh=new THREE.Mesh(geometry,fill(color,opacity));mesh.position.set(x,y,z);mesh.scale.set(sx,sy,sz);parent.add(mesh);
  if(opacity<.8){if(!edgeCache.has(geometry.uuid)&&[unitBox,unitSphere].includes(geometry))edgeCache.set(geometry.uuid,new THREE.EdgesGeometry(geometry));const edges=edgeCache.get(geometry.uuid)||new THREE.EdgesGeometry(geometry);mesh.add(new THREE.LineSegments(edges,new THREE.LineBasicMaterial({color,transparent:true,opacity:.85})));}return mesh;
}
function box(parent,color,x,y,z,w,h,d,opacity=.38){return modelPart(parent,unitBox,color,x,y,z,w,h,d,opacity);}
function halo(parent,color,x,y,z,size){const s=new THREE.Sprite(new THREE.SpriteMaterial({map:glowTexture,color,transparent:true,opacity:.4,blending:THREE.AdditiveBlending,depthWrite:false}));s.position.set(x,y,z);s.scale.setScalar(size);parent.add(s);}
function roundedPlate(w,d){
  const s=new THREE.Shape(),r=.65,x=-w/2,z=-d/2;s.moveTo(x+r,z);s.lineTo(x+w-r,z);s.quadraticCurveTo(x+w,z,x+w,z+r);s.lineTo(x+w,z+d-r);s.quadraticCurveTo(x+w,z+d,x+w-r,z+d);s.lineTo(x+r,z+d);s.quadraticCurveTo(x,z+d,x,z+d-r);s.lineTo(x,z+r);s.quadraticCurveTo(x,z,x+r,z);
  const g=new THREE.ExtrudeGeometry(s,{depth:.58,bevelEnabled:true,bevelSegments:2,steps:1,bevelSize:.1,bevelThickness:.1,curveSegments:6});g.rotateX(-Math.PI/2);return g;
}
function route(a,b){
  const z=(a.z+b.z)/2,points=[a.clone(),new THREE.Vector3(a.x,a.y,z),new THREE.Vector3(b.x,a.y,z),b.clone()],curve=new THREE.CurvePath();let previous=points[0];
  for(let i=1;i<points.length-1;i++){const at=points[i],before=at.clone().lerp(points[i-1],Math.min(.25,.35/Math.max(.001,at.distanceTo(points[i-1])))),after=at.clone().lerp(points[i+1],Math.min(.25,.35/Math.max(.001,at.distanceTo(points[i+1]))));curve.add(new THREE.LineCurve3(previous,before));curve.add(new THREE.QuadraticBezierCurve3(before,at,after));previous=after;}curve.add(new THREE.LineCurve3(previous,b));return curve;
}
function tube(curve,color,opacity=.65,radius=.055){const mesh=new THREE.Mesh(new THREE.TubeGeometry(curve,48,radius,6,false),fill(color,opacity));scene.add(mesh);return mesh;}
function cable(a,b,color,vm=null,pending=false){const curve=route(a,b);let line;if(pending){const g=new THREE.BufferGeometry().setFromPoints(curve.getPoints(80));line=new THREE.Line(g,new THREE.LineDashedMaterial({color,dashSize:.3,gapSize:.22}));line.computeLineDistances();scene.add(line);}else line=tube(curve,color,vm&&vm.state!=='running'?.16:.65);beams.push({curve,line,a,b,vm});const stats=vm&&window.NetworkUI.getData().stats[vm.uuid],speed=(stats?.net_rx_bps||0)+(stats?.net_tx_bps||0);
  if(vm?.state==='running'&&speed>0){const intensity=Math.min(1,Math.log10(1+speed)/8);for(let i=0;i<1+Math.floor(intensity*4);i++){const dot=new THREE.Mesh(unitSphere,fill('#cfffff',1));dot.scale.setScalar(.08);scene.add(dot);particles.push({mesh:dot,curve,offset:i/5,speed:.08+intensity*.25});}}
}
function rack(parent,color,x,z){equipment.push(box(parent,color,x,1.9,z,1.45,2.65,1.2,.45));box(parent,'#092b46',x,1.9,z+.62,1.23,2.38,.06,.9);for(let i=0;i<6;i++){box(parent,color,x,.92+i*.37,z+.69,1.08,.23,.1);lamps.push(box(parent,'#69ffc5',x+.39,.92+i*.37,z+.76,.09,.055,.04,1));}deviceDecal(parent,'node',x,1.9,z+.8,1.15);halo(parent,color,x,1.6,z,4);}
function switchModel(parent,color,x,z){equipment.push(box(parent,color,x,.87,z,2.5,.42,1.05,.5));for(let i=0;i<8;i++)lamps.push(box(parent,'#69ffc5',x-.95+i*.27,.87,z+.55,.14,.13,.05,1));deviceDecal(parent,'switch',x,.9,z+.59,.65);for(const dx of [-1,1])box(parent,color,x+dx,1.35,z-.36,.06,.8,.06,.8);}
// Decode our shared SVG once per kind/state into a retained CanvasTexture.
// Textures survive scene refreshes; per-model materials are disposed by update().
const deviceTextures=new Map();
function deviceTexture(kind,stopped=false,symbolOnly=false){
  const key=kind+':'+stopped+':'+symbolOnly;
  if(deviceTextures.has(key))return deviceTextures.get(key);
  const canvas=document.createElement('canvas');canvas.width=canvas.height=256;
  const texture=new THREE.CanvasTexture(canvas);texture.colorSpace=THREE.SRGBColorSpace;deviceTextures.set(key,texture);
  const image=document.createElement('img');
  image.onload=()=>{const context=canvas.getContext('2d');context.clearRect(0,0,256,256);context.drawImage(image,0,0,256,256);texture.needsUpdate=true;wake();};
  image.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(core.deviceIcon(kind,{stopped,symbolOnly}));
  return texture;
}
function deviceDecal(parent,kind,x,y,z,size,stopped=false,symbolOnly=false){
  const material=new THREE.MeshBasicMaterial({map:deviceTexture(kind,stopped,symbolOnly),transparent:true,opacity:stopped ? .5 : 1,depthWrite:false,side:THREE.DoubleSide});
  const mesh=new THREE.Mesh(new THREE.PlaneGeometry(size,size),material);mesh.position.set(x,y,z);parent.add(mesh);return mesh;
}
function labelHeight(l){return mode==='top'&&l.icon?(l.kind==='vm'||l.kind==='ghost'?100:112):l.kind==='vm'?38:26;}
function vmModel(vm,position,ghost=false){
  const g=new THREE.Group();g.position.copy(position);const stopped=vm.state!=='running',color=stopped?'#768da7':'#6dffb3',os=core.detectOS(vm);
  if(os==='windows'){
    box(g,color,0,1.05,0,1.35,.9,.18);box(g,'#061a29',0,1.05,.105,1.2,.76,.03,.95);
    box(g,color,0,.48,0,.12,.35,.12);box(g,color,0,.28,.08,.78,.12,.52);
    deviceDecal(g,os,0,1.05,.13,.96,stopped,true);
  }else if(os==='linux'){
    box(g,color,0,.95,0,1.05,1.65,.75);box(g,'#061a29',0,1.05,.4,.9,1.3,.04,.95);
    deviceDecal(g,os,0,1.05,.43,1.1,stopped,true);
  }else{
    box(g,color,0,.7,0,1.05,.95,.85);deviceDecal(g,os,0,.7,.44,.85,stopped,true);
  }
  if(!ghost)halo(g,color,0,.45,0,2.8);g.scale.setScalar(ghost ? .95 : 1.8);scene.add(g);return g;
}
function selectVM(uuid){selectedVM=uuid;$('#nw-three-wrap').classList.remove('nw-panel-closed');const button=host.querySelector('.nw-panel-toggle');button.textContent='Hide details';button.setAttribute('aria-expanded','true');window.NetworkUI.selectVM(uuid);
  const data=window.NetworkUI.getData(),vm=data.vms.find(v=>v.uuid===uuid);if(!vm)return;
  const panel=$('#nw-vm-panel');
  if(editMode){
    const title=document.createElement('h3');title.textContent='NIC editor · staged';panel.append(title);
    const hint=document.createElement('p');hint.className='hint';hint.textContent=vm.restart_needed?'Restart to apply changes. Network adapter changes take effect immediately, including while running.':'Network adapter changes take effect immediately when saved, including while running.';panel.append(hint);
    vm.nics.forEach((nic,i)=>{const label=document.createElement('label');label.textContent=`NIC ${i+1}`;const select=document.createElement('select');select.disabled=!!data.saving;data.networks.forEach(n=>{const o=document.createElement('option');o.value=n.name;o.textContent=`${n.name} · VLAN ${n.vlan||1}`;o.selected=n.name===nic.network;select.append(o);});select.onchange=()=>stageMove(uuid,i,select.value);label.append(select);panel.append(label);const mac=document.createElement('p');mac.className='hint';mac.textContent='MAC: '+(nic.mac||'assigned by server')+' (read only)';panel.append(mac);});
    if(data.editError){const error=document.createElement('p');error.className='nw-edit-error';error.textContent=data.editError;panel.append(error);}
  }
  const apply=document.createElement('button');apply.id='nw-3d-policy-apply';apply.textContent='Apply firewall draft (safe apply)';apply.disabled=data.canApplyFirewall===false;apply.onclick=()=>window.NetworkUI.applyRules?.();panel.append(apply);
}
function addLabel(text,position,kind,uuid=null,owner=null) {
  const el=document.createElement('button');el.className=`nw-label ${kind}`;el.textContent=text;el.title=text;el.setAttribute('aria-label',text);if(kind==='vm'||kind==='ghost'){if(owner)el.setAttribute('data-state',owner.userData.vm.state);const [name,...ip]=text.split(' · ');el.replaceChildren();const strong=document.createElement('span'),small=document.createElement('small');strong.textContent=name;small.textContent=ip.join(' · ')||(owner?.userData.vm.ips?.[0]||'No IP');el.append(strong,small);}
  if(owner){el.title=[owner.userData.vm.name,...(owner.userData.ghost?['Also connected to '+owner.userData.net]:[]),...(owner.userData.vm.ips||[]),...owner.userData.vm.nics.map(n=>n.network)].join(' · ');el.setAttribute('aria-label',el.title);}
  if(uuid)el.onclick=()=>selectVM(uuid);else el.disabled=true;
  let icon=null;
  const device=owner?core.detectOS(owner.userData.vm):kind==='shield'?'firewall':kind==='external'?'internet':kind==='equipment'?(text.startsWith('ibHiCS')?'node':'switch'):null;
  if(device){icon=document.createElement('span');icon.className='nw-top-device';icon.innerHTML=core.deviceIcon(device,{stopped:owner&&owner.userData.vm.state!=='running'});el.append(icon);}
  overlay.append(el);labels.push({el,position,kind,uuid,owner,icon});
}
// Project each object's bounds independently to avoid an oversized world-axis box.
function sceneFitPoints(){
  scene.updateMatrixWorld();const points=[];
  scene.traverse(o=>{
    if(o.isSprite){const scale=o.getWorldScale(new THREE.Vector3()),center=o.getWorldPosition(new THREE.Vector3());for(const x of [-.5,.5])for(const y of [-.5,.5])points.push(new THREE.Vector3(x*scale.x,y*scale.y,0).applyQuaternion(camera.quaternion).add(center));}
    else if(o.geometry&&(o.isMesh||o.isLine)){o.geometry.computeBoundingBox();const b=o.geometry.boundingBox;if(!b||b.isEmpty())return;for(const x of [b.min.x,b.max.x])for(const y of [b.min.y,b.max.y])for(const z of [b.min.z,b.max.z])points.push(new THREE.Vector3(x,y,z).applyMatrix4(o.matrixWorld));}
  });return points;
}
function fit(){
  if(!camera)return;
  const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;
  camera.zoom=1;fitAspect=w/h;camera.updateMatrixWorld();fitPoints=sceneFitPoints();if(!fitPoints.length)return;
  const projected=fitPoints.map(p=>p.clone().applyMatrix4(camera.matrixWorldInverse));
  const xs=projected.map(p=>p.x),ys=projected.map(p=>p.y),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);
  const offset=new THREE.Vector3((minX+maxX)/2,(minY+maxY)/2,0).applyQuaternion(camera.quaternion);camera.position.add(offset);controls.target.add(offset);camera.updateMatrixWorld();
  const half=Math.max((maxY-minY)/2,(maxX-minX)/2/fitAspect)/.88;
  camera.left=-half*fitAspect;camera.right=half*fitAspect;camera.top=half;camera.bottom=-half;camera.updateProjectionMatrix();
  // Include measured HTML label rectangles, then center the combined projection.
  for(let pass=0;pass<3;pass++){
    layout(w,h);
    const ps=fitPoints.map(p=>project(p,w,h)),boxes=labels.filter(l=>!l.el.hidden).map(l=>({x:parseFloat(l.el.style.left),y:parseFloat(l.el.style.top),w:parseFloat(l.el.style.width),h:labelHeight(l)}));
    const left=Math.min(...ps.map(p=>p.x),...boxes.map(b=>b.x)),right=Math.max(...ps.map(p=>p.x),...boxes.map(b=>b.x+b.w)),top=Math.min(...ps.map(p=>p.y),...boxes.map(b=>b.y)),bottom=Math.max(...ps.map(p=>p.y),...boxes.map(b=>b.y+b.h));
    const scale=Math.max((right-left)/(w*.88),(bottom-top)/(h*.88));
    const shift=new THREE.Vector3(((left+right)/2-w/2)*(camera.right-camera.left)/w,(h/2-(top+bottom)/2)*(camera.top-camera.bottom)/h,0).applyQuaternion(camera.quaternion);
    camera.position.add(shift);controls.target.add(shift);camera.updateMatrixWorld();camera.left*=scale;camera.right*=scale;camera.top*=scale;camera.bottom*=scale;camera.updateProjectionMatrix();
  }
  lastLabelLayout=0;
}
function reset(){if(!camera)return;const center=bounds.isEmpty()?new THREE.Vector3():bounds.getCenter(new THREE.Vector3());camera.up.set(0,1,0);camera.zoom=1;controls.target.copy(center);camera.position.copy(center).add(mode==='top'?new THREE.Vector3(0,100,.001):new THREE.Vector3(58,58,58));controls.enableRotate=mode!=='top';if(mode==='top')camera.up.set(0,0,-1);controls.update();fit();wake();}
function colors() {
  const box=$('#nw-layer-colors');if(!box || !window.NetworkUI)return;
  box.replaceChildren();const title=document.createElement('h3');title.textContent='Zone colors';box.append(title);
  window.NetworkUI.getData().networks.forEach((n,i)=>{const label=document.createElement('label');label.textContent=n.name;const input=document.createElement('input');input.type='color';input.value=prefs.colors?.[n.name] || palette[i%palette.length];input.oninput=()=>{(prefs.colors ||= {})[n.name]=input.value;savePrefs();update();};label.append(input);box.append(label);});
}
function update() {
  if(!window.NetworkUI || !ensure())return;
  const data=window.NetworkUI.getData();if(key!==data.key){key=data.key;prefs=readPrefs(key);update.signature='';}
  if(drag)return;
  const shared=new Set([unitBox,unitSphere,...edgeCache.values()]),disposed=new Set();
  scene.traverse(o=>{if(o.geometry&&!shared.has(o.geometry)&&!disposed.has(o.geometry)){o.geometry.dispose();disposed.add(o.geometry);}if(o.material&&!materialCache.has(o.material)){o.material.dispose();}});scene.clear();materialCache.forEach(m=>m.dispose());materialCache.clear();fills.clear();
  overlay.replaceChildren();leaders.replaceChildren();nodes=[];labels=[];beams=[];particles=[];lamps=[];scans=[];equipment=[];fitPoints=[];bounds.makeEmpty();
  const networks=[...data.networks].sort((a,b)=>(a.name==='default'?-1:b.name==='default'?1:(a.vlan||1)-(b.vlan||1)));
  const positions=new Map(), layerMap=new Map(), layers=core.dioramaLayout(networks,data.vms,$('#nw-stopped').checked);
  zones=layers;zoneNetworks=networks;
  const columns=Math.max(1,Math.ceil(Math.sqrt(networks.length))), split=Math.floor(columns/2);
  const left=layers.filter((_,i)=>i%columns<split),right=layers.filter((_,i)=>i%columns===split);
  const serviceX=left.length?(Math.max(...left.map(l=>l.x+l.w/2))+Math.min(...right.map(l=>l.x-l.w/2)))/2:layers.length?layers[0].x-layers[0].w/2-4.5:0;
  const names=[...new Set([...networks.flatMap(n=>n.nodes||[n.node].filter(Boolean)),...data.vms.map(v=>v.node).filter(Boolean)])];
  const minX=Math.min(serviceX-5,...layers.map(l=>l.x-l.w/2))-2,maxX=Math.max(serviceX+5,...layers.map(l=>l.x+l.w/2))+2;
  const minZ=Math.min(-7,-2-Math.max(0,names.length-1)*3.4-2,...layers.map(l=>l.z-l.d/2))-2,maxZ=Math.max(5,...layers.map(l=>l.z+l.d/2))+2;
  if(layers.length){
    const base=new THREE.Group();base.position.set((minX+maxX)/2,0,(minZ+maxZ)/2);scene.add(base);
    modelPart(base,roundedPlate(maxX-minX,maxZ-minZ),'#46dfff',0,0,0,1,1,1,.18);
    bounds.set(new THREE.Vector3(minX,-.7,minZ),new THREE.Vector3(maxX,6,maxZ));
  }
  const trunk=new THREE.Vector3(serviceX,.79,1.5);
  switchModel(scene,'#3ef0ff',serviceX,1.5);addLabel('Trunk switch (uplink)',trunk.clone().add(new THREE.Vector3(0,1,0)),'equipment');
  names.forEach((name,j)=>{const z=-2-j*3.4;rack(scene,'#3ef0ff',serviceX,z);addLabel('ibHiCS node: '+name,new THREE.Vector3(serviceX,3.7,z),'equipment');cable(new THREE.Vector3(serviceX,.79,z),trunk,'#3ef0ff');bounds.expandByPoint(new THREE.Vector3(serviceX,4,z-2));});
  layers.forEach((layer,i)=>{
    const net=networks[i],color=prefs.colors?.[net.name]||palette[i%palette.length],origin=new THREE.Vector3(layer.x,0,layer.z),group=new THREE.Group();group.position.copy(origin);scene.add(group);layerMap.set(net.name,origin);
    // Flat translucent zone with a rounded luminous outline on the shared slab.
    const shape=roundedPlate(layer.w,layer.d);modelPart(group,shape,color,0,.72,0,1,.015,1,.10);
    const outline=new THREE.EdgesGeometry(shape),rim=new THREE.LineSegments(outline,new THREE.LineBasicMaterial({color,transparent:true,opacity:.85}));rim.scale.y=.015;rim.position.y=.72;group.add(rim);
    const grid=new THREE.GridHelper(1,12,color,color);grid.scale.set(layer.w,1,layer.d);grid.position.y=.74;grid.material.transparent=true;grid.material.opacity=.10;group.add(grid);
    addLabel(net.name+' · VLAN '+(net.vlan||1),origin.clone().add(new THREE.Vector3(-layer.w/2+2,.8,-layer.d/2+1)),'vlan');
    if(!layer.slots.length)addLabel(data.vms.some(v=>v.nics.some(n=>n.network===net.name))?'No running VMs':'No VMs',origin.clone().setY(.8),'empty');
    layer.slots.forEach(slot=>{
      const vm=data.vms.find(v=>v.uuid===slot.uuid),locationKey=vm.uuid+':'+net.name,local=core.clampSlot(prefs.positions?.[locationKey]||slot,layer),position=origin.clone().add(new THREE.Vector3(local.x,.76,local.z)),model=vmModel(vm,position,slot.ghost);
      model.userData={vm,net:net.name,locationKey,layer,origin,ghost:slot.ghost};nodes.push(model);if(!slot.ghost)positions.set(vm.uuid,[position]);
      const pending=data.pending?.includes(vm.uuid);
      const name=Array.from(vm.name),short=name.length>24?name.slice(0,23).join('')+'…':vm.name;
      addLabel(slot.ghost?(name.length>12?name.slice(0,11).join('')+'…':vm.name)+' ↗':short+' · '+(vm.ips?.[0]||'No IP'),position,slot.ghost?'ghost':'vm',vm.uuid,model);
      if(pending)labels.at(-1).el.classList.add('pending');
      cable(position.clone().setY(.81),trunk,color,vm,pending);
      if(slot.ghost){
        const primary=layers[networks.findIndex(n=>n.name===vm.nics[0]?.network)],primarySlot=primary?.slots.find(s=>s.uuid===vm.uuid);
        if(primarySlot){const local=core.clampSlot(prefs.positions?.[vm.uuid+':'+vm.nics[0].network]||primarySlot,primary);cable(new THREE.Vector3(primary.x+local.x,.81,primary.z+local.z),position.clone().setY(.81),color,vm,pending);}
      }
    });
  });
  if(layers.length){
    const shield=new THREE.Shape();shield.moveTo(-.65,.65);shield.lineTo(0,.9);shield.lineTo(.65,.65);shield.lineTo(.55,-.2);shield.quadraticCurveTo(.3,-.65,0,-.9);shield.quadraticCurveTo(-.3,-.65,-.55,-.2);shield.closePath();
    const mesh=modelPart(scene,new THREE.ExtrudeGeometry(shield,{depth:.12,bevelEnabled:false}),'#b49bff',serviceX,4,1.5);mesh.rotation.y=Math.PI/4;equipment.push(mesh);const badge=deviceDecal(scene,'firewall',serviceX,4,1.62,1.5);badge.rotation.y=Math.PI/4;
    addLabel('Firewall · '+data.rules.length+' rules',new THREE.Vector3(serviceX,5.2,1.5),'shield');
    const external=new THREE.Vector3(serviceX,5,minZ+1);
    const globe=modelPart(scene,unitSphere,'#70dfff',external.x,external.y,external.z,1.3,1.3,1.3,.18);globe.userData.endpoint='any';equipment.push(globe);deviceDecal(scene,'internet',external.x,external.y,external.z+1.32,1.9);globe.add(new THREE.LineSegments(new THREE.WireframeGeometry(unitSphere),new THREE.LineBasicMaterial({color:'#70dfff',transparent:true,opacity:.4})));halo(scene,'#45e5ff',external.x,external.y,external.z,6);
    addLabel('Internet / external router',external.clone().add(new THREE.Vector3(0,1.7,0)),'external');bounds.expandByPoint(external.clone().addScalar(2));
    cable(trunk,external,'#70dfff');
    if($('#nw-lines').checked){const ends=end=>{if(end==='any')return [external];if(layerMap.has(end))return [layerMap.get(end).clone().add(new THREE.Vector3(0,2,0))];return data.vms.filter(v=>core.matches(end,{vm:v,ips:v.ips||[],networks:v.nics.map(n=>n.network)},data.groups.members)).flatMap(v=>positions.get(v.uuid)?.slice(0,1)||[]);};
      for(const rule of data.rules)for(const a of ends(rule.src))for(const b of ends(rule.dst)){if(a.distanceTo(b)<.1)continue;const from=a.clone().add(new THREE.Vector3(0,1.5,0)),to=b.clone().add(new THREE.Vector3(0,1.5,0)),mid=from.clone().lerp(to,.5);mid.y+=3;const curve=new THREE.QuadraticBezierCurve3(from,mid,to);tube(curve,rule.action==='allow'?'#6dffb3':'#ff6b7a',rule.enabled===false?.16:.6,.035);beams.push({curve,rule:true});}
    }
    const dust=new Float32Array(72*3);for(let i=0;i<72;i++){dust[i*3]=bounds.min.x+(Math.sin(i*127.1)*.5+.5)*(bounds.max.x-bounds.min.x);dust[i*3+1]=2+(i%11)*.7;dust[i*3+2]=bounds.min.z+(Math.cos(i*311.7)*.5+.5)*(bounds.max.z-bounds.min.z);}
    const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(dust,3));scene.add(new THREE.Points(geometry,new THREE.PointsMaterial({map:glowTexture,color:'#8bdfff',size:.18,transparent:true,opacity:.35,depthWrite:false,blending:THREE.AdditiveBlending})));
  }
  syncEditToolbar();if(selectedVM&&data.vms.some(v=>v.uuid===selectedVM))selectVM(selectedVM);colors();const signature=JSON.stringify(layers.map(l=>[l.x,l.y,l.z,l.w,l.d]));if(signature!==update.signature){update.signature=signature;reset();}wake();
}
function project(p,w,h){const v=p.clone().project(camera);return {x:(v.x+1)*w/2,y:(1-v.y)*h/2,visible:v.z>-1&&v.z<1};}
function layout(w,h) {
  host.setAttribute('data-view',mode);labels.forEach(l=>{l.height=labelHeight(l);l.el.style.height=l.height+'px';});
  scene.updateMatrixWorld();const obstacles=[{x:8,y:8,w:editMode?310:90,h:38},{x:w-136,y:8,w:128,h:38},{x:8,y:h-34,w:w-16,h:26},{x:10,y:h-44-(host.querySelector('.nw-scene-legend').offsetHeight||124),w:host.querySelector('.nw-scene-legend').offsetWidth||244,h:host.querySelector('.nw-scene-legend').offsetHeight||124}];
  if(editMode&&!editHintDismissed)obstacles.push({x:12,y:55,w:w-24,h:64});
  for(const n of mode==='top'?[]:[...nodes,...equipment]){const b=new THREE.Box3();n.traverse(o=>{if(o.isMesh){o.geometry.computeBoundingBox();b.union(o.geometry.boundingBox.clone().applyMatrix4(o.matrixWorld));}});const points=[];for(const x of [b.min.x,b.max.x])for(const y of [b.min.y,b.max.y])for(const z of [b.min.z,b.max.z])points.push(project(new THREE.Vector3(x,y,z),w,h));const xs=points.map(p=>p.x),ys=points.map(p=>p.y);obstacles.push({x:Math.min(...xs),y:Math.min(...ys),w:Math.max(...xs)-Math.min(...xs),h:Math.max(...ys)-Math.min(...ys)});}
  for(const b of mode==='top'?[]:beams)for(const p of b.curve.getPoints(32)){const v=project(p,w,h);if(v.visible)obstacles.push({x:v.x-2,y:v.y-2,w:4,h:4});}
  const front=new THREE.Vector3().subVectors(camera.position,controls.target);front.y=0;front.normalize().multiplyScalar(2.5);
  const points=labels.filter(l=>$('#nw-labels').checked || l.uuid===hover).map(l=>{
    const p=project(l.owner?l.owner.position.clone().add(mode==='top'?new THREE.Vector3():front):l.position,w,h);if(mode==='top'&&l.icon)p.y-=labelHeight(l)/2+12;const lines=l.kind==='vm'?[[l.el.children[0].textContent,11],[l.el.children[1].textContent,10]]:[[l.el.textContent,12]];
    const width=Math.max(mode==='top'&&l.icon?112:0,...lines.map(([text,size])=>{ctx.font=`${size}px monospace`;return (ctx.measureText?ctx.measureText(text).width:text.length*7.4)+24;}));
    return {...p,w:Math.min(w-8,Math.max(90,width)),h:labelHeight(l),l};
  }).filter(p=>p.visible);
  points.sort((a,b)=>(b.l.uuid===hover)-(a.l.uuid===hover) || (b.l.kind==='vm')-(a.l.kind==='vm'));
  labels.forEach(l=>l.el.hidden=true);leaders.replaceChildren();
  const empties=points.filter(p=>p.l.kind==='empty').map(p=>({...p,box:{x:p.x-p.w/2,y:p.y-p.h/2,w:p.w,h:p.h}}));
  const insetX=w*.06,insetY=h*.06,shift=b=>({...b,x:b.x-insetX,y:b.y-insetY});
  const placed=core.placeLabels(points.filter(p=>p.l.kind!=='empty').map(shift),w*.88,h*.88,[...obstacles,...empties.map(p=>p.box)].map(shift)).map(p=>({...p,x:p.x+insetX,y:p.y+insetY,box:p.box?{...p.box,x:p.box.x+insetX,y:p.box.y+insetY}:null}));
  for(const p of [...empties,...placed]){
    if(!p.box)continue;const {l,box}=p;l.el.hidden=false;l.el.style.width=`${box.w}px`;l.el.style.left=`${box.x}px`;l.el.style.top=`${box.y}px`;
    if(l.kind==='empty')continue;
    const line=document.createElementNS(leaders.namespaceURI,'line');line.setAttribute('x1',p.x);line.setAttribute('y1',p.y);line.setAttribute('x2',box.x+box.w/2);line.setAttribute('y2',box.y+box.h/2);leaders.append(line);
  }
}
function draw(time=0) {
  frame=0;if(!active())return;
  const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;
  if(renderer.domElement.width!==Math.round(w*renderer.getPixelRatio())||renderer.domElement.height!==Math.round(h*renderer.getPixelRatio()))renderer.setSize(w,h,false);
  if(Math.abs(fitAspect-w/h)>.001)fit();
  for(const p of particles){p.mesh.visible=$('#nw-traffic').checked;if(p.mesh.visible)p.mesh.position.copy(p.curve.getPoint(motion.matches?p.offset:(time/1000*p.speed+p.offset)%1));}
  renderer.render(scene,camera);if(!lastLabelLayout){layout(w,h);lastLabelLayout=time || 1;}
  if(!motion.matches&&$('#nw-traffic').checked&&particles.length)frame=requestAnimationFrame(draw);
}
function wake(){lastLabelLayout=0;if(active()&&!frame)frame=requestAnimationFrame(draw);else if(!active()&&frame){cancelAnimationFrame(frame);frame=0;}}
function pick(event){const r=renderer.domElement.getBoundingClientRect();pointer.set((event.clientX-r.left)/r.width*2-1,-(event.clientY-r.top)/r.height*2+1);ray.setFromCamera(pointer,camera);let hit=ray.intersectObjects(nodes,true).find(h=>h.object.isMesh);if(!hit)return;let node=hit.object;while(node&&!node.userData.vm)node=node.parent;return node;}
function editToolbar(){
  const bar=document.createElement('div');bar.className='nw-edit-tools';
  for(const [id,text,fn] of [['nw-3d-edit','Edit 3D',()=>{if(editMode){if(window.NetworkUI.getData().pending?.length&&!window.confirm('Discard staged NIC changes and exit Edit?'))return;window.NetworkUI.discardEdit?.();}else window.NetworkUI.beginEdit?.();editMode=!!window.NetworkUI.getData().editing;editHintDismissed=false;update();}],['nw-3d-save','Save',async()=>{const saving=window.NetworkUI.saveEdit?.();update();await saving;editMode=!!window.NetworkUI.getData().editing;update();}],['nw-3d-discard','Discard',()=>{window.NetworkUI.discardEdit?.();editMode=false;update();}]]){const b=document.createElement('button');b.id=id;b.textContent=text;b.onclick=fn;bar.append(b);}
  const status=document.createElement('span');status.id='nw-3d-status';status.setAttribute('role','status');bar.append(status);host.append(bar);
  sceneGuide();
  $('#nw-view-3d').textContent='3D zones';const heading=$('#nw-vm-panel').querySelector('h3');if(heading)heading.textContent='VLAN zones';
  host.querySelector('.nw-scene-hint').textContent='Click a VM for details · Drag empty space to orbit · Scroll to zoom';
}
function syncEditToolbar(){const data=window.NetworkUI.getData();editMode=!!data.editing;host.classList.toggle('nw-editing',editMode);renderer.domElement.style.cursor='';host.querySelector('.nw-edit-guide').hidden=!editMode||editHintDismissed;$('#nw-3d-status').textContent=data.editError||(editMode?`${data.pending?.length||0} VM changes pending`: '');$('#nw-3d-edit').textContent=editMode?'Exit Edit':'Edit 3D';$('#nw-3d-edit').setAttribute('aria-pressed',String(editMode));$('#nw-3d-edit').disabled=data.canEdit===false||!!data.saving;for(const id of ['nw-3d-save','nw-3d-discard']){$('#'+id).hidden=!editMode;$('#'+id).disabled=!!data.saving;}}
function sceneGuide(){
  const guide=document.createElement('div');guide.className='nw-edit-guide';guide.hidden=true;
  const text=document.createElement('span');text.textContent='Drag a VM to another zone to move its network · Shift+drag between VMs to create a firewall rule · Save when done';guide.append(text);
  const close=document.createElement('button');close.textContent='×';close.setAttribute('aria-label','Dismiss edit instructions');close.onclick=()=>{editHintDismissed=true;guide.hidden=true;wake();};guide.append(close);host.append(guide);
  const legend=document.createElement('div');legend.className='nw-scene-legend';legend.setAttribute('aria-label','Topology legend');
  // Folded to one small chip by default so it never covers the scene; click to open.
  legend.classList.add('folded');const legendToggle=document.createElement('button');legendToggle.type='button';legendToggle.className='nw-legend-toggle';legendToggle.textContent='Legend ▸';legendToggle.setAttribute('aria-expanded','false');
  legendToggle.onclick=()=>{const open=legend.classList.toggle('folded')===false;legendToggle.textContent=open?'Legend ▾':'Legend ▸';legendToggle.setAttribute('aria-expanded',String(open));wake();};legend.append(legendToggle);
  for(const [symbol,text,kind] of [['▧','Zone color = VLAN','zone'],['━ •','Pipe + moving light = traffic','traffic'],['▣','Green screen = running · gray = stopped','state'],['━ ━','Green / red = firewall allow / block','rules'],['◎','Globe = external network','external']]){const row=document.createElement('div'),icon=document.createElement('span');icon.className='nw-key-'+kind;icon.textContent=symbol;row.append(icon,document.createTextNode(text));legend.append(row);}const devices=document.createElement('div');devices.className='nw-device-legend-wrap';devices.innerHTML=core.deviceLegend();legend.append(devices);host.append(legend);
  const toolbar=$('.nw-view-tools');
  for(const [title,ids] of [['View',['nw-view-3d','nw-view-top','nw-view-edit','nw-reset']],['Show',['nw-labels','nw-traffic','nw-lines','nw-stopped']]]){const group=document.createElement('div');group.className='nw-tool-group';group.setAttribute('role','group');group.setAttribute('aria-label',title);const name=document.createElement('span');name.className='nw-tool-title';name.textContent=title;group.append(name);for(const id of ids){const el=$('#'+id);group.append(el.closest('label')||el);}toolbar.append(group);}
}
function floorPoint(e){pick(e);plane.set(new THREE.Vector3(0,1,0),-.76);return ray.ray.intersectPlane(plane,intersection);}
function targetAt(e){
  const node=pick(e);if(node)return `vm:${node.userData.vm.name}`;
  if(ray.intersectObjects(equipment,true).some(h=>h.object.userData.endpoint==='any'))return 'any';
  if(!floorPoint(e))return null;const i=core.zoneAt(zones,intersection);return i<0?null:zoneNetworks[i].name;
}
function down(e){if(e.button!==0)return;const node=pick(e);if(!node)return;e.stopImmediatePropagation?.();if(!editMode || window.NetworkUI.getData().saving)return;controls.enabled=false;drag={node,x:e.clientX,y:e.clientY,moved:false,rule:e.shiftKey,original:node.position.clone()};renderer.domElement.setPointerCapture(e.pointerId);}
function move(e){if(!drag){const node=pick(e),next=node?.userData.vm.uuid || null;renderer.domElement.style.cursor=node&&editMode?'grab':'';if(next!==hover){hover=next;wake();}return;}
  if(Math.hypot(e.clientX-drag.x,e.clientY-drag.y)<5 && !drag.moved)return;
  drag.moved=true;
  if(floorPoint(e)){
    const n=drag.node;
    if(drag.rule){if(rulePreview){scene.remove(rulePreview);rulePreview.geometry.dispose();rulePreview.material.dispose();}const g=new THREE.BufferGeometry().setFromPoints([n.position,intersection.clone()]);rulePreview=new THREE.Line(g,new THREE.LineDashedMaterial({color:'#ffcb66',dashSize:.3,gapSize:.2}));rulePreview.computeLineDistances();scene.add(rulePreview);}
    else {n.position.x=intersection.x;n.position.z=intersection.z;}
    wake();
  }
}
function finishDrag(save=true){if(!drag)return;const {node,moved,original,rule}=drag;drag=null;controls.enabled=true;
  if(rulePreview){scene.remove(rulePreview);rulePreview.geometry.dispose();rulePreview.material.dispose();rulePreview=null;}
  if(moved&&save&&!rule){(prefs.positions ||= {})[node.userData.locationKey]={x:node.position.x-node.userData.origin.x,z:node.position.z-node.userData.origin.z};savePrefs();}else if(moved)node.position.copy(original);
}
function stageMove(uuid,index,network){window.NetworkUI.stageNic?.(uuid,index,network);update();selectVM(uuid);}
function chooseNic(vm,network){
  if(vm.nics.length===1){stageMove(vm.uuid,0,network);return;}
  selectVM(vm.uuid);const panel=$('#nw-vm-panel'),box=document.createElement('div');box.className='nw-nic-choice';const title=document.createElement('p');title.textContent='Choose the NIC to move to '+network;box.append(title);
  vm.nics.forEach((nic,i)=>{const b=document.createElement('button');b.textContent=`NIC ${i+1} · ${nic.network} · ${nic.mac||'auto'}`;b.onclick=()=>stageMove(vm.uuid,i,network);box.append(b);});panel.prepend(box);
}
function up(e){if(!drag)return;const {node,moved,rule}=drag;
  if(!moved){finishDrag(false);selectVM(node.userData.vm.uuid);wake();return;}
  if(rule){const target=targetAt(e),draft=core.draftRuleFromDrag(node.userData.vm,target);finishDrag(false);if(draft)window.NetworkUI.openRule?.(draft.src,draft.dst);update();return;}
  const i=core.zoneAt(zones,node.position),network=zoneNetworks[i]?.name;
  if(network&&network!==node.userData.net){finishDrag(false);update();chooseNic(node.userData.vm,network);wake();return;}
  else if(network){const {layer,origin}=node.userData,p=core.clampSlot({x:node.position.x-origin.x,z:node.position.z-origin.z},layer);node.position.x=origin.x+p.x;node.position.z=origin.z+p.z;finishDrag();}
  else finishDrag(false);
  update();wake();
}
rendererClickSetup();
function rendererClickSetup(){document.addEventListener('keydown',e=>{if(e.key==='Escape'&&drag){finishDrag(false);update();wake();}});host.addEventListener('click',e=>{if(!editMode&&renderer&&e.target===renderer.domElement){const node=pick(e);if(node)selectVM(node.userData.vm.uuid);}});}
function setView(next){if(drag)finishDrag(false);if(ntopoEditing()&&!editMode)return;mode=next;$('#nw-legacy').hidden=next!=='edit';$('#nw-three-wrap').hidden=next==='edit';$('#nw-view-3d').setAttribute('aria-pressed',String(next==='3d'));$('#nw-view-top').setAttribute('aria-pressed',String(next==='top'));$('#nw-view-edit').setAttribute('aria-pressed',String(next==='edit'));if(next!=='edit'){ensure();update();reset();}else window.drawTopology?.();wake();}
function ntopoEditing(){if(!editMode&&$('#topo-edit-tools').hidden===false){window.alert('Save or cancel the topology draft before switching views.');return true;}return false;}
$('#nw-view-3d').onclick=()=>setView('3d');$('#nw-view-top').onclick=()=>setView('top');$('#nw-view-edit').onclick=()=>setView('edit');$('#nw-reset').onclick=reset;
for(const id of ['nw-labels','nw-traffic','nw-lines','nw-stopped'])$('#'+id).onchange=()=>{update();fit();wake();};
new MutationObserver(wake).observe($('#page-network'),{attributes:true,attributeFilter:['class']});
new MutationObserver(wake).observe($('#tab-net-topology'),{attributes:true,attributeFilter:['class']});
document.addEventListener('visibilitychange',wake);motion.addEventListener('change',wake);
window.Network3D={update,colors,dragging:()=>Boolean(drag)};
if(window.NetworkUI?.getData().networks.length)update();
