/* Infrastructure map: the cluster in three dimensions plus time.

   Space: nodes are holographic towers with their VMs on top, Virtual Storage is a
   spinning core of data rings under a light column, every network is a ring with
   orbiting satellites, and curved links carry packets with light trails. Time: each
   reading (VM statistics, every 3 s) is kept for 15 minutes; every node wears a
   ribbon of its own traffic history, and the time bar replays any moment.

   Canvas 2D only, no libraries: additive blending gives the glow. Drag to turn
   (with inertia), scroll to zoom, double-click a node to fly to it, click a node or
   a VM to open it. */
window.FabricMap = (() => {
  const page = document.getElementById("page-fabric");
  const canvas = document.getElementById("fabric-canvas");
  const tip = document.getElementById("fabric-tip");
  const hud = document.getElementById("fabric-hud");
  const timeBar = document.getElementById("fabric-time");
  const timeLabel = document.getElementById("fabric-time-label");
  const liveBtn = document.getElementById("fabric-live");
  const playBtn = document.getElementById("fabric-play");
  const nodePanel = document.getElementById("fabric-nodes");
  const ctx = canvas.getContext("2d");
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const HOME = { yaw: -0.6, pitch: 1.05, zoom: 1, cx: 0, cy: 0 };
  const view = { ...HOME, spin: true, vyaw: 0, target: null };
  const NET_COLORS = ["#54e8f0", "#ed99da", "#9d8cff", "#7cf0a8", "#ffcb66", "#ff907a"];
  const HISTORY = 300; // 15 minutes of 3-second readings
  const shown = new Map();
  const history = []; // [{at, nodes:{name:{rx,tx,read,write,cpu}}, nets:{name:{rx,tx}}, links:{key:{up,down}}}]
  const stars = Array.from({ length: 420 }, () => {
    const r = 200 + Math.random() * 1100, a = Math.random() * Math.PI * 2;
    return { x: Math.cos(a) * r, y: Math.sin(a) * r, z: -60 + Math.random() * 760, s: Math.random(), tw: Math.random() * 6 };
  });
  let scene = { nodes: [], vms: [], nets: [], pools: [], links: [], ringR: 400 };
  let hits = [], placed = [], hover = null, frame = 0, poll = 0, drag = null, last = 0, nextStep = 0;
  let cursor = -1, playing = false; // cursor -1: live, else an index into history

  const rate = (bytes) => {
    let n = Math.max(0, bytes || 0) * 8, i = 0;
    const units = ["b/s", "Kb/s", "Mb/s", "Gb/s"];
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
    return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
  };
  const bytesRate = (b) => {
    let n = Math.max(0, b || 0), i = 0;
    const units = ["B/s", "KB/s", "MB/s", "GB/s"];
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
  };
  const hexA = (hex, a) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${Math.max(0, Math.min(1, a))})`;
  };
  const level = (bytes) => Math.min(1, Math.log10(1 + Math.max(0, bytes || 0)) / 8); // 0..1, log scale

  // ---------- scene from the app's state ----------

  function build() {
    const members = (state.cluster?.members || []).filter((m) => m.role === "compute");
    const nets = (state.networks.length ? state.networks : [{ name: "default" }]).map((n, i) => ({
      name: n.name, color: NET_COLORS[i % NET_COLORS.length], height: 170 + i * 52, rx: 0, tx: 0, index: i,
    }));
    const byNet = Object.fromEntries(nets.map((n) => [n.name, n]));
    const R = members.length > 1 ? 260 + members.length * 20 : 0;
    const nodes = members.map((m, i) => {
      const a = (i / Math.max(1, members.length)) * Math.PI * 2 - Math.PI / 2;
      const s = state.summaries?.[m.name] || {};
      return {
        name: m.name, healthy: m.healthy !== false, x: Math.cos(a) * R, y: Math.sin(a) * R, angle: a,
        cpu: Number.isFinite(s.cpu_percent) ? s.cpu_percent : null,
        mem: s.memory_mb ? (s.memory_used_mb / s.memory_mb) * 100 : null,
        read: 0, write: 0, rx: 0, tx: 0, nets: {},
      };
    });
    if (members.length === 1) Object.assign(nodes[0], { y: 230, angle: Math.PI / 2 });
    const byNode = Object.fromEntries(nodes.map((n) => [n.name, n]));
    const vms = [];
    for (const vm of state.vms) {
      const node = byNode[vm.node];
      if (!node) continue;
      const st = state.stats?.[vm.uuid] || {};
      node.read += st.disk_read_bps || 0;
      node.write += st.disk_write_bps || 0;
      node.rx += st.net_rx_bps || 0;
      node.tx += st.net_tx_bps || 0;
      const nics = (vm.nics || []).filter((n) => byNet[n.network]);
      for (const nic of nics) {
        const share = 1 / nics.length, link = (node.nets[nic.network] ||= { rx: 0, tx: 0 });
        link.rx += (st.net_rx_bps || 0) * share; link.tx += (st.net_tx_bps || 0) * share;
        byNet[nic.network].rx += (st.net_rx_bps || 0) * share; byNet[nic.network].tx += (st.net_tx_bps || 0) * share;
      }
      vms.push({ vm, node, running: vm.state === "running", cpu: st.cpu_pct, rx: st.net_rx_bps || 0, tx: st.net_tx_bps || 0 });
    }
    const pools = [];
    for (const p of state.pools || []) {
      if (pools.some((x) => x.key === p.name)) continue;
      pools.push({ key: p.name, name: p.display_name || p.name, used: p.capacity_gb ? (p.allocation_gb / p.capacity_gb) * 100 : 0, cap: p.capacity_gb || 0 });
    }
    pools.sort((a, b) => b.cap - a.cap).splice(4);
    const ringR = nodes.length > 1 ? R + 190 : 400;
    const links = [];
    for (const node of nodes) {
      links.push({ key: `io:${node.name}`, kind: "io", node, color: "#bb83ff",
        a: [node.x, node.y, 34], b: [0, 0, 40 + pools.length * 24], up: node.write, down: node.read });
      for (const [name, t] of Object.entries(node.nets)) {
        const net = byNet[name];
        links.push({ key: `net:${node.name}:${name}`, kind: "net", node, net, color: net.color,
          a: [node.x, node.y, towerTop(node, vms)], b: [Math.cos(node.angle) * ringR, Math.sin(node.angle) * ringR, net.height],
          up: t.tx, down: t.rx });
      }
    }
    scene = { nodes, vms, nets, pools, links, ringR };
    record();
    renderHud();
  }
  const towerTop = (node, vms) => 110 + Math.ceil(vms.filter((v) => v.node === node).length / 9) * 22;

  // ---------- the fourth dimension: time ----------

  function record() {
    const snap = { at: Date.now(), nodes: {}, nets: {}, links: {} };
    for (const n of scene.nodes) snap.nodes[n.name] = { rx: n.rx, tx: n.tx, read: n.read, write: n.write, cpu: n.cpu };
    for (const n of scene.nets) snap.nets[n.name] = { rx: n.rx, tx: n.tx };
    for (const l of scene.links) snap.links[l.key] = { up: l.up, down: l.down };
    const prev = history.at(-1);
    if (prev && snap.at - prev.at < 1500) history[history.length - 1] = snap; else history.push(snap);
    if (history.length > HISTORY) { history.shift(); if (cursor > 0) cursor--; }
    timeBar.max = String(Math.max(0, history.length - 1));
    if (cursor < 0) timeBar.value = timeBar.max;
    showTime();
  }
  const moment = () => (cursor < 0 ? history.at(-1) : history[cursor]) || { nodes: {}, nets: {}, links: {} };
  function showTime() {
    const snap = moment(), live = cursor < 0;
    page.classList.toggle("fabric-replay", !live);
    liveBtn.setAttribute("aria-pressed", String(live));
    timeLabel.textContent = live
      ? `LIVE · ${history.length} readings kept`
      : `REPLAY · T−${Math.max(0, Math.round((Date.now() - snap.at) / 1000))} s · ${new Date(snap.at).toLocaleTimeString()}`;
  }
  function setPlaying(on) { playing = on; playBtn.textContent = on ? "❚❚ Pause" : "▶ Replay"; playBtn.setAttribute("aria-pressed", String(on)); }
  timeBar.addEventListener("input", () => {
    const i = Number(timeBar.value);
    cursor = i >= history.length - 1 ? -1 : i;
    setPlaying(false); showTime(); renderHud();
  });
  liveBtn.addEventListener("click", () => { cursor = -1; setPlaying(false); timeBar.value = timeBar.max; showTime(); renderHud(); });
  playBtn.addEventListener("click", () => {
    if (!playing && (cursor < 0 || cursor >= history.length - 1)) cursor = 0;
    setPlaying(!playing); timeBar.value = String(cursor < 0 ? timeBar.max : cursor); showTime(); renderHud();
  });
  function step() {
    cursor = cursor < 0 ? 0 : cursor + 1;
    if (cursor >= history.length - 1) { cursor = -1; setPlaying(false); }
    timeBar.value = String(cursor < 0 ? timeBar.max : cursor);
    showTime(); renderHud();
  }

  function renderNodes() {
    const snap = moment();
    const bar = (p, cls) => `<div class="fn-bar ${cls}"><i style="width:${Math.max(0, Math.min(100, p || 0))}%"></i></div>`;
    nodePanel.innerHTML = `<h4>NODES</h4>` + scene.nodes.map((n) => {
      const live = snap.nodes[n.name] || {}, vms = scene.vms.filter((v) => v.node === n), cpu = live.cpu ?? n.cpu;
      return `<a class="fn-node${n.healthy ? "" : " off"}" href="#node/${encodeURIComponent(n.name)}">
        <div class="fn-top"><b>${esc(n.name)}</b><span>${n.healthy ? "Online" : "Offline"}</span></div>
        <div class="fn-row"><span>CPU</span>${bar(cpu, "cpu")}<em>${cpu == null ? "—" : cpu.toFixed(0) + "%"}</em></div>
        <div class="fn-row"><span>MEM</span>${bar(n.mem, "mem")}<em>${n.mem == null ? "—" : n.mem.toFixed(0) + "%"}</em></div>
        <div class="fn-io"><span title="Network received / sent by its VMs">⇅ ${rate(live.rx)} / ${rate(live.tx)}</span><span title="Disk read / write by its VMs">⛁ ${bytesRate(live.read)} / ${bytesRate(live.write)}</span></div>
        <div class="fn-vms">${vms.length ? vms.map((v) => `<i class="${v.running ? "on" : ""}" title="${esc(v.vm.name)}"></i>`).join("") : "<small>No VMs</small>"} <small>${vms.filter((v) => v.running).length}/${vms.length} VMs running</small></div>
      </a>`;
    }).join("");
  }

  function renderHud() {
    renderNodes();
    const snap = moment();
    const nets = Object.values(snap.nets), nodes = Object.values(snap.nodes);
    const rx = nets.reduce((s, n) => s + n.rx, 0), tx = nets.reduce((s, n) => s + n.tx, 0);
    const io = nodes.reduce((s, n) => s + n.read + n.write, 0);
    const cell = (label, value, cls = "") => `<div class="fabric-stat ${cls}"><span>${label}</span><b>${value}</b></div>`;
    hud.innerHTML = cell("NODES", `${scene.nodes.filter((n) => n.healthy).length}<small>/${scene.nodes.length}</small>`)
      + cell("VMS RUNNING", `${scene.vms.filter((v) => v.running).length}<small>/${scene.vms.length}</small>`)
      + cell("NETWORK IN", rate(rx), "c-cyan") + cell("NETWORK OUT", rate(tx), "c-pink")
      + cell("STORAGE I/O", bytesRate(io), "c-violet");
  }

  // ---------- projection ----------

  let W = 0, H = 0;
  function project(x, y, z) {
    x -= view.cx; y -= view.cy;
    const c = Math.cos(view.yaw), s = Math.sin(view.yaw);
    const x1 = x * c - y * s, y1 = x * s + y * c;
    const cp = Math.cos(view.pitch), sp = Math.sin(view.pitch);
    const depth = y1 * sp - (z - 90) * cp, up = y1 * cp + (z - 90) * sp;
    const fit = (Math.min(W, H * 1.6) / 1300) * view.zoom, k = 2400 / Math.max(300, 2400 + depth);
    return [W / 2 + x1 * k * fit, H * 0.5 - up * k * fit, depth, k * fit];
  }
  const P = (p) => project(p[0], p[1], p[2]);

  // ---------- primitives ----------

  function path(points, close) {
    ctx.beginPath();
    points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (close) ctx.closePath();
  }
  // Glow without shadowBlur (slow on canvas): a wide faint stroke under the line, added
  // together by the "lighter" blend.
  function glowLine(points, color, width, glow, close = false) {
    path(points, close);
    ctx.strokeStyle = color;
    if (glow > 0) {
      ctx.globalAlpha = 0.16; ctx.lineWidth = width + glow * 0.7; ctx.stroke();
      ctx.globalAlpha = 1;
    }
    ctx.lineWidth = width; ctx.stroke();
  }
  const sprites = new Map();
  function sprite(color) { // a soft round light, drawn once per colour
    if (sprites.has(color)) return sprites.get(color);
    const c = document.createElement("canvas");
    c.width = c.height = 64;
    const g = c.getContext("2d"), r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    r.addColorStop(0, "#ffffff"); r.addColorStop(0.18, color); r.addColorStop(0.45, hexA(color, 0.35)); r.addColorStop(1, hexA(color, 0));
    g.fillStyle = r; g.fillRect(0, 0, 64, 64);
    sprites.set(color, c);
    return c;
  }
  function circle3d(cx, cy, r, z, steps = 96, from = 0, to = Math.PI * 2) {
    const pts = [];
    for (let i = 0; i <= steps; i++) {
      const a = from + ((to - from) * i) / steps;
      pts.push(project(cx + Math.cos(a) * r, cy + Math.sin(a) * r, z));
    }
    return pts;
  }
  function dot(p, r, color, blur = 14) {
    const size = Math.max(2, r * (blur > 6 ? 5 : 3));
    ctx.drawImage(sprite(color), p[0] - size / 2, p[1] - size / 2, size, size);
  }
  function label(x, y, lines, color, opts = {}) {
    ctx.font = `${opts.bold ? "600 " : ""}${opts.size || 11}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 18, h = lines.length * 15 + 10;
    const bx = x - w / 2;
    let by = y - h - (opts.lift ?? 12);
    for (let tries = 0; tries < 6; tries++) { // never cover a label already drawn
      const hit = placed.find((r) => bx < r.x + r.w + 4 && bx + w + 4 > r.x && by < r.y + r.h + 3 && by + h + 3 > r.y);
      if (!hit) break;
      by = hit.y - h - 4;
    }
    placed.push({ x: bx, y: by, w, h });
    const g = ctx.createLinearGradient(bx, by, bx + w, by + h);
    g.addColorStop(0, "rgba(14,18,46,.86)"); g.addColorStop(1, "rgba(30,16,52,.82)");
    ctx.fillStyle = g; ctx.strokeStyle = hexA(color, 0.75); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.roundRect(bx, by, w, h, 6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = hexA(color, 0.95); ctx.fillRect(bx + 6, by, Math.min(26, w - 12), 2); // HUD notch
    ctx.textAlign = "center";
    lines.forEach((l, i) => { ctx.fillStyle = i === 0 && opts.title ? "#f3f6ff" : color; ctx.fillText(l, x, by + 18 + i * 15); });
  }
  function tween(key, value) {
    const v = shown.get(key) ?? 0;
    const next = still.matches ? value : v + (value - v) * 0.09;
    shown.set(key, Math.abs(next - value) < 0.5 ? value : next);
    return shown.get(key);
  }

  // ---------- frame ----------

  function draw(t) {
    W = canvas.clientWidth; H = canvas.clientHeight;
    const dpr = devicePixelRatio || 1;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.clearRect(0, 0, W, H);
    const dt = Math.min(64, t - last || 16); last = t;
    camera(dt, t);
    if (playing && t >= nextStep) { nextStep = t + 450; step(); }
    hits = []; placed = [];
    const snap = moment();
    const { nodes, vms, nets, pools, links, ringR } = scene;

    ctx.globalCompositeOperation = "lighter";
    drawStars(t);
    drawFloor(t, ringR + 110);
    nets.forEach((net) => drawNetRing(net, ringR, t, snap));
    const items = [{ d: project(0, 0, 0)[2], draw: () => drawCore(pools, t, snap) }];
    nodes.forEach((node) => items.push({ d: project(node.x, node.y, 0)[2], draw: () => drawNode(node, vms.filter((v) => v.node === node), t, snap) }));
    items.sort((a, b) => b.d - a.d).forEach((i) => i.draw());
    links.forEach((link) => drawLink(link, t, snap));
    ctx.globalCompositeOperation = "source-over";
    nets.forEach((net) => netLabel(net, ringR, snap));
    labels(snap);
    drawScan(t);
  }

  function camera(dt, t) {
    if (view.target) {
      const k = 1 - Math.pow(0.9, dt / 16);
      for (const key of ["cx", "cy", "zoom", "pitch"]) view[key] += (view.target[key] - view[key]) * k;
      if (Math.abs(view.target.zoom - view.zoom) < 0.002 && Math.abs(view.target.cx - view.cx) < 0.5) view.target = null;
    }
    if (!drag) {
      view.yaw += view.vyaw * dt; view.vyaw *= Math.pow(0.94, dt / 16);
      if (view.spin && !still.matches) {
        view.yaw += dt * 0.00004;
        if (!view.target) view.pitch = Math.max(0.3, Math.min(1.4, view.pitch + Math.sin(t / 5200) * 0.00005 * dt));
      }
    }
  }

  function drawStars(t) {
    for (const s of stars) {
      const p = project(s.x, s.y, s.z);
      if (p[0] < -5 || p[0] > W + 5 || p[1] < -5 || p[1] > H + 5) continue;
      const tw = still.matches ? 0.6 : 0.45 + 0.4 * Math.sin(t / 900 + s.tw);
      ctx.fillStyle = s.s > 0.82 ? `rgba(237,153,218,${tw * 0.7})` : `rgba(140,200,255,${tw * 0.55})`;
      const r = Math.max(0.4, p[3] * (0.8 + s.s * 1.6));
      ctx.fillRect(p[0] - r / 2, p[1] - r / 2, r, r);
    }
  }

  function drawFloor(t, R) {
    for (let r = 110; r <= R; r += 110) glowLine(circle3d(0, 0, r, 0), "rgba(125,160,255,.10)", 1, 0);
    for (let i = 0; i < 24; i++) {
      const a = (i / 24) * Math.PI * 2;
      path([project(0, 0, 0), project(Math.cos(a) * R, Math.sin(a) * R, 0)]);
      ctx.strokeStyle = "rgba(125,160,255,.05)"; ctx.lineWidth = 1; ctx.stroke();
    }
    if (still.matches) return;
    const a = (t / 2600) % (Math.PI * 2); // radar sweep
    for (let k = 0; k < 14; k++) {
      const b = a - k * 0.03;
      path([project(0, 0, 0), project(Math.cos(b) * R, Math.sin(b) * R, 0)]);
      ctx.strokeStyle = `rgba(84,232,240,${0.16 * (1 - k / 14)})`; ctx.lineWidth = 2; ctx.stroke();
    }
    for (let k = 0; k < 3; k++) { // pulse rings from the core
      const f = (t / 4200 + k / 3) % 1;
      glowLine(circle3d(0, 0, 60 + f * (R - 60), 0, 72), `rgba(187,131,255,${0.22 * (1 - f)})`, 1.5, 6);
    }
  }

  function drawNetRing(net, R, t, snap) {
    const live = snap.nets[net.name] || { rx: 0, tx: 0 }, lvl = level(live.rx + live.tx);
    glowLine(circle3d(0, 0, R, net.height), hexA(net.color, 0.12 + lvl * 0.2), 7, 0);
    const spin = still.matches ? 0 : t * (0.00012 + lvl * 0.0006) * (net.index % 2 ? -1 : 1);
    for (const [off, alpha, dr, dz] of [[0, 0.95, 0, 0], [Math.PI / 24, 0.45, 7, 5]]) { // two strands
      for (let seg = 0; seg < 24; seg++) {
        const from = spin + off + (seg / 24) * Math.PI * 2;
        glowLine(circle3d(0, 0, R + dr, net.height + dz, 6, from, from + Math.PI / 36), hexA(net.color, alpha), 1.8, 10);
      }
    }
    for (let k = 0; k < 3; k++) { // satellites
      const a = -spin * 3 + (k / 3) * Math.PI * 2;
      dot(project(Math.cos(a) * R, Math.sin(a) * R, net.height), 3.2, net.color, 18);
    }
  }

  function netLabel(net, R, snap) {
    const live = snap.nets[net.name] || { rx: 0, tx: 0 };
    const a = -view.yaw + 0.35 - net.index * 0.5, [rx, ry] = P([Math.cos(a) * R, Math.sin(a) * R, net.height]);
    const x = W - 140, y = 150 + net.index * 64;
    ctx.beginPath(); ctx.moveTo(rx, ry); ctx.lineTo(x - 70, y - 14);
    ctx.strokeStyle = hexA(net.color, 0.45); ctx.lineWidth = 1; ctx.stroke();
    dot([rx, ry], 3, net.color, 12);
    label(x, y, [net.name.toUpperCase(), `↓ ${rate(tween(`n:${net.name}:rx`, live.rx))}  ↑ ${rate(tween(`n:${net.name}:tx`, live.tx))}`], net.color, { title: true, lift: 0 });
  }

  function drawCore(pools, t, snap) {
    const levels = pools.length ? pools : [{ name: "No storage pool", used: 0 }];
    const io = Object.values(snap.nodes).reduce((s, n) => s + n.read + n.write, 0), lvl = level(io);
    const base = project(0, 0, 0), top = project(0, 0, 430); // light column
    const g = ctx.createLinearGradient(base[0], base[1], top[0], top[1]);
    g.addColorStop(0, `rgba(187,131,255,${0.16 + lvl * 0.26})`); g.addColorStop(1, "rgba(84,232,240,0)");
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.moveTo(base[0] - 26 * base[3], base[1]); ctx.lineTo(top[0] - 3, top[1]); ctx.lineTo(top[0] + 3, top[1]); ctx.lineTo(base[0] + 26 * base[3], base[1]); ctx.fill();
    levels.forEach((pool, i) => {
      const z0 = 12 + i * 24, z1 = z0 + 16, r = 96 - i * 7;
      glowLine(circle3d(0, 0, r, z0), "rgba(187,131,255,.45)", 1.2, 4);
      glowLine(circle3d(0, 0, r, z1), "rgba(187,131,255,.75)", 1.4, 8);
      glowLine(circle3d(0, 0, r, z1 + 1, 64, -Math.PI / 2, -Math.PI / 2 + (pool.used / 100) * Math.PI * 2), "#ed99da", 3.2, 14);
      const spin = still.matches ? 0 : (t / (1800 - lvl * 900)) * (i % 2 ? -1 : 1);
      for (let k = 0; k < 6; k++) { // data segments spinning in alternate directions
        const from = spin + (k / 6) * Math.PI * 2;
        glowLine(circle3d(0, 0, r + 12, (z0 + z1) / 2, 8, from, from + 0.35), "rgba(84,232,240,.75)", 2, 10);
      }
    });
    const crown = 12 + levels.length * 24;
    const pulse = still.matches ? 0.6 : 0.45 + 0.35 * Math.sin(t / 500);
    glowLine(circle3d(0, 0, 26, crown + 6, 48), `rgba(125,229,255,${pulse})`, 2.2, 16);
    dot(project(0, 0, crown + 8), 5 + lvl * 4, "#e9f6ff", 30);
  }

  function prism(cx, cy, z0, z1, half, color, alpha, edges = 0.8) {
    const c = [[-half, -half], [half, -half], [half, half], [-half, half]];
    const bottom = c.map(([dx, dy]) => project(cx + dx, cy + dy, z0));
    const top = c.map(([dx, dy]) => project(cx + dx, cy + dy, z1));
    const sides = [0, 1, 2, 3].map((i) => ({ i, d: (bottom[i][2] + bottom[(i + 1) % 4][2]) / 2 })).sort((a, b) => b.d - a.d);
    for (const { i } of sides) {
      const j = (i + 1) % 4, g = ctx.createLinearGradient(bottom[i][0], bottom[i][1], top[i][0], top[i][1]);
      g.addColorStop(0, hexA(color, alpha * 0.4)); g.addColorStop(1, hexA(color, alpha));
      path([bottom[i], bottom[j], top[j], top[i]], true); ctx.fillStyle = g; ctx.fill();
    }
    path(top, true); ctx.fillStyle = hexA(color, alpha * 1.3); ctx.fill();
    glowLine(top, hexA("#ffffff", edges * 0.6), 1, 6, true);
    for (let i = 0; i < 4; i++) glowLine([bottom[i], top[i]], hexA(color, edges), 1, 8);
    glowLine(bottom, hexA(color, edges * 0.6), 1, 4, true);
    return top;
  }

  function drawNode(node, vms, t, snap) {
    const color = node.healthy ? "#4fafff" : "#ff6b7a";
    const live = snap.nodes[node.name] || {};
    const spin = still.matches ? 0 : t / 4000;
    glowLine(circle3d(node.x, node.y, 74, 1, 6, spin, spin + Math.PI * 2), hexA(color, 0.5), 1.4, 10, true); // hex pad
    glowLine(circle3d(node.x, node.y, 86, 1, 48, -spin, -spin + Math.PI * 1.3), hexA("#54e8f0", 0.35), 1, 6);
    const top = prism(node.x, node.y, 0, 100, 48, color, 0.16);
    const cpu = live.cpu ?? node.cpu;
    if (cpu != null) prism(node.x, node.y, 2, 2 + 94 * Math.min(1, cpu / 100), 40, "#54e8f0", 0.12, 0.3);
    if (!still.matches) { // scan line climbing the glass
      const z = (t / 22) % 100;
      glowLine(circle3d(node.x, node.y, 48 * Math.SQRT2, z, 4, Math.PI / 4, Math.PI / 4 + Math.PI * 2), "rgba(220,250,255,.55)", 1.4, 14, true);
    }
    hits.push({ kind: "node", node, pts: top });
    ribbon(node, color);
    vms.forEach((v, i) => {
      const layer = Math.floor(i / 9), slot = i % 9, gx = (slot % 3) - 1, gy = Math.floor(slot / 3) - 1;
      const cx = node.x + gx * 28, cy = node.y + gy * 28, z0 = 103 + layer * 22;
      const pulse = v.running && !still.matches ? 0.12 * Math.sin(t / 380 + i) : 0;
      const col = v.running ? (hover?.v === v ? "#ffffff" : "#7cf0a8") : "#6c7590";
      const pts = prism(cx, cy, z0, z0 + 15, 10, col, (v.running ? 0.32 : 0.12) + pulse, v.running ? 0.9 : 0.4);
      hits.push({ kind: "vm", v, pts });
    });
  }

  function ribbon(node, color) {
    // The node's traffic over time as a curved wall behind the tower: the 4th dimension.
    if (history.length < 2) return;
    const span = Math.min(history.length, 120), from = history.length - span;
    const r = 72, arc = Math.PI * 1.2, start = node.angle + Math.PI - arc / 2;
    const vals = history.slice(from).map((h) => { const n = h.nodes[node.name]; return n ? level(n.rx + n.tx + n.read + n.write) : 0; });
    const pts = vals.map((v, i) => {
      const a = start + (i / (span - 1)) * arc, x = node.x + Math.cos(a) * r, y = node.y + Math.sin(a) * r;
      return [project(x, y, 6), project(x, y, 6 + v * 160)];
    });
    ctx.beginPath();
    pts.forEach(([, hi], i) => (i ? ctx.lineTo(hi[0], hi[1]) : ctx.moveTo(hi[0], hi[1])));
    for (let i = pts.length - 1; i >= 0; i--) ctx.lineTo(pts[i][0][0], pts[i][0][1]);
    ctx.closePath(); ctx.fillStyle = hexA(color, 0.1); ctx.fill();
    glowLine(pts.map(([, hi]) => hi), hexA("#54e8f0", 0.8), 1.4, 10);
    if (cursor >= from) { const [lo, hi] = pts[cursor - from]; glowLine([lo, hi], "#ffffff", 1.6, 12); }
  }

  function drawLink(link, t, snap) {
    const live = snap.links[link.key] || { up: 0, down: 0 };
    const total = live.up + live.down, lvl = level(total), hot = hover?.node === link.node || hover?.v?.node === link.node;
    const mid = link.a.map((v, k) => (v + link.b[k]) / 2);
    mid[2] += link.kind === "io" ? 70 : 40; // arc through space
    const at = (f) => { const u = 1 - f; return link.a.map((v, k) => u * u * v + 2 * u * f * mid[k] + f * f * link.b[k]); };
    const pts = Array.from({ length: 33 }, (_, i) => P(at(i / 32)));
    glowLine(pts, hexA(link.color, total > 0 ? 0.25 + lvl * 0.5 + (hot ? 0.25 : 0) : 0.12), total > 0 ? 1.4 + lvl * 2.5 : 1, total > 0 ? 12 : 0);
    if (total > 0 && !still.matches) {
      const count = 2 + Math.round(lvl * 9), speed = 0.00012 + lvl * 0.0007;
      for (const [dir, amount, col] of [[1, live.down, link.color], [-1, live.up, "#ffffff"]]) {
        if (amount <= 0) continue;
        for (let i = 0; i < count; i++) {
          const f0 = (t * speed + i / count) % 1;
          for (let tail = 0; tail < 6; tail++) { // comet trail
            const f = f0 - tail * 0.012;
            if (f < 0) continue;
            ctx.globalAlpha = 1 - tail / 6;
            dot(P(at(dir > 0 ? 1 - f : f)), (2.4 + lvl * 2) * (1 - tail / 8), col, tail ? 4 : 16);
          }
          ctx.globalAlpha = 1;
        }
      }
    }
    link.labelAt = P(at(link.kind === "io" ? 0.62 : 0.4 + (0.45 * link.net.index) / Math.max(1, scene.nets.length - 1)));
  }

  function labels(snap) {
    const pools = scene.pools.length ? scene.pools : [{ name: "No storage pool", used: 0 }];
    const crown = project(0, 0, 12 + pools.length * 24 + 46);
    label(crown[0], crown[1], ["VIRTUAL STORAGE", ...pools.map((p) => `${p.name}  ${p.used.toFixed(0)}%`)], "#bb83ff", { title: true, bold: true });
    for (const node of scene.nodes) {
      const live = snap.nodes[node.name] || {}, vms = scene.vms.filter((v) => v.node === node), cpu = live.cpu ?? node.cpu;
      const anchor = project(node.x, node.y, towerTop(node, scene.vms) + 8);
      const [x, y] = project(node.x, node.y, towerTop(node, scene.vms) + 40);
      const color = node.healthy ? "#4fafff" : "#ff6b7a";
      label(x, y, [node.name, `${vms.filter((v) => v.running).length}/${vms.length} VMs · CPU ${cpu == null ? "—" : cpu.toFixed(0) + "%"}`], color, { title: true, bold: true });
      const box = placed.at(-1);
      ctx.beginPath(); ctx.moveTo(anchor[0], anchor[1]); ctx.lineTo(x, box.y + box.h);
      ctx.strokeStyle = hexA(color, 0.6); ctx.lineWidth = 1; ctx.stroke();
    }
    const focus = hover?.node || hover?.v?.node; // figures on links only for the node pointed at
    for (const link of scene.links) {
      if (!link.labelAt || link.node !== focus) continue;
      const live = snap.links[link.key] || { up: 0, down: 0 };
      const up = tween(`${link.key}:up`, live.up), down = tween(`${link.key}:down`, live.down);
      label(link.labelAt[0], link.labelAt[1], [link.kind === "io" ? `R ${bytesRate(down)} · W ${bytesRate(up)}` : `↓ ${rate(down)} ↑ ${rate(up)}`], link.color, { size: 10, lift: 4 });
    }
  }

  function drawScan(t) {
    if (still.matches) return;
    const y = ((t / 9) % (H + 200)) - 100; // a faint holographic scan down the view
    const g = ctx.createLinearGradient(0, y - 40, 0, y + 2);
    g.addColorStop(0, "rgba(125,229,255,0)"); g.addColorStop(1, "rgba(125,229,255,.07)");
    ctx.fillStyle = g; ctx.fillRect(0, y - 40, W, 42);
  }

  // ---------- interaction ----------

  function inside(pts, x, y) {
    let c = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [xi, yi] = pts[i], [xj, yj] = pts[j];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
    }
    return c;
  }
  function pick(event) {
    const r = canvas.getBoundingClientRect(), x = event.clientX - r.left, y = event.clientY - r.top;
    return [...hits].reverse().find((h) => inside(h.pts, x, y)) || null;
  }
  canvas.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, y: e.clientY, yaw: view.yaw, pitch: view.pitch, moved: false, lastX: e.clientX, lastT: performance.now() };
    view.vyaw = 0;
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y, now = performance.now();
      if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
      view.yaw = drag.yaw + dx * 0.006;
      view.pitch = Math.max(0.3, Math.min(1.4, drag.pitch + dy * 0.004));
      view.vyaw = ((e.clientX - drag.lastX) * 0.006) / Math.max(1, now - drag.lastT);
      drag.lastX = e.clientX; drag.lastT = now;
      return;
    }
    hover = pick(e);
    canvas.style.cursor = hover ? "pointer" : "grab";
    if (!hover) { tip.hidden = true; return; }
    const r = page.getBoundingClientRect();
    tip.hidden = false;
    tip.style.left = `${e.clientX - r.left + 16}px`; tip.style.top = `${e.clientY - r.top + 16}px`;
    if (hover.kind === "vm") {
      const v = hover.v;
      tip.innerHTML = `<b>${esc(v.vm.name)}</b><span>${esc(v.vm.state)} · ${v.vm.vcpus} vCPU · ${gbText(v.vm.memory_mb)}</span><span>CPU ${Number.isFinite(v.cpu) ? v.cpu.toFixed(0) + "%" : "—"} · ↓ ${rate(v.rx)} ↑ ${rate(v.tx)}</span><em>Click to open</em>`;
    } else {
      const n = hover.node;
      tip.innerHTML = `<b>${esc(n.name)}</b><span>${n.healthy ? "Online" : "Offline"} · CPU ${n.cpu == null ? "—" : n.cpu.toFixed(0) + "%"}</span><span>Network ↓ ${rate(n.rx)} ↑ ${rate(n.tx)}</span><span>Disk R ${bytesRate(n.read)} · W ${bytesRate(n.write)}</span><em>Click to open · double-click to fly there</em>`;
    }
  });
  canvas.addEventListener("pointerup", (e) => {
    const moved = drag?.moved;
    drag = null;
    if (moved) return;
    const hit = pick(e);
    clearTimeout(canvas.clickTimer);
    canvas.clickTimer = setTimeout(() => { // a double-click flies instead
      if (hit?.kind === "vm") location.hash = `vm/${encodeURIComponent(hit.v.vm.uuid)}`;
      else if (hit?.kind === "node") location.hash = `node/${encodeURIComponent(hit.node.name)}`;
    }, 280);
  });
  canvas.addEventListener("dblclick", (e) => {
    clearTimeout(canvas.clickTimer);
    const hit = pick(e), node = hit?.node || hit?.v?.node;
    view.target = node ? { cx: node.x, cy: node.y, zoom: 1.9, pitch: 0.82 } : { ...HOME };
  });
  canvas.addEventListener("pointerleave", () => { tip.hidden = true; hover = null; });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault(); view.target = null;
    view.zoom = Math.max(0.45, Math.min(3, view.zoom * (e.deltaY > 0 ? 0.92 : 1.08)));
  }, { passive: false });
  document.getElementById("fabric-spin").addEventListener("click", (e) => {
    view.spin = !view.spin;
    e.currentTarget.textContent = view.spin ? "Pause rotation" : "Rotate";
    e.currentTarget.setAttribute("aria-pressed", String(!view.spin));
  });
  document.getElementById("fabric-reset").addEventListener("click", () => { view.target = { ...HOME }; });

  function loop(t) {
    if (!page.classList.contains("active")) { frame = 0; return; }
    try { draw(t); } catch (err) { console.error("fabric draw failed", err?.stack || err); }
    frame = requestAnimationFrame(loop);
  }
  function start() {
    build();
    if (!frame) frame = requestAnimationFrame(loop);
    clearInterval(poll);
    poll = setInterval(async () => {
      if (!page.classList.contains("active")) { clearInterval(poll); poll = 0; return; }
      if (document.hidden) return;
      try { await load("stats"); build(); } catch { /* next tick */ }
    }, 3000);
  }
  return { render: start, rate, bytesRate, view };
})();
