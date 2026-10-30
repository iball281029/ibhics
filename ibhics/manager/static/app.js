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

const state = { me: null, cluster: null, hosts: {}, vms: [], pools: [], targets: [], sources: [], policies: [], jobs: [], moves: [], imports: [], users: [] };
const TOPOLOGY = { "standalone": "Standalone", "two-node-witness": "2 nodes + witness", "cluster": "Cluster", "incomplete": "Incomplete" };
const POOL_TYPES = { dir: "Local", nfs: "NFS", iscsi: "iSCSI", lvm: "LVM (FC / disk)" };
const TARGET_TYPES = { local: "Local disk", nfs: "NFS", smb: "SMB", usb: "USB", iscsi: "iSCSI", fc: "FC", s3: "S3" };
const SOURCE_TYPES = { vmware: "VMware", ova: "OVA folder", disk: "Disk images" };
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
const currentPage = () => (PAGES.includes(location.hash.slice(1)) ? location.hash.slice(1) : "home");

function show() {
  const page = currentPage();
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${page}`));
  $$(".rail a[data-page]").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
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
  $("#pool-names").innerHTML = [...new Set(state.pools.map((p) => p.name))].map((n) => `<option value="${esc(n)}">`).join("");
}

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

async function refresh(page) {
  await loadCluster();
  if (page === "home") { await load("vms", "pools", "targets", "policies", "jobs", "moves", "imports", "users"); renderHome(); }
  if (page === "compute") { await load("vms", "pools", "targets", "moves"); renderVMs(); renderMoves(); }
  if (page === "storage") { await load("pools", "vms"); renderPools(); }
  if (page === "nodes") { await load("vms"); renderNodes(); }
  if (page === "backup") { await load("targets", "policies", "jobs"); fillSelects(); renderTargets(); renderPolicies(); renderJobs(); await renderBackups(); }
  if (page === "migrate") { await load("sources", "imports", "pools"); fillSelects(); renderSources(); renderImports(); await renderSourceVMs(); }
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

function renderVMs() {
  const q = $("#vm-search").value.trim().toLowerCase();
  const f = $("#vm-filter").value;
  const vms = state.vms.filter((v) => (!q || v.name.toLowerCase().includes(q)) && (!f || v.state === f));
  const running = state.vms.filter((v) => v.state === "running").length;
  $("#vm-count").textContent = `${state.vms.length} total · ${running} running`;
  const op = canOperate();
  const multi = computeMembers().length > 1;
  $("#vms").innerHTML = vms.length ? vms.map((vm) => `
    <tr>
      <td><div class="name-cell">${avatar("i-compute")}<div><span class="vm-name">${esc(vm.name)}</span><span class="vm-sub clip" title="${esc(vm.description)}">${esc(vm.description || vm.uuid.slice(0, 8))}</span></div></div></td>
      <td><span class="badge ${esc(vm.state)}">${esc(vm.state)}</span></td>
      <td>${esc(vm.node || "")}</td>
      <td>${vm.vcpus}</td>
      <td>${gbText(vm.memory_mb)}</td>
      <td>${vm.disks.map((d) => `${d.size_gb} GB <span class="vm-sub" style="display:inline">${esc(d.pool)}</span>`).join("<br>")}</td>
      <td>${esc(vm.firmware.toUpperCase())}</td>
      <td><div class="actions">${!op ? "" : `
        ${vm.state === "running"
          ? `<button class="small" data-vm="console" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Console</button>`
          : `<button class="small" data-vm="start" data-id="${vm.uuid}">Power on</button>`}
        <details class="more"><summary class="small" title="More actions">⋯</summary><div class="dropdown open">
          ${vm.state === "running" ? `<button data-vm="stop" data-id="${vm.uuid}">Shut down</button>
            <button data-vm="poweroff" data-id="${vm.uuid}">Power off</button>` : ""}
          ${multi ? `<button data-vm="migrate" data-id="${vm.uuid}" data-name="${esc(vm.name)}" data-node="${esc(vm.node)}">Migrate to another node</button>` : ""}
          <button data-vm="backup" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Back up now</button>
          ${vm.state !== "running" ? `<button class="danger" data-vm="delete" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Delete</button>` : ""}
        </div></details>`}
      </div></td>
    </tr>`).join("") : empty(8, state.vms.length ? "No VMs match" : "No virtual machines yet. Create one, or bring VMs over with Migrate.");
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

$("#vms").addEventListener("click", async (e) => {
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
    }
    await refresh("compute");
  } catch (err) { fail(err); }
});

handlers["vm-dialog"] = async (f) => {
  const body = {
    name: f.get("name"),
    vcpus: Number(f.get("vcpus")),
    memory_mb: Number(f.get("memory_mb")),
    firmware: f.get("firmware"),
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

handlers["pool-dialog"] = async (f) => {
  const type = f.get("type");
  const body = { name: f.get("name"), type, confirm_wipe: f.get("confirm_wipe") === "on" };
  if (type === "dir" && f.get("path")) body.path = f.get("path");
  if (type === "nfs" || type === "iscsi") body.host = f.get("host");
  if (type === "nfs") body.export = f.get("export");
  if (type === "iscsi") { body.iqn = f.get("iqn"); body.lun = Number(f.get("lun")); }
  if (type === "lvm") body.device = f.get("device");
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
    vms: f.get("vms").split(",").map((v) => v.trim()).filter(Boolean),
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
  toast(`Restoring ${body.name}. Progress is under Jobs.`);
  await refresh("backup");
};

// ---------- migrate ----------

function renderSources() {
  const admin = isAdmin();
  $("#sources").innerHTML = state.sources.length ? state.sources.map((s) => {
    const st = s.settings;
    const where = s.type === "vmware" ? `${st.host}${st.path ? ` / ${st.path}` : ""}` : st.path;
    return `<tr>
      <td><div class="name-cell">${avatar("i-migrate")}<span class="vm-name">${esc(s.name)}</span></div></td>
      <td><span class="badge info plain">${esc(SOURCE_TYPES[s.type] || s.type)}</span></td>
      <td class="vm-sub" style="display:table-cell">${esc(where)}</td>
      <td>${esc(st.username || "–")}</td>
      <td><div class="actions">${admin ? `<button class="small danger" data-source-del="${esc(s.name)}">Remove</button>` : ""}</div></td>
    </tr>`;
  }).join("") : empty(5, "No sources yet. Add an ESXi host, a vCenter or a folder of exports.");
}

let sourceVMs = [];
async function renderSourceVMs() {
  const name = $("#browse-source").value || state.sources[0]?.name;
  const tbody = $("#source-vms");
  if (!name) { tbody.innerHTML = empty(8, "Add a source first (Sources tab)"); return; }
  tbody.innerHTML = empty(8, "Reading the source…");
  try {
    sourceVMs = await api(`/migrate/sources/${enc(name)}/vms`);
  } catch (err) {
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
    <td class="vm-sub" style="display:table-cell">${esc(vm.guest_os || "")}</td>
    <td><div class="actions">${op ? `<button class="small primary" data-import="${i}">Import</button>` : ""}</div></td>
  </tr>`).join("") : empty(8, "No VMs found on this source");
}
$("#browse-source").addEventListener("change", () => renderSourceVMs());
$("#refresh-source").addEventListener("click", () => renderSourceVMs());

function renderImports() {
  $("#imports").innerHTML = state.imports.length ? state.imports.map((j) => `<tr>
    <td class="vm-sub" style="display:table-cell" title="${esc(j.vm)}">${esc(j.vm.split("/").pop())}</td><td>${esc(j.source)}</td>
    <td class="vm-name">${esc(j.name)}</td><td>${esc(j.node)}</td><td>${esc(when(j.created))}</td>
    <td><span class="badge ${esc(j.state)}">${JOB_STATE[j.state] || esc(j.state)}</span></td>
    <td class="vm-sub" style="display:table-cell">${esc(j.error || "")}</td></tr>`).join("") : empty(7, "No imports yet");
}

$("#page-migrate").addEventListener("click", async (e) => {
  const imp = e.target.closest("button[data-import]");
  const rm = e.target.closest("button[data-source-del]");
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
    Object.assign(body, { host: f.get("host"), username: f.get("username"), password: f.get("password"), verify_tls: f.get("verify_tls") === "on" });
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
    ["Product", "ibHiCS Hyper-Converged Infrastructure"],
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
  if (!document.querySelector("dialog[open]") && !document.hidden) refresh(currentPage()).catch(() => {});
}, 10000);
