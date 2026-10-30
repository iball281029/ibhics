"use strict";

// ---------- helpers ----------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function toLogin() {
  location.href = `/login.html?next=${encodeURIComponent(location.pathname + location.hash)}`;
  return new Promise(() => {}); // the page is going away
}

async function api(path, opts = {}) {
  const r = await fetch(`/api/v1${path}`, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join(", ") : body.detail;
    if (r.status === 401 || (r.status === 403 && /initial password/.test(detail || ""))) return toLogin();
    throw new Error(detail || r.statusText);
  }
  return r.status === 204 ? null : r.json();
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body ?? {}) });
const patch = (path, body) => api(path, { method: "PATCH", body: JSON.stringify(body ?? {}) });
const del = (path) => api(path, { method: "DELETE" });
const enc = encodeURIComponent;

function toast(text, error = false) {
  const el = document.createElement("div");
  el.className = `toast${error ? " error" : ""}`;
  el.textContent = text;
  $("#toast").append(el);
  setTimeout(() => el.remove(), error ? 7000 : 3500);
}
const fail = (err) => toast(err.message || String(err), true);

const size = (bytes) => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i += 1; }
  return `${bytes.toFixed(i ? 1 : 0)} ${units[i]}`;
};
const gbText = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(mb % 1024 ? 1 : 0)} GB` : `${mb} MB`);
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "–");
const ago = (iso) => {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
};
const pct = (used, total) => (total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0);
const level = (p) => (p >= 90 ? "bad" : p >= 75 ? "warn" : "");
const empty = (cols, text) => `<tr><td colspan="${cols}" class="empty">${esc(text)}</td></tr>`;
const bar = (p) => `<div class="bar ${level(p)}"><i style="width:${p}%"></i></div>`;
const segments = (p, n = 24) => `<div class="segments ${level(p)}" style="grid-template-columns:repeat(${n},1fr)">${
  Array.from({ length: n }, (_, i) => `<i class="${(i + 0.5) / n * 100 <= p ? "on" : ""}"></i>`).join("")}</div>`;
const avatar = (icon) => `<span class="avatar"><svg><use href="#${icon}"/></svg></span>`;

// ---------- state ----------

const state = { me: null, cluster: null, hosts: {}, guests: {}, vms: [], pools: [], targets: [], sources: [], policies: [], jobs: [], moves: [], imports: [], users: [] };
const TOPOLOGY = { "standalone": "Standalone", "two-node-witness": "2 nodes + witness", "cluster": "Cluster", "incomplete": "Incomplete" };
const POOL_TYPES = { dir: "Local", nfs: "NFS", iscsi: "iSCSI", lvm: "LVM (FC / disk)" };
const TARGET_TYPES = { local: "Local disk", nfs: "NFS", smb: "SMB", usb: "USB", iscsi: "iSCSI", fc: "FC", s3: "S3" };
const SOURCE_TYPES = { vmware: "VMware", ova: "OVA folder", disk: "Disk images", upload: "Uploaded files" };
const JOB_STATE = { queued: "Queued", running: "Running", done: "Succeeded", failed: "Failed" };
const isAdmin = () => !state.me || state.me.role === "admin";
const canOperate = () => !state.me || state.me.role !== "viewer";
const computeMembers = () => (state.cluster?.members || []).filter((m) => m.role === "compute");

// ---------- theme and user menu ----------

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  $$("[data-theme-set]").forEach((b) => b.classList.toggle("on", (b.dataset.themeSet === "dark") === dark));
}
try { applyTheme(localStorage.getItem("ibhics-theme")); } catch { applyTheme(null); }
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-theme-set]");
  if (t) {
    applyTheme(t.dataset.themeSet);
    try { localStorage.setItem("ibhics-theme", t.dataset.themeSet); } catch { /* private mode */ }
  }
  const menu = e.target.closest("#user-menu");
  $("#user-dropdown").classList.toggle("open", Boolean(menu) && !e.target.closest(".dropdown"));
  const tasks = e.target.closest("#tasks-menu");
  if (!tasks || !e.target.closest(".dropdown")) $("#tasks-panel").classList.toggle("open", Boolean(tasks) && !$("#tasks-panel").classList.contains("open"));
  $$("details.more[open]").forEach((d) => { if (!d.contains(e.target)) d.removeAttribute("open"); });
  const link = e.target.closest("[data-tab-link]");
  if (link) setTimeout(() => selectTab("system-tabs", link.dataset.tabLink), 0);
});
$("#logout").addEventListener("click", async () => {
  await fetch("/api/v1/auth/logout", { method: "POST" }).catch(() => {});
  location.href = "/login.html";
});

async function loadMe() {
  state.me = await api("/auth/me");
  $("#whoami").textContent = state.me ? state.me.name : "local";
  document.body.classList.toggle("can-admin", isAdmin());
  document.body.classList.toggle("can-operate", canOperate());
}

// ---------- navigation ----------

const PAGES = ["home", "compute", "storage", "nodes", "backup", "migrate", "activity", "system"];
const currentPage = () => {
  const hash = location.hash.slice(1);
  if (hash.startsWith("vm/")) return "vm";
  return PAGES.includes(hash) ? hash : "home";
};
const vmFromHash = () => decodeURIComponent(location.hash.slice(4));

function show() {
  const page = currentPage();
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${page}`));
  const section = page === "vm" ? "compute" : page;
  $$(".rail a[data-page]").forEach((a) => a.classList.toggle("active", a.dataset.page === section));
  if (page === "vm") selectTab("vm-tabs", "vm-summary");
  refresh(page).catch(fail);
}
window.addEventListener("hashchange", show);

document.addEventListener("click", (e) => {
  const goto = e.target.closest("[data-goto]");
  if (goto) location.hash = goto.dataset.goto;
  const open = e.target.closest("[data-open]");
  if (open) openDialog(open.dataset.open);
});

function selectTab(tabsId, name) {
  const tabs = document.getElementById(tabsId);
  $$("button[data-tab]", tabs).forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".subpage", tabs.parentElement).forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
}
$$(".tabs").forEach((tabs) => tabs.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (btn) selectTab(tabs.id, btn.dataset.tab);
}));

// ---------- dialogs ----------

function syncSwitch(form) {
  const sw = $("[data-switch]", form);
  if (!sw) return;
  $$("[data-for]", form).forEach((el) => { el.style.display = el.dataset.for.split(" ").includes(sw.value) ? "" : "none"; });
}
$$("dialog form").forEach((form) => {
  const sw = $("[data-switch]", form);
  if (sw) sw.addEventListener("change", () => syncSwitch(form));
});

const handlers = {};
let dialogContext = null;

function openDialog(id, context = null, prefill = {}) {
  const dlg = document.getElementById(id);
  const form = $("form", dlg);
  form.reset();
  fillSelects();
  for (const [k, v] of Object.entries(prefill)) if (form.elements[k] && v != null) form.elements[k].value = v;
  syncSwitch(form);
  dialogContext = context;
  dlg.showModal();
  // Pickers need the current pools and VMs even when the page did not load them.
  const want = [$(".pool-list", form) && "pools", $(".vm-check-list", form) && "vms"].filter(Boolean);
  if (want.length) load(...want).then(() => fillSelects()).catch(() => {});
  if ($(".disk-list", form)) fillDisks(form);
}

$$("dialog").forEach((dlg) => {
  const form = $("form", dlg);
  form.addEventListener("submit", async (e) => {
    if (e.submitter?.value !== "ok") return; // cancel closes the dialog
    e.preventDefault();
    if (!form.reportValidity()) return;
    const button = e.submitter;
    button.disabled = true;
    try {
      await handlers[dlg.id](new FormData(form), dialogContext);
      dlg.close();
    } catch (err) {
      fail(err);
    } finally {
      button.disabled = false;
    }
  });
});

