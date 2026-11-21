/* Layered VLAN hologram. All dependencies are vendored; no network services. */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
const $ = s => document.querySelector(s), core = window.NetworkCore;
const host = $('#nw-three'), motion = matchMedia('(prefers-reduced-motion: reduce)');
let lastLabelLayout = 0;
let renderer, scene, camera, controls, frame = 0, nodes = [], labels = [], beams = [], particles = [], key = '', prefs = {}, mode = '3d', drag = null, hover = null;
const ray = new THREE.Raycaster(), pointer = new THREE.Vector2(), plane = new THREE.Plane(), intersection = new THREE.Vector3();
const overlay = document.createElement('div'); overlay.className = 'nw-labels';
const leaders = document.createElementNS('http://www.w3.org/2000/svg','svg'); leaders.classList.add('nw-leaders');
host.append(leaders, overlay);
const palette = ['#3ef0ff','#b49bff','#5fb4ff','#ffcb66','#ff8fd8'];
function readPrefs(k) {try{return JSON.parse(localStorage.getItem(k+':hologram') || '{}');}catch{return {};}}
function savePrefs() {try{localStorage.setItem(key+':hologram',JSON.stringify(prefs));}catch{/* Storage may be disabled. Arrangement still works for this session. */}}
function active() {return !!renderer && !document.hidden && $('#page-network').classList.contains('active') && $('#tab-net-topology').classList.contains('active') && !$('#nw-three-wrap').hidden;}
function ensure() {
  if(renderer)return true;
  try {
    renderer=new THREE.WebGLRenderer({antialias:true,alpha:true});renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));host.prepend(renderer.domElement);
    scene=new THREE.Scene();camera=new THREE.PerspectiveCamera(42,1,.1,500);
    controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=false;controls.minDistance=8;controls.maxDistance=150;
    controls.addEventListener('change',wake);
    renderer.domElement.addEventListener('pointerdown',down);
    renderer.domElement.addEventListener('pointermove',move);
    renderer.domElement.addEventListener('pointerup',up);
    renderer.domElement.addEventListener('pointercancel',()=>finishDrag(false));
    renderer.domElement.addEventListener('lostpointercapture',()=>{if(drag)finishDrag(false);});
    new ResizeObserver(wake).observe(host);return true;
  }catch(e){host.querySelector('.nw-scene-hint').textContent='3D unavailable. Use the 2D NIC editor to view and manage connections.';return false;}
}
function wire(a,b,color,dashed=false) {
  const geo=new THREE.BufferGeometry().setFromPoints([a,b]);
  const mat=dashed?new THREE.LineDashedMaterial({color,dashSize:.3,gapSize:.2,transparent:true,opacity:.7}):new THREE.LineBasicMaterial({color,transparent:true,opacity:.45});
  const line=new THREE.Line(geo,mat);if(dashed)line.computeLineDistances();scene.add(line);return line;
}
function addLabel(text,position,kind,uuid=null) {
  // A VM with several NICs gets one label, on its first layer.
  if(uuid && labels.some(l=>l.uuid===uuid))return;
  const el=document.createElement('button');el.className=`nw-label ${kind}`;el.textContent=text;el.title=text;el.setAttribute('aria-label',text);
  if(uuid)el.onclick=()=>window.NetworkUI.selectVM(uuid);else el.disabled=true;
  overlay.append(el);labels.push({el,position,kind,uuid});
}
function reset() {
  if(!camera)return;const count=window.NetworkUI.getData().networks.length, y=Math.max(0,(count-1)*2);
  camera.up.set(0,1,0);camera.position.set(19,Math.max(16,count*4+7),23);controls.target.set(0,y,0);
  if(mode==='top'){camera.up.set(0,0,-1);camera.position.set(0,Math.max(40,count*6+20),.01);controls.enableRotate=false;}else controls.enableRotate=true;
  controls.update();wake();
}
function colors() {
  const box=$('#nw-layer-colors');if(!box || !window.NetworkUI)return;
  box.replaceChildren();const title=document.createElement('h3');title.textContent='Layer colors';box.append(title);
  window.NetworkUI.getData().networks.forEach((n,i)=>{const label=document.createElement('label');label.textContent=n.name;const input=document.createElement('input');input.type='color';input.value=prefs.colors?.[n.name] || palette[i%palette.length];input.oninput=()=>{(prefs.colors ||= {})[n.name]=input.value;savePrefs();update();};label.append(input);box.append(label);});
}
function update() {
  if(!window.NetworkUI || !ensure())return;
  const data=window.NetworkUI.getData();if(key!==data.key){key=data.key;prefs=readPrefs(key);}
  finishDrag(false);
  for(const child of [...scene.children]){child.traverse(o=>{o.geometry?.dispose();if(o.material){const mats=Array.isArray(o.material)?o.material:[o.material];mats.forEach(m=>m.dispose());}});scene.remove(child);}
  overlay.replaceChildren();leaders.replaceChildren();nodes=[];labels=[];beams=[];particles=[];
  const networks=[...data.networks].sort((a,b)=>(a.name==='default'?-1:b.name==='default'?1:(a.vlan||1)-(b.vlan||1)));
  const positions=new Map(), layerMap=new Map(), spread=Math.max(8,Math.ceil(Math.sqrt(data.vms.length))*2.2);
  networks.forEach((net,i)=>{
    const y=i*4, color=prefs.colors?.[net.name] || palette[i%palette.length];layerMap.set(net.name,y);
    const geometry=new THREE.BoxGeometry(spread*2,.06,spread*1.55), material=new THREE.MeshBasicMaterial({color,transparent:true,opacity:.065,depthWrite:false});
    const plate=new THREE.Mesh(geometry,material);plate.position.y=y;scene.add(plate);
    const edges=new THREE.LineSegments(new THREE.EdgesGeometry(geometry),new THREE.LineBasicMaterial({color,transparent:true,opacity:.7}));edges.position.y=y;scene.add(edges);
    const grid=new THREE.GridHelper(spread*2,16,color,color);grid.position.y=y+.04;grid.material.transparent=true;grid.material.opacity=.12;scene.add(grid);
    addLabel(`${net.name} · VLAN ${net.vlan || 1}${net.name==='default'?' · native':''}`,new THREE.Vector3(-spread,y,spread*.78),'vlan');
    const trunk=new THREE.Vector3(spread+2,y,0);wire(new THREE.Vector3(spread,y,0),trunk,color);
    const vms=data.vms.filter(v=>v.nics.some(n=>n.network===net.name) && ($('#nw-stopped').checked || v.state==='running'));
    vms.forEach((vm,j)=>{
      const locationKey=`${vm.uuid}:${net.name}`, stored=prefs.positions?.[locationKey];
      const index=data.vms.findIndex(v=>v.uuid===vm.uuid), cols=Math.max(1,Math.ceil(Math.sqrt(data.vms.length))), x=(index%cols-(cols-1)/2)*2.8, z=(Math.floor(index/cols)-(Math.ceil(data.vms.length/cols)-1)/2)*2.6;
      const position=new THREE.Vector3(stored?Math.max(-spread+.5,Math.min(spread-.5,stored.x)):x,y+.32,stored?Math.max(-spread*.7,Math.min(spread*.7,stored.z)):z);
      const color=vm.state==='running'?'#6dffb3':vm.state==='paused'?'#ffcb66':'#768da7';
      const mesh=new THREE.Mesh(new THREE.OctahedronGeometry(.3),new THREE.MeshBasicMaterial({color}));mesh.position.copy(position);scene.add(mesh);
      mesh.userData={vm,net:net.name,locationKey,spread};nodes.push(mesh);
      const ps=positions.get(vm.uuid)||[];ps.push(position);positions.set(vm.uuid,ps);
      addLabel(vm.name,position,'vm',vm.uuid);
      const link=wire(position,trunk,color);beams.push({a:position,b:trunk,line:link,vm});
      const stats=data.stats[vm.uuid], speed=stats ? (stats.net_rx_bps || 0)+(stats.net_tx_bps || 0) : null;
      const intensity=speed===null?.08:Math.min(1,Math.log10(1+speed)/8);
      if(vm.state==='running' && (speed===null || speed>0)) {
        for(let p=0;p<1+Math.floor(intensity*5);p++){
          const dot=new THREE.Mesh(new THREE.SphereGeometry(.045+intensity*.035,6,4),new THREE.MeshBasicMaterial({color:'#cfffff'}));scene.add(dot);particles.push({mesh:dot,a:position,b:trunk,offset:p/6,speed:.06+intensity*.35});
        }
      }
    });
  });
  const trunkHeight=Math.max(4,(networks.length-1)*4+3);
  wire(new THREE.Vector3(spread+2,0,0),new THREE.Vector3(spread+2,trunkHeight,0),'#3ef0ff');
  for(const [radius,opacity] of [[.035,.9],[.13,.12],[.26,.04]]){
    const light=new THREE.Mesh(new THREE.CylinderGeometry(radius,radius,trunkHeight,12),new THREE.MeshBasicMaterial({color:'#3ef0ff',transparent:true,opacity,depthWrite:false,blending:THREE.AdditiveBlending}));light.position.set(spread+2,trunkHeight/2,0);scene.add(light);
  }
  addLabel('External switch · trunk',new THREE.Vector3(spread+2,Math.max(4,(networks.length-1)*4+3),0),'external');
  for(const ps of positions.values())for(let i=1;i<ps.length;i++)wire(ps[i-1],ps[i],'#b49bff',true);
  if($('#nw-lines').checked){
    const ends=end=>{
      if(end==='any')return [new THREE.Vector3(spread+2,4,0)];
      if(layerMap.has(end))return [new THREE.Vector3(-spread,layerMap.get(end),0)];
      return data.vms.filter(v=>end===`vm:${v.name}` || (end.startsWith('group:') && data.groups.members[v.uuid]===end.slice(6))).flatMap(v=>positions.get(v.uuid)?.slice(0,1)||[]);
    };
    for(const r of data.rules){const a=ends(r.src),b=ends(r.dst);for(const from of a)for(const to of b)wire(from,to,r.action==='allow'?'#6dffb3':'#ff6b7a',!r.enabled);}
  }
  colors();if(!update.initialized){update.initialized=true;reset();}wake();
}
function project(p,w,h){const v=p.clone().project(camera);return {x:(v.x+1)*w/2,y:(1-v.y)*h/2,visible:v.z>-1&&v.z<1};}
function layout(w,h) {
  const anchors=nodes.map(n=>project(n.position,w,h)).filter(p=>p.visible).map(p=>({x:p.x-10,y:p.y-10,w:20,h:20}));
  const obstacles=[...anchors];
  // Reserve the projected wires as narrow obstacles so captions avoid their paths.
  for(const child of scene.children){if(!child.isLine || !child.geometry.attributes.position || child.isLineSegments)continue;const attr=child.geometry.attributes.position;for(let j=1;j<attr.count;j++){const a=project(new THREE.Vector3().fromBufferAttribute(attr,j-1),w,h),b=project(new THREE.Vector3().fromBufferAttribute(attr,j),w,h);if(!a.visible||!b.visible)continue;const steps=Math.ceil(Math.hypot(a.x-b.x,a.y-b.y)/12);for(let k=0;k<=steps;k++)obstacles.push({x:a.x+(b.x-a.x)*k/(steps||1)-2,y:a.y+(b.y-a.y)*k/(steps||1)-2,w:4,h:4});}}
  const points=labels.filter(l=>$('#nw-labels').checked || l.uuid===hover).map(l=>{const p=project(l.position,w,h);return {...p,w:Math.min(l.kind==='vm'?170:210,l.el.textContent.length*7.4+18),h:26,l};}).filter(p=>p.visible);
  points.sort((a,b)=>(b.l.uuid===hover)-(a.l.uuid===hover) || (a.l.kind==='vm')-(b.l.kind==='vm'));
  labels.forEach(l=>l.el.hidden=true);leaders.replaceChildren();
  for(const p of core.placeLabels(points,w,h,obstacles)){
    if(!p.box)continue;const {l,box}=p;l.el.hidden=false;l.el.style.left=`${box.x}px`;l.el.style.top=`${box.y}px`;
    const line=document.createElementNS(leaders.namespaceURI,'line');line.setAttribute('x1',p.x);line.setAttribute('y1',p.y);line.setAttribute('x2',box.x+box.w/2);line.setAttribute('y2',box.y+box.h/2);leaders.append(line);
  }
}
function draw(time=0) {
  frame=0;if(!active())return;
  const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;
  if(renderer.domElement.width!==Math.round(w*renderer.getPixelRatio())||renderer.domElement.height!==Math.round(h*renderer.getPixelRatio()))renderer.setSize(w,h,false);
  camera.aspect=w/h;camera.updateProjectionMatrix();
  for(const p of particles){p.mesh.visible=$('#nw-traffic').checked;if(p.mesh.visible)p.mesh.position.lerpVectors(p.a,p.b,motion.matches?p.offset:(time/1000*p.speed+p.offset)%1);}
  renderer.render(scene,camera);if(!lastLabelLayout || time-lastLabelLayout>120){layout(w,h);lastLabelLayout=time || 1;}
  if(!motion.matches && $('#nw-traffic').checked && particles.length)frame=requestAnimationFrame(draw);
}
function wake(){lastLabelLayout=0;if(active()&&!frame)frame=requestAnimationFrame(draw);else if(!active()&&frame){cancelAnimationFrame(frame);frame=0;}}
function pick(event){const r=renderer.domElement.getBoundingClientRect();pointer.set((event.clientX-r.left)/r.width*2-1,-(event.clientY-r.top)/r.height*2+1);ray.setFromCamera(pointer,camera);return ray.intersectObjects(nodes)[0]?.object;}
function down(e){if(e.button!==0)return;const node=pick(e);if(!node)return;controls.enabled=false;drag={node,x:e.clientX,y:e.clientY,moved:false,original:node.position.clone()};plane.set(new THREE.Vector3(0,1,0),-node.position.y);renderer.domElement.setPointerCapture(e.pointerId);}
function move(e){if(!drag){const node=pick(e),next=node?.userData.vm.uuid || null;if(next!==hover){hover=next;renderer.domElement.style.cursor=node?'grab':'';wake();}return;}
  if(Math.hypot(e.clientX-drag.x,e.clientY-drag.y)<5 && !drag.moved)return;
  drag.moved=true;pick(e);if(ray.ray.intersectPlane(plane,intersection)){const n=drag.node,s=n.userData.spread;n.position.x=THREE.MathUtils.clamp(intersection.x,-s+.5,s-.5);n.position.z=THREE.MathUtils.clamp(intersection.z,-s*.7,s*.7);
    // Beam and label vectors are shared with the selected node's original position.
    labels.filter(l=>l.uuid===n.userData.vm.uuid && Math.abs(l.position.y-n.position.y)<.1).forEach(l=>l.position.copy(n.position));
    for(const b of beams)if(b.vm.uuid===n.userData.vm.uuid && Math.abs(b.a.y-n.position.y)<.1){b.a.copy(n.position);b.line.geometry.setFromPoints([b.a,b.b]);}wake();}}
