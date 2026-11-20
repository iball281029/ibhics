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
  const liveBtn = $id("fabric-live"), playBtn = $id("fabric-play"), backBtn = $id("fx-back");
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
  let cursor = -1, playing = false, W = 0, H = 0, mode = "globe", entered = 0;
  const extras = { nodes: {}, at: 0 }; // per node: time zone, NICs, LLDP switch ports

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
    const witness = (state.cluster?.members || []).find((m) => m.role === "witness") || null;
    const tz = members.map((m) => extras.nodes[m.name]?.tz).find(Boolean) || "";
    scene = { nodes, vms, nets, pools, witness, tz };
    record();
    panels();
    if (inside3d) inside3d.update(insideData());
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
    const kpi = (label, value, d, c, href) => `<a class="fx-kpi" href="#${href}">${icon(d, c)}<span>${label}</span><b>${value}</b></a>`;
    $id("fabric-hud").innerHTML = `<h3>Cluster</h3>`
      + kpi("Nodes online", `${nodes.filter((n) => n.healthy).length}<small> / ${nodes.length}</small>`, "M10 12h14v10H10zM13 22v3m8-3v3M10 25h14", H1, "cluster")
      + kpi("Network in", rate(rx), "M17 9v16m-5-5 5 5 5-5", H1, "network")
      + kpi("Network out", rate(tx), "M17 25V9m-5 5 5-5 5 5", PINK, "network")
      + kpi("Storage I/O", bytesRate(io), "M10 11c0-2 14-2 14 0v12c0 2-14 2-14 0zM10 11c0 2 14 2 14 0M10 17c0 2 14 2 14 0", "#b49bff", "storage");

    const running = vms.filter((v) => v.running).length, off = vms.length - running;
    const total = Math.max(1, vms.length), r = 42, c = 2 * Math.PI * r, on = (running / total) * c;
    $id("fx-donut").innerHTML = `<a class="fx-donut" href="#compute"><svg viewBox="0 0 110 110" aria-hidden="true">
        <circle cx="55" cy="55" r="${r}" fill="none" stroke="#3ef0ff1f" stroke-width="12"/>
        <circle cx="55" cy="55" r="${r}" fill="none" stroke="${GREEN}" stroke-width="12" stroke-dasharray="${on.toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 55 55)" style="filter:drop-shadow(0 0 6px ${GREEN})"/>
        <circle cx="55" cy="55" r="${r}" fill="none" stroke="#5d6f88" stroke-width="12" stroke-dasharray="${((off / total) * c).toFixed(1)} ${c.toFixed(1)}" stroke-dashoffset="${(-on).toFixed(1)}" transform="rotate(-90 55 55)"/>
        <text x="55" y="58" text-anchor="middle" fill="#e8fbff" font-size="20" font-family="ui-monospace,monospace">${vms.length}</text>
        <text x="55" y="74" text-anchor="middle" fill="#6b8fb4" font-size="8" letter-spacing="1.5" font-family="ui-monospace,monospace">VMS</text></svg>
      <div class="fx-legend"><div style="--c:${GREEN}"><i></i>Running<b>${running}</b></div><div style="--c:#5d6f88"><i></i>Stopped<b>${off}</b></div>
      ${nodes.map((n) => `<div style="--c:${n.healthy ? H2 : RED}"><i></i>${esc(n.name)}<b>${vms.filter((v) => v.node === n).length}</b></div>`).join("")}</div></a>`;

    const span = history.slice(-40), sum = (h) => Object.values(h.nets).reduce((s, n) => s + n.rx + n.tx, 0);
    const mx = Math.max(1, ...span.map(sum)), bw = 240 / Math.max(1, span.length);
    $id("fx-trend").onclick = () => { location.hash = "network"; };
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
      return `<a class="fx-bar" href="#network" style="--c:${n.color}"><span>${esc(n.name)}</span><b>↓${rate(t.rx)} ↑${rate(t.tx)}</b><u><i style="width:${(((t.rx + t.tx) / netMax) * 100).toFixed(1)}%"></i></u></a>`;
    }).join("")}</div>`;
    $id("fx-pools").innerHTML = `<div class="fx-bars">${scene.pools.slice(0, 5).map((p) => `<a class="fx-bar" href="#storage" style="--c:#b49bff"><span>${esc(p.name)}</span><b>${p.used.toFixed(0)}% · ${Math.round(p.cap)} GB</b><u><i style="width:${pct(p.used) ?? 0}%"></i></u></a>`).join("") || `<span style="font-size:11px;color:#6b8fb4">No storage pools</span>`}</div>`;

    const avg = (k) => { const v = nodes.map((n) => pct(live(n)[k] ?? n[k])).filter((x) => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
    const cap = scene.pools.reduce((s, p) => s + p.cap, 0), store = cap ? (scene.pools.reduce((s, p) => s + p.alloc, 0) / cap) * 100 : null;
    const gauge = (label, value, color, text, href) => `<a class="fx-gauge" href="#${href}" style="--c:${color}">${ring(value, color)}<b>${text ?? (value == null ? "—" : `${Math.round(value)}<small>%</small>`)}</b><span>${label}</span></a>`;
    const net = rate(rx + tx).split(" ");
    $id("fx-gauges").innerHTML = gauge("Cluster CPU", avg("cpu"), H1, null, "cluster") + gauge("Cluster memory", avg("mem"), PINK, null, "cluster")
      + gauge("Storage used", store, "#b49bff", null, "storage") + gauge("VMs running", vms.length ? (running / vms.length) * 100 : null, GREEN, null, "compute")
      + gauge("Network load", level(rx + tx) * 100, AMBER, `${net[0]}<small>${net[1]}</small>`, "network");
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
      if (view.spin && !still.matches) { // sway around the datacenter, so it stays in view
        const face = Math.PI / 2 - sitePosition().lon;
        view.yaw += ((face + Math.sin(t / 6000) * 0.55 - view.yaw) * dt) / 2500;
      }
    }
    if (playing && t >= nextStep) { nextStep = t + 450; step(); }
    hits = [];
    const snap = moment(), cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.33 * view.zoom;
    ctx.globalCompositeOperation = "lighter";
    if (mode === "inside") { if (!inside3d) drawInside(t, snap); ctx.globalCompositeOperation = "source-over"; return; }

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

    // Network orbits with lights running along them (the cluster's networks).
    for (const net of scene.nets) {
      const t1 = snap.nets[net.name] || { rx: 0, tx: 0 }, nl = level(t1.rx + t1.tx);
      const orbit = (f) => { const a = f * Math.PI * 2; return [Math.cos(a) * net.radius, Math.sin(a) * net.radius * Math.sin(net.incl), Math.sin(a) * net.radius * Math.cos(net.incl)]; };
      const pts = Array.from({ length: 97 }, (_, i) => project(orbit(i / 96)));
      for (let i = 1; i < pts.length; i++) {
        ctx.beginPath(); ctx.moveTo(pts[i - 1][0], pts[i - 1][1]); ctx.lineTo(pts[i][0], pts[i][1]);
        ctx.strokeStyle = hexA(net.color, pts[i][2] > -0.2 ? 0.45 + nl * 0.3 : 0.12); ctx.lineWidth = 1.3; ctx.stroke();
      }
      for (let k = 0; k < 3; k++) {
        const f = still.matches ? k / 3 : (((t * (0.00004 + nl * 0.0002) * (net.index % 2 ? -1 : 1) + k / 3) % 1) + 1) % 1;
        const q = project(orbit(f)); glow(q[0], q[1], 8, net.color, q[2] > -0.2 ? 1 : 0.3);
      }
      net.anchor = orbit;
    }

    // The datacenter: a tower at the place the cluster's time zone points to.
    const site = sitePosition(), p = project(site.pos), front = p[2] > -0.05;
    const up = project(site.pos.map((x) => x * 1.32));
    for (const net of scene.nets) { // uplinks from the site into each network orbit
      if (!net.anchor) continue;
      const target = net.anchor(site.lon / (Math.PI * 2) + 0.08);
      line(Array.from({ length: 25 }, (_, i) => project(arcPoint(site.pos, target, i / 24, 0.3))), net.color, 1.2, front ? 0.6 : 0.2);
      if (!still.matches) {
        const f = (t * 0.0004 + net.index / 3) % 1, q = project(arcPoint(site.pos, target, f, 0.3));
        glow(q[0], q[1], 7, net.color, front ? 1 : 0.3);
      }
    }
    const beam = ctx.createLinearGradient(p[0], p[1], up[0], up[1]);
    beam.addColorStop(0, hexA(H1, front ? 0.55 : 0.15)); beam.addColorStop(1, hexA(H1, 0));
    ctx.strokeStyle = beam; ctx.lineWidth = 10; ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(up[0], up[1]); ctx.stroke();
    for (let k = 0; k < 3; k++) { // pulse rings on the ground
      const f = still.matches ? 0.5 : ((t / 1600 + k / 3) % 1);
      ctx.beginPath(); ctx.ellipse(p[0], p[1], 10 + f * 46, (10 + f * 46) * 0.38, 0, 0, Math.PI * 2);
      ctx.strokeStyle = hexA(H1, (front ? 0.7 : 0.2) * (1 - f)); ctx.lineWidth = 1.4; ctx.stroke();
    }
    building(p[0], p[1], front ? 1 : 0.35, t);
    hits.push({ kind: "site", x: p[0], y: p[1] - 22, r: 34 });
    if (front) {
      const online = scene.nodes.filter((n) => n.healthy).length;
      tag(p[0] + 10, p[1] - 40, [`DATACENTER · ${site.tz}`, `${scene.nodes.length} nodes${scene.witness ? " + witness" : ""} · ${online} online`, "Click to look inside ▸"], H1);
    }
    ctx.globalCompositeOperation = "source-over";
  }

  // A small holographic datacenter: two towers with lit floors.
  function building(x, y, a, t) {
    ctx.globalAlpha = a;
    const tower = (bx, w, h, d) => {
      ctx.fillStyle = "rgba(62,240,255,.10)"; ctx.strokeStyle = H1; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(bx, y); ctx.lineTo(bx + w, y - d); ctx.lineTo(bx + w, y - d - h); ctx.lineTo(bx, y - h); ctx.closePath(); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(bx, y - h); ctx.lineTo(bx - w * 0.8, y - h - d * 0.6); ctx.lineTo(bx - w * 0.8 + w, y - h - d * 1.6); ctx.lineTo(bx + w, y - d - h); ctx.closePath();
      ctx.fillStyle = "rgba(62,240,255,.22)"; ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(bx, y); ctx.lineTo(bx - w * 0.8, y - d * 0.6); ctx.lineTo(bx - w * 0.8, y - h - d * 0.6); ctx.lineTo(bx, y - h); ctx.closePath();
      ctx.fillStyle = "rgba(62,240,255,.06)"; ctx.fill(); ctx.stroke();
      for (let f = 6; f < h - 2; f += 6) { // lit floors
        const on = still.matches || Math.sin(t / 500 + f + bx) > -0.6;
        ctx.strokeStyle = hexA(on ? "#b8fbff" : H1, on ? 0.8 : 0.25);
        ctx.beginPath(); ctx.moveTo(bx + 2, y - f - 0.3 * (f / h) * 0); ctx.lineTo(bx + w - 2, y - d - f + 1); ctx.stroke();
      }
    };
    tower(x - 6, 16, 44, 8); tower(x + 12, 12, 30, 6);
    ctx.globalAlpha = 1;
    glow(x + 4, y - 50, 10, "#ffffff", a);
  }

  // Where to put the datacenter: the cluster's time zone, else its UTC offset.
  const TZ_PLACES = {
    "Asia/Bangkok": [13.75, 100.5], "Asia/Ho_Chi_Minh": [10.8, 106.7], "Asia/Jakarta": [-6.2, 106.8], "Asia/Singapore": [1.35, 103.8],
    "Asia/Kuala_Lumpur": [3.14, 101.7], "Asia/Manila": [14.6, 121], "Asia/Hong_Kong": [22.3, 114.2], "Asia/Shanghai": [31.2, 121.5],
    "Asia/Tokyo": [35.7, 139.7], "Asia/Seoul": [37.6, 127], "Asia/Kolkata": [19.1, 72.9], "Asia/Dubai": [25.2, 55.3],
    "Europe/London": [51.5, -0.1], "Europe/Berlin": [52.5, 13.4], "Europe/Paris": [48.9, 2.35], "Europe/Amsterdam": [52.4, 4.9],
    "America/New_York": [40.7, -74], "America/Chicago": [41.9, -87.6], "America/Los_Angeles": [34.05, -118.2], "America/Sao_Paulo": [-23.5, -46.6],
    "Australia/Sydney": [-33.9, 151.2], "UTC": [51.5, 0], "Etc/UTC": [51.5, 0],
  };
  function sitePosition() {
    const tz = scene.tz || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    let [lat, lon] = TZ_PLACES[tz] || [];
    if (lat == null) {
      const off = /GMT([+-]\d+)/.exec(new Date().toLocaleString("en-US", { timeZone: tz, timeZoneName: "shortOffset" }) || "");
      lat = 15; lon = (off ? Number(off[1]) : 0) * 15;
    }
    const la = (lat * Math.PI) / 180, lo = (-lon * Math.PI) / 180;
    return { tz, lat: la, lon: lo, pos: [Math.cos(la) * Math.cos(lo), Math.sin(la), Math.cos(la) * Math.sin(lo)] };
  }


  // ---------- inside the datacenter: the HCI system in zones ----------

  function storeDevices() {
    const raw = state.pools || [], out = new Map();
    for (const p of raw) {
      const src = String(p.source || ""), vstore = p.name === "virtualstorage" || /gateway-exports/.test(src) || /virtual storage/i.test(p.display_name || "");
      const kind = vstore ? "vstore" : p.type === "iscsi" ? "iscsi" : p.type === "nfs" ? "nas" : p.type === "lvm" ? "san" : "local";
      const key = vstore ? "vstore" : kind === "local" ? `local:${p.node}:${p.name}` : `${kind}:${src || p.name}`;
      const d = out.get(key) || { key, kind, name: p.display_name || p.name, source: src, nodes: new Set(), cap: 0, alloc: 0 };
      d.nodes.add(p.node); d.cap = Math.max(d.cap, p.capacity_gb || 0); d.alloc = Math.max(d.alloc, p.allocation_gb || 0);
      out.set(key, d);
    }
    const order = { vstore: 0, san: 1, iscsi: 2, nas: 3, local: 4 };
    return [...out.values()].sort((a, b) => order[a.kind] - order[b.kind]).slice(0, 6);
  }
  const STORE_LABEL = { vstore: "vStore · Virtual Storage", san: "FC / SAN array", iscsi: "iSCSI target", nas: "NAS · NFS", local: "Local disk" };

  function zone(z, t) {
    const { x, y, w, h, title, color } = z;
    ctx.globalCompositeOperation = "source-over";
    const g = ctx.createLinearGradient(x, y, x, y + h);
    g.addColorStop(0, hexA(color, 0.07)); g.addColorStop(1, hexA(color, 0.02));
    ctx.fillStyle = g; ctx.fillRect(x, y, w, h);
    ctx.strokeStyle = hexA(color, 0.35); ctx.lineWidth = 1; ctx.setLineDash([3, 5]); ctx.strokeRect(x, y, w, h); ctx.setLineDash([]);
    ctx.strokeStyle = hexA(color, 0.06);
    for (let gx = x + 24; gx < x + w; gx += 24) { ctx.beginPath(); ctx.moveTo(gx, y); ctx.lineTo(gx, y + h); ctx.stroke(); }
    for (let gy = y + 24; gy < y + h; gy += 24) { ctx.beginPath(); ctx.moveTo(x, gy); ctx.lineTo(x + w, gy); ctx.stroke(); }
    ctx.strokeStyle = color; ctx.lineWidth = 2.5; // corner brackets
    for (const [cx, cy, dx, dy] of [[x, y, 1, 1], [x + w, y, -1, 1], [x, y + h, 1, -1], [x + w, y + h, -1, -1]]) {
      ctx.beginPath(); ctx.moveTo(cx + 16 * dx, cy); ctx.lineTo(cx, cy); ctx.lineTo(cx, cy + 16 * dy); ctx.stroke();
    }
    ctx.fillStyle = color; ctx.fillRect(x + 12, y + 10, 3, 14);
    ctx.font = "600 11px ui-monospace, SFMono-Regular, Menlo, monospace"; ctx.textAlign = "left";
    ctx.fillStyle = hexA(color, 0.95); ctx.fillText(title, x + 22, y + 21);
    if (!still.matches) { // a scan line moving through the zone
      const sx = x + ((t / 14 + w * z.phase) % w);
      const sg = ctx.createLinearGradient(sx - 50, 0, sx, 0); sg.addColorStop(0, hexA(color, 0)); sg.addColorStop(1, hexA(color, 0.12));
      ctx.fillStyle = sg; ctx.fillRect(Math.max(x, sx - 50), y, Math.min(50, sx - x), h);
    }
    ctx.globalCompositeOperation = "lighter";
  }
  function text(x, y, str, color, size = 11, align = "center", weight = "") {
    ctx.globalCompositeOperation = "source-over";
    ctx.font = `${weight} ${size}px ui-monospace, SFMono-Regular, Menlo, monospace`; ctx.textAlign = align;
    ctx.fillStyle = color; ctx.fillText(str, x, y);
    ctx.globalCompositeOperation = "lighter";
  }
  function wire(a, b, color, t, amount, bend = 0) {
    const mid = [(a[0] + b[0]) / 2 + bend, (a[1] + b[1]) / 2];
    const at = (f) => { const u = 1 - f; return [u * u * a[0] + 2 * u * f * mid[0] + f * f * b[0], u * u * a[1] + 2 * u * f * mid[1] + f * f * b[1]]; };
    line(Array.from({ length: 21 }, (_, i) => at(i / 20)), color, 1.3, 0.75);
    const lv = level(amount);
    if (!still.matches) for (let k = 0; k < 2 + Math.round(lv * 4); k++) {
      const q = at((t * (0.00025 + lv * 0.0008) + k / (2 + Math.round(lv * 4))) % 1);
      glow(q[0], q[1], 6 + lv * 5, color);
    }
  }
  function server(x, y, w, h, color, healthy) { // a 2U chassis seen slightly from above
    ctx.fillStyle = hexA(color, 0.14); ctx.strokeStyle = color; ctx.lineWidth = 1.3;
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + 14, y - 10); ctx.lineTo(x + w + 14, y - 10); ctx.lineTo(x + w, y); ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.rect(x, y, w, h); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x + w, y); ctx.lineTo(x + w + 14, y - 10); ctx.lineTo(x + w + 14, y + h - 10); ctx.lineTo(x + w, y + h); ctx.closePath();
    ctx.fillStyle = hexA(color, 0.08); ctx.fill(); ctx.stroke();
    for (let i = 0; i < 6; i++) { // drive bays
      ctx.strokeStyle = hexA(color, 0.6); ctx.strokeRect(x + 8 + i * ((w - 40) / 6), y + 8, (w - 40) / 6 - 3, h - 16);
    }
    for (let i = 0; i < 3; i++) glow(x + w - 22 + i * 7, y + h / 2, 5, healthy ? GREEN : RED);
  }
  function device(x, y, w, h, color, kind) { // switch, SAN, NAS, cloud
    ctx.strokeStyle = color; ctx.fillStyle = hexA(color, 0.12); ctx.lineWidth = 1.3;
    if (kind === "cloud") {
      ctx.beginPath();
      ctx.arc(x + w * 0.3, y + h * 0.6, h * 0.35, Math.PI * 0.5, Math.PI * 1.5); ctx.arc(x + w * 0.5, y + h * 0.35, h * 0.35, Math.PI, 0);
      ctx.arc(x + w * 0.72, y + h * 0.6, h * 0.32, Math.PI * 1.5, Math.PI * 0.5); ctx.closePath(); ctx.fill(); ctx.stroke(); return;
    }
    ctx.beginPath(); ctx.rect(x, y, w, h); ctx.fill(); ctx.stroke();
    if (kind === "switch") for (let i = 0; i < 12; i++) { ctx.strokeStyle = hexA(color, 0.7); ctx.strokeRect(x + 8 + i * ((w - 16) / 12), y + h - 12, (w - 16) / 12 - 2, 6); }
    if (kind === "disks") for (let i = 0; i < 3; i++) {
      ctx.beginPath(); ctx.ellipse(x + w / 2, y + 10 + i * ((h - 20) / 2), w * 0.36, 6, 0, 0, Math.PI * 2); ctx.strokeStyle = hexA(color, 0.8); ctx.stroke();
    }
    if (kind === "array") for (let r = 0; r < 3; r++) for (let c = 0; c < 6; c++) { ctx.strokeStyle = hexA(color, 0.6); ctx.strokeRect(x + 6 + c * ((w - 12) / 6), y + 6 + r * ((h - 12) / 3), (w - 12) / 6 - 2, (h - 12) / 3 - 3); }
  }

  function drawInside(t, snap) {
    const ent = still.matches ? 1 : Math.min(1, (performance.now() - entered) / 650);
    ctx.save(); ctx.translate(W / 2, H / 2); ctx.scale(0.85 + 0.15 * ent, 0.85 + 0.15 * ent); ctx.translate(-W / 2, -H / 2);
    ctx.globalAlpha = ent;
    const Z = {
      net: { x: W * 0.01, y: H * 0.05, w: W * 0.22, h: H * 0.9, title: "NETWORK", color: PINK, phase: 0.1 },
      vm: { x: W * 0.26, y: H * 0.05, w: W * 0.73, h: H * 0.25, title: "WORKLOADS · VM GROUPS", color: GREEN, phase: 0.4 },
      cmp: { x: W * 0.26, y: H * 0.35, w: W * 0.73, h: H * 0.29, title: "HCI CLUSTER · COMPUTE", color: H1, phase: 0.7 },
      sto: { x: W * 0.26, y: H * 0.69, w: W * 0.73, h: H * 0.26, title: "STORAGE", color: "#b49bff", phase: 0.2 },
    };
    for (const z of Object.values(Z)) zone(z, t);

    // Compute: one server per node, then the witness.
    const members = [...scene.nodes.map((n) => ({ n })), ...(scene.witness ? [{ w: scene.witness }] : [])];
    const slot = Z.cmp.w / Math.max(1, members.length), pos = {};
    members.forEach((m, i) => {
      const cx = Z.cmp.x + slot * (i + 0.5), sw = Math.min(150, slot * 0.6), sh = 44, x = cx - sw / 2, y = Z.cmp.y + Z.cmp.h * 0.38;
      if (m.n) {
        const n = m.n, L = snap.nodes[n.name] || {}, color = n.healthy ? H1 : RED;
        server(x, y, sw, sh, color, n.healthy);
        text(cx, y + sh + 18, n.name, "#e8fbff", 12, "center", "600");
        const cpu = pct(L.cpu ?? n.cpu), mem = pct(n.mem);
        text(cx, y + sh + 33, `CPU ${cpu == null ? "—" : cpu.toFixed(0) + "%"} · MEM ${mem == null ? "—" : mem.toFixed(0) + "%"} · ${scene.vms.filter((v) => v.node === n).length} VMs`, hexA(color, 0.9), 10);
        pos[n.name] = { top: [cx, y - 10], left: [x, y + sh / 2], bottom: [cx, y + sh], right: [x + sw + 14, y + sh / 2] };
        hits.push({ kind: "node", node: n, x: cx, y: y + sh / 2, r: sw / 2 });
      } else {
        const ww = Math.min(90, sw * 0.6);
        server(cx - ww / 2, y + 8, ww, 30, AMBER, m.w.healthy !== false);
        text(cx, y + 58, `WITNESS · ${m.w.name}`, AMBER, 11, "center", "600");
        text(cx, y + 72, "quorum vote · no VM data", hexA(AMBER, 0.8), 10);
        pos.__witness = { left: [cx - ww / 2, y + 23] };
        hits.push({ kind: "witness", w: m.w, x: cx, y: y + 23, r: ww / 2 });
      }
    });
    const ids = scene.nodes.map((n) => pos[n.name]).filter(Boolean);
    for (let i = 1; i < ids.length; i++) wire(ids[i - 1].right, ids[i].left, H2, t, 1e6, 0);
    if (ids.length && pos.__witness) wire(ids.at(-1).right, pos.__witness.left, AMBER, t, 1e3, 0);
    if (ids.length > 1) text((ids[0].right[0] + ids[1].left[0]) / 2, ids[0].right[1] - 12, "cluster + replication", hexA(H2, 0.9), 10);

    // Workloads: VM groups, wired to the nodes their VMs run on.
    const gdefs = state.groups?.groups || [], members2 = state.groups?.members || {};
    const groups = [...gdefs.map((g) => ({ id: g.id, name: g.name })), { id: "", name: "Ungrouped" }]
      .map((g) => ({ ...g, vms: scene.vms.filter((v) => (members2[v.vm.uuid] || "") === g.id) })).filter((g) => g.vms.length);
    const gslot = Z.vm.w / Math.max(1, groups.length);
    groups.forEach((g, i) => {
      const cx = Z.vm.x + gslot * (i + 0.5), gw = Math.min(200, gslot * 0.8), gh = 64, x = cx - gw / 2, y = Z.vm.y + Z.vm.h * 0.3;
      ctx.fillStyle = hexA(GREEN, 0.1); ctx.strokeStyle = hexA(GREEN, 0.8); ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.roundRect(x, y, gw, gh, 14); ctx.fill(); ctx.stroke();
      text(cx, y + 18, g.name, "#e8fbff", 12, "center", "600");
      text(cx, y + 32, `${g.vms.filter((v) => v.running).length}/${g.vms.length} running`, hexA(GREEN, 0.9), 10);
      g.vms.slice(0, 14).forEach((v, k) => glow(x + 16 + k * ((gw - 32) / Math.max(1, Math.min(14, g.vms.length) - 1 || 1)), y + 48, 7, v.running ? GREEN : "#6c7a92"));
      hits.push({ kind: "group", g, x: cx, y: y + gh / 2, r: gw / 2 });
      for (const node of new Set(g.vms.map((v) => v.node))) {
        const nb = pos[node.name];
        if (nb) wire([cx, y + gh], nb.top, GREEN, t, g.vms.filter((v) => v.node === node).reduce((s, v) => s + v.rx + v.tx, 0));
      }
    });
    if (!groups.length) text(Z.vm.x + Z.vm.w / 2, Z.vm.y + Z.vm.h / 2, "No VMs yet", hexA(GREEN, 0.8), 12);

    // Network: each node's cabled ports to the switch LLDP reports, then outside.
    const sw = new Map();
    for (const n of scene.nodes) for (const nb of extras.nodes[n.name]?.lldp || []) {
      const key = nb.system_name || nb.chassis_id || "switch";
      const e = sw.get(key) || { key, desc: nb.system_desc || "", mgmt: nb.management || "", links: [] };
      e.links.push({ node: n, iface: nb.interface, port: nb.port_id || nb.port_desc || "" }); sw.set(key, e);
    }
    if (!sw.size) sw.set("switch", { key: "Upstream switch", desc: "not announced (LLDP off)", mgmt: "", links: scene.nodes.map((n) => ({ node: n, iface: (extras.nodes[n.name]?.nics || []).find((c) => c.kind === "ethernet" && c.link)?.name || "uplink", port: "" })) });
    const cloud = { x: Z.net.x + Z.net.w * 0.18, y: Z.net.y + 40, w: Z.net.w * 0.64, h: 56 };
    device(cloud.x, cloud.y, cloud.w, cloud.h, PINK, "cloud");
    text(cloud.x + cloud.w / 2, cloud.y + cloud.h + 16, "EXTERNAL NETWORK", hexA(PINK, 0.9), 10, "center", "600");
    const switches = [...sw.values()], sslot = (Z.net.h * 0.42) / Math.max(1, switches.length);
    switches.forEach((s2, i) => {
      const w = Z.net.w * 0.78, h = 34, x = Z.net.x + (Z.net.w - w) / 2, y = Z.net.y + Z.net.h * 0.3 + i * sslot;
      device(x, y, w, h, PINK, "switch");
      const model = (/C\d{3,4}\w*/.exec(s2.desc) || [""])[0];
      text(x + w / 2, y + 14, s2.key, "#ffe6f6", 11, "center", "600");
      text(x + w / 2, y + h + 14, [model || s2.desc.slice(0, 24), s2.mgmt].filter(Boolean).join(" · "), hexA(PINK, 0.85), 10);
      hits.push({ kind: "switch", s: s2, x: x + w / 2, y: y + h / 2, r: w / 2 });
      wire([x + w / 2, y], [cloud.x + cloud.w / 2, cloud.y + cloud.h], PINK, t, Object.values(snap.nets).reduce((a, n) => a + n.rx + n.tx, 0));
      for (const l of s2.links) {
        const nb = pos[l.node.name];
        if (!nb) continue;
        const nic = (extras.nodes[l.node.name]?.nics || []).find((c) => c.name === l.iface);
        wire([x + w, y + h / 2], nb.left, PINK, t, (snap.nodes[l.node.name]?.rx || 0) + (snap.nodes[l.node.name]?.tx || 0), 0);
        const speed = nic?.speed_mbps ? ` ${nic.speed_mbps >= 1000 ? nic.speed_mbps / 1000 + "G" : nic.speed_mbps + "M"}` : "";
        text(x + 4, y + h + 30 + s2.links.indexOf(l) * 14, `${l.port || "?"} ⇄ ${l.node.name} ${l.iface}${speed}`, hexA(PINK, 0.9), 10, "left");
      }
    });
    const nets = state.networks || [];
    nets.slice(0, 6).forEach((n, i) => {
      const y = Z.net.y + Z.net.h * 0.78 + i * 18;
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = hexA(scene.nets[i]?.color || PINK, 0.9); ctx.fillRect(Z.net.x + 16, y - 8, 8, 8);
      text(Z.net.x + 30, y, `${n.name}${n.vlan ? ` · VLAN ${n.vlan}` : " · untagged"}`, "#ffe6f6", 10, "left");
    });
    text(Z.net.x + 16, Z.net.y + Z.net.h * 0.78 - 18, "VM NETWORKS", hexA(PINK, 0.8), 9, "left", "600");

    // Storage: vStore, external arrays and local disks, wired to the nodes using them.
    const stores = storeDevices(), dslot = Z.sto.w / Math.max(1, stores.length);
    stores.forEach((d, i) => {
      const cx = Z.sto.x + dslot * (i + 0.5), w = Math.min(130, dslot * 0.55), h = 50, x = cx - w / 2, y = Z.sto.y + Z.sto.h * 0.35;
      const color = d.kind === "vstore" ? "#b49bff" : d.kind === "local" ? "#7f95b8" : "#c39bff";
      device(x, y, w, h, color, d.kind === "vstore" || d.kind === "local" ? "disks" : "array");
      text(cx, y + h + 16, STORE_LABEL[d.kind], "#f1e6ff", 11, "center", "600");
      const used = d.cap ? (d.alloc / d.cap) * 100 : 0;
      text(cx, y + h + 30, `${d.name} · ${used.toFixed(0)}% of ${Math.round(d.cap)} GB${d.source && d.kind !== "vstore" && d.kind !== "local" ? " · " + d.source.slice(0, 22) : ""}`, hexA(color, 0.9), 10);
      hits.push({ kind: "store", d, x: cx, y: y + h / 2, r: w / 2 });
      for (const name of d.nodes) {
        const nb = pos[name];
        if (!nb) continue;
        wire(nb.bottom, [cx, y], color, t, (snap.nodes[name]?.read || 0) + (snap.nodes[name]?.write || 0));
      }
      if (d.kind === "vstore" && d.nodes.size > 1) text(cx, y - 8, `replicated on ${d.nodes.size} nodes`, hexA(color, 0.9), 10);
    });
    ctx.restore();
    ctx.globalAlpha = 1;
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
    if (drag && mode === "inside") drag = null;
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
    } else if (hover.kind === "site") {
      tip.innerHTML = `<b>Datacenter · ${esc(scene.tz || "time zone not reported")}</b><span>${scene.nodes.length} nodes${scene.witness ? " + witness" : ""}, ${scene.vms.length} VMs</span><em>Click to look inside</em>`;
    } else if (hover.kind === "switch") {
      const s2 = hover.s;
      tip.innerHTML = `<b>${esc(s2.key)}</b><span>${esc(s2.desc || "")}</span>${s2.mgmt ? `<span>Management ${esc(s2.mgmt)}</span>` : ""}${s2.links.map((l) => `<span>${esc(l.node.name)} ${esc(l.iface)} → ${esc(l.port || "?")}</span>`).join("")}<em>From LLDP announcements</em>`;
    } else if (hover.kind === "group") {
      const g = hover.g;
      tip.innerHTML = `<b>${esc(g.name)}</b>${g.vms.slice(0, 8).map((v) => `<span>${v.running ? "●" : "○"} ${esc(v.vm.name)} · ${esc(v.node.name)}</span>`).join("")}<em>Click to open Virtual machines</em>`;
    } else if (hover.kind === "store") {
      const d = hover.d;
      tip.innerHTML = `<b>${esc(STORE_LABEL[d.kind])} · ${esc(d.name)}</b><span>${Math.round(d.alloc)} of ${Math.round(d.cap)} GB used</span>${d.source ? `<span>${esc(d.source)}</span>` : ""}<span>Nodes: ${esc([...d.nodes].join(", "))}</span><em>Click to open Storage</em>`;
    } else if (hover.kind === "witness") {
      tip.innerHTML = `<b>Witness · ${esc(hover.w.name)}</b><span>Votes in quorum, stores no VM data</span>`;
    } else {
      tip.innerHTML = `<b>Virtual Storage</b>${scene.pools.slice(0, 4).map((p) => `<span>${esc(p.name)} · ${p.used.toFixed(0)}% of ${Math.round(p.cap)} GB</span>`).join("")}<em>Click to open Storage</em>`;
    }
  });
  canvas.addEventListener("pointerup", (e) => {
    const moved = drag?.moved; drag = null;
    if (moved) return;
    const hit = pick(e);
    if (hit?.kind === "site") { setMode("inside"); return; }
    if (hit?.kind === "group") { location.hash = "compute"; return; }
    if (hit?.kind === "store") { location.hash = "storage"; return; }
    if (hit?.kind === "vm") location.hash = `vm/${encodeURIComponent(hit.v.vm.uuid)}`;
    else if (hit?.kind === "node") location.hash = `node/${encodeURIComponent(hit.node.name)}`;
    else if (hit?.kind === "core") location.hash = "storage";
  });
  canvas.addEventListener("pointerleave", () => { tip.hidden = true; hover = null; });
  canvas.addEventListener("wheel", (e) => { if (mode === "inside") return; e.preventDefault(); view.zoom = Math.max(0.6, Math.min(2.2, view.zoom * (e.deltaY > 0 ? 0.93 : 1.07))); }, { passive: false });
  $id("fabric-spin").addEventListener("click", (e) => {
    view.spin = !view.spin;
    e.currentTarget.textContent = view.spin ? "Pause rotation" : "Rotate";
    e.currentTarget.setAttribute("aria-pressed", String(!view.spin));
  });
  $id("fabric-reset").addEventListener("click", () => { Object.assign(view, HOME, { vyaw: 0 }); inside3d?.home(); });

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
  async function loadExtras() {
    if (Date.now() - extras.at < 60000) return;
    extras.at = Date.now();
    const members = (state.cluster?.members || []).filter((m) => m.role === "compute" && m.healthy !== false);
    await Promise.all(members.map(async (m) => {
      const name = encodeURIComponent(m.name), get = (path) => api(path).catch(() => null);
      const [settings, nics, lldp] = await Promise.all([get(`/nodes/${name}/settings`), get(`/nodes/${name}/nics`), get(`/nodes/${name}/lldp`)]);
      extras.nodes[m.name] = { tz: settings?.timezone || "", nics: nics || [], lldp: lldp || [] };
    }));
    build();
  }
  let inside3d = null;
  function setMode(next) {
    mode = next; entered = performance.now();
    backBtn.hidden = mode !== "inside";
    tip.hidden = true;
    inside3d?.dispose(); inside3d = null; $id("fx-3d")?.remove();
    if (mode === "inside" && window.Fabric3D) { // real 3D when it loaded; the 2D zones otherwise
      const box = document.createElement("div"); box.className = "fx-3d"; box.id = "fx-3d";
      canvas.parentElement.insertBefore(box, tip);
      try { inside3d = window.Fabric3D.inside(box); inside3d.update(insideData()); } catch (err) { console.error(err); box.remove(); inside3d = null; }
    }
    page.classList.toggle("fabric-inside", !!inside3d);
  }
  const osOf = (vm) => { const k = typeof osKind === "function" ? osKind(osName(vm, state.guests?.[vm.uuid])) : ""; return k.toLowerCase(); };
  function insideData() {
    const snap = moment(), gdefs = state.groups?.groups || [], gm = state.groups?.members || {};
    const groups = [...gdefs.map((g) => ({ id: g.id, name: g.name })), { id: "", name: "Ungrouped" }]
      .map((g) => { const vms = scene.vms.filter((v) => (gm[v.vm.uuid] || "") === g.id).map((v) => ({ name: v.vm.name, running: v.running, os: osOf(v.vm) })); return { name: g.name, vms, running: vms.filter((v) => v.running).length }; })
      .filter((g) => g.vms.length);
    const sw = new Map();
    for (const n of scene.nodes) for (const nb of extras.nodes[n.name]?.lldp || []) {
      const key = nb.system_name || nb.chassis_id || "switch";
      const e = sw.get(key) || { name: key, model: (/C\d{3,4}\w*/.exec(nb.system_desc || "") || [""])[0] || (nb.system_desc || "").slice(0, 30), mgmt: nb.management || "", links: [] };
      const nic = (extras.nodes[n.name]?.nics || []).find((c) => c.name === nb.interface);
      e.links.push({ node: n.name, iface: nb.interface, port: nb.port_id || nb.port_desc || "", speed: nic?.speed_mbps ? (nic.speed_mbps >= 1000 ? `${nic.speed_mbps / 1000}G` : `${nic.speed_mbps}M`) : "" });
      sw.set(key, e);
    }
    if (!sw.size) sw.set("switch", { name: "Upstream switch", model: "not announced (LLDP off)", mgmt: "", links: scene.nodes.map((n) => ({ node: n.name, iface: (extras.nodes[n.name]?.nics || []).find((c) => c.kind === "ethernet" && c.link)?.name || "uplink", port: "", speed: "" })) });
    const nets = Object.values(snap.nets), nodes = Object.values(snap.nodes);
    return {
      nodes: scene.nodes.map((n) => ({ name: n.name, healthy: n.healthy, cpu: snap.nodes[n.name]?.cpu ?? n.cpu, mem: n.mem, vms: scene.vms.filter((v) => v.node === n).length,
        vmList: scene.vms.filter((v) => v.node === n).sort((a, b) => b.running - a.running).map((v) => ({ name: v.vm.name, running: v.running, os: osOf(v.vm) })) })),
      witness: scene.witness ? { name: scene.witness.name } : null,
      vmsTotal: scene.vms.length, vmsRunning: scene.vms.filter((v) => v.running).length,
      groups, switches: [...sw.values()], nets: (state.networks || []).map((n) => ({ name: n.name, vlan: n.vlan })),
      stores: (() => { // Virtual Storage always first, also before it is set up
        const list = storeDevices().map((d) => ({ key: d.key, kind: d.kind, label: STORE_LABEL[d.kind], name: d.name, used: d.cap ? (d.alloc / d.cap) * 100 : 0, cap: d.cap, nodes: [...d.nodes] }));
        if (!list.some((d) => d.kind === "vstore")) list.unshift({ key: "vstore", kind: "vstore", label: STORE_LABEL.vstore, name: "Virtual Storage", used: 0, cap: 0, nodes: [] });
        return list;
      })(),
      traffic: { vm: scene.vms.reduce((a, v) => a + v.rx + v.tx, 0), net: nets.reduce((a, n) => a + n.rx + n.tx, 0), sto: nodes.reduce((a, n) => a + n.read + n.write, 0) },
    };
  }
  backBtn.addEventListener("click", () => setMode("globe"));
  function start() {
    build();
    loadExtras();
    if (!frame) frame = requestAnimationFrame(loop);
    clearInterval(poll);
    poll = setInterval(async () => {
      if (!page.classList.contains("active")) { clearInterval(poll); poll = 0; return; }
      if (document.hidden) return;
      try { await load("stats"); build(); loadExtras(); } catch { /* next tick */ }
    }, 3000);
  }
  return { render: start, rate, bytesRate, view };
})();
