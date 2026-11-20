/* Real 3D for the infrastructure map and the Overview pictures (three.js, vendored).

   inside(container): the HCI cluster on a platform in the middle, and around it on a
   circle the zones it connects to: VM groups, the network (switch and outside) and
   storage (vStore, SAN, iSCSI, NAS, local disks). Beams carry light at the pace of
   the live traffic. Drag to turn, scroll to zoom; it turns slowly by itself.
   hero(container, kind): a small projected hologram for Compute, Storage or Network.

   Holographic look: additive translucent faces, bright edges and a bloom pass. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";

const COL = { cyan: 0x3ef0ff, blue: 0x5fb4ff, green: 0x6dffb3, pink: 0xff8fd8, violet: 0xb49bff, amber: 0xffcb66, red: 0xff6b7a, white: 0xe8fbff };
const reduced = matchMedia("(prefers-reduced-motion: reduce)");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const level = (bytes) => Math.min(1, Math.log10(1 + Math.max(0, bytes || 0)) / 8);

// ---------- building blocks ----------

function holo(geometry, color, fill = 0.07, edges = 0.95) {
  const g = new THREE.Group();
  g.add(new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: fill, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide })));
  g.add(new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 20), new THREE.LineBasicMaterial({ color, transparent: true, opacity: edges, blending: THREE.AdditiveBlending })));
  return g;
}
function circle(radius, color, opacity = 0.8, segments = 128, dash = null) {
  const pts = Array.from({ length: segments + 1 }, (_, i) => { const a = (i / segments) * Math.PI * 2; return new THREE.Vector3(Math.cos(a) * radius, 0, Math.sin(a) * radius); });
  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  const mat = dash ? new THREE.LineDashedMaterial({ color, transparent: true, opacity, dashSize: dash[0], gapSize: dash[1], blending: THREE.AdditiveBlending })
    : new THREE.LineBasicMaterial({ color, transparent: true, opacity, blending: THREE.AdditiveBlending });
  const line = new THREE.Line(geo, mat);
  if (dash) line.computeLineDistances();
  return line;
}
function glowSprite(color, size) {
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const g = c.getContext("2d"), grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  const hex = `#${new THREE.Color(color).getHexString()}`;
  grad.addColorStop(0, "#ffffff"); grad.addColorStop(0.25, hex); grad.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
  s.scale.set(size, size, 1);
  return s;
}
function platform(radius, color) { // a projector pad: disc, rings, ticks and a turning dashed ring
  const g = new THREE.Group();
  const disc = new THREE.Mesh(new THREE.CircleGeometry(radius, 96), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.03, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide }));
  disc.rotation.x = -Math.PI / 2; g.add(disc);
  g.add(circle(radius, color, 0.9));
  g.add(circle(radius * 0.82, color, 0.35));
  const spin = circle(radius * 1.08, color, 0.8, 160, [0.5, 0.35]); spin.userData.spin = 0.25; g.add(spin);
  const ticks = [];
  for (let i = 0; i < 72; i++) {
    const a = (i / 72) * Math.PI * 2, r1 = radius * 0.9, r2 = radius * (i % 6 ? 0.94 : 0.98);
    ticks.push(new THREE.Vector3(Math.cos(a) * r1, 0, Math.sin(a) * r1), new THREE.Vector3(Math.cos(a) * r2, 0, Math.sin(a) * r2));
  }
  g.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(ticks), new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.6 })));
  const cone = new THREE.Mesh(new THREE.CylinderGeometry(radius * 0.15, radius * 0.95, radius * 1.4, 64, 1, true), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.012, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide }));
  cone.position.y = radius * 0.7; g.add(cone);
  return g;
}
function label(html, cls = "", onClick = null) {
  const el = document.createElement("div");
  el.className = `f3-label ${cls}`; el.innerHTML = html;
  if (onClick) { el.classList.add("f3-click"); el.addEventListener("click", onClick); }
  return new CSS2DObject(el);
}

// Models: everything built from primitives, so the look stays one family.
function rack(color, healthy) {
  const g = new THREE.Group();
  g.add(holo(new THREE.BoxGeometry(1.6, 3.4, 1.4), color, 0.05));
  for (let i = 0; i < 6; i++) {
    const blade = holo(new THREE.BoxGeometry(1.36, 0.36, 1.2), color, 0.1, 0.7);
    blade.position.y = -1.35 + i * 0.54; g.add(blade);
    const led = glowSprite(healthy ? COL.green : COL.red, 0.28); led.position.set(0.55, -1.35 + i * 0.54, 0.62); led.userData.blink = i * 0.7; g.add(led);
  }
  const scan = new THREE.Mesh(new THREE.PlaneGeometry(1.8, 1.6), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.18, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide }));
  scan.rotation.x = -Math.PI / 2; scan.userData.scan = 3.2; g.add(scan);
  return g;
}
function vmToken(running) {
  const g = holo(new THREE.OctahedronGeometry(0.7), running ? COL.green : 0x6c7a92, running ? 0.18 : 0.06);
  g.userData.spinSelf = running ? 1.2 : 0.2;
  return g;
}
function switchBox() {
  const g = new THREE.Group();
  g.add(holo(new THREE.BoxGeometry(4.2, 0.5, 1.6), COL.pink, 0.08));
  for (let i = 0; i < 12; i++) {
    const port = holo(new THREE.BoxGeometry(0.22, 0.16, 0.05), COL.pink, 0.2, 0.9);
    port.position.set(-1.7 + i * 0.31, 0, 0.82); g.add(port);
    const led = glowSprite(i % 3 ? COL.green : COL.amber, 0.16); led.position.set(-1.7 + i * 0.31, 0.16, 0.84); led.userData.blink = i; g.add(led);
  }
  return g;
}
function internet() {
  const g = holo(new THREE.IcosahedronGeometry(1.3, 1), COL.pink, 0.05, 0.7);
  g.userData.spinSelf = 0.25;
  return g;
}
function store(kind) {
  const g = new THREE.Group(), color = kind === "local" ? COL.blue : COL.violet;
  if (kind === "vstore" || kind === "local") {
    const n = kind === "vstore" ? 3 : 2;
    for (let i = 0; i < n; i++) {
      const disk = holo(new THREE.CylinderGeometry(1.1, 1.1, 0.42, 48), color, 0.1); disk.position.y = i * 0.62; g.add(disk);
      const ring = circle(1.45, COL.cyan, 0.7, 120, [0.4, 0.25]); ring.position.y = i * 0.62; ring.userData.spin = i % 2 ? -0.8 : 0.8; g.add(ring);
    }
  } else {
    g.add(holo(new THREE.BoxGeometry(1.8, 2.2, 1.2), color, 0.06));
    for (let r = 0; r < 4; r++) for (let c = 0; c < 3; c++) {
      const bay = holo(new THREE.BoxGeometry(0.46, 0.38, 0.05), color, 0.18, 0.8);
      bay.position.set(-0.55 + c * 0.55, -0.75 + r * 0.5, 0.62); g.add(bay);
    }
  }
  return g;
}

// ---------- detailed models (our own holographic take, built from primitives) ----------

function lines(points, color, opacity = 0.9) { // pairs of points -> line segments
  return new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color, transparent: true, opacity, blending: THREE.AdditiveBlending }));
}
function screenGlyph(os, color) { // a VM's screen: a window frame and a Windows or Linux mark
  const g = new THREE.Group(), V = (x, y) => new THREE.Vector3(x, y, 0);
  g.add(holo(new THREE.PlaneGeometry(1.5, 1.05), color, 0.16, 0.9));
  g.add(lines([V(-0.75, 0.38), V(0.75, 0.38)], color, 0.7));
  for (let i = 0; i < 3; i++) { const d = glowSprite(color, 0.09); d.position.set(-0.64 + i * 0.1, 0.45, 0.01); g.add(d); }
  if (os === "windows") {
    for (const [x, y] of [[-0.17, 0.04], [0.04, 0.04], [-0.17, -0.17], [0.04, -0.17]]) {
      const pane = new THREE.Mesh(new THREE.PlaneGeometry(0.18, 0.18), new THREE.MeshBasicMaterial({ color: 0x8fdcff, transparent: true, opacity: 0.85, blending: THREE.AdditiveBlending }));
      pane.position.set(x + 0.09, y + 0.09, 0.01); g.add(pane);
    }
  } else if (os === "linux") { // a penguin from two ellipses and a beak
    const body = new THREE.Mesh(new THREE.CircleGeometry(0.2, 24), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.85 }));
    body.scale.set(0.8, 1.15, 1); body.position.set(0, -0.08, 0.01); g.add(body);
    const head = new THREE.Mesh(new THREE.CircleGeometry(0.12, 20), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.85 }));
    head.position.set(0, 0.17, 0.012); g.add(head);
    const beak = new THREE.Mesh(new THREE.CircleGeometry(0.045, 3), new THREE.MeshBasicMaterial({ color: COL.amber }));
    beak.rotation.z = -Math.PI / 2; beak.position.set(0, 0.13, 0.015); g.add(beak);
  } else {
    g.add(lines([V(-0.3, -0.05), V(0.3, -0.05), V(-0.3, 0.1), V(0.15, 0.1)], color, 0.8));
  }
  return g;
}
function vmCube(os, running) { // a glass cube with the VM's screen and a stack of drives inside
  const color = running ? 0x8fdcff : 0x6c7a92, g = new THREE.Group();
  g.add(holo(new THREE.BoxGeometry(2, 1.9, 1.6), color, running ? 0.06 : 0.03, running ? 0.95 : 0.4));
  const scr = screenGlyph(os, color); scr.position.set(0, 0.25, 0.81); g.add(scr);
  for (let i = 0; i < 3; i++) { const plate = holo(new THREE.BoxGeometry(1.5, 0.08, 1.1), color, 0.12, 0.6); plate.position.set(0, -0.55 + i * 0.13, 0); g.add(plate); }
  return g;
}
function serverOpen(color, healthy) { // a 2U server with its lid off: board, CPUs, DIMMs, fan, front bays
  const g = new THREE.Group();
  g.add(holo(new THREE.BoxGeometry(6.4, 0.9, 3.6), color, 0.05));
  const board = holo(new THREE.BoxGeometry(6, 0.05, 3.2), color, 0.12, 0.5); board.position.y = -0.35; g.add(board);
  for (let k = 0; k < 2; k++) { // CPUs with heatsink fins
    const cpu = holo(new THREE.BoxGeometry(0.9, 0.18, 0.9), color, 0.25); cpu.position.set(-1.6 + k * 1.3, -0.2, 0.1); g.add(cpu);
    const fins = [];
    for (let i = 0; i < 7; i++) { const x = -1.6 + k * 1.3 - 0.4 + i * 0.13; fins.push(new THREE.Vector3(x, -0.1, -0.35), new THREE.Vector3(x, -0.1, 0.55)); }
    g.add(lines(fins, color, 0.7));
  }
  for (let i = 0; i < 6; i++) { // DIMMs standing up
    const d = holo(new THREE.BoxGeometry(0.08, 0.5, 1.5), color, 0.2, 0.8); d.position.set(0.7 + i * 0.22, -0.05, 0.2); g.add(d);
  }
  const fan = circle(0.32, color, 0.9, 32); fan.rotation.x = Math.PI / 2; fan.position.set(2.7, 0, 1.81); fan.userData.spin = 0; g.add(fan);
  const blades = lines([new THREE.Vector3(-0.28, 0, 0), new THREE.Vector3(0.28, 0, 0), new THREE.Vector3(0, -0.28, 0), new THREE.Vector3(0, 0.28, 0)], color);
  blades.position.set(2.7, 0, 1.82); blades.userData.fan = true; g.add(blades);
  for (let i = 0; i < 4; i++) { // front drive bays and LEDs
    const bay = holo(new THREE.BoxGeometry(1, 0.32, 0.04), color, 0.15, 0.8); bay.position.set(-2.4 + i * 1.15, 0.1, 1.82); g.add(bay);
    const led = glowSprite(healthy ? COL.green : COL.red, 0.2); led.position.set(-2.0 + i * 1.15, 0.1, 1.86); led.userData.blink = i; g.add(led);
  }
  const traces = []; // glowing board traces
  for (let i = 0; i < 6; i++) traces.push(new THREE.Vector3(-1.6, -0.3, 0.6 + i * 0.12), new THREE.Vector3(0.7 + (i % 3) * 0.4, -0.3, 0.6 + i * 0.12));
  g.add(lines(traces, COL.cyan, 0.5));
  return g;
}
function nasBox(color = COL.violet) { // a desktop NAS: front drive trays with pull rings, a lit display
  const g = new THREE.Group();
  g.add(holo(new THREE.BoxGeometry(4.2, 2.8, 3), color, 0.05));
  const panel = new THREE.Mesh(new THREE.PlaneGeometry(1.5, 0.32), new THREE.MeshBasicMaterial({ color: COL.cyan, transparent: true, opacity: 0.75, blending: THREE.AdditiveBlending }));
  panel.position.set(0.9, 1.05, 1.51); panel.userData.blink = 0.3; g.add(panel);
  for (let i = 0; i < 5; i++) {
    const tray = holo(new THREE.BoxGeometry(0.58, 1.9, 0.12), color, 0.14, 0.9); tray.position.set(-1.35 + i * 0.7, -0.25, 1.5); g.add(tray);
    const ring = circle(0.16, 0xffffff, 0.9, 24); ring.rotation.x = Math.PI / 2; ring.position.set(-1.35 + i * 0.7, 0.35, 1.58); g.add(ring);
    const led = glowSprite(i === 4 ? COL.amber : COL.green, 0.18); led.position.set(-1.35 + i * 0.7, -1.05, 1.6); led.userData.blink = i * 0.6; g.add(led);
  }
  const vents = [];
  for (let i = 0; i < 8; i++) vents.push(new THREE.Vector3(2.11, -1 + i * 0.25, -1.2), new THREE.Vector3(2.11, -1 + i * 0.25, 1.2));
  g.add(lines(vents, color, 0.5));
  return g;
}
function laptop(color) {
  const g = new THREE.Group();
  g.add(holo(new THREE.BoxGeometry(1.3, 0.06, 0.9), color, 0.2));
  const keys = [];
  for (let i = 0; i < 4; i++) keys.push(new THREE.Vector3(-0.5, 0.04, -0.25 + i * 0.12), new THREE.Vector3(0.5, 0.04, -0.25 + i * 0.12));
  g.add(lines(keys, color, 0.6));
  const scr = holo(new THREE.PlaneGeometry(1.3, 0.85), color, 0.22); scr.position.set(0, 0.42, -0.5); scr.rotation.x = -0.25; g.add(scr);
  return g;
}
function numberSprite(text, color) {
  const c = document.createElement("canvas"); c.width = 256; c.height = 64;
  const x = c.getContext("2d"); x.font = "600 34px ui-monospace, Menlo, monospace"; x.fillStyle = `#${new THREE.Color(color).getHexString()}`; x.fillText(text, 8, 44);
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), transparent: true, opacity: 0.8, depthWrite: false, blending: THREE.AdditiveBlending }));
  s.scale.set(2.4, 0.6, 1);
  return s;
}
function netGlobe(clients, color = COL.cyan) { // the outside world with clients on spokes
  const g = new THREE.Group(), R = 1.9;
  g.add(holo(new THREE.SphereGeometry(R, 32, 20), color, 0.06, 0.25));
  const dots = [];
  for (let i = 0; i < 380; i++) { // land-ish dot bands for a world feel
    const y = 1 - (2 * (i + 0.5)) / 380, r = Math.sqrt(1 - y * y), a = i * 2.399963;
    if (Math.sin(a * 3) + Math.cos(y * 5) > 0.3) dots.push(Math.cos(a) * r * R * 1.01, y * R * 1.01, Math.sin(a) * r * R * 1.01);
  }
  const geo = new THREE.BufferGeometry(); geo.setAttribute("position", new THREE.Float32BufferAttribute(dots, 3));
  const world = new THREE.Points(geo, new THREE.PointsMaterial({ color: 0xbff6ff, size: 0.09, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending, depthWrite: false }));
  world.userData.spin = 0.2; g.add(world);
  const n = Math.max(4, Math.min(10, clients));
  for (let i = 0; i < n; i++) {
    const a = (i / n) * Math.PI * 2, y = (i % 3 - 1) * 0.9, p = new THREE.Vector3(Math.cos(a) * 4.4, y, Math.sin(a) * 4.4);
    const lap = laptop(color); lap.position.copy(p); lap.lookAt(0, y, 0); g.add(lap);
    const hub = p.clone().setLength(R); const knob = glowSprite(color, 0.4); knob.position.copy(hub); g.add(knob);
    const b = beam(hub, p, color, 0); b.userData.beam.level = 0.5; g.add(b);
  }
  ["25.36%", "653.25", "0.25", "+2.55"].forEach((tx, i) => { const s = numberSprite(tx, color); s.position.set(-3 + i * 2, 3.2 + (i % 2) * 0.6, -2); s.userData.bob = i; s.userData.baseY = s.position.y; g.add(s); });
  return g;
}
function squarePad(w, d, color) { // an isometric slab with glowing edges and a grid on top
  const g = new THREE.Group();
  const slab = holo(new THREE.BoxGeometry(w, 0.5, d), color, 0.018, 0.9); slab.position.y = -0.25; g.add(slab);
  const top = new THREE.Mesh(new THREE.PlaneGeometry(w * 0.94, d * 0.94), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.012, depthWrite: false, blending: THREE.AdditiveBlending }));
  top.rotation.x = -Math.PI / 2; top.position.y = 0.01; g.add(top);
  const grid = [];
  for (let x = -w / 2 + 1; x < w / 2; x += 1) grid.push(new THREE.Vector3(x, 0.02, -d / 2 + 0.2), new THREE.Vector3(x, 0.02, d / 2 - 0.2));
  for (let z = -d / 2 + 1; z < d / 2; z += 1) grid.push(new THREE.Vector3(-w / 2 + 0.2, 0.02, z), new THREE.Vector3(w / 2 - 0.2, 0.02, z));
  g.add(lines(grid, color, 0.08));
  const glowEdge = holo(new THREE.BoxGeometry(w + 0.25, 0.08, d + 0.25), color, 0.03, 0.5); glowEdge.position.y = -0.52; g.add(glowEdge);
  return g;
}

// A beam that arcs between two points with lights running along it.
function beam(a, b, color, height = 3) {
  const mid = a.clone().lerp(b, 0.5); mid.y += height;
  const curve = new THREE.QuadraticBezierCurve3(a, mid, b);
  const g = new THREE.Group();
  g.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 48, 0.05, 6, false), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.55, depthWrite: false, blending: THREE.AdditiveBlending })));
  g.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 48, 0.16, 8, false), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.08, depthWrite: false, blending: THREE.AdditiveBlending })));
  const sparks = Array.from({ length: 6 }, () => { const s = glowSprite(color, 0.55); g.add(s); return s; });
  g.userData.beam = { curve, sparks, level: 0.2 };
  return g;
}

// ---------- a stage: renderer, bloom, labels, controls, loop ----------

class Stage {
  constructor(container, { controls = true, distance = 40, fov = 45, bloom = 0.75 } = {}) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.5));
    this.renderer.setClearColor(0x020a14, bloom ? 1 : 0);
    container.appendChild(this.renderer.domElement);
    this.labels = new CSS2DRenderer();
    this.labels.domElement.className = "f3-labels";
    container.appendChild(this.labels.domElement);
    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.FogExp2(0x020a14, 0.01);
    this.camera = new THREE.PerspectiveCamera(fov, 1, 0.1, 500);
    this.camera.position.set(0, distance * 0.62, distance);
    if (controls) {
      this.controls = new OrbitControls(this.camera, this.labels.domElement);
      Object.assign(this.controls, { enableDamping: true, dampingFactor: 0.06, autoRotate: !reduced.matches, autoRotateSpeed: 0.45, minDistance: 14, maxDistance: 70, maxPolarAngle: Math.PI * 0.47, enablePan: false });
    }
    if (bloom) { // bloom needs an opaque frame; without it the canvas stays see-through
      this.composer = new EffectComposer(this.renderer);
      this.composer.addPass(new RenderPass(this.scene, this.camera));
      this.bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), bloom, 0.5, 0.25);
      this.composer.addPass(this.bloom);
      this.composer.addPass(new OutputPass());
    }
    this.clock = new THREE.Clock();
    this.root = new THREE.Group(); this.scene.add(this.root);
    this.resize = () => {
      const w = Math.max(1, container.clientWidth), h = Math.max(1, container.clientHeight);
      this.renderer.setSize(w, h); this.labels.setSize(w, h);
      if (this.composer) { this.composer.setSize(w, h); this.bloom.resolution.set(w / 2, h / 2); }
      this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
    };
    this.observer = new ResizeObserver(this.resize); this.observer.observe(container); this.resize();
    const loop = () => {
      if (this.dead) return;
      this.frame = requestAnimationFrame(loop);
      if (document.hidden || !container.isConnected || !container.offsetParent) return;
      const dt = Math.min(0.05, this.clock.getDelta()), t = this.clock.elapsedTime;
      this.tick(dt, t); this.onTick?.(dt, t);
      this.controls?.update();
      if (this.composer) this.composer.render(); else this.renderer.render(this.scene, this.camera);
      this.labels.render(this.scene, this.camera);
    };
    loop();
  }
  tick(dt, t) {
    const still = reduced.matches;
    this.scene.traverse((o) => {
      const u = o.userData;
      if (u.spin && !still) o.rotation.y += u.spin * dt;
      if (u.spinSelf && !still) { o.rotation.y += u.spinSelf * dt; o.rotation.x += u.spinSelf * 0.4 * dt; }
      if (u.blink != null) o.material.opacity = still ? 1 : 0.45 + 0.55 * Math.abs(Math.sin(t * 2.2 + u.blink));
      if (u.scan) o.position.y = (still ? 0 : Math.sin(t * 1.3)) * (u.scan / 2);
      if (u.bob != null && !still) o.position.y = u.baseY + Math.sin(t * 1.4 + u.bob) * 0.25;
      if (u.orbit && !still) { const a = t * u.orbit.speed + u.orbit.phase; o.position.set(Math.cos(a) * u.orbit.r, u.orbit.y, Math.sin(a) * u.orbit.r); }
      if (u.beam) {
        const { curve, sparks, level: lv } = u.beam, n = Math.max(1, Math.round(1 + lv * (sparks.length - 1)));
        sparks.forEach((s, i) => {
          s.visible = i < n;
          if (!s.visible) return;
          const f = still ? i / n : (t * (0.12 + lv * 0.5) + i / n) % 1;
          s.position.copy(curve.getPoint(f));
        });
      }
      if (u.fan && !still) o.rotation.z += 9 * dt;
      if (u.sway && !still) o.rotation.y = Math.sin(t * 0.25) * 0.35;
      if (u.rise) { // zones arrive from below, one after the other
        const f = still ? 1 : Math.min(1, Math.max(0, (t - (this.born ?? (this.born = t)) - u.rise.delay) / 0.9));
        const e = 1 - Math.pow(1 - f, 3); o.position.y = u.rise.from + (u.rise.to - u.rise.from) * e;
        if (f >= 1) delete u.rise;
      }
      if (u.pulse && !still) { const f = (t * 0.5 + u.pulse) % 1; o.scale.setScalar(1 + f * 2.2); o.material.opacity = 0.7 * (1 - f); }
    });
  }
  clear() {
    for (const o of [...this.root.children]) {
      this.root.remove(o);
      o.traverse((c) => {
        c.geometry?.dispose?.();
        if (c.material) (Array.isArray(c.material) ? c.material : [c.material]).forEach((m) => { m.map?.dispose?.(); m.dispose?.(); });
        if (c.isCSS2DObject) c.element.remove();
      });
    }
  }
  dispose() {
    this.dead = true; cancelAnimationFrame(this.frame); this.observer.disconnect();
    this.clear(); this.controls?.dispose(); this.composer?.dispose?.(); this.renderer.dispose();
    this.renderer.domElement.remove(); this.labels.domElement.remove();
  }
}

function backdrop(scene) { // stars and a floor grid, once per stage
  const pts = [];
  for (let i = 0; i < 900; i++) {
    const r = 60 + Math.random() * 120, a = Math.random() * Math.PI * 2, y = -10 + Math.random() * 90;
    pts.push(Math.cos(a) * r, y, Math.sin(a) * r);
  }
  const geo = new THREE.BufferGeometry(); geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  scene.add(new THREE.Points(geo, new THREE.PointsMaterial({ color: 0x8fdcff, size: 0.35, transparent: true, opacity: 0.7, blending: THREE.AdditiveBlending, depthWrite: false })));
  const grid = new THREE.GridHelper(120, 60, 0x1d6a80, 0x0c3346);
  grid.material.transparent = true; grid.material.opacity = 0.35; grid.position.y = -0.05;
  scene.add(grid);
  const outer = circle(30, COL.cyan, 0.25, 256, [1, 0.8]); outer.userData.spin = 0.02; scene.add(outer);
}

// ---------- inside the datacenter ----------

function rise(o, delay) { o.userData.rise = { delay, from: -7, to: o.position.y }; o.position.y = -7; return o; }


function inside(container) {
  const stage = new Stage(container, { distance: 44, fov: 42, bloom: 0.6 });
  backdrop(stage.scene);
  let signature = "", pads = [], hovered = null;
  const beams = [], R = 20, ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
  stage.labels.domElement.addEventListener("pointermove", (e) => {
    const r = stage.labels.domElement.getBoundingClientRect();
    mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(mouse, stage.camera);
    const hit = ray.intersectObjects(pads.map((p) => p.hitbox), false)[0];
    hovered = hit ? pads.find((p) => p.hitbox === hit.object) : null;
    stage.labels.domElement.style.cursor = hovered ? "pointer" : "";
  });
  stage.labels.domElement.addEventListener("click", () => { if (hovered?.href) location.hash = hovered.href; });
  stage.onTick = (dt) => { // the pointed-at zone lifts; the turning pauses while one is pointed at
    for (const p of pads) {
      if (p.group.userData.rise) continue;
      const want = p === hovered ? 1.2 : 0, cur = p.group.userData.lift || 0, next = cur + (want - cur) * Math.min(1, dt * 6);
      p.group.userData.lift = next; p.group.position.y = p.baseY + next;
    }
    stage.controls.autoRotate = !hovered && !reduced.matches;
  };

  function build(d) {
    stage.clear(); beams.length = 0; pads = [];
    const root = stage.root;
    const zone = (x, z, radius, color, title, cls, href, delay) => {
      const group = new THREE.Group(); group.position.set(x, 0, z); root.add(group);
      group.add(platform(radius, color));
      const hitbox = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, 5, 24), new THREE.MeshBasicMaterial({ visible: false })); hitbox.position.y = 2.5; group.add(hitbox);
      const head = label(`<b>${title}</b>`, `f3-zone ${cls}`); head.position.set(0, 0.3, radius + 0.8); group.add(head);
      if (delay != null) rise(group, delay);
      pads.push({ group, hitbox, href, baseY: 0 });
      return group;
    };

    // Centre: the HCI cluster, servers opened up with their VMs floating above.
    const center = zone(0, 0, 8, COL.cyan, "HCI CLUSTER", "f3-cyan", "cluster", 0);
    const nodes = d.nodes.slice(0, 4), sc = nodes.length > 2 ? 0.55 : 0.85;
    nodes.forEach((n, i) => {
      const color = n.healthy ? COL.cyan : COL.red, z = (i - (nodes.length - 1) / 2) * 4.2 * (nodes.length > 2 ? 0.75 : 1);
      const srv = serverOpen(color, n.healthy); srv.scale.setScalar(sc); srv.position.set(0, 0.6, z); center.add(srv);
      const l = label(`<b>${esc(n.name)}</b><span>CPU ${n.cpu == null ? "—" : Math.round(n.cpu) + "%"} · MEM ${n.mem == null ? "—" : Math.round(n.mem) + "%"}</span><span>${n.vms} VMs · ${n.healthy ? "online" : "offline"}</span>`, `f3-node ${n.healthy ? "" : "f3-bad"}`, () => { location.hash = `node/${encodeURIComponent(n.name)}`; });
      l.position.set(-3.6, 1.2, z); center.add(l);
      (n.vmList || []).slice(0, 2).forEach((v, k) => {
        const cube = vmCube(v.os, v.running); cube.scale.setScalar(0.6);
        const cx = (k - 0.5) * 2.2, cy = 3.6 + k * 0.4; cube.position.set(cx, cy, z); cube.userData.bob = k + i; cube.userData.baseY = cy; center.add(cube);
        const dash = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(cx, cy - 0.6, z), new THREE.Vector3(cx * 0.6, 0.75, z)]),
          new THREE.LineDashedMaterial({ color: 0x8fdcff, dashSize: 0.16, gapSize: 0.12, transparent: true, opacity: 0.9 }));
        dash.computeLineDistances(); center.add(dash);
      });
    });
    const summary = label(`<b>${d.nodes.length} nodes${d.witness ? " + witness" : ""}</b><span>${d.vmsRunning}/${d.vmsTotal} VMs running</span>`, "f3-item f3-cyan");
    summary.position.set(0, 7.2, 0); center.add(summary);

    // The ring of zones around it.
    const sats = [
      { key: "vm", color: COL.green, title: "WORKLOADS · VM GROUPS", cls: "f3-green", href: "compute" },
      { key: "net", color: COL.pink, title: "NETWORK", cls: "f3-pink", href: "network" },
      { key: "sto", color: COL.violet, title: "STORAGE", cls: "f3-violet", href: "storage" },
      ...(d.witness ? [{ key: "wit", color: COL.amber, title: "WITNESS · QUORUM", cls: "f3-amber", href: "cluster" }] : []),
    ];
    sats.forEach((s, i) => {
      const a = (i / sats.length) * Math.PI * 2 - Math.PI / 2, x = Math.cos(a) * R, z = Math.sin(a) * R;
      s.group = zone(x, z, 5.2, s.color, s.title, s.cls, s.href, 0.4 + i * 0.2);
      const bm = beam(new THREE.Vector3(x * 0.4, 0.3, z * 0.4), new THREE.Vector3(x * 0.74, 0.3, z * 0.74), s.color, 3); root.add(bm); beams.push({ key: s.key, b: bm });
    });
    const by = (k) => sats.find((s) => s.key === k)?.group;

    const vg = by("vm"), groups = d.groups.slice(0, 4);
    groups.forEach((g, i) => {
      const a = (i / Math.max(1, groups.length)) * Math.PI * 2, gx = groups.length > 1 ? Math.cos(a) * 2.4 : 0, gz = groups.length > 1 ? Math.sin(a) * 2.4 : 0;
      g.vms.slice(0, 3).forEach((v, k) => {
        const c = vmCube(v.os, v.running); c.scale.setScalar(0.42);
        c.position.set(gx + (k - 1) * 0.95, 1 + (k % 2) * 0.35, gz); c.userData.bob = k * 0.7 + i; c.userData.baseY = c.position.y; vg.add(c);
      });
      const l = label(`<b>${esc(g.name)}</b><span>${g.running}/${g.vms.length} running</span>`, "f3-item f3-green"); l.position.set(gx, 2.4, gz); vg.add(l);
    });
    if (!groups.length) { const l = label("<span>No VMs yet</span>", "f3-item f3-green"); l.position.set(0, 1, 0); vg.add(l); }

    const ng = by("net"), globe = netGlobe(Math.max(6, d.nets.length * 3)); globe.scale.setScalar(0.55); globe.position.set(0, 3, -0.6); ng.add(globe);
    const sw = switchBox(); sw.scale.setScalar(0.85); sw.position.set(0, 0.45, 2.4); ng.add(sw);
    const s0 = d.switches[0];
    if (s0) {
      const l = label(`<b>${esc(s0.name)}</b><span>${esc([s0.model, s0.mgmt].filter(Boolean).join(" · "))}</span>${s0.links.map((k) => `<span>${esc(k.port || "?")} ⇄ ${esc(k.node)} ${esc(k.iface)}${k.speed ? " · " + esc(k.speed) : ""}</span>`).join("")}`, "f3-item f3-pink");
      l.position.set(0, 1.3, 3.2); ng.add(l);
    }
    const vl = label(`<b>VM NETWORKS</b>${d.nets.slice(0, 5).map((n) => `<span>${esc(n.name)} · ${n.vlan ? "VLAN " + n.vlan : "untagged"}</span>`).join("")}`, "f3-item f3-pink"); vl.position.set(0, 6, -0.6); ng.add(vl);

    const sg = by("sto"), stores = d.stores.slice(0, 2);
    stores.forEach((s, i) => {
      const x = (i - (stores.length - 1) / 2) * 3.8, m = nasBox(s.kind === "vstore" ? COL.violet : COL.blue);
      m.scale.setScalar(stores.length > 1 ? 0.62 : 0.8); m.position.set(x, 1.2, 0); sg.add(m);
      const l = label(`<b>${esc(s.label)}</b><span>${esc(s.name)} · ${Math.round(s.used)}% of ${Math.round(s.cap)} GB</span><span>${s.kind === "vstore" ? `replicated on ${s.nodes.length} nodes` : esc(s.nodes.join(", "))}</span>`, "f3-item f3-violet", () => { location.hash = "storage"; });
      l.position.set(x, 3.2, 0); sg.add(l);
    });
    if (!stores.length) { const l = label("<span>No storage pools</span>", "f3-item f3-violet"); l.position.set(0, 1, 0); sg.add(l); }

    const wg = by("wit");
    if (wg) {
      const w = serverOpen(COL.amber, true); w.scale.setScalar(0.5); w.position.set(0, 0.6, 0); wg.add(w);
      const l = label(`<b>${esc(d.witness.name)}</b><span>votes in quorum · no VM data</span>`, "f3-item f3-amber"); l.position.set(0, 1.9, 0); wg.add(l);
    }
    for (let k = 0; k < 2; k++) { const p = circle(8, COL.cyan, 0.7); p.userData.pulse = k * 0.5; root.add(p); }
  }

  function update(d) {
    const sig = JSON.stringify([d.nodes.map((n) => [n.name, n.healthy, n.vms, Math.round(n.cpu ?? -1), Math.round(n.mem ?? -1)]), d.witness?.name, d.groups.map((g) => [g.name, g.running, g.vms.length]),
      d.switches.map((s) => [s.name, s.links.length]), d.stores.map((s) => [s.key, Math.round(s.used)]), d.nets.map((n) => n.name)]);
    if (sig !== signature) { signature = sig; build(d); }
    for (const { key, b } of beams) b.userData.beam.level = level(d.traffic[key] || 0) || 0.15;
  }
  return { update, dispose: () => stage.dispose() };
}

// ---------- Overview pictures ----------

function hero(container, kind) {
  const stage = new Stage(container, { controls: false, distance: 16, fov: 36, bloom: 0 });
  stage.scene.fog = null;
  const root = stage.root, spin = new THREE.Group(); spin.userData.spin = 0.3; root.add(spin);
  if (kind === "compute") {
    stage.camera.position.set(0, 7, 13); stage.camera.lookAt(0, 2.2, 0);
    const srv = serverOpen(COL.cyan, true); srv.position.y = 0.6; spin.add(srv);
    [["windows", -1.6], ["linux", 1.6]].forEach(([os, x], i) => {
      const c = vmCube(os, true); c.position.set(x, 3.7, 0.2); c.userData.bob = i; c.userData.baseY = 3.7; spin.add(c);
      const dash = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(x, 2.7, 0.2), new THREE.Vector3(x * 0.8, 0.8, 0.4)]), new THREE.LineDashedMaterial({ color: 0x8fdcff, dashSize: 0.16, gapSize: 0.12 }));
      dash.computeLineDistances(); spin.add(dash);
    });
    root.add(squarePad(8, 5, COL.cyan));
  } else if (kind === "storage") {
    stage.camera.position.set(0, 5.5, 12); stage.camera.lookAt(0, 1.4, 0);
    const nas = nasBox(COL.violet); nas.position.y = 1.5; spin.add(nas);
    for (let i = 0; i < 10; i++) { const s = glowSprite(COL.cyan, 0.3); s.userData.orbit = { r: 3.3, y: 0.4 + (i / 10) * 3, speed: 0.9, phase: i * 0.8 }; root.add(s); }
    root.add(squarePad(6.5, 5, COL.violet));
  } else if (kind === "home") { // the whole system at a glance: the cluster and what it serves
    stage.camera.position.set(21, 18, 21); stage.camera.lookAt(0, 0.4, 0);
    spin.userData.spin = 0; spin.userData.sway = true;
    const centerPad = squarePad(7, 7, COL.cyan); spin.add(centerPad);
    for (let i = 0; i < 2; i++) { // two server towers
      const t = new THREE.Group();
      t.add(holo(new THREE.BoxGeometry(1.6, 3.6, 1.8), COL.cyan, 0.06));
      for (let k = 0; k < 7; k++) {
        const u = holo(new THREE.BoxGeometry(1.4, 0.34, 1.7), COL.cyan, 0.12, 0.7); u.position.y = -1.5 + k * 0.5; t.add(u);
        const led = glowSprite(k % 3 ? COL.green : COL.cyan, 0.22); led.position.set(0.55, -1.5 + k * 0.5, 0.92); led.userData.blink = k + i; t.add(led);
      }
      t.position.set(-1.1 + i * 2.2, 1.85, -0.3); spin.add(t);
    }
    const panel = holo(new THREE.PlaneGeometry(1.8, 1.1), COL.cyan, 0.15); panel.position.set(2.6, 4.2, 0.6); panel.rotation.y = -0.6; panel.userData.bob = 0; panel.userData.baseY = 4.2; spin.add(panel);
    const sats = [
      [-7.5, -7.5, COL.green, () => { const g = new THREE.Group(); [["windows", -0.9], ["linux", 0.9]].forEach(([os, x], k) => { const c = vmCube(os, true); c.scale.setScalar(0.55); c.position.set(x, 0.9 + k * 0.2, 0); c.userData.bob = k; c.userData.baseY = c.position.y; g.add(c); }); return g; }],
      [7.5, -7.5, COL.violet, () => { const n = nasBox(COL.violet); n.scale.setScalar(0.55); n.position.y = 0.8; return n; }],
      [7.5, 7.5, COL.pink, () => { const w = netGlobe(6, COL.pink); w.scale.setScalar(0.32); w.position.y = 1.4; return w; }],
      [-7.5, 7.5, COL.amber, () => { const sw = switchBox(); sw.scale.setScalar(0.55); sw.position.y = 0.4; return sw; }],
    ];
    sats.forEach(([x, z, color, make], i) => {
      const g = new THREE.Group(); g.position.set(x, 0, z); g.add(squarePad(4.2, 4.2, color)); g.add(make()); rise(g, 0.25 + i * 0.15); spin.add(g);
      const b = beam(new THREE.Vector3(x * 0.42, 0.05, z * 0.42), new THREE.Vector3(x * 0.74, 0.05, z * 0.74), color, 0); b.userData.beam.level = 0.6; spin.add(b);
    });
  } else {
    stage.camera.position.set(0, 4.5, 17); stage.camera.lookAt(0, 0.4, 0);
    const globe = netGlobe(9); globe.position.y = 0.6; spin.add(globe);
  }
  return { dispose: () => stage.dispose() };
}

window.Fabric3D = { inside, hero };


// Overview: the whole system at rest; pointing at Compute, Storage or Network projects its hologram.
(() => {
  const visual = document.querySelector(".nexus-visual");
  if (!visual) return;
  const homeBox = visual.querySelector(".nexus-alt-home .nexus-3d");
  if (homeBox) { try { hero(homeBox, "home"); visual.classList.add("has-3d"); } catch (err) { console.error(err); } }
  for (const kind of ["compute", "storage", "network"]) {
    const link = visual.querySelector(`.nexus-${kind}`), box = visual.querySelector(`.nexus-alt-${kind} .nexus-3d`);
    if (!link || !box) continue;
    let live = null, timer = 0;
    const show = () => { clearTimeout(timer); if (!live) { try { live = hero(box, kind); } catch (err) { console.error(err); } } };
    const hide = () => { timer = setTimeout(() => { live?.dispose(); live = null; }, 450); };
    link.addEventListener("pointerenter", show); link.addEventListener("focus", show);
    link.addEventListener("pointerleave", hide); link.addEventListener("blur", hide);
  }
})();
