/* Infrastructure map: the whole cluster in 3D, drawn on a canvas.
   Nodes are glass towers with their VMs on top, Virtual Storage is the core in the
   middle and every network is a ring around the cluster. Links carry live traffic
   (from /vms/stats): particles flow faster with more traffic and the figures count
   towards the latest reading. Drag to turn, scroll to zoom, click a node or a VM. */
window.FabricMap = (() => {
  const page = document.getElementById("page-fabric");
  const canvas = document.getElementById("fabric-canvas");
  const tip = document.getElementById("fabric-tip");
  const hud = document.getElementById("fabric-hud");
  const ctx = canvas.getContext("2d");
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const view = { yaw: -0.6, pitch: 0.95, zoom: 1, spin: true };
  const shown = new Map(); // tweened figures, keyed by link
  let scene = { nodes: [], vms: [], nets: [], pools: [], links: [] };
  let hits = [], placed = [], hover = null, frame = 0, poll = 0, drag = null, last = 0;
  const NET_COLORS = ["#54e8f0", "#ed99da", "#9d8cff", "#7cf0a8", "#ffcb66", "#ff907a"];

  const rate = (bytes) => {
    const v = Math.max(0, bytes || 0) * 8;
    const units = ["b/s", "Kb/s", "Mb/s", "Gb/s"];
    let i = 0, n = v;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
    return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
  };
  const bytesRate = (b) => {
    const units = ["B/s", "KB/s", "MB/s", "GB/s"];
    let i = 0, n = Math.max(0, b || 0);
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
  };

  // ---------- scene from the app's state ----------

  function build() {
    const members = (state.cluster?.members || []).filter((m) => m.role === "compute");
    const nets = (state.networks.length ? state.networks : [{ name: "default", nodes: [] }]).map((n, i) => ({
      name: n.name, color: NET_COLORS[i % NET_COLORS.length], height: 150 + i * 46, rx: 0, tx: 0,
    }));
    const byNet = Object.fromEntries(nets.map((n) => [n.name, n]));
    const R = members.length > 1 ? 250 + members.length * 18 : 0;
    const nodes = members.map((m, i) => {
      const a = (i / Math.max(1, members.length)) * Math.PI * 2 - Math.PI / 2;
      const s = state.summaries?.[m.name] || {};
      return {
        name: m.name, healthy: m.healthy !== false, x: Math.cos(a) * R, y: Math.sin(a) * R, angle: a,
        cpu: Number.isFinite(s.cpu_percent) ? s.cpu_percent : null,
        mem: s.memory_mb ? (s.memory_used_mb / s.memory_mb) * 100 : null,
        read: 0, write: 0, nets: {},
      };
    });
    if (members.length === 1) nodes[0].y = 210;
    const byNode = Object.fromEntries(nodes.map((n) => [n.name, n]));
    const vms = [];
    for (const vm of state.vms) {
      const node = byNode[vm.node];
      if (!node) continue;
      const st = state.stats?.[vm.uuid] || {};
      const running = vm.state === "running";
      node.read += st.disk_read_bps || 0;
      node.write += st.disk_write_bps || 0;
      const nics = (vm.nics || []).filter((n) => byNet[n.network]);
      for (const nic of nics) {
        const share = 1 / nics.length, link = (node.nets[nic.network] ||= { rx: 0, tx: 0 });
        link.rx += (st.net_rx_bps || 0) * share;
        link.tx += (st.net_tx_bps || 0) * share;
        byNet[nic.network].rx += (st.net_rx_bps || 0) * share;
        byNet[nic.network].tx += (st.net_tx_bps || 0) * share;
      }
      const index = vms.filter((v) => v.node === node).length;
      vms.push({ vm, node, index, running, cpu: st.cpu_pct, rx: st.net_rx_bps || 0, tx: st.net_tx_bps || 0 });
    }
    const pools = [];
    for (const p of state.pools || []) {
      if (pools.some((x) => x.name === p.name)) continue;
      pools.push({ name: p.display_name || p.name, used: p.capacity_gb ? (p.allocation_gb / p.capacity_gb) * 100 : 0, cap: p.capacity_gb });
    }
    pools.sort((a, b) => b.cap - a.cap).splice(4);
    const links = [];
    for (const node of nodes) {
      links.push({ key: `io:${node.name}`, kind: "io", node, a: [node.x, node.y, 30], b: [0, 0, 40 + pools.length * 22],
        up: node.write, down: node.read, color: "#bb83ff" });
      for (const [name, t] of Object.entries(node.nets)) {
        const net = byNet[name], r = netRadius(nodes);
        links.push({ key: `net:${node.name}:${name}`, kind: "net", node, net,
          a: [node.x, node.y, towerTop(node, vms)], b: [Math.cos(node.angle) * r, Math.sin(node.angle) * r, net.height],
          up: t.tx, down: t.rx, color: net.color });
      }
    }
    scene = { nodes, vms, nets, pools, links };
    renderHud();
  }
  const netRadius = (nodes) => (nodes.length > 1 ? Math.hypot(nodes[0].x, nodes[0].y) + 170 : 380);
  const towerTop = (node, vms) => 96 + Math.ceil(vms.filter((v) => v.node === node).length / 9) * 20;

  function renderHud() {
    const { nodes, vms, nets } = scene;
    const rx = nets.reduce((s, n) => s + n.rx, 0), tx = nets.reduce((s, n) => s + n.tx, 0);
    const io = nodes.reduce((s, n) => s + n.read + n.write, 0);
    const cell = (label, value, cls = "") => `<div class="fabric-stat ${cls}"><span>${label}</span><b>${value}</b></div>`;
    hud.innerHTML = cell("NODES", `${nodes.filter((n) => n.healthy).length}<small>/${nodes.length}</small>`)
      + cell("VMS RUNNING", `${vms.filter((v) => v.running).length}<small>/${vms.length}</small>`)
      + cell("NETWORK IN", rate(rx), "c-cyan") + cell("NETWORK OUT", rate(tx), "c-pink")
      + cell("STORAGE I/O", bytesRate(io), "c-violet");
  }

  // ---------- projection ----------

  function project(x, y, z) {
    const c = Math.cos(view.yaw), s = Math.sin(view.yaw);
    const x1 = x * c - y * s, y1 = x * s + y * c;
    const cp = Math.cos(view.pitch), sp = Math.sin(view.pitch);
    const depth = y1 * sp - (z - 80) * cp;
    const up = y1 * cp + (z - 80) * sp;
    const w = canvas.width / devicePixelRatio, h = canvas.height / devicePixelRatio;
    const fit = Math.min(w, h * 1.6) / 1250 * view.zoom;
    const k = 2600 / (2600 + depth);
    return [w / 2 + x1 * k * fit, h * 0.54 - up * k * fit, depth, k * fit];
  }
  const P = (p) => project(p[0], p[1], p[2]);

  // ---------- drawing ----------

  function poly(points, fill, stroke, width = 1) {
    ctx.beginPath();
    points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = width; ctx.stroke(); }
  }

  function box(cx, cy, z0, z1, half, color, alpha = 0.22) {
    const corners = [[-half, -half], [half, -half], [half, half], [-half, half]];
    const bottom = corners.map(([dx, dy]) => project(cx + dx, cy + dy, z0));
    const top = corners.map(([dx, dy]) => project(cx + dx, cy + dy, z1));
    const sides = [0, 1, 2, 3].map((i) => {
      const j = (i + 1) % 4;
      return { pts: [bottom[i], bottom[j], top[j], top[i]], d: (bottom[i][2] + bottom[j][2]) / 2 };
    }).sort((a, b) => b.d - a.d);
    for (const [n, side] of sides.entries()) {
      poly(side.pts, hexA(color, alpha * (0.6 + n * 0.18)), hexA(color, 0.75), 1);
    }
    poly(top, hexA(color, alpha + 0.12), hexA("#ffffff", 0.55), 1);
    return { top, bottom };
  }

  function ring(radius, z, color, dash, offset, width = 1.4) {
    ctx.beginPath();
    for (let i = 0; i <= 96; i++) {
      const a = (i / 96) * Math.PI * 2, [x, y] = project(Math.cos(a) * radius, Math.sin(a) * radius, z);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.strokeStyle = color; ctx.lineWidth = width;
    ctx.setLineDash(dash || []); ctx.lineDashOffset = offset || 0;
    ctx.stroke(); ctx.setLineDash([]);
  }

  function hexA(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${Math.max(0, Math.min(1, a))})`;
  }

  function label(x, y, lines, color, opts = {}) {
    ctx.font = `${opts.size || 11}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 16, h = lines.length * 15 + 9;
    const bx = x - w / 2;
    let by = y - h - (opts.lift ?? 10);
    // Keep labels from covering each other: move up past any label already drawn.
    for (let tries = 0; tries < 6; tries++) {
      const hit = placed.find((r) => bx < r.x + r.w + 4 && bx + w + 4 > r.x && by < r.y + r.h + 3 && by + h + 3 > r.y);
      if (!hit) break;
      by = hit.y - h - 4;
    }
    placed.push({ x: bx, y: by, w, h });
    ctx.fillStyle = "rgba(9,12,32,.78)"; ctx.strokeStyle = hexA(color, 0.7); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.roundRect(bx, by, w, h, 7); ctx.fill(); ctx.stroke();
    ctx.shadowColor = color; ctx.shadowBlur = 8; ctx.textAlign = "center";
    lines.forEach((l, i) => { ctx.fillStyle = i === 0 && opts.title ? "#f1f4ff" : color; ctx.fillText(l, x, by + 17 + i * 15); });
    ctx.shadowBlur = 0;
  }

  function tween(key, value) {
    const v = shown.get(key) ?? 0;
    const next = still.matches ? value : v + (value - v) * 0.08;
    shown.set(key, Math.abs(next - value) < 1 ? value : next);
    return shown.get(key);
  }

  function draw(t) {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== Math.round(w * devicePixelRatio) || canvas.height !== Math.round(h * devicePixelRatio)) {
      canvas.width = Math.round(w * devicePixelRatio); canvas.height = Math.round(h * devicePixelRatio);
    }
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (view.spin && !drag && !still.matches) view.yaw += (t - last) * 0.000035;
    last = t;
    hits = []; placed = [];
    const { nodes, vms, nets, pools, links } = scene;
    const R = netRadius(nodes), floor = R + 90;

    // Floor: grid circles and spokes.
    for (let r = 120; r <= floor; r += 120) ring(r, 0, "rgba(125,160,255,.13)", [], 0, 1);
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * Math.PI * 2, p0 = project(0, 0, 0), p1 = project(Math.cos(a) * floor, Math.sin(a) * floor, 0);
      poly([p0, p1], null, "rgba(125,160,255,.07)");
    }

    // Network rings with a flowing dash.
    nets.forEach((net, i) => {
      ring(R, net.height, hexA(net.color, 0.18), [], 0, 5);
      ring(R, net.height, hexA(net.color, 0.85), [14, 10], -t * 0.03, 1.6);
      // Labels stay in a column on the right, each tied to its ring by a thin line.
      const a = -view.yaw + 0.35 - i * 0.5, [rx, ry] = project(Math.cos(a) * R, Math.sin(a) * R, net.height);
      const x = w - 130, y = 150 + i * 62;
      ctx.beginPath(); ctx.moveTo(rx, ry); ctx.lineTo(x, y - 8); ctx.strokeStyle = hexA(net.color, 0.35); ctx.lineWidth = 1; ctx.stroke();
      ctx.beginPath(); ctx.arc(rx, ry, 3, 0, Math.PI * 2); ctx.fillStyle = net.color; ctx.fill();
      label(x, y, [net.name.toUpperCase(), `↓ ${rate(tween(`n:${net.name}:rx`, net.rx))}  ↑ ${rate(tween(`n:${net.name}:tx`, net.tx))}`], net.color, { title: true });
    });

    // Everything that needs depth order: storage core, towers, links.
    const items = [];
    items.push({ d: project(0, 0, 0)[2], draw: () => drawCore(pools, t) });
    nodes.forEach((node) => items.push({ d: project(node.x, node.y, 0)[2], draw: () => drawNode(node, vms.filter((v) => v.node === node), t) }));
    items.sort((a, b) => b.d - a.d).forEach((i) => i.draw());
    links.forEach((link) => drawLink(link, t));
  }

  function drawCore(pools, t) {
    const levels = pools.length ? pools : [{ name: "No storage pool", used: 0 }];
    levels.forEach((pool, i) => {
      const z0 = 10 + i * 22, z1 = z0 + 16, r = 92 - i * 6;
      for (let k = 0; k < 2; k++) ring(r, k ? z1 : z0, "rgba(187,131,255,.55)", [], 0, 1.2);
      const a = (pool.used / 100) * Math.PI * 2;
      ctx.beginPath();
      for (let s = 0; s <= 48; s++) {
        const ang = -Math.PI / 2 + (s / 48) * a, [x, y] = project(Math.cos(ang) * r, Math.sin(ang) * r, z1);
        s ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.strokeStyle = "#ed99da"; ctx.lineWidth = 3; ctx.shadowColor = "#ed99da"; ctx.shadowBlur = 10; ctx.stroke(); ctx.shadowBlur = 0;
    });
    const top = 10 + levels.length * 22;
    const glow = 0.4 + 0.25 * Math.sin(t / 600);
    ring(30, top + 4, `rgba(125,229,255,${glow})`, [], 0, 2);
    const [x, y] = project(0, 0, top + 30);
    label(x, y, ["VIRTUAL STORAGE", ...levels.map((p) => `${p.name}  ${p.used.toFixed(0)}%`)], "#bb83ff", { title: true });
  }

  function drawNode(node, vms, t) {
    const color = node.healthy ? "#4fafff" : "#ff6b7a";
    const { top } = box(node.x, node.y, 0, 90, 46, color, 0.18);
    // CPU level glowing inside the tower.
    if (node.cpu != null) box(node.x, node.y, 2, 2 + 84 * Math.min(1, node.cpu / 100), 40, "#54e8f0", 0.16);
    hits.push({ kind: "node", node, pts: top });
    vms.forEach((v, i) => {
      const layer = Math.floor(i / 9), slot = i % 9, gx = (slot % 3) - 1, gy = Math.floor(slot / 3) - 1;
      const cx = node.x + gx * 26, cy = node.y + gy * 26, z0 = 92 + layer * 20;
      const pulse = v.running && !still.matches ? 0.08 * Math.sin(t / 400 + i) : 0;
      const col = v.running ? (v === hover?.v ? "#ffffff" : "#7cf0a8") : "#6c7590";
      const b = box(cx, cy, z0, z0 + 14, 9, col, (v.running ? 0.35 : 0.15) + pulse);
      hits.push({ kind: "vm", v, pts: b.top });
    });
    const [x, y] = project(node.x, node.y, towerTop(node, scene.vms) + 26);
    label(x, y, [node.name, `CPU ${node.cpu == null ? "—" : node.cpu.toFixed(0) + "%"} · MEM ${node.mem == null ? "—" : node.mem.toFixed(0) + "%"}`, `${vms.filter((v) => v.running).length}/${vms.length} VMs`], color, { title: true });
  }

  function drawLink(link, t) {
    const a = P(link.a), b = P(link.b);
    const total = link.up + link.down;
    ctx.strokeStyle = hexA(link.color, total > 0 ? 0.55 : 0.18);
    ctx.lineWidth = total > 0 ? 1.6 : 1;
    ctx.setLineDash(total > 0 ? [] : [4, 6]);
    ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); ctx.setLineDash([]);
    // Particles: more and faster with more traffic (log scale), both directions.
    if (total > 0 && !still.matches) {
      const level = Math.min(1, Math.log10(1 + total) / 8);
      const count = 2 + Math.round(level * 8), speed = 0.00012 + level * 0.0006;
      for (const [dir, amount] of [[1, link.down], [-1, link.up]]) {
        if (amount <= 0) continue;
        for (let i = 0; i < count; i++) {
          let f = ((t * speed + i / count) % 1);
          if (dir < 0) f = 1 - f;
          const p = P(link.b.map((v, k) => v + (link.a[k] - v) * f));
          ctx.beginPath(); ctx.arc(p[0], p[1], 2.2 + level * 1.6, 0, Math.PI * 2);
          ctx.fillStyle = dir > 0 ? link.color : "#ffffff";
          ctx.shadowColor = link.color; ctx.shadowBlur = 12; ctx.fill(); ctx.shadowBlur = 0;
        }
      }
    }
    const at = link.kind === "io" ? 0.72 : 0.4 + (0.5 * scene.nets.indexOf(link.net)) / Math.max(1, scene.nets.length - 1); // apart from other labels
    const mid = P(link.a.map((v, k) => v + (link.b[k] - v) * at));
    const up = tween(`${link.key}:up`, link.up), down = tween(`${link.key}:down`, link.down);
    const text = link.kind === "io" ? `R ${bytesRate(down)} · W ${bytesRate(up)}` : `↓ ${rate(down)} ↑ ${rate(up)}`;
    label(mid[0], mid[1], [text], link.color, { size: 10, lift: 4 });
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
  canvas.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, yaw: view.yaw, pitch: view.pitch, moved: false }; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
      view.yaw = drag.yaw + dx * 0.006;
      view.pitch = Math.max(0.35, Math.min(1.35, drag.pitch + dy * 0.004));
      return;
    }
    hover = pick(e);
    canvas.style.cursor = hover ? "pointer" : "grab";
    if (!hover) { tip.hidden = true; return; }
    const r = page.getBoundingClientRect();
    tip.hidden = false;
    tip.style.left = `${e.clientX - r.left + 14}px`; tip.style.top = `${e.clientY - r.top + 14}px`;
    if (hover.kind === "vm") {
      const v = hover.v;
      tip.innerHTML = `<b>${esc(v.vm.name)}</b><span>${esc(v.vm.state)} · ${v.vm.vcpus} vCPU · ${gbText(v.vm.memory_mb)}</span><span>CPU ${Number.isFinite(v.cpu) ? v.cpu.toFixed(0) + "%" : "—"} · ↓ ${rate(v.rx)} ↑ ${rate(v.tx)}</span><em>Click to open</em>`;
    } else {
      const n = hover.node;
      tip.innerHTML = `<b>${esc(n.name)}</b><span>${n.healthy ? "Online" : "Offline"} · CPU ${n.cpu == null ? "—" : n.cpu.toFixed(0) + "%"}</span><span>Disk R ${bytesRate(n.read)} · W ${bytesRate(n.write)}</span><em>Click to open</em>`;
    }
  });
  canvas.addEventListener("pointerup", (e) => {
    const moved = drag?.moved;
    drag = null;
    if (moved) return;
    const hit = pick(e);
    if (hit?.kind === "vm") location.hash = `vm/${encodeURIComponent(hit.v.vm.uuid)}`;
    else if (hit?.kind === "node") location.hash = `node/${encodeURIComponent(hit.node.name)}`;
  });
  canvas.addEventListener("pointerleave", () => { tip.hidden = true; hover = null; });
  canvas.addEventListener("wheel", (e) => { e.preventDefault(); view.zoom = Math.max(0.5, Math.min(2.4, view.zoom * (e.deltaY > 0 ? 0.92 : 1.08))); }, { passive: false });
  document.getElementById("fabric-spin").addEventListener("click", (e) => {
    view.spin = !view.spin;
    e.currentTarget.textContent = view.spin ? "Pause rotation" : "Rotate";
    e.currentTarget.setAttribute("aria-pressed", String(!view.spin));
  });
  document.getElementById("fabric-reset").addEventListener("click", () => Object.assign(view, { yaw: -0.6, pitch: 0.95, zoom: 1 }));

  function loop(t) {
    if (!page.classList.contains("active")) { frame = 0; return; }
    draw(t);
    frame = requestAnimationFrame(loop);
  }
  function start() {
    build();
    if (!frame) frame = requestAnimationFrame(loop);
    // Traffic every 3 s while the map is open; the rest comes with the page refresh.
    clearInterval(poll);
    poll = setInterval(async () => {
      if (!page.classList.contains("active")) { clearInterval(poll); poll = 0; return; }
      if (document.hidden) return;
      try { await load("stats"); build(); } catch { /* next tick */ }
    }, 3000);
  }
  return { render: start, rate, bytesRate };
})();
