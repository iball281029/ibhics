/* Infrastructure map as a holographic command centre.

   Centre: a wireframe globe. Each compute node is a beacon on its surface, its VMs
   orbit it as small lights, every network is an orbit ring around the globe, and
   Virtual Storage glows at the core. Arcs carry live traffic (VM statistics every
   3 s) as moving light. Panels around it give the same facts as numbers, bars and
   gauges; the time bar replays the last 15 minutes kept while the page is open.

   Canvas 2D only, no libraries. Drag to turn, scroll to zoom, click a node or a VM
   to open it. */
window.FabricMap = (() => {
  const $id = (id) => document.getElementById(id);
  const page = $id("page-fabric"), canvas = $id("fabric-canvas"), tip = $id("fabric-tip");
  const timeBar = $id("fabric-time"), timeLabel = $id("fabric-time-label");
  const liveBtn = $id("fabric-live"), playBtn = $id("fabric-play");
  const ctx = canvas.getContext("2d");
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const H1 = "#3ef0ff", H2 = "#7fb8ff", PINK = "#ff8fd8", GREEN = "#6dffb3", RED = "#ff6b7a", AMBER = "#ffcb66";
  const NET_COLORS = [H1, PINK, "#b49bff", GREEN, AMBER, "#ff9f7a"];
  const HOME = { yaw: 0.6, tilt: 0.38, zoom: 1 };
  const view = { ...HOME, spin: true, vyaw: 0 };
  const HISTORY = 300; // 15 minutes of 3-second readings
  const history = [];
  // Points evenly spread over the sphere: the holographic dot texture.
  const DOTS = Array.from({ length: 900 }, (_, i) => {
    const y = 1 - (2 * (i + 0.5)) / 900, r = Math.sqrt(1 - y * y), a = i * 2.399963;
    return [Math.cos(a) * r, y, Math.sin(a) * r];
  });
  let scene = { nodes: [], vms: [], nets: [], pools: [] };
  let hits = [], hover = null, frame = 0, poll = 0, drag = null, last = 0, nextStep = 0;
  let cursor = -1, playing = false, W = 0, H = 0;

  const rate = (bytes) => {
    let n = Math.max(0, bytes || 0) * 8, i = 0;
    const u = ["b/s", "Kb/s", "Mb/s", "Gb/s"];
    while (n >= 1000 && i < 3) { n /= 1000; i++; }
    return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${u[i]}`;
  };
  const bytesRate = (b) => {
    let n = Math.max(0, b || 0), i = 0;
    const u = ["B/s", "KB/s", "MB/s", "GB/s"];
    while (n >= 1024 && i < 3) { n /= 1024; i++; }
    return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${u[i]}`;
  };
  const hexA = (hex, a) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${Math.max(0, Math.min(1, a))})`;
  };
  const level = (bytes) => Math.min(1, Math.log10(1 + Math.max(0, bytes || 0)) / 8);
  const pct = (v) => (Number.isFinite(v) ? Math.max(0, Math.min(100, v)) : null);

  // ---------- data ----------

  function build() {
    const members = (state.cluster?.members || []).filter((m) => m.role === "compute");
    const nets = (state.networks.length ? state.networks : [{ name: "default" }]).map((n, i) => ({
      name: n.name, color: NET_COLORS[i % NET_COLORS.length], index: i, rx: 0, tx: 0,
      incl: 0.35 + i * 0.32, radius: 1.2 + i * 0.1,
    }));
    const byNet = Object.fromEntries(nets.map((n) => [n.name, n]));
    const nodes = members.map((m, i) => {
      const s = state.summaries?.[m.name] || {};
      const lon = (i / Math.max(1, members.length)) * Math.PI * 2, lat = 0.32 * (i % 2 ? -1 : 1);
      return {
        name: m.name, healthy: m.healthy !== false, lon, lat,
        pos: [Math.cos(lat) * Math.cos(lon), Math.sin(lat), Math.cos(lat) * Math.sin(lon)],
        cpu: Number.isFinite(s.cpu_percent) ? s.cpu_percent : null,
        mem: s.memory_mb ? (s.memory_used_mb / s.memory_mb) * 100 : null,
        read: 0, write: 0, rx: 0, tx: 0, nets: {},
      };
    });
    const byNode = Object.fromEntries(nodes.map((n) => [n.name, n]));
    const vms = [];
    for (const vm of state.vms) {
      const node = byNode[vm.node];
      if (!node) continue;
      const st = state.stats?.[vm.uuid] || {};
      node.read += st.disk_read_bps || 0; node.write += st.disk_write_bps || 0;
      node.rx += st.net_rx_bps || 0; node.tx += st.net_tx_bps || 0;
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
      pools.push({ key: p.name, name: p.display_name || p.name, used: p.capacity_gb ? (p.allocation_gb / p.capacity_gb) * 100 : 0, cap: p.capacity_gb || 0, alloc: p.allocation_gb || 0 });
    }
    pools.sort((a, b) => b.cap - a.cap);
    scene = { nodes, vms, nets, pools };
    record();
    panels();
  }

  // ---------- time ----------

  function record() {
    const snap = { at: Date.now(), nodes: {}, nets: {} };
    for (const n of scene.nodes) snap.nodes[n.name] = { rx: n.rx, tx: n.tx, read: n.read, write: n.write, cpu: n.cpu, mem: n.mem, nets: n.nets };
    for (const n of scene.nets) snap.nets[n.name] = { rx: n.rx, tx: n.tx };
    const prev = history.at(-1);
    if (prev && snap.at - prev.at < 1500) history[history.length - 1] = snap; else history.push(snap);
    if (history.length > HISTORY) { history.shift(); if (cursor > 0) cursor--; }
    timeBar.max = String(Math.max(0, history.length - 1));
    if (cursor < 0) timeBar.value = timeBar.max;
    showTime();
  }
  const moment = () => (cursor < 0 ? history.at(-1) : history[cursor]) || { nodes: {}, nets: {} };
  function showTime() {
    const snap = moment(), live = cursor < 0;
    page.classList.toggle("fabric-replay", !live);
    liveBtn.setAttribute("aria-pressed", String(live));
    timeLabel.textContent = live ? `LIVE · ${history.length} readings` : `REPLAY · T−${Math.max(0, Math.round((Date.now() - snap.at) / 1000))} s`;
  }
  function setPlaying(on) { playing = on; playBtn.textContent = on ? "❚❚ Pause" : "▶ Replay"; playBtn.setAttribute("aria-pressed", String(on)); }
  timeBar.addEventListener("input", () => {
    const i = Number(timeBar.value);
    cursor = i >= history.length - 1 ? -1 : i;
    setPlaying(false); showTime(); panels();
  });
  liveBtn.addEventListener("click", () => { cursor = -1; setPlaying(false); timeBar.value = timeBar.max; showTime(); panels(); });
  playBtn.addEventListener("click", () => {
    if (!playing && (cursor < 0 || cursor >= history.length - 1)) cursor = 0;
    setPlaying(!playing); timeBar.value = String(cursor < 0 ? timeBar.max : cursor); showTime(); panels();
  });
  function step() {
    cursor = cursor < 0 ? 0 : cursor + 1;
    if (cursor >= history.length - 1) { cursor = -1; setPlaying(false); }
    timeBar.value = String(cursor < 0 ? timeBar.max : cursor);
    showTime(); panels();
  }

  // ---------- panels around the globe ----------

  const icon = (d, c) => `<svg viewBox="0 0 34 34" aria-hidden="true"><circle cx="17" cy="17" r="15" fill="none" stroke="${c}" stroke-opacity=".5"/><path d="${d}" fill="none" stroke="${c}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  function ring(value, color, size = 118) {
    const r = size / 2 - 12, c = 2 * Math.PI * r, p = pct(value) ?? 0, cx = size / 2;
    const ticks = Array.from({ length: 48 }, (_, i) => {
      const a = (i / 48) * Math.PI * 2, r1 = r + 6, r2 = r + (i % 4 ? 8 : 10);
      return `<line x1="${(cx + Math.cos(a) * r1).toFixed(1)}" y1="${(cx + Math.sin(a) * r1).toFixed(1)}" x2="${(cx + Math.cos(a) * r2).toFixed(1)}" y2="${(cx + Math.sin(a) * r2).toFixed(1)}"/>`;
    }).join("");
    return `<svg viewBox="0 0 ${size} ${size}" aria-hidden="true"><g stroke="${color}" stroke-opacity=".35">${ticks}</g>
      <circle cx="${cx}" cy="${cx}" r="${r}" fill="none" stroke="${color}" stroke-opacity=".12" stroke-width="7"/>
      <circle cx="${cx}" cy="${cx}" r="${r}" fill="none" stroke="${color}" stroke-width="7" stroke-dasharray="${((p / 100) * c).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 ${cx} ${cx})" style="filter:drop-shadow(0 0 6px ${color})"/>
      <circle cx="${cx}" cy="${cx}" r="${r - 12}" fill="none" stroke="${color}" stroke-opacity=".25" stroke-dasharray="2 4"/></svg>`;
  }
  function panels() {
    const snap = moment(), nodes = scene.nodes, vms = scene.vms;
    const live = (n) => snap.nodes[n.name] || {};
    const rx = Object.values(snap.nets).reduce((s, n) => s + n.rx, 0), tx = Object.values(snap.nets).reduce((s, n) => s + n.tx, 0);
    const io = nodes.reduce((s, n) => s + (live(n).read || 0) + (live(n).write || 0), 0);
    const kpi = (label, value, d, c) => `<div class="fx-kpi">${icon(d, c)}<span>${label}</span><b>${value}</b></div>`;
    $id("fabric-hud").innerHTML = `<h3>Cluster</h3>`
      + kpi("Nodes online", `${nodes.filter((n) => n.healthy).length}<small> / ${nodes.length}</small>`, "M10 12h14v10H10zM13 22v3m8-3v3M10 25h14", H1)
      + kpi("Network in", rate(rx), "M17 9v16m-5-5 5 5 5-5", H1)
      + kpi("Network out", rate(tx), "M17 25V9m-5 5 5-5 5 5", PINK)
      + kpi("Storage I/O", bytesRate(io), "M10 11c0-2 14-2 14 0v12c0 2-14 2-14 0zM10 11c0 2 14 2 14 0M10 17c0 2 14 2 14 0", "#b49bff");

    const running = vms.filter((v) => v.running).length, off = vms.length - running;
    const total = Math.max(1, vms.length), r = 42, c = 2 * Math.PI * r, on = (running / total) * c;
    $id("fx-donut").innerHTML = `<div class="fx-donut"><svg viewBox="0 0 110 110" aria-hidden="true">
        <circle cx="55" cy="55" r="${r}" fill="none" stroke="#3ef0ff1f" stroke-width="12"/>
        <circle cx="55" cy="55" r="${r}" fill="none" stroke="${GREEN}" stroke-width="12" stroke-dasharray="${on.toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 55 55)" style="filter:drop-shadow(0 0 6px ${GREEN})"/>
        <circle cx="55" cy="55" r="${r}" fill="none" stroke="#5d6f88" stroke-width="12" stroke-dasharray="${((off / total) * c).toFixed(1)} ${c.toFixed(1)}" stroke-dashoffset="${(-on).toFixed(1)}" transform="rotate(-90 55 55)"/>
        <text x="55" y="58" text-anchor="middle" fill="#e8fbff" font-size="20" font-family="ui-monospace,monospace">${vms.length}</text>
        <text x="55" y="74" text-anchor="middle" fill="#6b8fb4" font-size="8" letter-spacing="1.5" font-family="ui-monospace,monospace">VMS</text></svg>
      <div class="fx-legend"><div style="--c:${GREEN}"><i></i>Running<b>${running}</b></div><div style="--c:#5d6f88"><i></i>Stopped<b>${off}</b></div>
      ${nodes.map((n) => `<div style="--c:${n.healthy ? H2 : RED}"><i></i>${esc(n.name)}<b>${vms.filter((v) => v.node === n).length}</b></div>`).join("")}</div></div>`;

    const span = history.slice(-40), sum = (h) => Object.values(h.nets).reduce((s, n) => s + n.rx + n.tx, 0);
    const mx = Math.max(1, ...span.map(sum)), bw = 240 / Math.max(1, span.length);
    $id("fx-trend").innerHTML = span.length < 2 ? `<p style="font-size:11px;color:#6b8fb4;margin:0">Collecting readings…</p>` : `<svg viewBox="0 0 260 110" aria-hidden="true">
      ${[0, 1, 2, 3].map((k) => `<line x1="20" x2="260" y1="${10 + k * 30}" y2="${10 + k * 30}" stroke="#3ef0ff" stroke-opacity=".08"/>`).join("")}
      ${span.map((h, i) => { const hh = (sum(h) / mx) * 90, now = cursor >= 0 && history[cursor] === h;
        return `<rect x="${(20 + i * bw + 1).toFixed(1)}" y="${(100 - hh).toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${hh.toFixed(1)}" fill="${now ? "#ffffff" : H1}" fill-opacity="${now ? 1 : (0.35 + 0.65 * (i / span.length)).toFixed(2)}"/>`; }).join("")}
      <text x="0" y="14" fill="#6b8fb4" font-size="8" font-family="ui-monospace,monospace">${rate(mx)}</text>
      <text x="0" y="104" fill="#6b8fb4" font-size="8" font-family="ui-monospace,monospace">0</text></svg>`;

    $id("fabric-nodes").innerHTML = `<div class="fx-bars">${nodes.map((n) => {
      const L = live(n), cpu = pct(L.cpu ?? n.cpu), mem = pct(L.mem ?? n.mem);
      return `<a class="fx-bar${n.healthy ? "" : " off"}" href="#node/${encodeURIComponent(n.name)}" style="--c:${H1}"><span>${esc(n.name)}${n.healthy ? "" : " · offline"}</span><b>CPU ${cpu == null ? "—" : cpu.toFixed(0) + "%"} · MEM ${mem == null ? "—" : mem.toFixed(0) + "%"}</b><u><i style="width:${cpu ?? 0}%"></i><i class="m" style="width:${mem ?? 0}%"></i></u></a>`;
    }).join("") || `<span style="font-size:11px;color:#6b8fb4">No compute nodes</span>`}</div>`;
    const netMax = Math.max(1, ...scene.nets.map((n) => (snap.nets[n.name]?.rx || 0) + (snap.nets[n.name]?.tx || 0)));
    $id("fx-nets").innerHTML = `<div class="fx-bars">${scene.nets.map((n) => {
      const t = snap.nets[n.name] || { rx: 0, tx: 0 };
      return `<div class="fx-bar" style="--c:${n.color}"><span>${esc(n.name)}</span><b>↓${rate(t.rx)} ↑${rate(t.tx)}</b><u><i style="width:${(((t.rx + t.tx) / netMax) * 100).toFixed(1)}%"></i></u></div>`;
    }).join("")}</div>`;
    $id("fx-pools").innerHTML = `<div class="fx-bars">${scene.pools.slice(0, 5).map((p) => `<div class="fx-bar" style="--c:#b49bff"><span>${esc(p.name)}</span><b>${p.used.toFixed(0)}% · ${Math.round(p.cap)} GB</b><u><i style="width:${pct(p.used) ?? 0}%"></i></u></div>`).join("") || `<span style="font-size:11px;color:#6b8fb4">No storage pools</span>`}</div>`;

    const avg = (k) => { const v = nodes.map((n) => pct(live(n)[k] ?? n[k])).filter((x) => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
    const cap = scene.pools.reduce((s, p) => s + p.cap, 0), store = cap ? (scene.pools.reduce((s, p) => s + p.alloc, 0) / cap) * 100 : null;
    const gauge = (label, value, color, text) => `<div class="fx-gauge" style="--c:${color}">${ring(value, color)}<b>${text ?? (value == null ? "—" : `${Math.round(value)}<small>%</small>`)}</b><span>${label}</span></div>`;
    const net = rate(rx + tx).split(" ");
    $id("fx-gauges").innerHTML = gauge("Cluster CPU", avg("cpu"), H1) + gauge("Cluster memory", avg("mem"), PINK)
      + gauge("Storage used", store, "#b49bff") + gauge("VMs running", vms.length ? (running / vms.length) * 100 : null, GREEN)
      + gauge("Network load", level(rx + tx) * 100, AMBER, `${net[0]}<small>${net[1]}</small>`);
  }

  // ---------- the globe ----------

  function project(p) {
    // Turn about the vertical axis, tilt towards the viewer, then perspective.
    const cy = Math.cos(view.yaw), sy = Math.sin(view.yaw), ct = Math.cos(view.tilt), st = Math.sin(view.tilt);
    const x1 = p[0] * cy - p[2] * sy, z1 = p[0] * sy + p[2] * cy;
    const y2 = p[1] * ct - z1 * st, z2 = p[1] * st + z1 * ct;
    const R = Math.min(W, H) * 0.33 * view.zoom, persp = 3.2 / (3.2 - z2);
    return [W / 2 + x1 * R * persp, H / 2 - y2 * R * persp, z2, persp];
  }
  const lerp3 = (a, b, f) => a.map((v, i) => v + (b[i] - v) * f);
  const norm = (v) => { const l = Math.hypot(...v) || 1; return v.map((x) => x / l); };
  function arcPoint(a, b, f, lift) { // along the sphere between a and b, raised above it
    const p = norm(lerp3(a, b, f)), h = 1 + lift * Math.sin(Math.PI * f);
    return p.map((x) => x * h);
  }
  function line(points, color, width, alpha = 1) {
    ctx.beginPath();
    points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.strokeStyle = color; ctx.globalAlpha = alpha * 0.18; ctx.lineWidth = width + 5; ctx.stroke();
    ctx.globalAlpha = alpha; ctx.lineWidth = width; ctx.stroke(); ctx.globalAlpha = 1;
  }
  const sprites = new Map();
  function glow(x, y, r, color, a = 1) { // a soft light, drawn once per colour
    if (!sprites.has(color)) {
      const c = document.createElement("canvas"); c.width = c.height = 64;
      const g = c.getContext("2d"), grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
      grad.addColorStop(0, "#ffffff"); grad.addColorStop(0.2, color); grad.addColorStop(0.5, hexA(color, 0.3)); grad.addColorStop(1, hexA(color, 0));
      g.fillStyle = grad; g.fillRect(0, 0, 64, 64); sprites.set(color, c);
    }
    ctx.globalAlpha = a; ctx.drawImage(sprites.get(color), x - r, y - r, r * 2, r * 2); ctx.globalAlpha = 1;
  }
  function tag(x, y, lines, color) {
    ctx.globalCompositeOperation = "source-over";
    ctx.font = "600 11px ui-monospace, SFMono-Regular, Menlo, monospace";
    const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 20, h = lines.length * 15 + 8;
    const bx = x + 26, by = y - h - 18;
    ctx.strokeStyle = hexA(color, 0.8); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x + 10, y - 10); ctx.lineTo(bx, by + h); ctx.lineTo(bx + w, by + h); ctx.stroke();
    ctx.fillStyle = "rgba(4,18,31,.84)"; ctx.fillRect(bx, by, w, h);
    ctx.fillStyle = color; ctx.fillRect(bx, by, 3, h);
    ctx.textAlign = "left";
    lines.forEach((l, i) => {
      ctx.font = `${i ? "" : "600 "}11px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.fillStyle = i ? hexA(color, 0.95) : "#e8fbff"; ctx.fillText(l, bx + 10, by + 16 + i * 15);
    });
    ctx.globalCompositeOperation = "lighter";
  }

  function draw(t) {
    W = canvas.clientWidth; H = canvas.clientHeight;
    const dpr = devicePixelRatio || 1;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalCompositeOperation = "source-over"; ctx.clearRect(0, 0, W, H);
    const dt = Math.min(64, t - last || 16); last = t;
    if (!drag) {
      view.yaw += view.vyaw * dt; view.vyaw *= Math.pow(0.94, dt / 16);
      if (view.spin && !still.matches) view.yaw += dt * 0.00012;
    }
    if (playing && t >= nextStep) { nextStep = t + 450; step(); }
    hits = [];
    const snap = moment(), cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.33 * view.zoom;
    ctx.globalCompositeOperation = "lighter";

    // HUD rings around the globe, turning slowly, with four brackets.
    const turn = still.matches ? 0 : t / 9000;
    for (const [r, dash, a, dir] of [[R * 1.22, [2, 6], 0.35, 1], [R * 1.3, [30, 10, 4, 10], 0.25, -1], [R * 1.42, [1, 3], 0.18, 1]]) {
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(turn * dir);
      ctx.beginPath(); ctx.arc(0, 0, r, 0, Math.PI * 2); ctx.setLineDash(dash);
      ctx.strokeStyle = hexA(H1, a); ctx.lineWidth = 1.2; ctx.stroke(); ctx.setLineDash([]); ctx.restore();
    }
    for (let k = 0; k < 4; k++) {
      const a = (k * Math.PI) / 2 + Math.PI / 4 + turn * 0.5;
      ctx.beginPath(); ctx.arc(cx, cy, R * 1.36, a - 0.12, a + 0.12);
      ctx.strokeStyle = hexA(H1, 0.8); ctx.lineWidth = 3; ctx.stroke();
    }

    // The globe: atmosphere, meridians and parallels, the dot texture, a scan band.
    const atm = ctx.createRadialGradient(cx, cy, R * 0.6, cx, cy, R * 1.15);
    atm.addColorStop(0, "rgba(20,120,160,.10)"); atm.addColorStop(0.85, "rgba(62,240,255,.12)"); atm.addColorStop(1, "rgba(62,240,255,0)");
    ctx.fillStyle = atm; ctx.beginPath(); ctx.arc(cx, cy, R * 1.15, 0, Math.PI * 2); ctx.fill();
    const grid = (pts) => {
      for (let i = 1; i < pts.length; i++) {
        const a = pts[i - 1], b = pts[i];
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
        ctx.strokeStyle = hexA(H1, (a[2] + b[2]) / 2 > 0 ? 0.22 : 0.06); ctx.lineWidth = 1; ctx.stroke();
      }
    };
    for (let lat = -60; lat <= 60; lat += 30) {
      const r = Math.cos((lat * Math.PI) / 180), y = Math.sin((lat * Math.PI) / 180);
      grid(Array.from({ length: 73 }, (_, i) => project([Math.cos((i / 72) * Math.PI * 2) * r, y, Math.sin((i / 72) * Math.PI * 2) * r])));
    }
    for (let lon = 0; lon < 180; lon += 30) {
      const a = (lon * Math.PI) / 180;
      grid(Array.from({ length: 73 }, (_, i) => { const b = (i / 72) * Math.PI * 2; return project([Math.cos(b) * Math.cos(a), Math.sin(b), Math.cos(b) * Math.sin(a)]); }));
    }
    for (const d of DOTS) {
      const p = project(d);
      if (p[2] < -0.1) continue;
      ctx.fillStyle = hexA(H1, 0.12 + p[2] * 0.45);
      ctx.fillRect(p[0] - 0.8, p[1] - 0.8, 1.6, 1.6);
    }
    if (!still.matches) {
      const y = Math.sin(t / 2200), r = Math.sqrt(Math.max(0, 1 - y * y));
      line(Array.from({ length: 49 }, (_, i) => project([Math.cos((i / 48) * Math.PI * 2) * r, y, Math.sin((i / 48) * Math.PI * 2) * r])), H1, 1.4, 0.5);
    }

    // Virtual Storage at the core: pulsing, brighter with disk traffic.
    const io = Object.values(snap.nodes).reduce((s, n) => s + n.read + n.write, 0), lvl = level(io);
    const pulse = still.matches ? 1 : 1 + 0.12 * Math.sin(t / 420);
    glow(cx, cy, R * (0.22 + lvl * 0.12) * pulse, "#b49bff", 0.9);
    glow(cx, cy, R * 0.08, "#ffffff", 0.9);
    hits.push({ kind: "core", x: cx, y: cy, r: R * 0.18 });

    // Network orbits with lights running along them.
    for (const net of scene.nets) {
      const t1 = snap.nets[net.name] || { rx: 0, tx: 0 }, nl = level(t1.rx + t1.tx);
      const orbit = (f) => { const a = f * Math.PI * 2; return [Math.cos(a) * net.radius, Math.sin(a) * net.radius * Math.sin(net.incl), Math.sin(a) * net.radius * Math.cos(net.incl)]; };
      const pts = Array.from({ length: 97 }, (_, i) => project(orbit(i / 96)));
      for (let i = 1; i < pts.length; i++) {
        const front = pts[i][2] > -0.2;
        ctx.beginPath(); ctx.moveTo(pts[i - 1][0], pts[i - 1][1]); ctx.lineTo(pts[i][0], pts[i][1]);
        ctx.strokeStyle = hexA(net.color, front ? 0.55 + nl * 0.3 : 0.15); ctx.lineWidth = front ? 1.6 : 1; ctx.stroke();
      }
      for (let k = 0; k < 4; k++) {
        const f = still.matches ? k / 4 : (((t * (0.00004 + nl * 0.0002) * (net.index % 2 ? -1 : 1) + k / 4) % 1) + 1) % 1;
        const p = project(orbit(f)); glow(p[0], p[1], 9, net.color, p[2] > -0.2 ? 1 : 0.3);
      }
      net.anchor = orbit;
    }

    // Arcs: node to each network orbit (network traffic), node to the core (disk traffic).
    for (const node of scene.nodes) {
      const L = snap.nodes[node.name] || { nets: {}, read: 0, write: 0 };
      for (const net of scene.nets) {
        const tr = (L.nets || {})[net.name];
        if (!tr || !net.anchor) continue;
        const target = net.anchor(node.lon / (Math.PI * 2) + 0.06), total = tr.rx + tr.tx, al = level(total);
        line(Array.from({ length: 25 }, (_, i) => project(arcPoint(node.pos, target, i / 24, 0.35))), net.color, 1 + al * 2, 0.35 + al * 0.6);
        if (total > 0 && !still.matches) for (let k = 0; k < 3; k++) {
          const f = (t * (0.0002 + al * 0.0008) + k / 3) % 1, p = project(arcPoint(node.pos, target, f, 0.35));
          glow(p[0], p[1], 7 + al * 6, net.color);
        }
      }
      const total = (L.read || 0) + (L.write || 0), al = level(total);
      line(Array.from({ length: 13 }, (_, i) => project(lerp3(node.pos, [0, 0, 0], i / 12))), "#b49bff", 1 + al * 2, 0.25 + al * 0.6);
      if (total > 0 && !still.matches) {
        const p = project(lerp3(node.pos, [0, 0, 0], (t * (0.0003 + al * 0.0008)) % 1));
        glow(p[0], p[1], 8, "#b49bff");
      }
    }

    // Nodes: beacons on the surface, their VMs circling them as small lights.
    for (const node of scene.nodes) {
      const p = project(node.pos), front = p[2] > -0.15, color = node.healthy ? H1 : RED;
      const beat = still.matches ? 0 : 0.3 * Math.sin(t / 300 + node.lon * 3);
      glow(p[0], p[1], 22 + beat * 10, color, front ? 1 : 0.35);
      ctx.beginPath(); ctx.arc(p[0], p[1], 14, 0, Math.PI * 2); ctx.strokeStyle = hexA(color, front ? 0.9 : 0.3); ctx.lineWidth = 1.4; ctx.stroke();
      hits.push({ kind: "node", node, x: p[0], y: p[1], r: 18 });
      const mine = scene.vms.filter((v) => v.node === node);
      mine.forEach((v, i) => {
        const a = (i / Math.max(1, mine.length)) * Math.PI * 2 + (still.matches ? 0 : t / 2600);
        const off = norm(node.pos.map((x, k) => x + [Math.cos(a), Math.sin(a), -Math.cos(a)][k] * 0.16));
        const q = project(off.map((x) => x * 1.06));
        glow(q[0], q[1], hover?.v === v ? 12 : 8, v.running ? GREEN : "#6c7a92", q[2] > -0.15 ? 1 : 0.3);
        hits.push({ kind: "vm", v, x: q[0], y: q[1], r: 7 });
      });
      if (front) {
        const L = snap.nodes[node.name] || {}, cpu = L.cpu ?? node.cpu;
        tag(p[0], p[1], [node.name, `${mine.filter((v) => v.running).length}/${mine.length} VMs · CPU ${cpu == null ? "—" : cpu.toFixed(0) + "%"}`], color);
      }
    }
    ctx.globalCompositeOperation = "source-over";
  }

  // ---------- interaction ----------

  const pick = (e) => {
    const r = canvas.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    return [...hits].reverse().find((h) => h.kind !== "core" && Math.hypot(h.x - x, h.y - y) <= h.r + 3)
      || hits.find((h) => h.kind === "core" && Math.hypot(h.x - x, h.y - y) <= h.r) || null;
  };
  canvas.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, y: e.clientY, yaw: view.yaw, tilt: view.tilt, moved: false, lastX: e.clientX, lastT: performance.now() };
    view.vyaw = 0; canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y, now = performance.now();
      if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
      view.yaw = drag.yaw + dx * 0.007;
      view.tilt = Math.max(-0.2, Math.min(1.2, drag.tilt + dy * 0.004));
      view.vyaw = ((e.clientX - drag.lastX) * 0.007) / Math.max(1, now - drag.lastT);
      drag.lastX = e.clientX; drag.lastT = now;
      return;
    }
    hover = pick(e);
    canvas.style.cursor = hover ? "pointer" : "grab";
    if (!hover) { tip.hidden = true; return; }
    const r = canvas.parentElement.getBoundingClientRect();
    tip.hidden = false;
    tip.style.left = `${e.clientX - r.left + 16}px`; tip.style.top = `${e.clientY - r.top + 16}px`;
    if (hover.kind === "vm") {
      const v = hover.v;
      tip.innerHTML = `<b>${esc(v.vm.name)}</b><span>${esc(v.vm.state)} · ${v.vm.vcpus} vCPU · ${gbText(v.vm.memory_mb)}</span><span>CPU ${Number.isFinite(v.cpu) ? v.cpu.toFixed(0) + "%" : "—"} · ↓ ${rate(v.rx)} ↑ ${rate(v.tx)}</span><em>Click to open</em>`;
    } else if (hover.kind === "node") {
      const n = hover.node;
      tip.innerHTML = `<b>${esc(n.name)}</b><span>${n.healthy ? "Online" : "Offline"} · CPU ${n.cpu == null ? "—" : n.cpu.toFixed(0) + "%"}</span><span>Network ↓ ${rate(n.rx)} ↑ ${rate(n.tx)}</span><span>Disk R ${bytesRate(n.read)} · W ${bytesRate(n.write)}</span><em>Click to open</em>`;
    } else {
      tip.innerHTML = `<b>Virtual Storage</b>${scene.pools.slice(0, 4).map((p) => `<span>${esc(p.name)} · ${p.used.toFixed(0)}% of ${Math.round(p.cap)} GB</span>`).join("")}<em>Click to open Storage</em>`;
    }
  });
  canvas.addEventListener("pointerup", (e) => {
    const moved = drag?.moved; drag = null;
    if (moved) return;
    const hit = pick(e);
    if (hit?.kind === "vm") location.hash = `vm/${encodeURIComponent(hit.v.vm.uuid)}`;
    else if (hit?.kind === "node") location.hash = `node/${encodeURIComponent(hit.node.name)}`;
    else if (hit?.kind === "core") location.hash = "storage";
  });
  canvas.addEventListener("pointerleave", () => { tip.hidden = true; hover = null; });
  canvas.addEventListener("wheel", (e) => { e.preventDefault(); view.zoom = Math.max(0.6, Math.min(2.2, view.zoom * (e.deltaY > 0 ? 0.93 : 1.07))); }, { passive: false });
  $id("fabric-spin").addEventListener("click", (e) => {
    view.spin = !view.spin;
    e.currentTarget.textContent = view.spin ? "Pause rotation" : "Rotate";
    e.currentTarget.setAttribute("aria-pressed", String(!view.spin));
  });
  $id("fabric-reset").addEventListener("click", () => Object.assign(view, HOME, { vyaw: 0 }));

  function clock() {
    $id("fx-clock").textContent = new Date().toLocaleString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  let ticked = 0;
  function loop(t) {
    if (!page.classList.contains("active")) { frame = 0; return; }
    try { draw(t); } catch (err) { console.error("fabric draw failed", err); }
    if (t - ticked > 1000) { ticked = t; clock(); }
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
