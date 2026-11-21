/* Single floor VLAN hologram. All dependencies are vendored; no network services. */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
const $ = s => document.querySelector(s), core = window.NetworkCore;
const host = $('#nw-three'), motion = matchMedia('(prefers-reduced-motion: reduce)');
let selectedVM=null, editMode=false, zones=[], zoneNetworks=[], rulePreview=null;
let lastLabelLayout=0, lamps=[], scans=[], equipment=[], fitAspect=0, fitPoints=[];
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
    controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=false;controls.minZoom=.25;controls.maxZoom=5;
    controls.addEventListener('change',wake);
    renderer.domElement.addEventListener('pointerdown',down);
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
function rack(parent,color,x,z){equipment.push(box(parent,color,x,1.9,z,1.45,2.65,1.2,.45));box(parent,'#092b46',x,1.9,z+.62,1.23,2.38,.06,.9);for(let i=0;i<6;i++){box(parent,color,x,.92+i*.37,z+.69,1.08,.23,.1);lamps.push(box(parent,'#69ffc5',x+.39,.92+i*.37,z+.76,.09,.055,.04,1));}halo(parent,color,x,1.6,z,4);}
function switchModel(parent,color,x,z){equipment.push(box(parent,color,x,.87,z,2.5,.42,1.05,.5));for(let i=0;i<8;i++)lamps.push(box(parent,'#69ffc5',x-.95+i*.27,.87,z+.55,.14,.13,.05,1));for(const dx of [-1,1])box(parent,color,x+dx,1.35,z-.36,.06,.8,.06,.8);}
function vmModel(vm,position){
  const g=new THREE.Group();g.position.copy(position);const color=vm.state==='running'?'#6dffb3':vm.state==='paused'?'#ffcb66':'#768da7',os=String(vm.os_type||'').toLowerCase();
  if(/windows/.test(os)){box(g,color,0,1.05,0,1.35,.9,.18);box(g,color,0,.48,0,.12,.35,.12);box(g,color,0,.28,.08,.78,.12,.52);for(const x of [-.18,.18])for(const y of [.89,1.22])box(g,'#c4fff0',x,y,.11,.29,.26,.03,1);}
  else if(/linux|ubuntu|debian|centos|fedora|rocky|rhel/.test(os)){box(g,color,0,.83,0,.75,1.3,.75);box(g,'#061a29',0,1,.4,.59,.55,.04,.95);const a=box(g,color,-.12,1.1,.44,.22,.035,.025,1),b=box(g,color,-.12,1,.44,.22,.035,.025,1);a.rotation.z=-.6;b.rotation.z=.6;box(g,color,.14,.93,.44,.16,.035,.025,1);}
  else{box(g,color,0,.7,0,1.05,.95,.85);box(g,color,0,.7,.44,.65,.12,.03,.9);}
  halo(g,color,0,.45,0,2.8);g.scale.setScalar(1.8);scene.add(g);return g;
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
  // A VM with several NICs gets one label, in its first VLAN zone.
  if(uuid && labels.some(l=>l.uuid===uuid))return;
  const el=document.createElement('button');el.className=`nw-label ${kind}`;el.textContent=text;el.title=text;el.setAttribute('aria-label',text);if(kind==='vm'){if(owner)el.setAttribute('data-state',owner.userData.vm.state);const [name,...ip]=text.split(' · ');el.replaceChildren();const strong=document.createElement('span'),small=document.createElement('small');strong.textContent=name;small.textContent=ip.join(' · ');el.append(strong,small);}
  if(uuid)el.onclick=()=>selectVM(uuid);else el.disabled=true;
  overlay.append(el);labels.push({el,position,kind,uuid,owner});
}
function fit(){
  if(!camera||bounds.isEmpty()||!fitPoints.length)return;camera.zoom=1;const aspect=Math.max(.3,host.clientWidth/Math.max(1,host.clientHeight));fitAspect=aspect;camera.updateMatrixWorld();let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  for(const point of fitPoints){const p=point.clone().applyMatrix4(camera.matrixWorldInverse);minX=Math.min(minX,p.x);maxX=Math.max(maxX,p.x);minY=Math.min(minY,p.y);maxY=Math.max(maxY,p.y);}
  const offset=new THREE.Vector3((minX+maxX)/2,(minY+maxY)/2,0).applyQuaternion(camera.quaternion);camera.position.add(offset);controls.target.add(offset);camera.updateMatrixWorld();
  const marginX=Math.min(125,host.clientWidth*.2),marginY=65;const half=Math.max((maxY-minY)/2/Math.max(.2,1-2*marginY/host.clientHeight),(maxX-minX)/2/aspect/Math.max(.2,1-2*marginX/host.clientWidth))/.85;camera.left=-half*aspect;camera.right=half*aspect;camera.top=half;camera.bottom=-half;camera.updateProjectionMatrix();
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
    for(const x of [minX,maxX])for(const z of [minZ,maxZ])for(const y of [-.7,6])fitPoints.push(new THREE.Vector3(x,y,z));
    bounds.set(new THREE.Vector3(minX,-.7,minZ),new THREE.Vector3(maxX,6,maxZ));
  }
  const trunk=new THREE.Vector3(serviceX,.79,1.5);
  switchModel(scene,'#3ef0ff',serviceX,1.5);addLabel('TRUNK / UPLINK',trunk.clone().add(new THREE.Vector3(0,1,0)),'equipment');
  names.forEach((name,j)=>{const z=-2-j*3.4;rack(scene,'#3ef0ff',serviceX,z);addLabel('ibHiCS · '+name,new THREE.Vector3(serviceX,3.7,z),'equipment');cable(new THREE.Vector3(serviceX,.79,z),trunk,'#3ef0ff');bounds.expandByPoint(new THREE.Vector3(serviceX,4,z-2));fitPoints.push(new THREE.Vector3(serviceX,4,z-2));});
  layers.forEach((layer,i)=>{
    const net=networks[i],color=prefs.colors?.[net.name]||palette[i%palette.length],origin=new THREE.Vector3(layer.x,0,layer.z),group=new THREE.Group();group.position.copy(origin);scene.add(group);layerMap.set(net.name,origin);
    // Flat translucent zone with a rounded luminous outline on the shared slab.
    const shape=roundedPlate(layer.w,layer.d);modelPart(group,shape,color,0,.72,0,1,.015,1,.10);
    const outline=new THREE.EdgesGeometry(shape),rim=new THREE.LineSegments(outline,new THREE.LineBasicMaterial({color,transparent:true,opacity:.85}));rim.scale.y=.015;rim.position.y=.72;group.add(rim);
    const grid=new THREE.GridHelper(1,12,color,color);grid.scale.set(layer.w,1,layer.d);grid.position.y=.74;grid.material.transparent=true;grid.material.opacity=.10;group.add(grid);
    addLabel(net.name+' · VLAN '+(net.vlan||1),origin.clone().add(new THREE.Vector3(-layer.w/2+2,.8,-layer.d/2+1)),'vlan');
    layer.slots.forEach(slot=>{
      const vm=data.vms.find(v=>v.uuid===slot.uuid),locationKey=vm.uuid+':'+net.name,local=core.clampSlot(prefs.positions?.[locationKey]||slot,layer),position=origin.clone().add(new THREE.Vector3(local.x,.76,local.z)),model=vmModel(vm,position);
      model.userData={vm,net:net.name,locationKey,layer,origin};nodes.push(model);positions.set(vm.uuid,[position]);
      const pending=data.pending?.includes(vm.uuid);
      addLabel(vm.name+' · '+(vm.ips?.join(', ')||'No IP')+(pending?' · pending':''),position,'vm',vm.uuid,model);
      if(pending)labels.at(-1).el.classList.add('pending');
      for(const nic of vm.nics){
        const ni=networks.findIndex(n=>n.name===nic.network),target=layers[ni];if(!target)continue;
        const nc=prefs.colors?.[nic.network]||palette[ni%palette.length];
        if(nic.network!==net.name)cable(position.clone().setY(.81),new THREE.Vector3(target.x,.81,target.z),nc,vm,pending);
        cable(nic.network===net.name?position.clone().setY(.81):new THREE.Vector3(target.x,.81,target.z),trunk,nc,vm,pending);
      }
    });
  });
  if(layers.length){
    const shield=new THREE.Shape();shield.moveTo(-.65,.65);shield.lineTo(0,.9);shield.lineTo(.65,.65);shield.lineTo(.55,-.2);shield.quadraticCurveTo(.3,-.65,0,-.9);shield.quadraticCurveTo(-.3,-.65,-.55,-.2);shield.closePath();
    const mesh=modelPart(scene,new THREE.ExtrudeGeometry(shield,{depth:.12,bevelEnabled:false}),'#b49bff',serviceX,4,1.5);mesh.rotation.y=Math.PI/4;equipment.push(mesh);
    addLabel('⬡ '+data.rules.length+' rules',new THREE.Vector3(serviceX,5.2,1.5),'shield');
    const external=new THREE.Vector3(serviceX,5,minZ+1);
    const globe=modelPart(scene,unitSphere,'#70dfff',external.x,external.y,external.z,1.3,1.3,1.3,.18);globe.userData.endpoint='any';equipment.push(globe);globe.add(new THREE.LineSegments(new THREE.WireframeGeometry(unitSphere),new THREE.LineBasicMaterial({color:'#70dfff',transparent:true,opacity:.4})));halo(scene,'#45e5ff',external.x,external.y,external.z,6);
    addLabel('External L3 / Internet',external.clone().add(new THREE.Vector3(0,1.7,0)),'external');bounds.expandByPoint(external.clone().addScalar(2));for(const x of [-2,2])for(const y of [-2,2])for(const z of [-2,2])fitPoints.push(external.clone().add(new THREE.Vector3(x,y,z)));
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
  scene.updateMatrixWorld();const obstacles=[{x:8,y:8,w:editMode?310:90,h:38},{x:w-136,y:8,w:128,h:38},{x:8,y:h-34,w:w-16,h:26}];
  for(const n of [...nodes,...equipment]){const b=new THREE.Box3();n.traverse(o=>{if(o.isMesh){o.geometry.computeBoundingBox();b.union(o.geometry.boundingBox.clone().applyMatrix4(o.matrixWorld));}});const points=[];for(const x of [b.min.x,b.max.x])for(const y of [b.min.y,b.max.y])for(const z of [b.min.z,b.max.z])points.push(project(new THREE.Vector3(x,y,z),w,h));const xs=points.map(p=>p.x),ys=points.map(p=>p.y);obstacles.push({x:Math.min(...xs),y:Math.min(...ys),w:Math.max(...xs)-Math.min(...xs),h:Math.max(...ys)-Math.min(...ys)});}
  for(const b of beams)for(const p of b.curve.getPoints(32)){const v=project(p,w,h);if(v.visible)obstacles.push({x:v.x-2,y:v.y-2,w:4,h:4});}
  const front=new THREE.Vector3().subVectors(camera.position,controls.target);front.y=0;front.normalize().multiplyScalar(2.5);
  const points=labels.filter(l=>$('#nw-labels').checked || l.uuid===hover).map(l=>{const p=project(l.owner?l.owner.position.clone().add(front):l.position,w,h);return {...p,w:Math.min(l.kind==='vm'?190:210,l.el.textContent.length*7.4+22),h:l.kind==='vm'?38:26,l};}).filter(p=>p.visible);
  points.sort((a,b)=>(b.l.uuid===hover)-(a.l.uuid===hover) || (b.l.kind==='vm')-(a.l.kind==='vm'));
  labels.forEach(l=>l.el.hidden=true);leaders.replaceChildren();
  for(const p of core.placeLabels(points,w,h,obstacles)){
    if(!p.box)continue;const {l,box}=p;l.el.hidden=false;l.el.style.width=`${box.w}px`;l.el.style.left=`${box.x}px`;l.el.style.top=`${box.y}px`;
    const line=document.createElementNS(leaders.namespaceURI,'line');line.setAttribute('x1',p.x);line.setAttribute('y1',p.y);line.setAttribute('x2',box.x+box.w/2);line.setAttribute('y2',box.y+box.h/2);leaders.append(line);
  }
}
function draw(time=0) {
  frame=0;if(!active())return;
  const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;
  if(renderer.domElement.width!==Math.round(w*renderer.getPixelRatio())||renderer.domElement.height!==Math.round(h*renderer.getPixelRatio()))renderer.setSize(w,h,false);
  if(Math.abs(fitAspect-w/h)>.001)fit();
  for(const p of particles){p.mesh.visible=$('#nw-traffic').checked;if(p.mesh.visible)p.mesh.position.copy(p.curve.getPoint(motion.matches?p.offset:(time/1000*p.speed+p.offset)%1));}
  if(!motion.matches){lamps.forEach((lamp,i)=>lamp.visible=Math.sin(time*.002+i*.8)>-.65);scans.forEach((s,i)=>s.mesh.position.z=((time*.00007+i*.3)%1-.5)*(s.d-1));}
  renderer.render(scene,camera);if(!lastLabelLayout){layout(w,h);lastLabelLayout=time || 1;}
  if(!motion.matches)frame=requestAnimationFrame(draw);
}
function wake(){lastLabelLayout=0;if(active()&&!frame)frame=requestAnimationFrame(draw);else if(!active()&&frame){cancelAnimationFrame(frame);frame=0;}}
function pick(event){const r=renderer.domElement.getBoundingClientRect();pointer.set((event.clientX-r.left)/r.width*2-1,-(event.clientY-r.top)/r.height*2+1);ray.setFromCamera(pointer,camera);let hit=ray.intersectObjects(nodes,true).find(h=>h.object.isMesh);if(!hit)return;let node=hit.object;while(node&&!node.userData.vm)node=node.parent;return node;}
function editToolbar(){
  const bar=document.createElement('div');bar.className='nw-edit-tools';
  for(const [id,text,fn] of [['nw-3d-edit','Edit 3D',()=>{if(editMode){if(window.NetworkUI.getData().pending?.length&&!window.confirm('Discard staged NIC changes and exit Edit?'))return;window.NetworkUI.discardEdit?.();}else window.NetworkUI.beginEdit?.();editMode=!!window.NetworkUI.getData().editing;update();}],['nw-3d-save','Save',async()=>{const saving=window.NetworkUI.saveEdit?.();update();await saving;editMode=!!window.NetworkUI.getData().editing;update();}],['nw-3d-discard','Discard',()=>{window.NetworkUI.discardEdit?.();editMode=false;update();}]]){const b=document.createElement('button');b.id=id;b.textContent=text;b.onclick=fn;bar.append(b);}
  const status=document.createElement('span');status.id='nw-3d-status';status.setAttribute('role','status');bar.append(status);host.append(bar);
  $('#nw-view-3d').textContent='3D zones';const heading=$('#nw-vm-panel').querySelector('h3');if(heading)heading.textContent='VLAN zones';
  host.querySelector('.nw-scene-hint').textContent='Drag empty space to orbit · Edit: drag VM to a VLAN · Shift + drag VM to draft a policy · Esc cancels gesture';
}
function syncEditToolbar(){const data=window.NetworkUI.getData();editMode=!!data.editing;$('#nw-3d-status').textContent=data.editError||(editMode?`${data.pending?.length||0} VM changes pending`: '');$('#nw-3d-edit').textContent=editMode?'Exit Edit':'Edit 3D';$('#nw-3d-edit').setAttribute('aria-pressed',String(editMode));$('#nw-3d-edit').disabled=data.canEdit===false||!!data.saving;for(const id of ['nw-3d-save','nw-3d-discard']){$('#'+id).hidden=!editMode;$('#'+id).disabled=!!data.saving;}}
function floorPoint(e){pick(e);plane.set(new THREE.Vector3(0,1,0),-.76);return ray.ray.intersectPlane(plane,intersection);}
function targetAt(e){
  const node=pick(e);if(node)return `vm:${node.userData.vm.name}`;
  if(ray.intersectObjects(equipment,true).some(h=>h.object.userData.endpoint==='any'))return 'any';
  if(!floorPoint(e))return null;const i=core.zoneAt(zones,intersection);return i<0?null:zoneNetworks[i].name;
}
function down(e){if(e.button!==0)return;const node=pick(e);if(!node || !editMode || window.NetworkUI.getData().saving)return;controls.enabled=false;drag={node,x:e.clientX,y:e.clientY,moved:false,rule:e.shiftKey,original:node.position.clone()};renderer.domElement.setPointerCapture(e.pointerId);}
function move(e){if(!drag){const node=pick(e),next=node?.userData.vm.uuid || null;if(next!==hover){hover=next;renderer.domElement.style.cursor=node&&editMode?'grab':'';wake();}return;}
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
for(const id of ['nw-labels','nw-traffic','nw-lines','nw-stopped'])$('#'+id).onchange=update;
new MutationObserver(wake).observe($('#page-network'),{attributes:true,attributeFilter:['class']});
new MutationObserver(wake).observe($('#tab-net-topology'),{attributes:true,attributeFilter:['class']});
document.addEventListener('visibilitychange',wake);motion.addEventListener('change',wake);
window.Network3D={update,colors,dragging:()=>Boolean(drag)};
if(window.NetworkUI?.getData().networks.length)update();