function fillSelects() {
  const online = computeMembers().filter((m) => m.healthy);
  const opts = online.map((m) => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join("");
  $$(".node-list").forEach((s) => { s.innerHTML = opts; });
  $$(".node-list-auto").forEach((s) => { s.innerHTML = `<option value="">Automatic</option>${opts}`; });
  const keep = (sel, items) => $$(sel).forEach((s) => {
    const was = s.value;
    s.innerHTML = items.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    if (items.includes(was)) s.value = was;
  });
  keep(".target-list", state.targets.map((t) => t.name));
  keep(".source-list", state.sources.map((s) => s.name));
  $$("form").forEach(fillPools);
  $$(".vm-check-list").forEach((box) => {
    box.innerHTML = state.vms.length
      ? [...state.vms].sort((a, b) => a.name.localeCompare(b.name)).map((v) =>
        `<label class="check"><input type="checkbox" name="vms" value="${esc(v.name)}"> ${esc(v.name)} <span class="muted">${esc(v.node || "")}</span></label>`).join("")
      : `<p class="hint">No VMs yet.</p>`;
  });
}

// Pool pickers list the pools of the chosen node (or every node's, for Automatic) with
// their free space, so nobody has to type a pool name.
function fillPools(form) {
  const sel = $(".pool-list", form);
  if (!sel) return;
  const node = form.elements.node?.value || "";
  const pools = state.pools.filter((p) => p.active && (!node || p.node === node));
  const byName = new Map();
  for (const p of pools) {
    const seen = byName.get(p.name);
    if (!seen || p.available_gb < seen.available_gb) byName.set(p.name, p); // the tightest node
  }
  const was = sel.value;
  const opts = [...byName.values()].sort((a, b) => b.available_gb - a.available_gb).map((p) =>
    `<option value="${esc(p.name)}">${esc(p.name)} (${Math.floor(p.available_gb).toLocaleString()} GB free, ${esc(p.type)})</option>`);
  if (sel.dataset.original) opts.unshift(`<option value="">${esc(sel.dataset.original)}</option>`);
  sel.innerHTML = opts.join("") || `<option value="">No storage pool on this node</option>`;
  if ([...sel.options].some((o) => o.value === was)) sel.value = was;
  else if (!sel.dataset.original && byName.has("default")) sel.value = "default";
}
$$("form").forEach((form) => {
  if ($(".pool-list", form) && form.elements.node) form.elements.node.addEventListener("change", () => fillPools(form));
});

// ---------- data ----------

async function loadCluster() {
  const c = await api("/cluster");
  state.cluster = c;
  $("#topology").textContent = TOPOLOGY[c.topology] || c.topology;
  $("#quorum").innerHTML = `<span class="dot ${c.quorum ? "ok" : "bad"}"></span> ${c.quorum ? "OK" : "Lost"}`;
  const hosts = await Promise.all(computeMembers().filter((m) => m.healthy).map((m) =>
    api(`/nodes/${enc(m.name)}/host`).then((h) => [m.name, h]).catch(() => [m.name, null])));
  state.hosts = Object.fromEntries(hosts);
}
const loaders = {
  vms: async () => { state.vms = await api("/vms"); },
  guests: async () => { state.guests = Object.fromEntries((await api("/vms/guests")).map((g) => [g.uuid, g])); },
  pools: async () => { state.pools = await api("/pools"); },
  targets: async () => { state.targets = await api("/backup/targets"); },
  policies: async () => { state.policies = await api("/backup/policies"); },
  jobs: async () => { state.jobs = await api("/backup/jobs"); },
  moves: async () => { state.moves = await api("/migrations"); },
  sources: async () => { state.sources = await api("/migrate/sources"); },
  imports: async () => { state.imports = await api("/migrate/jobs"); },
  users: async () => { state.users = isAdmin() && state.me ? await api("/users") : []; },
};
const load = (...names) => Promise.all(names.map((n) => loaders[n]()));

// ---------- tasks ----------

// Everything long-running the system does, newest first: imports, backups and restores,
// VM moves. Running ones show their current step and how far along they are.
function allTasks() {
  const tasks = [
    ...state.imports.map((j) => ({
      kind: "Import", title: j.name, sub: `from ${j.source} to ${j.node}`, state: j.state,
      step: j.step, percent: j.percent, created: j.created, finished: j.finished, error: j.error, log: j.id,
    })),
    ...state.jobs.map((j) => ({
      kind: j.kind === "restore" ? "Restore" : "Backup", title: j.vm, sub: `${j.kind === "restore" ? "from" : "to"} ${j.target}`,
      state: j.state, step: "", percent: null, created: j.created, finished: j.finished, error: j.error,
    })),
    ...state.moves.map((m) => ({
      kind: m.live ? "Live migration" : "Move", title: m.vm, sub: `${m.source} → ${m.target || "auto"}`,
      state: m.state, step: "", percent: null, created: m.created, finished: m.finished, error: m.error,
    })),
  ];
  return tasks.sort((a, b) => String(b.created).localeCompare(String(a.created)));
}
const isActive = (t) => t.state === "running" || t.state === "queued";
function renderTasks() {
  const tasks = allTasks();
  const active = tasks.filter(isActive);
  $("#tasks-count").innerHTML = active.length
    ? `<span class="spinner"></span> ${active.length} running`
    : `<span class="dot ok"></span> None running`;
  const row = (t) => `<div class="task ${esc(t.state)}">
    <div class="task-head"><span class="badge plain">${esc(t.kind)}</span><b>${esc(t.title)}</b><span class="vm-sub">${esc(t.sub)}</span>
      <span class="spacer"></span><span class="badge ${esc(t.state)}">${JOB_STATE[t.state] || esc(t.state)}</span></div>
    ${isActive(t) ? `<div class="task-step">${esc(t.step || (t.state === "queued" ? "Waiting for a free slot" : "Working"))}${t.percent != null ? ` · ${t.percent}%` : ""}</div>
      <div class="bar${t.percent == null ? " busy" : ""}"><i style="width:${t.percent ?? 30}%"></i></div>` : ""}
    ${t.error ? `<div class="task-error">${esc(t.error)}</div>` : ""}
    <div class="task-foot"><span>${esc(when(t.created))}</span>${t.log ? `<button class="small" data-task-log="${esc(t.log)}" data-task-title="${esc(t.title)}">Log</button>` : ""}</div>
  </div>`;
  $("#tasks-panel").innerHTML = tasks.length
    ? `<div class="tasks-title">Tasks</div>${tasks.slice(0, 15).map(row).join("")}`
    : `<p class="hint" style="padding:10px">Nothing has run yet.</p>`;
}
let logTimer = null;
async function showLog(id, title) {
  $("#log-title").textContent = `Import log: ${title}`;
  const dlg = $("#log-dialog");
  const pull = async () => {
    const { log } = await api(`/migrate/jobs/${enc(id)}/log`).catch((err) => ({ log: err.message }));
    const pre = $("#log-text");
    const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = log || "No output yet.";
    if (atEnd) pre.scrollTop = pre.scrollHeight;
  };
  await pull();
  dlg.showModal();
  clearInterval(logTimer);
  logTimer = setInterval(() => (dlg.open ? pull() : clearInterval(logTimer)), 3000);
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-task-log]");
  if (b) showLog(b.dataset.taskLog, b.dataset.taskTitle);
});
// While something runs, keep the tasks fresh every few seconds, whatever page is open.
setInterval(async () => {
  if (!allTasks().some(isActive) && !$("#tasks-panel").classList.contains("open")) return;
  try { await load("imports", "jobs", "moves"); renderTasks(); } catch { /* next tick */ }
}, 3000);

async function refresh(page, auto = false) {
  await loadCluster();
  await load("imports", "jobs", "moves").then(renderTasks).catch(() => {});
  if (page === "home") { await load("vms", "pools", "targets", "policies", "jobs", "moves", "imports", "users"); renderHome(); }
  if (page === "compute") { await load("vms", "guests", "pools", "targets", "moves"); renderVMs(); renderMoves(); }
  if (page === "vm") { await load("vms", "pools", "targets", "jobs", "moves", "imports"); await renderVMDetail(); }
  if (page === "storage") { await load("pools", "vms"); renderPools(); }
  if (page === "nodes") { await load("vms"); renderNodes(); }
  if (page === "backup") { await load("targets", "policies", "jobs"); fillSelects(); renderTargets(); renderPolicies(); renderJobs(); await renderBackups(); }
  if (page === "migrate") { await load("sources", "imports", "pools"); fillSelects(); renderSources(); renderImports(); if (!auto || !sourceVMsFor) await renderSourceVMs(); }
  if (page === "activity") { await load("jobs", "moves", "imports"); $("#activity-feed").innerHTML = feed(100) || `<li class="hint">Nothing has happened yet.</li>`; }
  if (page === "system") { await load("users"); await renderSystem(); }
  fillSelects();
}

