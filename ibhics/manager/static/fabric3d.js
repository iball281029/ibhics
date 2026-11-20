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
      this.tick(dt, t);
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

function inside(container) {
  const stage = new Stage(container);
  backdrop(stage.scene);
  let signature = "";
  const beams = [];
  const R = 19; // radius of the ring of zones

  function zoneAt(angle) { return new THREE.Vector3(Math.cos(angle) * R, 0, Math.sin(angle) * R); }

  function build(d) {
    stage.clear(); beams.length = 0;
    const root = stage.root;

    // Centre: the HCI cluster.
    const core = platform(7.5, COL.cyan); root.add(core);
    const lab = label(`<b>HCI CLUSTER</b><span>${d.nodes.length} nodes${d.witness ? " + witness" : ""} · ${d.vmsRunning}/${d.vmsTotal} VMs running</span>`, "f3-zone f3-cyan");
    lab.position.set(0, 0.4, 9.2); root.add(lab); // at the front edge of the platform
    const members = [...d.nodes.map((n) => ({ n })), ...(d.witness ? [{ w: d.witness }] : [])];
    const ringR = members.length > 1 ? 4 : 0;
    const nodePos = {};
    members.forEach((m, i) => {
      const a = (i / Math.max(1, members.length)) * Math.PI * 2 + Math.PI / 2, x = Math.cos(a) * ringR, z = Math.sin(a) * ringR;
      if (m.n) {
        const n = m.n, r = rack(n.healthy ? COL.cyan : COL.red, n.healthy);
        r.scale.setScalar(1.35); r.position.set(x, 2.3, z); root.add(r); nodePos[n.name] = new THREE.Vector3(x, 1.7, z);
        const l = label(`<b>${esc(n.name)}</b><span>CPU ${n.cpu == null ? "—" : Math.round(n.cpu) + "%"} · MEM ${n.mem == null ? "—" : Math.round(n.mem) + "%"}</span><span>${n.vms} VMs · ${n.healthy ? "online" : "offline"}</span>`, `f3-node ${n.healthy ? "" : "f3-bad"}`, () => { location.hash = `node/${encodeURIComponent(n.name)}`; });
        l.position.set(x * 2.1, 4.4, z * 2.1); root.add(l);
      } else {
        const w = holo(new THREE.BoxGeometry(1.2, 1.2, 1.2), COL.amber, 0.1); w.position.set(x, 0.9, z); w.userData.spinSelf = 0.3; root.add(w);
        const l = label(`<b>WITNESS · ${esc(m.w.name)}</b><span>quorum vote · no VM data</span>`, "f3-node f3-amber");
        l.position.set(x * 2.1, 2.6, z * 2.1); root.add(l);
      }
    });
    const inter = circle(Math.max(1.6, ringR), COL.blue, 0.8, 128, [0.3, 0.2]); inter.position.y = 0.3; inter.userData.spin = 0.6; root.add(inter);

    // Zones around the circle.
    const zones = [
      { key: "vm", title: "WORKLOADS · VM GROUPS", color: COL.green, cls: "f3-green", angle: -Math.PI / 2 },
      { key: "net", title: "NETWORK", color: COL.pink, cls: "f3-pink", angle: Math.PI / 6 },
      { key: "sto", title: "STORAGE", color: COL.violet, cls: "f3-violet", angle: (5 * Math.PI) / 6 },
    ];
    for (const z of zones) {
      const c = zoneAt(z.angle), pad = platform(5.2, z.color); pad.position.copy(c); root.add(pad);
      const head = label(`<b>${z.title}</b>`, `f3-zone ${z.cls}`); head.position.set(c.x * 1.32, 0.4, c.z * 1.32); root.add(head); // outer rim
      const b = beam(new THREE.Vector3(0, 0.3, 0).lerp(c, 0.36), c.clone().lerp(new THREE.Vector3(), 0.24).setY(0.3), z.color, 4);
      root.add(b); beams.push({ key: z.key, b });
      z.center = c;
    }
    const at = (z, dx, y, dz) => new THREE.Vector3(z.center.x + dx, y, z.center.z + dz);

    // VM groups: a cluster of VM tokens per group.
    const vz = zones[0], groups = d.groups.slice(0, 5);
    groups.forEach((g, i) => {
      const a = (i / Math.max(1, groups.length)) * Math.PI * 2, gx = Math.cos(a) * (groups.length > 1 ? 2.6 : 0), gz = Math.sin(a) * (groups.length > 1 ? 2.6 : 0);
      const tokens = g.vms.slice(0, 10);
      tokens.forEach((v, k) => {
        const t = vmToken(v.running), ka = (k / Math.max(1, tokens.length)) * Math.PI * 2;
        t.position.copy(at(vz, gx + Math.cos(ka) * 1.6, 1.8, gz + Math.sin(ka) * 1.6));
        t.userData.baseY = 1.8; t.userData.bob = k * 0.8; root.add(t);
      });
      const l = label(`<b>${esc(g.name)}</b><span>${g.running}/${g.vms.length} running</span>`, "f3-item f3-green", () => { location.hash = "compute"; });
      l.position.copy(at(vz, gx, 3.1, gz)); root.add(l);
    });
    if (!groups.length) { const l = label("<span>No VMs yet</span>", "f3-item f3-green"); l.position.copy(at(vz, 0, 2, 0)); root.add(l); }

    // Network: switch, the outside above it, the cabling and VLANs.
    const nz = zones[1];
    d.switches.slice(0, 2).forEach((s, i) => {
      const sw = switchBox(); sw.position.copy(at(nz, 0, 1 + i * 1.3, (i - (d.switches.length > 1 ? 0.5 : 0)) * 2.6)); root.add(sw);
      const ports = s.links.map((l) => `<span>${esc(l.port || "?")} ⇄ ${esc(l.node)} ${esc(l.iface)}${l.speed ? " · " + esc(l.speed) : ""}</span>`).join("");
      const l = label(`<b>${esc(s.name)}</b><span>${esc([s.model, s.mgmt].filter(Boolean).join(" · ") || "switch")}</span>${ports}`, "f3-item f3-pink");
      l.position.copy(at(nz, 0, 2.3 + i * 1.3, (i - (d.switches.length > 1 ? 0.5 : 0)) * 2.6 + 1.6)); root.add(l);
    });
    const web = internet(); web.position.copy(at(nz, 0, 4.3, -1.6)); root.add(web);
    const ext = label(`<b>EXTERNAL NETWORK</b>${d.nets.slice(0, 5).map((n) => `<span>${esc(n.name)} · ${n.vlan ? "VLAN " + n.vlan : "untagged"}</span>`).join("")}`, "f3-item f3-pink");
    ext.position.copy(at(nz, 0, 6.2, -1.6)); root.add(ext);
    const up = beam(at(nz, 0, 1.4, 0), at(nz, 0, 3.4, -1.6), COL.pink, 0.6); root.add(up); beams.push({ key: "net", b: up });

    // Storage: devices in a row on the storage pad.
    const sz = zones[2], stores = d.stores.slice(0, 4);
    stores.forEach((s, i) => {
      const off = (i - (stores.length - 1) / 2) * 3, m = store(s.kind); m.position.copy(at(sz, off * 0.6, 0.6, off)); root.add(m);
      const l = label(`<b>${esc(s.label)}</b><span>${esc(s.name)} · ${Math.round(s.used)}% of ${Math.round(s.cap)} GB</span><span>${s.kind === "vstore" ? `replicated on ${s.nodes.length} nodes` : esc(s.nodes.join(", "))}</span>`, "f3-item f3-violet", () => { location.hash = "storage"; });
      l.position.copy(at(sz, off * 0.6, 3.4, off)); root.add(l);
    });
    if (!stores.length) { const l = label("<span>No storage pools</span>", "f3-item f3-violet"); l.position.copy(at(sz, 0, 2, 0)); root.add(l); }

    // Pulses rising from the centre.
    for (let k = 0; k < 2; k++) { const p = circle(7.5, COL.cyan, 0.7); p.userData.pulse = k * 0.5; root.add(p); }
  }

  function update(d) {
    const sig = JSON.stringify([d.nodes.map((n) => [n.name, n.healthy, n.vms, Math.round(n.cpu ?? -1), Math.round(n.mem ?? -1)]), d.witness?.name, d.groups.map((g) => [g.name, g.running, g.vms.length]),
      d.switches.map((s) => [s.name, s.links.length]), d.stores.map((s) => [s.key, Math.round(s.used)]), d.nets.map((n) => n.name)]);
    if (sig !== signature) { signature = sig; build(d); }
    for (const { key, b } of beams) b.userData.beam.level = level(d.traffic[key] || 0);
  }
  return { update, dispose: () => stage.dispose() };
}

