"use strict";

// ---------- helpers ----------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts = {}) {
  const r = await fetch(`/api/v1${path}`, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join(", ") : body.detail;
    throw new Error(detail || r.statusText);
  }
  return r.status === 204 ? null : r.json();
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body ?? {}) });
const del = (path) => api(path, { method: "DELETE" });

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
const pct = (used, total) => (total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0);
const empty = (cols, text) => `<tr><td colspan="${cols}" class="empty">${esc(text)}</td></tr>`;
const bar = (p) => `<div class="bar ${p >= 90 ? "bad" : p >= 75 ? "warn" : ""}"><i style="width:${p}%"></i></div>`;

// ---------- state ----------

const state = { cluster: null, hosts: {}, vms: [], pools: [], targets: [] };
const TOPOLOGY = {
  "standalone": "Standalone",
  "two-node-witness": "2 nodes + witness",
  "cluster": "Cluster",
  "incomplete": "Incomplete (no HA)",
};
const POOL_TYPES = { dir: "Local", nfs: "NFS", iscsi: "iSCSI", lvm: "LVM (FC / disk)" };
const TARGET_TYPES = { local: "Local disk", nfs: "NFS", smb: "SMB", usb: "USB", iscsi: "iSCSI", fc: "FC", s3: "S3" };

const computeMembers = () => (state.cluster?.members || []).filter((m) => m.role === "compute");

// ---------- navigation ----------

const PAGES = ["home", "compute", "storage", "nodes", "backup", "system"];