// ---------- home ----------

function totals() {
  let cpus = 0, mem = 0;
  for (const m of computeMembers()) {
    const h = state.hosts[m.name];
    if (h) { cpus += h.cpus; mem += h.memory_mb; }
  }
  const live = state.vms.filter((v) => v.state !== "stopped");
  return {
    cpus, mem,
    vcpus: live.reduce((s, v) => s + v.vcpus, 0),
    vmem: live.reduce((s, v) => s + v.memory_mb, 0),
    cap: state.pools.reduce((s, p) => s + p.capacity_gb, 0),
    used: state.pools.reduce((s, p) => s + p.allocation_gb, 0),
  };
}

function activities() {
  const items = [
    ...state.jobs.map((j) => ({ kind: j.kind === "backup" ? "Backup" : "Restore", icon: "i-backup", what: j.vm, where: j.target, state: j.state, created: j.created, error: j.error })),
    ...state.moves.map((m) => ({ kind: "Live migration", icon: "i-migrate", what: m.vm, where: `${m.source} → ${m.target || "?"}`, state: m.state, created: m.created, error: m.error })),
    ...state.imports.map((j) => ({ kind: "Import", icon: "i-compute", what: j.name, where: j.source, state: j.state, created: j.created, error: j.error })),
  ];
  return items.sort((a, b) => (b.created || "").localeCompare(a.created || ""));
}
const feed = (limit) => activities().slice(0, limit).map((a) => `<li>
  ${avatar(a.icon)}
  <div><b>${esc(a.kind)}</b> · ${esc(a.what)} <span class="vm-sub">${esc(a.where)}${a.error ? ` · ${esc(a.error)}` : ""}</span></div>
  <span class="badge ${esc(a.state)}">${JOB_STATE[a.state] || esc(a.state)}</span>
  <span class="when">${esc(ago(a.created))}</span></li>`).join("");

function topo(icon, num, cap, note, page, flow = false) {
  return `<div class="topo${flow ? " flow" : ""}" data-goto="${page}">
    <div class="topo-icon"><svg><use href="#${icon}"/></svg></div>
    <div class="num">${esc(num)}</div><div class="cap">${esc(cap)}</div><div class="note">${note}</div></div>`;
}

function renderHome() {
  const c = state.cluster, t = totals();
  const members = c.members, offline = members.filter((m) => !m.healthy);
  const running = state.vms.filter((v) => v.state === "running").length;
  const storagePct = pct(t.used, t.cap);
  const failed = activities().filter((a) => a.state === "failed" && Date.now() - new Date(a.created) < 86400e3);

  let mood = ["ok", "Everything Is ", "Great!"];
  if (!c.quorum) mood = ["bad", "Cluster Has ", "Lost Quorum"];
  else if (offline.length) mood = ["warn", `${offline.length} Node${offline.length > 1 ? "s" : ""} `, "Offline"];
  else if (storagePct >= 85 || failed.length) mood = ["warn", "Needs ", "Attention"];
  $("#health").innerHTML = `${esc(mood[1])}<em class="${mood[0]}">${esc(mood[2])}</em>`;

  const cs = $("#cluster-state");
  cs.textContent = c.quorum ? (c.ha_capable ? "Highly available" : "Healthy") : "No quorum";
  cs.className = `val ${c.quorum ? "ok" : "bad"}`;
  const row = (k, v) => `<div class="row"><span>${esc(k)}</span><b>${v}</b></div>`;
  $("#cluster-box").innerHTML = [
    row("Deployment", esc(TOPOLOGY[c.topology] || c.topology)),
    row("High availability", c.ha_capable ? "Yes" : "No"),
    row("Members online", `${members.length - offline.length} / ${members.length}`),
    row("Quorum", `<span class="dot ${c.quorum ? "ok" : "bad"}"></span> ${c.quorum ? "OK" : "Lost"}`),
    row("Physical cores", t.cpus),
    row("Physical memory", gbText(t.mem)),
    `<div class="hint">${c.ha_capable ? "VMs can be moved between nodes while running." : "Add a second compute node and a witness for high availability."}</div>`,
  ].join("");

  const infra = $("#infra-state");
  const infraLevel = offline.length || storagePct >= 90 ? "bad" : storagePct >= 75 || failed.length ? "warn" : "ok";
  infra.textContent = { ok: "Excellent", warn: "Fair", bad: "Poor" }[infraLevel];
  infra.className = `val ${infraLevel}`;
  const computes = computeMembers();
  $("#topology-strip").innerHTML = [
    topo("t-node", computes.length, "Nodes", `${computes.filter((m) => m.healthy).length} online`, "nodes"),
    topo("t-storage", state.pools.length, "Storage pools", `${storagePct}% used`, "storage", true),
    topo("t-vm", state.vms.length, "Virtual machines", `${running} running`, "compute"),
    topo("t-backup", state.targets.length, "Backup targets", `${state.policies.length} schedule${state.policies.length === 1 ? "" : "s"}`, "backup"),
    topo("t-users", state.me ? (state.users.length || 1) : "–", "Users", state.me ? esc(state.me.role) : "sign-in off", "system"),
  ].join("");

  const meter = (name, p, detail) => `<div class="meter"><div class="top"><b>${esc(name)}</b><span>${detail}</span></div>${segments(p)}</div>`;
  $("#usage").innerHTML = [
    meter("CPU", pct(t.vcpus, t.cpus), `${t.vcpus} vCPU of ${t.cpus} cores · ${pct(t.vcpus, t.cpus)}%`),
    meter("Memory", pct(t.vmem, t.mem), `${gbText(t.vmem)} of ${gbText(t.mem)} · ${pct(t.vmem, t.mem)}%`),
    meter("Storage", storagePct, `${Math.round(t.used)} of ${Math.round(t.cap)} GB · ${storagePct}%`),
  ].join("");

  $("#home-nodes").innerHTML = members.map((m) => {
    const h = state.hosts[m.name];
    const vms = state.vms.filter((v) => v.node === m.name && v.state !== "stopped");
    const cpu = h ? pct(vms.reduce((s, v) => s + v.vcpus, 0), h.cpus) : 0;
    const mem = h ? pct(vms.reduce((s, v) => s + v.memory_mb, 0), h.memory_mb) : 0;
    const usage = m.role === "compute" && h
      ? `<div class="mini">CPU ${cpu}%${bar(cpu)}</div><div class="mini">Memory ${mem}%${bar(mem)}</div>`
      : `<div class="mini">${m.role === "witness" ? "Quorum vote only" : "No data"}</div><div></div>`;
    return `<div class="node-row">${avatar("i-nodes")}
      <div><span class="vm-name">${esc(m.name)}</span> <span class="dot ${m.healthy ? "ok" : "bad"}"></span>
      <span class="vm-sub">${esc(m.role)}${m.local ? " · manager" : ""} · ${esc(m.address || "local")}</span></div>${usage}</div>`;
  }).join("");
  $("#home-feed").innerHTML = feed(6) || `<li class="hint">No jobs yet. Backups, migrations and imports show up here.</li>`;
}

// ---------- compute ----------

// What the guest says about itself wins over the label the admin typed in.
const osName = (vm, g) => (g?.agent && g.os_name) || vm.os_type || "";
const osKind = (name) => (/windows/i.test(name) ? "Windows" : name ? "Linux" : "");
const ipList = (g) => g?.ips || [];

function vmMenu(vm, { summary = false } = {}) {
  if (!canOperate()) return summary ? `<a class="btn small" href="#vm/${vm.uuid}">Summary</a>` : "";
  const multi = computeMembers().length > 1;
  return `
    ${summary ? `<a class="btn small" href="#vm/${vm.uuid}">Summary</a>` : ""}
    ${vm.state === "running"
      ? `<button class="small" data-vm="console" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Console</button>`
      : `<button class="small" data-vm="start" data-id="${vm.uuid}">Power on</button>`}
    <details class="more"><summary class="small" title="More actions">⋯</summary><div class="dropdown open">
      ${vm.state === "running" ? `<button data-vm="stop" data-id="${vm.uuid}">Shut down</button>
        <button data-vm="poweroff" data-id="${vm.uuid}">Power off</button>` : ""}
      ${multi ? `<button data-vm="migrate" data-id="${vm.uuid}" data-name="${esc(vm.name)}" data-node="${esc(vm.node)}">Migrate to another node</button>` : ""}
      <button data-vm="backup" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Back up now</button>
      ${vm.state !== "running" ? `<button class="danger" data-vm="delete" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Delete</button>` : ""}
    </div></details>`;
}