// ---------- Overview pictures ----------

function hero(container, kind) {
  const stage = new Stage(container, { controls: false, distance: 16, fov: 40, bloom: 0 });
  stage.camera.position.set(0, 6.5, 14); stage.camera.lookAt(0, 2.2, 0);
  stage.scene.fog = null;
  const root = stage.root, spin = new THREE.Group(); spin.userData.spin = 0.35; root.add(spin);
  const pad = platform(4.2, kind === "storage" ? COL.violet : kind === "network" ? COL.pink : COL.cyan); root.add(pad);
  if (kind === "compute") {
    const r = rack(COL.cyan, true); r.position.y = 2.2; spin.add(r);
    for (let i = 0; i < 6; i++) {
      const v = vmToken(true); v.userData.orbit = { r: 3.2, y: 2 + Math.sin(i) * 0.8, speed: 0.6, phase: (i / 6) * Math.PI * 2 }; root.add(v);
    }
    const orbit = circle(3.2, COL.green, 0.5, 128, [0.3, 0.25]); orbit.position.y = 2; orbit.userData.spin = -0.3; root.add(orbit);
  } else if (kind === "storage") {
    for (let i = 0; i < 4; i++) {
      const disk = holo(new THREE.CylinderGeometry(1.9, 1.9, 0.32, 64), COL.violet, 0.12); disk.position.y = 0.9 + i * 0.95; spin.add(disk);
      const ring = circle(2.5, COL.cyan, 0.8, 160, [0.5, 0.3]); ring.position.y = 0.9 + i * 0.95; ring.userData.spin = i % 2 ? -1 : 1; root.add(ring);
      const hub = glowSprite(COL.white, 0.9); hub.position.y = 0.9 + i * 0.95; root.add(hub);
    }
    for (let i = 0; i < 14; i++) { // data rising in a helix
      const s = glowSprite(COL.cyan, 0.35); s.userData.orbit = { r: 2.2, y: 0.6 + (i / 14) * 3.6, speed: 1.1, phase: i * 0.9 }; root.add(s);
    }
  } else {
    const hubM = holo(new THREE.IcosahedronGeometry(0.9, 1), COL.cyan, 0.1); hubM.position.y = 2.6; hubM.userData.spinSelf = 0.5; root.add(hubM);
    const pts = Array.from({ length: 6 }, (_, i) => { const a = (i / 6) * Math.PI * 2; return new THREE.Vector3(Math.cos(a) * 3.3, 1.6 + (i % 2) * 1.6, Math.sin(a) * 3.3); });
    pts.forEach((p, i) => {
      const n = holo(new THREE.SphereGeometry(0.42, 16, 12), COL.pink, 0.15); n.position.copy(p); spin.add(n);
      const b = beam(new THREE.Vector3(0, 2.6, 0), p, i % 2 ? COL.pink : COL.cyan, 0.4); b.userData.beam.level = 0.6; spin.add(b);
    });
    for (let k = 0; k < 2; k++) { const p = circle(1.2, COL.cyan, 0.7); p.position.y = 2.6; p.userData.pulse = k * 0.5; root.add(p); }
  }
  return { dispose: () => stage.dispose() };
}

window.Fabric3D = { inside, hero };

// Overview: pointing at Compute, Storage or Network projects its hologram.
(() => {
  const visual = document.querySelector(".nexus-visual");
  if (!visual) return;
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