function show() {
  const page = PAGES.includes(location.hash.slice(1)) ? location.hash.slice(1) : "home";
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${page}`));
  $$("#menu a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  refresh(page).catch(fail);
}
window.addEventListener("hashchange", show);

document.addEventListener("click", (e) => {
  const goto = e.target.closest("[data-goto]");
  if (goto) location.hash = goto.dataset.goto;
  const open = e.target.closest("[data-open]");
  if (open) openDialog(open.dataset.open);
});

$("#backup-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (!btn) return;
  $$("#backup-tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  $$("#page-backup .subpage").forEach((p) => p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
});

// ---------- dialogs ----------

function syncSwitch(form) {
  const sw = $("[data-switch]", form);
  if (!sw) return;
  $$("[data-for]", form).forEach((el) => {
    el.style.display = el.dataset.for.split(" ").includes(sw.value) ? "" : "none";
  });
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
  for (const [k, v] of Object.entries(prefill)) if (form.elements[k]) form.elements[k].value = v;
  syncSwitch(form);
  fillSelects();
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
  const targets = state.targets.map((t) => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
  $$(".target-list").forEach((s) => {
    const keep = s.value;
    s.innerHTML = targets;
    if (keep && state.targets.some((t) => t.name === keep)) s.value = keep;
  });
  const pools = [...new Set(state.pools.map((p) => p.name))];
  $("#pool-names").innerHTML = pools.map((n) => `<option value="${esc(n)}">`).join("");
}

// ---------- data ----------

async function loadCluster() {
  const c = await api("/cluster");
  state.cluster = c;
  $("#topology").textContent = `${TOPOLOGY[c.topology] || c.topology} · ${c.members.length} member${c.members.length === 1 ? "" : "s"}`;
  $("#quorum").innerHTML = `<span class="dot ${c.quorum ? "ok" : "bad"}"></span> ${c.quorum ? "Quorum OK" : "No quorum"}`;
  const hosts = await Promise.all(computeMembers().filter((m) => m.healthy).map((m) =>
    api(`/nodes/${encodeURIComponent(m.name)}/host`).then((h) => [m.name, h]).catch(() => [m.name, null])));
  state.hosts = Object.fromEntries(hosts);
}
const loadVMs = async () => { state.vms = await api("/vms"); };
const loadPools = async () => { state.pools = await api("/pools"); };
const loadTargets = async () => { state.targets = await api("/backup/targets"); fillSelects(); };

async function refresh(page) {
  await loadCluster();
  if (page === "home") { await Promise.all([loadVMs(), loadPools(), loadTargets()]); renderHome(); }
  if (page === "compute") { await Promise.all([loadVMs(), loadPools(), loadTargets()]); renderVMs(); }
  if (page === "storage") { await Promise.all([loadPools(), loadVMs()]); renderPools(); }
  if (page === "nodes") { await loadVMs(); renderNodes(); }
  if (page === "backup") { await loadTargets(); await Promise.all([renderBackups(), renderPolicies(), renderJobs()]); renderTargets(); }
  if (page === "system") renderSystem();
  fillSelects();
}

// ---------- home ----------

function ring(label, value, unit, percent, meta) {
  const r = 52;
  const c = 2 * Math.PI * r;
  const off = c * (1 - Math.max(0, Math.min(100, percent)) / 100);
  return `<div class="ring">
    <svg viewBox="0 0 124 124">
      <circle class="track" cx="62" cy="62" r="${r}" fill="none" stroke-width="5"/>
      <circle class="value" cx="62" cy="62" r="${r}" fill="none" stroke-width="5" stroke-linecap="round"
        stroke-dasharray="${c}" stroke-dashoffset="${off}" transform="rotate(-90 62 62)"/>
      <text class="label" x="62" y="42" text-anchor="middle">${esc(label)}</text>
      <text x="62" y="76" text-anchor="middle"><tspan class="num">${esc(value)}</tspan><tspan class="unit" dx="3">${esc(unit)}</tspan></text>
    </svg>
    <div class="meta">${meta}</div>
  </div>`;
}

function totals() {
  const computes = computeMembers();
  let cpus = 0, mem = 0;
  for (const m of computes) {
    const h = state.hosts[m.name];
    if (h) { cpus += h.cpus; mem += h.memory_mb; }
  }
  const vcpus = state.vms.reduce((s, v) => s + v.vcpus, 0);
  const vmem = state.vms.reduce((s, v) => s + v.memory_mb, 0);
  const cap = state.pools.reduce((s, p) => s + p.capacity_gb, 0);
  const used = state.pools.reduce((s, p) => s + p.allocation_gb, 0);
  return { computes, cpus, mem, vcpus, vmem, cap, used };
}

function renderHome() {
  const t = totals();
  const running = state.vms.filter((v) => v.state === "running").length;
  const members = state.cluster.members;
  const online = members.filter((m) => m.healthy).length;
  $("#rings").innerHTML = [
    ring("VMs", state.vms.length, "", state.vms.length ? (running / state.vms.length) * 100 : 0,
      `Running: ${running}<br>Stopped: ${state.vms.length - running}`),
    ring("Nodes", members.length, "", members.length ? (online / members.length) * 100 : 0,
      `Online: ${online}<br>Offline: ${members.length - online}`),
    ring("CPU Allocation", pct(t.vcpus, t.cpus), "%", pct(t.vcpus, t.cpus),
      `vCPU: ${t.vcpus}<br>Physical: ${t.cpus} cores`),
    ring("Mem Allocation", pct(t.vmem, t.mem), "%", pct(t.vmem, t.mem),
      `Allocated: ${gbText(t.vmem)}<br>Total: ${gbText(t.mem)}`),
    ring("Storage", pct(t.used, t.cap), "%", pct(t.used, t.cap),
      `Used: ${Math.round(t.used)} GB<br>Total: ${Math.round(t.cap)} GB`),
    ring("Backup", state.targets.length, state.targets.length === 1 ? "target" : "targets", state.targets.length ? 100 : 0,
      `<a href="#backup" style="color:#fff">Manage backup</a><br>&nbsp;`),
  ].join("");

  const nodeCards = members.map((m) => {
    const h = state.hosts[m.name];
    const vms = state.vms.filter((v) => v.node === m.name);
    const vcpu = vms.reduce((s, v) => s + v.vcpus, 0);
    const vmem = vms.reduce((s, v) => s + v.memory_mb, 0);
    const rows = m.role === "witness"
      ? `<div class="hint">Witness: votes for quorum, runs no VMs.</div>`
      : h ? `
        <div class="bar-row"><span>CPU Allocation</span><span>${pct(vcpu, h.cpus)}%</span>${bar(pct(vcpu, h.cpus))}</div>
        <div class="bar-row"><span>Mem Allocation</span><span>${pct(vmem, h.memory_mb)}%</span>${bar(pct(vmem, h.memory_mb))}</div>
        <div class="bar-row"><span>VMs</span><span>${vms.length}</span></div>
        <div class="hint">${h.cpus} cores · ${gbText(h.memory_mb)} · ${esc(h.hypervisor)}</div>`
        : `<div class="hint">${esc(m.last_error || "No data")}</div>`;
    return `<div class="card node-card">
      <svg class="icon"><use href="#i-server"/></svg>
      <div>${rows}</div>
      <div class="name"><span class="dot ${m.healthy ? "ok" : "bad"}"></span>${esc(m.name)}${m.local ? " (manager)" : ""}
        <span class="role">${esc(m.role)} · ${esc(m.address || "local")}</span></div>
    </div>`;
  });
  const poolCards = state.pools.map((p) => {
    const used = pct(p.allocation_gb, p.capacity_gb);
    return `<div class="card node-card">
      <svg class="icon"><use href="#i-disk"/></svg>
      <div>
        <div class="bar-row"><span>Usage</span><span>${used}%</span>${bar(used)}</div>
        <div class="bar-row"><span>Used</span><span>${p.allocation_gb} GB</span></div>
        <div class="bar-row"><span>Total</span><span>${p.capacity_gb} GB</span></div>
      </div>
      <div class="name"><span class="dot ${p.active ? "ok" : "bad"}"></span>${esc(p.name)}
        <span class="role">${esc(POOL_TYPES[p.type] || p.type)} · ${esc(p.node)}</span></div>
    </div>`;
  });
  $("#home-cards").innerHTML = nodeCards.concat(poolCards).join("");
}

// ---------- compute ----------

function renderVMs() {
  const q = $("#vm-search").value.trim().toLowerCase();
  const f = $("#vm-filter").value;
  const vms = state.vms.filter((v) => (!q || v.name.toLowerCase().includes(q)) && (!f || v.state === f));
  const running = state.vms.filter((v) => v.state === "running").length;
  $("#vm-count").textContent = `${state.vms.length} total · ${running} running`;
  $("#vms").innerHTML = vms.length ? vms.map((vm) => `
    <tr>
      <td><div class="vm-name">${esc(vm.name)}</div><div class="vm-sub">${esc(vm.description || vm.uuid.slice(0, 8))}</div></td>
      <td><span class="badge ${esc(vm.state)}">${esc(vm.state)}</span></td>
      <td>${esc(vm.node || "")}</td>
      <td>${vm.vcpus}</td>
      <td>${gbText(vm.memory_mb)}</td>
      <td>${vm.disks.map((d) => `${d.size_gb} GB <span class="vm-sub">${esc(d.pool)}</span>`).join("<br>")}</td>
      <td>${esc(vm.firmware.toUpperCase())}</td>
      <td><div class="actions">
        ${vm.state === "running"
          ? `<button class="small" data-vm="console" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Console</button>
             <button class="small" data-vm="stop" data-id="${vm.uuid}">Shut down</button>
             <button class="small" data-vm="poweroff" data-id="${vm.uuid}">Power off</button>`
          : `<button class="small" data-vm="start" data-id="${vm.uuid}">Power on</button>
             <button class="small danger" data-vm="delete" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Delete</button>`}
        <button class="small" data-vm="backup" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Back up</button>
      </div></td>
    </tr>`).join("") : empty(8, state.vms.length ? "No VMs match" : "No virtual machines yet. Click + New VM to create one.");
}
$("#vm-search").addEventListener("input", renderVMs);
$("#vm-filter").addEventListener("change", renderVMs);

$("#vms").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-vm]");
  if (!btn) return;
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
  location.hash === "#compute" ? await refresh("compute") : (location.hash = "compute");
};

handlers["backup-dialog"] = async (f, ctx) => {
  const body = { target: f.get("target") };
  if (f.get("keep_last")) body.keep_last = Number(f.get("keep_last"));
  await post(`/vms/${ctx.id}/backup`, body);
  toast(`Backing up ${ctx.name} to ${body.target}. Progress is under Backup → Jobs.`);
};

// ---------- storage ----------

function renderPools() {
  $("#pools").innerHTML = state.pools.length ? state.pools.map((p) => {
    const used = pct(p.allocation_gb, p.capacity_gb);
    return `<tr>
      <td><span class="vm-name">${esc(p.name)}</span></td>
      <td>${esc(p.node)}</td>
      <td><span class="badge info">${esc(POOL_TYPES[p.type] || p.type)}</span></td>
      <td class="vm-sub">${esc(p.source)}</td>
      <td><div class="bar-row"><span>${p.allocation_gb} / ${p.capacity_gb} GB</span><span>${used}%</span>${bar(used)}</div></td>
      <td><button class="small danger" data-pool="${esc(p.name)}" data-node="${esc(p.node)}">Remove</button></td>
    </tr>`;
  }).join("") : empty(6, "No storage pools");
}

$("#pools").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-pool]");
  if (!btn || !confirm(`Remove pool ${btn.dataset.pool} from ${btn.dataset.node}? Data on the storage stays.`)) return;
  try {
    await del(`/nodes/${encodeURIComponent(btn.dataset.node)}/pools/${encodeURIComponent(btn.dataset.pool)}`);
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
  await post(`/nodes/${encodeURIComponent(f.get("node"))}/pools`, body);
  toast(`Pool ${body.name} added`);
  await refresh("storage");
};

// ---------- nodes ----------

function renderNodes() {
  const c = state.cluster;
  $("#nodes-sub").textContent = `${TOPOLOGY[c.topology] || c.topology} · ${c.ha_capable ? "HA capable" : "no HA"} · ${c.quorum ? "quorum OK" : "no quorum"}`;
  $("#nodes").innerHTML = c.members.map((m) => {
    const h = state.hosts[m.name];
    const vms = state.vms.filter((v) => v.node === m.name).length;
    return `<tr>
      <td><span class="vm-name">${esc(m.name)}</span>${m.local ? ' <span class="badge info">manager</span>' : ""}</td>
      <td>${esc(m.role)}</td>
      <td class="vm-sub">${esc(m.address || "local")}</td>
      <td><span class="badge ${m.healthy ? "online" : "offline"}">${m.healthy ? "online" : "offline"}</span></td>
      <td>${h ? `${h.cpus} cores` : "–"}</td>
      <td>${h ? gbText(h.memory_mb) : "–"}</td>
      <td>${m.role === "compute" ? vms : "–"}</td>
      <td>${m.local ? "" : `<button class="small danger" data-node="${esc(m.name)}">Remove</button>`}</td>
    </tr>`;
  }).join("");
}

$("#nodes").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-node]");
  if (!btn || !confirm(`Remove ${btn.dataset.node} from the cluster?`)) return;
  try { await del(`/cluster/nodes/${encodeURIComponent(btn.dataset.node)}`); await refresh("nodes"); } catch (err) { fail(err); }
});

handlers["node-dialog"] = async (f) => {
  await post("/cluster/nodes", { name: f.get("name"), role: f.get("role"), address: f.get("address") });
  toast(`${f.get("name")} joined the cluster`);
  await refresh("nodes");
};

// ---------- backup ----------

function renderTargets() {
  $("#targets").innerHTML = state.targets.length ? state.targets.map((t) => `<tr>
    <td><span class="vm-name">${esc(t.name)}</span></td>
    <td><span class="badge info">${esc(TARGET_TYPES[t.type] || t.type)}</span></td>
    <td class="vm-sub">${esc(t.source)}</td>
    <td>${t.on_demand ? '<span class="badge waiting">Attached only during jobs</span>' : '<span class="badge">Always available</span>'}</td>
    <td><button class="small danger" data-target="${esc(t.name)}">Remove</button></td>
  </tr>`).join("") : empty(5, "No backup targets. Add one to start protecting VMs.");
}

async function renderBackups() {
  const target = $("#browse-target").value || state.targets[0]?.name;
  const tbody = $("#backups");
  if (!target) { tbody.innerHTML = empty(6, "Add a backup target first"); return; }
  try {
    const backups = await api(`/backup/targets/${encodeURIComponent(target)}/backups`);
    tbody.innerHTML = backups.length ? backups.slice().reverse().map((b) => `<tr>
      <td><span class="vm-name">${esc(b.vm_name)}</span></td>
      <td>${esc(when(b.created))}</td>
      <td>${esc(b.node || "shared")}</td>
      <td>${size(b.size_bytes)}</td>
      <td>${size(b.stored_bytes)}</td>
      <td><div class="actions">
        <button class="small" data-restore="${esc(b.id)}" data-vm="${esc(b.vm_name)}" data-node="${esc(b.node || "")}">Restore</button>
        <button class="small danger" data-backup-del="${esc(b.id)}" data-node="${esc(b.node || "")}">Delete</button>
      </div></td>
    </tr>`).join("") : empty(6, "No restore points on this target yet");
  } catch (err) {
    tbody.innerHTML = empty(6, err.message);
  }
}

async function renderPolicies() {
  const policies = await api("/backup/policies");
  $("#policies").innerHTML = policies.length ? policies.map((p) => `<tr>
    <td><span class="vm-name">${esc(p.name)}</span></td>
    <td>${esc(p.target)}</td>
    <td>${p.at ? `Daily at ${esc(p.at)}` : `Every ${p.every_hours} h`}</td>
    <td>${p.vms.length ? esc(p.vms.join(", ")) : "All VMs"}</td>
    <td>${p.keep_last}</td>
    <td>${esc(when(p.last_run))}</td>
    <td><div class="actions">
      <button class="small" data-policy-run="${esc(p.name)}">Run now</button>
      <button class="small danger" data-policy-del="${esc(p.name)}">Delete</button>
    </div></td>
  </tr>`).join("") : empty(7, "No schedules yet");
}

const JOB_STATE = { queued: "Queued", running: "Running", done: "Succeeded", failed: "Failed" };

async function renderJobs() {
  const jobs = (await api("/backup/jobs")).slice(0, 50);
  $("#jobs").innerHTML = jobs.length ? jobs.map((j) => `<tr>
    <td>${j.kind === "backup" ? "Backup" : "Restore"}${j.policy ? ` <span class="vm-sub">(${esc(j.policy)})</span>` : ""}</td>
    <td>${esc(j.vm)}</td>
    <td>${esc(j.target)}</td>
    <td>${esc(j.node || "")}</td>
    <td>${esc(when(j.created))}</td>
    <td><span class="badge ${esc(j.state)}">${JOB_STATE[j.state] || esc(j.state)}</span></td>
    <td class="vm-sub">${esc(j.error || j.result || "")}</td>
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
      await del(`/backup/targets/${encodeURIComponent(d.target)}`);
    } else if (d.policyRun) {
      const jobs = await post(`/backup/policies/${encodeURIComponent(d.policyRun)}/run`);
      toast(`Started ${jobs.length} backup job${jobs.length === 1 ? "" : "s"}`);
    } else if (d.policyDel) {
      if (!confirm(`Delete schedule ${d.policyDel}?`)) return;
      await del(`/backup/policies/${encodeURIComponent(d.policyDel)}`);
    } else if (d.restore) {
      openDialog("restore-dialog", { target, id: d.restore, sourceNode: d.node }, { name: `${d.vm}-restore` });
      return;
    } else if (d.backupDel) {
      if (!confirm(`Delete restore point ${d.backupDel}?`)) return;
      const q = d.node ? `?node=${encodeURIComponent(d.node)}` : "";
      await del(`/backup/targets/${encodeURIComponent(target)}/backups/${encodeURIComponent(d.backupDel)}${q}`);
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
  await post(`/backup/targets/${encodeURIComponent(ctx.target)}/backups/${encodeURIComponent(ctx.id)}/restore`, body);
  toast(`Restoring ${body.name}. Progress is under Jobs.`);
  await refresh("backup");
};

// ---------- system ----------

async function renderSystem() {
  const health = await api("/health");
  const c = state.cluster;
  const rows = [
    ["Product", "ibHiCS Hyper-Converged Infrastructure"],
    ["Version", health.version],
    ["Deployment", `${TOPOLOGY[c.topology] || c.topology} (${c.members.length} member${c.members.length === 1 ? "" : "s"})`],
    ["High availability", c.ha_capable ? "Yes" : "No: add a second compute node and a witness"],
    ["Quorum", c.quorum ? "OK" : "Lost: changes are blocked"],
    ["License", 'GNU AGPLv3 · <a href="https://github.com/iball281029/ibhics" target="_blank">Source code</a>'],
  ];
  $("#about").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${k === "License" ? v : esc(v)}</dd>`).join("");
}

// ---------- start ----------

show();
setInterval(() => {
  const page = location.hash.slice(1) || "home";
  if (!document.querySelector("dialog[open]")) refresh(page).catch(() => {});
}, 10000);