function renderVMs() {
  const q = $("#vm-search").value.trim().toLowerCase();
  const f = $("#vm-filter").value;
  const haystack = (v) => [v.name, osName(v, state.guests[v.uuid]), state.guests[v.uuid]?.hostname, ...ipList(state.guests[v.uuid])].join(" ").toLowerCase();
  const vms = state.vms.filter((v) => (!q || haystack(v).includes(q)) && (!f || v.state === f));
  const running = state.vms.filter((v) => v.state === "running").length;
  $("#vm-count").textContent = `${state.vms.length} total · ${running} running`;
  $("#vms").innerHTML = vms.length ? vms.map((vm) => {
    const g = state.guests[vm.uuid], os = osName(vm, g), ips = ipList(g);
    return `<tr>
      <td><div class="name-cell">${avatar("i-compute")}<div><a class="vm-name vm-link" href="#vm/${vm.uuid}">${esc(vm.name)}</a><span class="vm-sub clip" title="${esc(vm.description)}">${esc(vm.description || vm.uuid.slice(0, 8))}</span></div></div></td>
      <td><span class="badge ${esc(vm.state)}">${esc(vm.state)}</span></td>
      <td>${os ? `<span class="os ${osKind(os).toLowerCase()}">${esc(os)}</span>` : `<span class="muted">Not set</span>`}
        <span class="vm-sub">${g?.agent ? "Guest agent running" : vm.state === "running" ? "No guest agent" : ""}</span></td>
      <td>${ips.length ? `<span class="mono">${esc(ips[0])}</span>${ips.length > 1 ? ` <span class="muted" title="${esc(ips.join(", "))}">+${ips.length - 1}</span>` : ""}` : `<span class="muted">–</span>`}
        <span class="vm-sub">${esc(g?.hostname || "")}</span></td>
      <td>${esc(vm.node || "")}</td>
      <td>${vm.vcpus}</td>
      <td>${gbText(vm.memory_mb)}</td>
      <td>${vm.disks.map((d) => `${d.size_gb} GB <span class="vm-sub" style="display:inline">${esc(d.pool)}</span>`).join("<br>")}</td>
      <td><div class="actions">${vmMenu(vm, { summary: true })}</div></td>
    </tr>`;
  }).join("") : empty(9, state.vms.length ? "No VMs match" : "No virtual machines yet. Create one, or bring VMs over with Migrate.");
}
$("#vm-search").addEventListener("input", renderVMs);
$("#vm-filter").addEventListener("change", renderVMs);

function renderMoves() {
  $("#moves-panel").hidden = !state.moves.length;
  $("#moves").innerHTML = state.moves.slice(0, 20).map((m) => `<tr>
    <td class="vm-name">${esc(m.vm)}</td><td>${esc(m.source)}</td><td>${esc(m.target || "–")}</td>
    <td>${m.live ? "Live" : "Offline"}${m.copy_storage ? " + disks" : ""}</td>
    <td>${esc(when(m.created))}</td>
    <td><span class="badge ${esc(m.state)}">${JOB_STATE[m.state] || esc(m.state)}</span></td>
    <td class="vm-sub" style="display:table-cell">${esc(m.error || "")}</td></tr>`).join("");
}

async function vmAction(e) {
  const btn = e.target.closest("button[data-vm]");
  if (!btn) return;
  btn.closest("details")?.removeAttribute("open");
  const { vm: act, id, name } = btn.dataset;
  if (act === "console") {
    window.open(`console.html?${new URLSearchParams({ vm: id, name })}`, `console-${id}`, "width=1100,height=760");
    return;
  }
  if (act === "backup") {
    if (!state.targets.length) { toast("Add a backup target first (Backup → Targets)", true); return; }
    openDialog("backup-dialog", { id, name });
    return;
  }
  if (act === "migrate") {
    openDialog("migrate-dialog", { id, name });
    const sel = $("#migrate-form select[name=node]");
    [...sel.options].forEach((o) => { if (o.value === btn.dataset.node) o.remove(); });
    return;
  }
  try {
    if (act === "start") await post(`/vms/${id}/start`);
    if (act === "stop") await post(`/vms/${id}/stop`);
    if (act === "poweroff") await post(`/vms/${id}/stop`, { force: true });
    if (act === "delete") {
      if (!confirm(`Delete ${name} and all its disks?`)) return;
      await del(`/vms/${id}`);
      toast(`${name} deleted`);
      if (currentPage() === "vm") { location.hash = "compute"; return; }
    }
    await refresh(currentPage());
  } catch (err) { fail(err); }
}
$("#vms").addEventListener("click", vmAction);
$("#vm-actions").addEventListener("click", vmAction);

handlers["vm-dialog"] = async (f) => {
  const body = {
    name: f.get("name"),
    vcpus: Number(f.get("vcpus")),
    memory_mb: Number(f.get("memory_mb")),
    firmware: f.get("firmware"),
    os_type: f.get("os_type").trim(),
    disks: [{ size_gb: Number(f.get("disk_gb")), pool: f.get("pool") || "default" }],
    nics: [{ network: f.get("network") || "default" }],
    node: f.get("node") || null,
  };
  if (f.get("iso")) body.iso = f.get("iso");
  const vm = await post("/vms", body);
  toast(`${vm.name} created on ${vm.node}`);
  if (currentPage() === "compute") await refresh("compute"); else location.hash = "compute";
};

handlers["backup-dialog"] = async (f, ctx) => {
  const body = { target: f.get("target") };
  if (f.get("keep_last")) body.keep_last = Number(f.get("keep_last"));
  await post(`/vms/${ctx.id}/backup`, body);
  toast(`Backing up ${ctx.name} to ${body.target}. Progress is under Backup → Jobs.`);
};

handlers["migrate-dialog"] = async (f, ctx) => {
  const body = { node: f.get("node") || null };
  if (f.get("copy_storage") === "false") body.copy_storage = false;
  if (f.get("bandwidth_mib")) body.bandwidth_mib = Number(f.get("bandwidth_mib"));
  const job = await post(`/vms/${ctx.id}/migrate`, body);
  toast(`Moving ${ctx.name} from ${job.source} to ${job.target}`);
  await refresh("compute");
};

// ---------- storage ----------

function renderPools() {
  const admin = isAdmin();
  $("#pools").innerHTML = state.pools.length ? state.pools.map((p) => {
    const used = pct(p.allocation_gb, p.capacity_gb);
    return `<tr>
      <td><div class="name-cell">${avatar("i-storage")}<span class="vm-name">${esc(p.name)}</span></div></td>
      <td>${esc(p.node)}</td>
      <td><span class="badge info plain">${esc(POOL_TYPES[p.type] || p.type)}</span></td>
      <td class="vm-sub" style="display:table-cell">${esc(p.source)}</td>
      <td><div class="bar-row"><span>${p.allocation_gb} / ${p.capacity_gb} GB</span><span>${used}%</span>${bar(used)}</div></td>
      <td><div class="actions">${admin ? `<button class="small danger" data-pool="${esc(p.name)}" data-node="${esc(p.node)}">Remove</button>` : ""}</div></td>
    </tr>`;
  }).join("") : empty(6, "No storage pools");
}

$("#pools").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-pool]");
  if (!btn || !confirm(`Remove pool ${btn.dataset.pool} from ${btn.dataset.node}? Data on the storage stays.`)) return;
  try {
    await del(`/nodes/${enc(btn.dataset.node)}/pools/${enc(btn.dataset.pool)}`);
    await refresh("storage");
  } catch (err) { fail(err); }
});

