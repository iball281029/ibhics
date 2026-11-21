/* Storage telemetry hologram. Vendored Three.js; no external assets or requests. */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const CYAN = 0x3ef0ff, BLUE = 0x5faaff, RED = 0xff637c;
let instance;
function material(color, opacity = .12) {
  return new THREE.MeshBasicMaterial({color, transparent: true, opacity, depthWrite: false,
    blending: THREE.AdditiveBlending, side: THREE.DoubleSide});
}
function hologram(geometry, color, opacity = .12) {
  const group = new THREE.Group();
  group.add(new THREE.Mesh(geometry, material(color, opacity)));
  group.add(new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 24),
    new THREE.LineBasicMaterial({color, transparent: true, opacity: .75})));
  return group;
}
function ring(radius, color, fraction = 1, thickness = .025) {
  const mesh = new THREE.Mesh(new THREE.TorusGeometry(radius, thickness, 8, 120,
    Math.PI * 2 * Math.max(.0001, fraction)), material(color, .85));
  mesh.rotation.x = Math.PI / 2;
  return mesh;
}
function disposeGroup(group) {
  group.traverse((object) => {
    object.geometry?.dispose();
    const materials = Array.isArray(object.material) ? object.material : [object.material];
    materials.filter(Boolean).forEach((mat) => { mat.map?.dispose(); mat.dispose(); });
  });
}
function create(host) {
  const renderer = new THREE.WebGLRenderer({alpha: false, antialias: true, powerPreference: 'low-power'});
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.5));
  renderer.setClearColor(0x020c18);
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x020c18, .037);
  const camera = new THREE.PerspectiveCamera(43, 1, .1, 90);
  camera.position.set(1.5, 7.5, 15);
  const viewport = document.createElement('div');
  viewport.className = 'vs-webgl';
  renderer.domElement.setAttribute('aria-label', 'Storage core, two data nodes and Arbiter. Drag to orbit; scroll to zoom.');
  renderer.domElement.setAttribute('role', 'img');
  viewport.append(renderer.domElement);
  const labels = document.createElement('div'); labels.className = 'vs-scene-labels';
  viewport.append(labels); host.prepend(viewport);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 1.4, 0);
  controls.enablePan = false; controls.enableDamping = false;
  controls.minDistance = 12; controls.maxDistance = 22;
  controls.minPolarAngle = .8; controls.maxPolarAngle = 1.25;
  controls.minAzimuthAngle = -.28; controls.maxAzimuthAngle = .28;
  controls.enableZoom = true;

  const grid = new THREE.GridHelper(64, 64, 0x1586a4, 0x123b51);
  grid.material.transparent = true; grid.material.opacity = .6;
  scene.add(grid);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(70, 70),
    new THREE.MeshBasicMaterial({color: 0x030f1d}));
  floor.rotation.x = -Math.PI / 2; floor.position.y = -.04; scene.add(floor);
  // Distant datacenter racks fade into the fog, giving the floor a real horizon.
  for (let i = 0; i < 12; i++) {
    const rack = hologram(new THREE.BoxGeometry(.65, 2.5 + i % 3, .8), BLUE, .035);
    rack.position.set((i % 2 ? 1 : -1) * (7 + Math.floor(i / 2) * .7), 1.5, -4 - Math.floor(i / 2) * 2);
    scene.add(rack);
  }
  // One radial texture supplies a restrained bloom and distant bokeh.
  const textureCanvas = document.createElement('canvas'); textureCanvas.width = textureCanvas.height = 32;
  const ctx = textureCanvas.getContext('2d');
  const gradient = ctx.createRadialGradient(16, 16, 0, 16, 16, 16);
  gradient.addColorStop(0, '#bfffff'); gradient.addColorStop(.15, '#3ef0ffaa'); gradient.addColorStop(1, '#3ef0ff00');
  ctx.fillStyle = gradient; ctx.fillRect(0, 0, 32, 32);
  const texture = new THREE.CanvasTexture(textureCanvas);
  const glow = new THREE.Mesh(new THREE.PlaneGeometry(12, 12),
    new THREE.MeshBasicMaterial({map: texture, transparent: true, opacity: .28, depthWrite: false, blending: THREE.AdditiveBlending}));
  glow.rotation.x = -Math.PI / 2; glow.position.y = .015; scene.add(glow);
  const dustPositions = [];
  for (let i = 0; i < 75; i++) dustPositions.push(Math.sin(i * 71) * 12, .5 + (i % 15) * .34, Math.cos(i * 37) * 13 - 5);
  const dust = new THREE.Points(new THREE.BufferGeometry().setAttribute('position',
    new THREE.Float32BufferAttribute(dustPositions, 3)),
  new THREE.PointsMaterial({map: texture, color: CYAN, size: .18, opacity: .3, transparent: true,
    depthWrite: false, blending: THREE.AdditiveBlending}));
  scene.add(dust);

  let telemetry = new THREE.Group(); scene.add(telemetry);
  let anchors = [], spins = [], particles = [], streams = [], connected = false;
  let signature = '', frame = 0, drawing = false, time = 0, previous = 0, disposed = false;
  const projected = new THREE.Vector3();
  function label(position, title, detail, kind = '') {
    const element = document.createElement('div'); element.className = `vs-scene-label ${kind}`;
    const heading = document.createElement('b'); heading.textContent = title;
    const small = document.createElement('small'); small.textContent = detail;
    element.append(heading, small); labels.append(element); anchors.push({position, element});
  }
  function update(data, pool, linkReady) {
    const next = JSON.stringify([data.nodes, data.cache, data.created?.spec, data.phase, pool?.allocation_gb, pool?.capacity_gb, pool?.display_name, linkReady]);
    if (next === signature) return;
    signature = next; connected = linkReady;
    disposeGroup(telemetry); scene.remove(telemetry);
    telemetry = new THREE.Group(); scene.add(telemetry);
    labels.replaceChildren(); anchors = []; spins = []; particles = []; streams = [];
    const linkColor = connected ? CYAN : RED;
    const spec = data.created?.spec;
    const usage = pool?.capacity_gb > 0 && Number.isFinite(pool.allocation_gb)
      ? Math.max(0, Math.min(1, pool.allocation_gb / pool.capacity_gb)) : null;
    // Tall translucent hexagonal storage core, with independently rotating data rings.
    const core = hologram(new THREE.CylinderGeometry(1.15, 1.4, 2.8, 6, 1, true), CYAN, .12);
    core.position.y = 1.7; telemetry.add(core);
    const inner = hologram(new THREE.CylinderGeometry(.58, .58, 2.65, 32, 1, true), BLUE, .09);
    inner.position.y = 1.7; telemetry.add(inner);
    for (let i = 0; i < 5; i++) {
      const orbit = ring(1.45 + (i % 2) * .18, i % 2 ? BLUE : CYAN, .78);
      orbit.position.y = .35 + i * .68; telemetry.add(orbit); spins.push(orbit);
    }
    const track = ring(1.95, 0x16445c, 1, .09); track.position.y = .18; telemetry.add(track);
    if (usage != null) {
      const fill = ring(1.95, CYAN, usage, .095); fill.position.y = .19; telemetry.add(fill);
    }
    // Labels sit beside the objects, never on them: core below, nodes outside, arbiter aside.
    label(new THREE.Vector3(0, -.9, 3.4), pool?.display_name || spec?.name || 'Storage core',
      `${spec?.virtual_ip ? `VIP ${spec.virtual_ip}` : 'VIP not configured'} · ${usage == null ? 'usage not reported' : `${(usage * 100).toFixed(1)}% used · ${pool.allocation_gb} / ${pool.capacity_gb} GB`}`, 'vs-label-core');
    data.nodes.filter((n) => n.role === 'compute').slice(0, 2).forEach((node, i) => {
      const x = i ? 4.3 : -4.3;
      const color = node.healthy ? CYAN : RED;
      const pad = hologram(new THREE.CylinderGeometry(1.65, 1.75, .18, 6), color, .13);
      pad.position.set(x, .12, 0); telemetry.add(pad);
      for (let level = 0; level < 3; level++) {
        const block = hologram(new THREE.BoxGeometry(1.9, .42, 1.4), color, .1 + level * .035);
        block.position.set(x, .85 + level * .57, 0); telemetry.add(block);
        for (let slot = 0; slot < 5; slot++) {
          const light = new THREE.Mesh(new THREE.BoxGeometry(.19, .025, .018), material(color, .9));
          light.position.set(x - .7 + slot * .34, 1 + level * .57, .72); telemetry.add(light);
        }
      }
      const cache = data.cache?.find((c) => c.node === node.name);
      if (cache) {
        const layer = hologram(new THREE.BoxGeometry(2.25, .18, 1.7), 0x62ffc8, .35);
        layer.position.set(x, .43, 0); telemetry.add(layer);
      }
      const cacheText = !cache ? '' : cache.dirty_bytes == null ? ' · SSD cache'
        : ` · cache ${(cache.dirty_bytes / 1024**2).toFixed(1)} MiB pending`;
      label(new THREE.Vector3(x + Math.sign(x || 1) * 2.7, 3.3, 0), node.display_name || node.name,
        (node.healthy ? 'DATA NODE · ONLINE' : 'DATA NODE · OFFLINE') + cacheText, node.healthy ? '' : 'vs-label-alert');
      // Transparent telemetry panes are physical objects, positioned behind each node.
      const pane = hologram(new THREE.BoxGeometry(2.2, .9, .025), BLUE, .055);
      pane.position.set(x, 2.2, -1.8); pane.rotation.y = i ? -.25 : .25; telemetry.add(pane);
      for (let line = 0; line < 4; line++) {
        const stripe = new THREE.Mesh(new THREE.BoxGeometry(.35 + line * .28, .025, .03), material(CYAN, .45));
        stripe.position.set(x - .3, 1.95 + line * .16, -1.75); telemetry.add(stripe);
      }
    });
    const witness = data.nodes.find((n) => n.role === 'witness');
    if (witness) {
      const arbiter = hologram(new THREE.OctahedronGeometry(.5), witness.healthy ? BLUE : RED, .23);
      arbiter.position.set(0, 4.6, -2.5); telemetry.add(arbiter); spins.push(arbiter);
      label(new THREE.Vector3(2.3, 4.7, -2.5), witness.display_name || witness.name,
        witness.healthy ? 'ARBITER · QUORUM VOTE' : 'ARBITER · OFFLINE', witness.healthy ? 'vs-label-arbiter' : 'vs-label-arbiter vs-label-alert');
      const beam = new THREE.Line(new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(0, 3.15, 0), arbiter.position]), new THREE.LineBasicMaterial({color: BLUE, transparent: true, opacity: .4}));
      telemetry.add(beam);
    }
    const link = new THREE.Line(new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-4.3, .28, 0), new THREE.Vector3(4.3, .28, 0)]),
    new THREE.LineBasicMaterial({color: linkColor, transparent: true, opacity: .7})); telemetry.add(link);
    // Link status is a fixed HUD chip, so it never covers the scene.
    const hud = document.createElement('div');
    hud.className = `vs-scene-label vs-scene-hud ${connected ? 'vs-label-link' : 'vs-label-link vs-label-alert'}`;
    const hudTitle = document.createElement('b'); hudTitle.textContent = connected ? 'DRBD · CONNECTED' : 'DRBD · CHECK REPLICAS';
    const hudDetail = document.createElement('small'); hudDetail.textContent = connected ? 'UpToDate · replication active' : 'Replication flow paused';
    hud.append(hudTitle, hudDetail); labels.append(hud);
    for (let i = 0; i < 18; i++) {
      const particle = new THREE.Mesh(new THREE.SphereGeometry(.035, 6, 4), material(linkColor, 1));
      particle.position.set(-4.3 + i / 18 * 8.6, .3, 0);
      telemetry.add(particle); particles.push(particle);
    }
    // Vertical data streams rise around the storage core and data blocks.
    for (let i = 0; i < 24; i++) {
      const angle = i * Math.PI * 2 / 24;
      const stream = new THREE.Mesh(new THREE.BoxGeometry(.012, .18 + (i % 4) * .1, .012), material(CYAN, .55));
      stream.position.set(Math.cos(angle) * 2.6, .4 + i % 7 * .45, Math.sin(angle) * 2.6);
      telemetry.add(stream); streams.push(stream);
    }
    sync();
  }
  function active() {
    return !disposed && !document.hidden && host.isConnected
      && document.getElementById('page-storage').classList.contains('active')
      && !host.closest('[hidden]');
  }
  function draw() {
    if (!active() || drawing) return;
    drawing = true;
    controls.update(); renderer.render(scene, camera);
    anchors.forEach(({position, element}) => {
      projected.copy(position).project(camera);
      const half = element.offsetWidth / 2 + 6;
      const x = Math.max(half, Math.min(viewport.clientWidth - half, (projected.x * .5 + .5) * viewport.clientWidth));
      let narrowOffset = 0;
      if (viewport.clientWidth < 500) {
        if (element.classList.contains('vs-label-arbiter')) narrowOffset = -60;
        if (element.classList.contains('vs-label-vip')) narrowOffset = -50;
        if (element.classList.contains('vs-label-link')) narrowOffset = 35;
        if (element.classList.contains('vs-label-core')) narrowOffset = 55;
        if (element.classList.contains('vs-label-cache')) narrowOffset = 110;
      }
      element.style.left = `${x}px`;
      element.style.top = `${(-projected.y * .5 + .5) * viewport.clientHeight + narrowOffset}px`;
      element.style.visibility = Math.abs(projected.x) > 1.15 || Math.abs(projected.y) > 1.1 || projected.z > 1 ? 'hidden' : 'visible';
    });
    if (!host.classList.contains('vs-has-webgl')) host.classList.add('vs-has-webgl');
    drawing = false;
  }
  function tick(now) {
    frame = 0;
    if (!active() || reduced.matches) return;
    const dt = previous ? Math.min(.05, (now - previous) / 1000) : 0;
    previous = now; time += dt;
    // Bounded orbit keeps left/right roles and projected labels easy to track.
    if (!interacting) {
      camera.position.x = Math.sin(time * .065) * 1.6;
      camera.lookAt(controls.target);
    }
    spins.forEach((mesh, i) => { mesh.rotation.y += dt * (i % 2 ? -.14 : .12); });
    if (connected) particles.forEach((particle, i) => { particle.position.x = ((time * 1.5 + i / 18 * 8.6) % 8.6) - 4.3; });
    if (connected) streams.forEach((stream, i) => { stream.position.y = .3 + ((time * (i % 2 ? .6 : -.4) + i * .31) % 3.2 + 3.2) % 3.2; });
    draw(); frame = requestAnimationFrame(tick);
  }
  let interacting = false;
  controls.addEventListener('start', () => { interacting = true; });
  controls.addEventListener('change', draw);
  let lastWidth = 0;
  function resize() {
    if (!active()) return;
    const width = viewport.clientWidth, height = viewport.clientHeight;
    if (!width || !height) return;
    camera.aspect = width / height;
    // Keep both replicas within the frustum on narrow screens.
    camera.fov = width < 650 ? 58 : 43;
    if (lastWidth !== width) {
      controls.minDistance = width < 650 ? 20 : 12;
      controls.maxDistance = width < 650 ? 34 : 22;
      camera.position.sub(controls.target).setLength(width < 650 ? 24 : 16).add(controls.target);
      lastWidth = width;
    }
    camera.updateProjectionMatrix(); renderer.setSize(width, height, false); draw();
  }
  function sync() {
    cancelAnimationFrame(frame); frame = 0; previous = 0;
    if (!active()) return;
    resize();
    if (!reduced.matches) frame = requestAnimationFrame(tick);
  }
  const observer = new MutationObserver(sync);
  observer.observe(document.getElementById('page-storage'), {attributes: true, subtree: true, attributeFilter: ['class', 'hidden']});
  const sizeObserver = new ResizeObserver(resize); sizeObserver.observe(viewport);
  document.addEventListener('visibilitychange', sync); reduced.addEventListener('change', sync);
  function dispose() {
    disposed = true; cancelAnimationFrame(frame); controls.dispose(); observer.disconnect(); sizeObserver.disconnect();
    document.removeEventListener('visibilitychange', sync); reduced.removeEventListener('change', sync);
    disposeGroup(scene); texture.dispose(); renderer.dispose(); viewport.remove(); host.classList.remove('vs-has-webgl');
  }
  renderer.domElement.addEventListener('webglcontextlost', (event) => { event.preventDefault(); dispose(); });
  return {host, update, dispose};
}
window.StorageScene = {
  mount(host, data, pool, connected) {
    if (!host) return;
    try {
      if (instance?.host !== host) { instance?.dispose(); instance = create(host); }
      instance.update(data, pool, connected);
    } catch (error) {
      instance?.dispose(); instance = null;
      host.querySelector('.vs-webgl')?.remove(); host.classList.remove('vs-has-webgl');
      console.warn('Storage hologram unavailable; using SVG topology.', error);
    }
  },
};
window.VirtualStorageUI?.mountScene();