function finishDrag(save=true){if(!drag)return;const {node,moved,original}=drag;drag=null;controls.enabled=true;if(moved&&save){(prefs.positions ||= {})[node.userData.locationKey]={x:node.position.x,z:node.position.z};savePrefs();}else if(moved)node.position.copy(original);}
function up(){if(!drag)return;const {node,moved}=drag;finishDrag();if(!moved)window.NetworkUI.selectVM(node.userData.vm.uuid);else update();wake();}
function setView(next){if(ntopoEditing())return;mode=next;$('#nw-legacy').hidden=next!=='edit';$('#nw-three-wrap').hidden=next==='edit';$('#nw-view-3d').setAttribute('aria-pressed',String(next==='3d'));$('#nw-view-top').setAttribute('aria-pressed',String(next==='top'));$('#nw-view-edit').setAttribute('aria-pressed',String(next==='edit'));if(next!=='edit'){ensure();reset();}else window.drawTopology?.();wake();}
function ntopoEditing(){if($('#topo-edit-tools').hidden===false){window.alert('Save or cancel the topology draft before switching views.');return true;}return false;}
$('#nw-view-3d').onclick=()=>setView('3d');$('#nw-view-top').onclick=()=>setView('top');$('#nw-view-edit').onclick=()=>setView('edit');$('#nw-reset').onclick=reset;
for(const id of ['nw-labels','nw-traffic','nw-lines','nw-stopped'])$('#'+id).onchange=update;
new MutationObserver(wake).observe($('#page-network'),{attributes:true,attributeFilter:['class']});
new MutationObserver(wake).observe($('#tab-net-topology'),{attributes:true,attributeFilter:['class']});
document.addEventListener('visibilitychange',wake);motion.addEventListener('change',wake);
window.Network3D={update,colors,dragging:()=>Boolean(drag)};
if(window.NetworkUI?.getData().networks.length)update();