// The disk picker lists the node's unused whole disks, so nobody has to find /dev/sdX.
async function fillDisks(form) {
  const sel = $(".disk-list", form);
  const node = form.elements.node.value;
  if (!node || form.elements.type.value !== "lvm") return;
  sel.innerHTML = `<option value="">Looking for disks on ${esc(node)}...</option>`;
  let disks = [];
  try { disks = await api(`/nodes/${enc(node)}/disks`); } catch (err) { /* offered by hand below */ }
  const label = (d) => [d.path, `${Math.round(d.size_gb).toLocaleString()} GB`, d.model, d.transport.toUpperCase(),
    d.empty ? "empty" : `has data: ${d.contents}`].filter(Boolean).join(" · ");
  sel.innerHTML = (disks.length ? disks.map((d) => `<option value="${esc(d.path)}">${esc(label(d))}</option>`).join("")
    : `<option value="">No unused disk found on ${esc(node)}</option>`) + `<option value="other">Other device path...</option>`;
  syncDiskPath(form);
}
function syncDiskPath(form) {
  $(".disk-path", form).hidden = form.elements.device.value !== "other";
}
{
  const form = $("#pool-form");
  form.elements.node.addEventListener("change", () => fillDisks(form));
  form.elements.type.addEventListener("change", () => fillDisks(form));
  form.elements.device.addEventListener("change", () => syncDiskPath(form));
}

handlers["pool-dialog"] = async (f) => {
  const type = f.get("type");
  const body = { name: f.get("name"), type, confirm_wipe: f.get("confirm_wipe") === "on" };
  if (type === "dir" && f.get("path")) body.path = f.get("path");
  if (type === "nfs" || type === "iscsi") body.host = f.get("host");
  if (type === "nfs") body.export = f.get("export");
  if (type === "iscsi") { body.iqn = f.get("iqn"); body.lun = Number(f.get("lun")); }
  if (type === "lvm") body.device = f.get("device") === "other" ? f.get("device_path") : f.get("device");
  if (type === "lvm" && !body.device) throw new Error("Pick a disk.");
  await post(`/nodes/${enc(f.get("node"))}/pools`, body);
  toast(`Pool ${body.name} added`);
  await refresh("storage");
};

// ---------- nodes ----------

function renderNodes() {
  const c = state.cluster, admin = isAdmin();
  $("#nodes-sub").textContent = `${TOPOLOGY[c.topology] || c.topology} · ${c.ha_capable ? "HA capable" : "no HA"} · ${c.quorum ? "quorum OK" : "no quorum"}`;
  $("#nodes").innerHTML = c.members.map((m) => {
    const h = state.hosts[m.name];
    const vms = state.vms.filter((v) => v.node === m.name).length;
    const buttons = [
      admin && m.role === "compute" && vms && computeMembers().length > 1 ? `<button class="small" data-evacuate="${esc(m.name)}">Evacuate</button>` : "",
      admin && !m.local ? `<button class="small danger" data-node="${esc(m.name)}">Remove</button>` : "",
    ].join("");
    return `<tr>
      <td><div class="name-cell">${avatar("i-nodes")}<div><span class="vm-name">${esc(m.name)}</span>${m.local ? '<span class="vm-sub">runs the manager</span>' : ""}</div></div></td>
      <td>${esc(m.role)}</td>
      <td class="vm-sub" style="display:table-cell">${esc(m.address || "local")}</td>
      <td><span class="badge ${m.healthy ? "online" : "offline"}">${m.healthy ? "online" : "offline"}</span></td>
      <td>${h ? `${h.cpus} cores` : "–"}</td>
      <td>${h ? gbText(h.memory_mb) : "–"}</td>
      <td>${m.role === "compute" ? vms : "–"}</td>
      <td><div class="actions">${buttons}</div></td>
    </tr>`;
  }).join("");
}

$("#nodes").addEventListener("click", async (e) => {
  const ev = e.target.closest("button[data-evacuate]");
  const rm = e.target.closest("button[data-node]");
  try {
    if (ev) {
      if (!confirm(`Move every VM off ${ev.dataset.evacuate}?`)) return;
      const jobs = await post(`/nodes/${enc(ev.dataset.evacuate)}/evacuate`);
      const failed = jobs.filter((j) => j.state === "failed");
      toast(`Moving ${jobs.length - failed.length} VM(s)${failed.length ? `; ${failed.length} cannot move: ${failed[0].error}` : ""}`, failed.length > 0);
    } else if (rm) {
      if (!confirm(`Remove ${rm.dataset.node} from the cluster?`)) return;
      await del(`/cluster/nodes/${enc(rm.dataset.node)}`);
    } else return;
    await refresh("nodes");
  } catch (err) { fail(err); }
});

handlers["node-dialog"] = async (f) => {
  await post("/cluster/nodes", { name: f.get("name"), role: f.get("role"), address: f.get("address") });
  toast(`${f.get("name")} joined the cluster`);
  await refresh("nodes");
};

// ---------- one VM ----------

async function renderVMDetail() {
  const uuid = vmFromHash();
  const vm = state.vms.find((v) => v.uuid === uuid);
  if (!vm) {
    $("#vm-title").textContent = "VM not found";
    $("#vm-crumb").textContent = uuid;
    $("#vm-subtitle").textContent = "It may have been deleted, or moved and renamed.";
    ["#vm-actions", "#vm-stats", "#vm-general", "#vm-guest", "#vm-net", "#vm-hw", "#vm-backups", "#vm-feed"].forEach((id) => { $(id).innerHTML = ""; });
    return;
  }
  let g = null;
  if (vm.state === "running") g = await api(`/vms/${uuid}/guest`).catch(() => null);
  const os = osName(vm, g), ips = ipList(g);
  const host = state.hosts[vm.node];
  const diskTotal = vm.disks.reduce((sum, d) => sum + d.size_gb, 0);

  $("#vm-crumb").textContent = vm.name;
  $("#vm-title").innerHTML = `${esc(vm.name)} <span class="badge ${esc(vm.state)}">${esc(vm.state)}</span>`;
  $("#vm-subtitle").textContent = [os || "Operating system not set", vm.node && `on ${vm.node}`, ips[0]].filter(Boolean).join(" · ");
  $("#vm-actions").innerHTML = vmMenu(vm);

  const stat = (label, value, note = "") => `<div class="stat"><span>${esc(label)}</span><b>${value}</b><em>${note}</em></div>`;
  $("#vm-stats").innerHTML = [
    stat("vCPU", vm.vcpus, host ? `of ${host.cpus} cores on ${esc(vm.node)}` : ""),
    stat("Memory", gbText(vm.memory_mb), host ? `of ${gbText(host.memory_mb)}` : ""),
    stat("Disk", `${diskTotal} GB`, `${vm.disks.length} disk${vm.disks.length === 1 ? "" : "s"}`),
    stat("Network", `${vm.nics.length} adapter${vm.nics.length === 1 ? "" : "s"}`, esc(ips.join(", ") || "No address reported")),
  ].join("");

  const kv = (rows) => rows.filter((r) => r).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
  const dash = `<span class="muted">–</span>`;
  $("#vm-general").innerHTML = kv([
    ["Name", esc(vm.name)],
    ["Status", `<span class="badge ${esc(vm.state)}">${esc(vm.state)}</span>`],
    ["Node", esc(vm.node || "–")],
    ["Operating system", vm.os_type ? esc(vm.os_type) : `<span class="muted">Not set</span>`],
    ["Firmware", vm.firmware === "uefi" ? "UEFI" : "BIOS"],
    ["CD/DVD", vm.iso ? `<span class="mono">${esc(vm.iso)}</span>` : "Empty"],
    ["Description", vm.description ? esc(vm.description) : dash],
    ["UUID", `<span class="mono">${esc(vm.uuid)}</span>`],
  ]);

  const agentText = g?.agent
    ? `<span class="dot ok"></span> Running`
    : vm.state === "running"
      ? `<span class="dot warn"></span> Not detected <span class="vm-sub">Install qemu-guest-agent in the guest to see its OS, hostname and IP addresses.</span>`
      : `<span class="dot"></span> VM is powered off`;
  $("#vm-guest").innerHTML = kv([
    ["Guest agent", agentText],
    ["Reported OS", g?.os_name ? esc(g.os_name) : dash],
    ["Version", g?.os_version ? esc(g.os_version) : dash],
    ["Kernel", g?.kernel ? `<span class="mono">${esc(g.kernel)}</span>` : dash],
    ["Hostname", g?.hostname ? esc(g.hostname) : dash],
    ["IP addresses", ips.length ? ips.map((ip) => `<span class="mono">${esc(ip)}</span>`).join("<br>") : dash],
  ]);

  const ifaces = g?.interfaces || [];
  const byMac = (mac) => ifaces.find((i) => mac && i.mac.toLowerCase() === mac.toLowerCase());
  $("#vm-net").innerHTML = vm.nics.length ? vm.nics.map((n, i) => {
    const iface = byMac(n.mac) || ifaces[i];
    return `<tr><td><span class="vm-name">Adapter ${i + 1}</span><span class="vm-sub">${esc(iface?.name || n.model)}</span></td>
      <td>${esc(n.network)}</td><td class="mono">${esc(n.mac || iface?.mac || "assigned at power on")}</td>
      <td>${iface?.ips.length ? iface.ips.map((ip) => `<span class="mono">${esc(ip)}</span>`).join("<br>") : dash}</td></tr>`;
  }).join("") : empty(4, "No network adapters");

  const hw = [
    ["i-compute", "Processor", `${vm.vcpus} vCPU`, host ? `Host has ${host.cpus} cores` : ""],
    ["i-server", "Memory", gbText(vm.memory_mb), `${vm.memory_mb} MB`],
    ...vm.disks.map((d, i) => ["i-disk", `Hard disk ${i + 1}`, `${d.size_gb} GB`, `Pool ${esc(d.pool)} · ${esc(d.backend)}`]),
    ...vm.nics.map((n, i) => ["i-migrate", `Network adapter ${i + 1}`, esc(n.network), `${esc(n.model)} · <span class="mono">${esc(n.mac || "auto")}</span>`]),
    ["i-disk", "CD/DVD drive", vm.iso ? "ISO attached" : "Empty", vm.iso ? `<span class="mono">${esc(vm.iso)}</span>` : ""],
    ["i-system", "Firmware", vm.firmware === "uefi" ? "UEFI" : "BIOS", "Guest agent channel enabled"],
  ];
  $("#vm-hw").innerHTML = hw.map(([icon, dev, conf, det]) => `<tr>
    <td><div class="name-cell">${avatar(icon)}<span class="vm-name">${esc(dev)}</span></div></td>
    <td>${conf}</td><td class="muted">${det}</td></tr>`).join("");

  const mine = activities().filter((a) => a.what === vm.name);
  $("#vm-feed").innerHTML = mine.length ? mine.slice(0, 50).map((a) => `<li>
    ${avatar(a.icon)}
    <div><b>${esc(a.kind)}</b> <span class="vm-sub">${esc(a.where)}${a.error ? ` · ${esc(a.error)}` : ""}</span></div>
    <span class="badge ${esc(a.state)}">${JOB_STATE[a.state] || esc(a.state)}</span>
    <span class="when">${esc(ago(a.created))}</span></li>`).join("") : `<li class="hint">No backups, restores or migrations for this VM yet.</li>`;

  await renderVMBackups(vm);
}

async function renderVMBackups(vm) {
  const tbody = $("#vm-backups");
  if (!state.targets.length) { tbody.innerHTML = empty(6, "Add a backup target first (Backup → Targets)"); return; }
  const lists = await Promise.all(state.targets.map((t) =>
    api(`/backup/targets/${enc(t.name)}/backups`).then((bs) => bs.map((b) => ({ ...b, target: t.name }))).catch(() => [])));
  const points = lists.flat().filter((b) => b.vm_name === vm.name).sort((a, b) => b.created.localeCompare(a.created));
  const op = canOperate();
  tbody.innerHTML = points.length ? points.map((b) => `<tr>
    <td>${esc(when(b.created))}</td><td>${esc(b.target)}</td><td>${esc(b.node || "shared")}</td>
    <td>${size(b.size_bytes)}</td><td>${size(b.stored_bytes)}</td>
    <td><div class="actions">${op ? `<button class="small" data-restore="${esc(b.id)}" data-target="${esc(b.target)}" data-node="${esc(b.node || "")}">Restore</button>` : ""}</div></td>
  </tr>`).join("") : empty(6, "No restore points for this VM yet");
}

$("#vm-backups").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-restore]");
  if (!btn) return;
  const d = btn.dataset, vm = state.vms.find((v) => v.uuid === vmFromHash());
  openDialog("restore-dialog", { target: d.target, id: d.restore, sourceNode: d.node }, { name: `${vm?.name || "vm"}-restore` });
});
$("#vm-backup-now").addEventListener("click", () => {
  const vm = state.vms.find((v) => v.uuid === vmFromHash());
  if (!vm) return;
  if (!state.targets.length) { toast("Add a backup target first (Backup → Targets)", true); return; }
  openDialog("backup-dialog", { id: vm.uuid, name: vm.name });
});

// ---------- backup ----------

function renderTargets() {
  const admin = isAdmin();
  $("#targets").innerHTML = state.targets.length ? state.targets.map((t) => `<tr>
    <td><div class="name-cell">${avatar("i-backup")}<span class="vm-name">${esc(t.name)}</span></div></td>
    <td><span class="badge info plain">${esc(TARGET_TYPES[t.type] || t.type)}</span></td>
    <td class="vm-sub" style="display:table-cell">${esc(t.source)}</td>
    <td>${t.on_demand ? '<span class="badge waiting">Attached only during jobs</span>' : '<span class="badge online">Always available</span>'}</td>
    <td><div class="actions">${admin ? `<button class="small danger" data-target="${esc(t.name)}">Remove</button>` : ""}</div></td>
  </tr>`).join("") : empty(5, "No backup targets. Add one to start protecting VMs.");
}

async function renderBackups() {
  const target = $("#browse-target").value || state.targets[0]?.name;
  const tbody = $("#backups");
  if (!target) { tbody.innerHTML = empty(6, "Add a backup target first"); return; }
  const op = canOperate(), admin = isAdmin();
  try {
    const backups = await api(`/backup/targets/${enc(target)}/backups`);
    tbody.innerHTML = backups.length ? backups.slice().reverse().map((b) => `<tr>
      <td class="vm-name">${esc(b.vm_name)}</td>
      <td>${esc(when(b.created))}</td>
      <td>${esc(b.node || "shared")}</td>
      <td>${size(b.size_bytes)}</td>
      <td>${size(b.stored_bytes)}</td>
      <td><div class="actions">
        ${op ? `<button class="small" data-restore="${esc(b.id)}" data-vm="${esc(b.vm_name)}" data-node="${esc(b.node || "")}">Restore</button>` : ""}
        ${admin ? `<button class="small danger" data-backup-del="${esc(b.id)}" data-node="${esc(b.node || "")}">Delete</button>` : ""}
      </div></td>
    </tr>`).join("") : empty(6, "No restore points on this target yet");
  } catch (err) {
    tbody.innerHTML = empty(6, err.message);
  }
}

function renderPolicies() {
  const admin = isAdmin(), op = canOperate();
  $("#policies").innerHTML = state.policies.length ? state.policies.map((p) => `<tr>
    <td class="vm-name">${esc(p.name)}</td>
    <td>${esc(p.target)}</td>
    <td>${p.at ? `Daily at ${esc(p.at)}` : `Every ${p.every_hours} h`}</td>
    <td>${p.vms.length ? esc(p.vms.join(", ")) : "All VMs"}</td>
    <td>${p.keep_last}</td>
    <td>${esc(when(p.last_run))}</td>
    <td><div class="actions">
      ${op ? `<button class="small" data-policy-run="${esc(p.name)}">Run now</button>` : ""}
      ${admin ? `<button class="small danger" data-policy-del="${esc(p.name)}">Delete</button>` : ""}
    </div></td>
  </tr>`).join("") : empty(7, "No schedules yet");
}

function renderJobs() {
  const jobs = state.jobs.slice(0, 50);
  $("#jobs").innerHTML = jobs.length ? jobs.map((j) => `<tr>
    <td>${j.kind === "backup" ? "Backup" : "Restore"}${j.policy ? ` <span class="vm-sub" style="display:inline">(${esc(j.policy)})</span>` : ""}</td>
    <td class="vm-name">${esc(j.vm)}</td>
    <td>${esc(j.target)}</td>
    <td>${esc(j.node || "")}</td>
    <td>${esc(when(j.created))}</td>
    <td><span class="badge ${esc(j.state)}">${JOB_STATE[j.state] || esc(j.state)}</span></td>
    <td class="vm-sub" style="display:table-cell">${esc(j.error || j.result || "")}</td>
  </tr>`).join("") : empty(7, "No jobs yet");
}

$("#browse-target").addEventListener("change", () => renderBackups());
$("#refresh-backups").addEventListener("click", () => renderBackups());

$("#page-backup").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-target], button[data-policy-run], button[data-policy-del], button[data-restore], button[data-backup-del]");
  if (!btn) return;
  const d = btn.dataset;
  const target = $("#browse-target").value;
  try {
    if (d.target) {
      if (!confirm(`Remove target ${d.target}? Backup files on it stay.`)) return;
      await del(`/backup/targets/${enc(d.target)}`);
    } else if (d.policyRun) {
      const jobs = await post(`/backup/policies/${enc(d.policyRun)}/run`);
      toast(`Started ${jobs.length} backup job${jobs.length === 1 ? "" : "s"}`);
    } else if (d.policyDel) {
      if (!confirm(`Delete schedule ${d.policyDel}?`)) return;
      await del(`/backup/policies/${enc(d.policyDel)}`);
    } else if (d.restore) {
      openDialog("restore-dialog", { target, id: d.restore, sourceNode: d.node }, { name: `${d.vm}-restore` });
      return;
    } else if (d.backupDel) {
      if (!confirm(`Delete restore point ${d.backupDel}?`)) return;
      const q = d.node ? `?node=${enc(d.node)}` : "";
      await del(`/backup/targets/${enc(target)}/backups/${enc(d.backupDel)}${q}`);
    }
    await refresh("backup");
  } catch (err) { fail(err); }
});

handlers["target-dialog"] = async (f) => {
  const FIELDS = {
    local: ["path"], nfs: ["host", "export"], smb: ["host", "share", "username", "password"],
    usb: ["device"], iscsi: ["host", "iqn"], fc: ["device"], s3: ["endpoint", "bucket", "access_key", "secret_key"],
  };
  const body = { name: f.get("name"), type: f.get("type") };
  for (const key of FIELDS[body.type]) if (f.get(key)) body[key] = f.get(key);
  if (body.type === "iscsi") body.lun = Number(f.get("lun"));
  if (body.type === "iscsi" || body.type === "fc") {
    body.confirm_wipe = f.get("confirm_wipe") === "on";
    if (body.confirm_wipe && !confirm("If the LUN has no filesystem it will be formatted. Continue?")) return;
  }
  await post("/backup/targets", body);
  toast(`Target ${body.name} added`);
  await refresh("backup");
};

handlers["policy-dialog"] = async (f) => {
  const body = {
    name: f.get("name"),
    target: f.get("target"),
    vms: f.getAll("vms"),
    keep_last: Number(f.get("keep_last")),
  };
  if (f.get("mode") === "at") body.at = f.get("at"); else body.every_hours = Number(f.get("every_hours"));
  await post("/backup/policies", body);
  toast(`Schedule ${body.name} created`);
  await refresh("backup");
};

handlers["restore-dialog"] = async (f, ctx) => {
  const body = { name: f.get("name") || null, node: f.get("node") || null, pool: f.get("pool") || null, source_node: ctx.sourceNode || null };
  await post(`/backup/targets/${enc(ctx.target)}/backups/${enc(ctx.id)}/restore`, body);
  toast(`Restoring ${body.name}. Progress is under Backup → Jobs.`);
  await refresh(currentPage());
};

// ---------- migrate ----------

function renderSources() {
  const admin = isAdmin();
  $("#sources").innerHTML = state.sources.length ? state.sources.map((s) => {
    const st = s.settings;
    const where = s.type === "vmware" ? `${st.host}${st.path ? ` / ${st.path}` : ""}`
      : s.type === "upload" ? "Files uploaded with Import VMs → Upload files" : st.path;
    return `<tr>
      <td><div class="name-cell">${avatar("i-migrate")}<span class="vm-name">${esc(s.name)}</span></div></td>
      <td><span class="badge info plain">${esc(SOURCE_TYPES[s.type] || s.type)}</span></td>
      <td class="vm-sub" style="display:table-cell">${esc(where)}</td>
      <td>${esc(st.username || "–")}</td>
      <td><div class="actions">${admin && s.type !== "upload" ? `<button class="small danger" data-source-del="${esc(s.name)}">Remove</button>` : ""}</div></td>
    </tr>`;
  }).join("") : empty(5, "No sources yet. Add an ESXi host, a vCenter or a folder of exports.");
}

let sourceVMs = [];
let sourceVMsFor = null;  // the source the list above was read from
async function renderSourceVMs() {
  const name = $("#browse-source").value || state.sources[0]?.name;
  const tbody = $("#source-vms");
  if (!name) { tbody.innerHTML = empty(8, "Add a source first (Sources tab)"); return; }
  // Reading ESXi or vCenter takes seconds, so the 10 s auto-refresh leaves this list
  // alone; it reloads only on Refresh or when another source is picked.
  tbody.innerHTML = empty(8, "Reading the source…");
  try {
    sourceVMs = await api(`/migrate/sources/${enc(name)}/vms`);
    sourceVMsFor = name;
  } catch (err) {
    sourceVMsFor = null;
    tbody.innerHTML = empty(8, err.message);
    return;
  }
  const op = canOperate();
  tbody.innerHTML = sourceVMs.length ? sourceVMs.map((vm, i) => `<tr>
    <td><div class="name-cell">${avatar("i-compute")}<div><span class="vm-name">${esc(vm.name)}</span><span class="vm-sub clip" title="${esc(vm.ref)}">${esc(vm.ref.split("/").pop())}</span></div></div></td>
    <td>${vm.state ? `<span class="badge ${vm.state === "running" ? "running" : ""}">${esc(vm.state)}</span>` : "–"}</td>
    <td>${vm.vcpus}</td><td>${gbText(vm.memory_mb)}</td>
    <td>${vm.disks_gb.map((g) => (g ? `${g} GB` : "?")).join(", ")}</td>
    <td>${esc(vm.firmware.toUpperCase())}</td>
    <td>${esc(vm.guest_os || "–")}${vm.notes ? `<span class="vm-sub clip" title="${esc(vm.notes)}">${esc(vm.notes)}</span>` : ""}</td>
    <td><div class="actions">${!op ? "" : vm.state === "running"
      ? `<button class="small" disabled title="Power the VM off at the source first">Import</button>`
      : `<button class="small primary" data-import="${i}">Import</button>`}${op && sourceVMsFor === "uploads"
      ? `<button class="small danger" data-upload-del="${esc(vm.ref.split("/").pop())}">Delete file</button>` : ""}</div></td>
  </tr>`).join("") : empty(8, "No VMs found on this source");
}
$("#browse-source").addEventListener("change", () => renderSourceVMs());

// Uploads stream straight to the node's disk; big exports take a while, so each file
// shows its own progress bar.
function uploadFile(file, row) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/v1/migrate/uploads/${enc(file.name)}`);
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = Math.floor((e.loaded / e.total) * 100);
      $(".bar i", row).style.width = `${pct}%`;
      $(".pct", row).textContent = `${pct}% of ${gbText(file.size / 1048576)}`;
    };
    xhr.onload = () => {
      if (xhr.status < 300) return resolve();
      let detail = xhr.statusText;
      try { detail = JSON.parse(xhr.responseText).detail || detail; } catch { /* keep statusText */ }
      reject(new Error(typeof detail === "string" ? detail : JSON.stringify(detail)));
    };
    xhr.onerror = () => reject(new Error("the upload was interrupted"));
    xhr.send(file);
  });
}
$("#upload-files").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  const box = $("#uploads-progress");
  for (const file of files) {
    const row = document.createElement("div");
    row.className = "upload-row";
    row.innerHTML = `<span class="vm-name">${esc(file.name)}</span><span class="bar"><i></i></span><span class="pct">0%</span>`;
    box.append(row);
    try {
      await uploadFile(file, row);
      $(".pct", row).textContent = "Uploaded";
      setTimeout(() => row.remove(), 5000);
    } catch (err) {
      row.classList.add("failed");
      $(".pct", row).textContent = err.message;
    }
  }
  await load("sources");
  fillSelects();
  $("#browse-source").value = "uploads";
  renderSourceVMs();
});
$("#refresh-source").addEventListener("click", () => renderSourceVMs());

function renderImports() {
  $("#imports").innerHTML = state.imports.length ? state.imports.map((j) => `<tr>
    <td class="vm-sub" style="display:table-cell" title="${esc(j.vm)}">${esc(j.vm.split("/").pop())}</td><td>${esc(j.source)}</td>
    <td class="vm-name">${esc(j.name)}</td><td>${esc(j.node)}</td><td>${esc(when(j.created))}</td>
    <td><span class="badge ${esc(j.state)}">${JOB_STATE[j.state] || esc(j.state)}</span></td>
    <td class="vm-sub" style="display:table-cell">${esc(j.error || (j.state === "running" ? `${j.step || "Working"}${j.percent != null ? ` · ${j.percent}%` : ""}` : ""))}
      <button class="small" data-task-log="${esc(j.id)}" data-task-title="${esc(j.name)}">Log</button></td></tr>`).join("") : empty(7, "No imports yet");
}

$("#page-migrate").addEventListener("click", async (e) => {
  const imp = e.target.closest("button[data-import]");
  const rm = e.target.closest("button[data-source-del]");
  const upDel = e.target.closest("button[data-upload-del]");
  if (upDel) {
    if (!confirm(`Delete the uploaded file ${upDel.dataset.uploadDel}?`)) return;
    try { await del(`/migrate/uploads/${enc(upDel.dataset.uploadDel)}`); renderSourceVMs(); } catch (err) { fail(err); }
  }
  if (imp) {
    const vm = sourceVMs[Number(imp.dataset.import)];
    if (vm.state === "running" && !confirm(`${vm.name} is running at the source. Its disks may change during the copy. Continue anyway?`)) return;
    openDialog("import-dialog", { source: $("#browse-source").value, vm }, { name: vm.name, vcpus: vm.vcpus, memory_mb: vm.memory_mb });
  }
  if (rm) {
    if (!confirm(`Remove source ${rm.dataset.sourceDel}?`)) return;
    try { await del(`/migrate/sources/${enc(rm.dataset.sourceDel)}`); await refresh("migrate"); } catch (err) { fail(err); }
  }
});

handlers["source-dialog"] = async (f) => {
  const type = f.get("type");
  const body = { name: f.get("name"), type };
  if (type === "vmware") {
    Object.assign(body, { host: f.get("host"), username: f.get("username"), password: f.get("password"), verify_tls: f.get("verify_tls") === "on", transport: f.get("transport") });
    if (f.get("vpath")) body.path = f.get("vpath");
  } else body.path = f.get("path");
  await post("/migrate/sources", body);
  toast(`Source ${body.name} added`);
  await refresh("migrate");
  $("#browse-source").value = body.name;
  selectTab("migrate-tabs", "import");
  await renderSourceVMs();
};

handlers["import-dialog"] = async (f, ctx) => {
  const body = {
    source: ctx.source, vm: ctx.vm.ref, name: f.get("name"), node: f.get("node") || null,
    pool: f.get("pool") || "default", network: f.get("network") || "default", start: f.get("start") === "on",
  };
  if (f.get("vcpus")) body.vcpus = Number(f.get("vcpus"));
  if (f.get("memory_mb")) body.memory_mb = Number(f.get("memory_mb"));
  const job = await post("/migrate/jobs", body);
  toast(`Importing ${job.name} to ${job.node}. Follow it under Jobs.`);
  await refresh("migrate");
  selectTab("migrate-tabs", "imports");
};

// ---------- settings ----------

async function renderSystem() {
  const health = await api("/health");
  const c = state.cluster;
  const rows = [
    ["Release", health.release || "ibHiCS Hyper-Converged Infrastructure"],
    ["Version", health.version],
    ["Deployment", `${TOPOLOGY[c.topology] || c.topology} (${c.members.length} member${c.members.length === 1 ? "" : "s"})`],
    ["High availability", c.ha_capable ? "Yes" : "No: add a second compute node and a witness"],
    ["Quorum", c.quorum ? "OK" : "Lost: changes are blocked"],
    ["Sign-in", health.auth ? `On · you are ${state.me?.name} (${state.me?.role})` : "Off"],
    ["License", 'GNU AGPLv3 · <a href="https://github.com/iball281029/ibhics" target="_blank">Source code</a>'],
  ];
  $("#about").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${k === "License" ? v : esc(v)}</dd>`).join("");
  renderUsers();
  if (isAdmin() && state.me) await renderAudit();
}

function renderUsers() {
  const me = state.me?.name;
  $("#users").innerHTML = state.users.length ? state.users.map((u) => `<tr>
    <td><div class="name-cell">${avatar("i-user")}<div><span class="vm-name">${esc(u.name)}</span>${u.name === me ? '<span class="vm-sub">you</span>' : ""}</div></div></td>
    <td><select data-role="${esc(u.name)}" style="width:auto" ${u.name === me ? "disabled" : ""}>
      ${["viewer", "operator", "admin"].map((r) => `<option value="${r}" ${u.role === r ? "selected" : ""}>${r[0].toUpperCase() + r.slice(1)}</option>`).join("")}
    </select></td>
    <td><span class="badge ${u.enabled ? "online" : "disabled"}">${u.enabled ? (u.must_change_password ? "Must set password" : "Active") : "Disabled"}</span></td>
    <td>${esc(u.last_login ? when(u.last_login) : "never")}</td>
    <td>${esc(when(u.created))}</td>
    <td><div class="actions">${u.name === me ? "" : `
      <button class="small" data-user-reset="${esc(u.name)}">Reset password</button>
      <button class="small" data-user-toggle="${esc(u.name)}" data-enabled="${u.enabled}">${u.enabled ? "Disable" : "Enable"}</button>
      <button class="small danger" data-user-del="${esc(u.name)}">Delete</button>`}</div></td>
  </tr>`).join("") : empty(6, state.me ? "No users" : "Sign-in is off on this manager");
}

async function renderAudit() {
  const entries = await api("/audit?limit=200");
  $("#audit").innerHTML = entries.length ? entries.map((e) => `<tr>
    <td>${esc(when(e.time))}</td><td class="vm-name">${esc(e.user)}</td><td>${esc(e.action)}</td>
    <td class="vm-sub" style="display:table-cell">${esc(e.target)}</td>
    <td><span class="badge ${e.result < 400 ? "done" : "failed"}">${e.result}</span></td><td>${esc(e.address)}</td></tr>`).join("") : empty(6, "Nothing recorded yet");
}
$("#refresh-audit").addEventListener("click", () => renderAudit().catch(fail));

$("#users").addEventListener("change", async (e) => {
  const sel = e.target.closest("select[data-role]");
  if (!sel) return;
  try { await patch(`/users/${enc(sel.dataset.role)}`, { role: sel.value }); toast(`${sel.dataset.role} is now ${sel.value}`); } catch (err) { fail(err); }
  await refresh("system");
});
$("#users").addEventListener("click", async (e) => {
  const d = e.target.closest("button")?.dataset || {};
  try {
    if (d.userReset) {
      const pw = prompt(`New temporary password for ${d.userReset} (8+ characters). They must change it at sign-in.`);
      if (!pw) return;
      await patch(`/users/${enc(d.userReset)}`, { password: pw });
      toast(`Password of ${d.userReset} reset`);
    } else if (d.userToggle) {
      await patch(`/users/${enc(d.userToggle)}`, { enabled: d.enabled !== "true" });
    } else if (d.userDel) {
      if (!confirm(`Delete user ${d.userDel}?`)) return;
      await del(`/users/${enc(d.userDel)}`);
    } else return;
    await refresh("system");
  } catch (err) { fail(err); }
});

handlers["user-dialog"] = async (f) => {
  await post("/users", { name: f.get("name"), role: f.get("role"), password: f.get("password") });
  toast(`User ${f.get("name")} added`);
  await refresh("system");
};

$("#password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  if (f.get("new") !== f.get("confirm")) { toast("The two new passwords do not match", true); return; }
  try {
    await post("/auth/password", { current_password: f.get("current"), new_password: f.get("new") });
    e.target.reset();
    toast("Password changed");
  } catch (err) { fail(err); }
});

// ---------- start ----------

loadMe().then(show).catch(fail);
setInterval(() => {
  if (!document.querySelector("dialog[open]") && !document.hidden) refresh(currentPage(), true).catch(() => {});
}, 10000);
