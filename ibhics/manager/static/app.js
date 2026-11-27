"use strict";

// ---------- helpers ----------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function toLogin() {
  location.href = `/login.html?next=${encodeURIComponent(location.pathname + location.hash)}`;
  return new Promise(() => {}); // the page is going away
}

class ApprovalPending extends Error {}

async function api(path, opts = {}) {
  const r = await fetch(`/api/v1${path}`, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => `${(d.loc || []).filter((x) => x !== "body").join(".") || "request"}: ${d.msg}`).join("; ") : body.detail;
    if (r.status === 401 && $("#twofa-recovery") && !$("#twofa-recovery").hidden) throw new Error("Save your recovery codes, then sign in again.");
    if (r.status === 401 || (r.status === 403 && /initial password/.test(detail || ""))) return toLogin();
    throw new Error(detail || r.statusText);
  }
  if (r.status === 204) return null;
  const result = await r.json();
  if (result?.approval_request_id) throw new ApprovalPending(`${result.detail} (${result.approval_request_id})`);
  return result;
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
const fail = (err) => toast(err.message || String(err), !(err instanceof ApprovalPending));

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

const state = { networks: [], me: null, cluster: null, hosts: {}, summaries: {}, clusterHA: null, guests: {}, stats: {}, groups: { groups: [], members: {} }, vms: [], pools: [], targets: [], sources: [], policies: [], jobs: [], moves: [], imports: [], replications: [], users: [], isos: [], templates: [], deploys: [], recycle: { days: 0, vms: [] } };
const TOPOLOGY = { "standalone": "Standalone", "two-node-witness": "2 nodes + witness", "cluster": "Cluster", "incomplete": "Incomplete" };
const POOL_TYPES = { dir: "Folder", disk: "Local disk · thin", nfs: "NFS", iscsi: "iSCSI", lvm: "Disks · LVM" };
const TARGET_TYPES = { local: "Local disk", nfs: "NFS", smb: "SMB", usb: "USB", iscsi: "iSCSI", fc: "FC", s3: "S3" };
const SOURCE_TYPES = { vmware: "VMware", kvm: "Proxmox / KVM", ova: "OVA folder", disk: "Disk images", upload: "Uploaded files" };
const JOB_STATE = { retry_wait: "Retry scheduled", accepted: "Accepted", skipped: "Skipped", queued: "Queued", running: "Running", done: "Succeeded", failed: "Failed", cancelled: "Cancelled" };
// Jobs that can be stopped from the Tasks page, and the API call that stops them.
const CANCEL_PATH = { Import: (id) => `/migrate/jobs/${enc(id)}/cancel`, "Live migration": (id) => `/migrations/${enc(id)}/cancel`, Move: (id) => `/migrations/${enc(id)}/cancel` };
const isAdmin = () => !state.me || state.me.role === "admin";
const canManage = (category) => !state.me || state.me.role === "admin" || (state.me.permissions || []).includes(category);
const canOperate = () => canManage(({storage:"storage",network:"network",backup:"backup",migrate:"replication",cluster:"cluster",node:"cluster"})[currentPage()] || "vms");
const JOB_KINDS = { verify: "Boot verification", recovery: "Recovery", synthetic_full: "Synthetic full", backup_copy: "Backup copy", archive: "Archive", backup: "Backup", restore: "Restore", snapshot: "Snapshot", "snapshot-restore": "Restore snapshot", "snapshot-delete": "Delete snapshot", rename: "Rename VM", "to-virtio": "Switch to VirtIO", restart: "Restart to apply changes", sysprep: "Generalize (sysprep)",
  "ct-ha-restart": "Container HA restart",
  "container-create": "Create container", "container-start": "Start container", "container-stop": "Stop container",
  "container-kill": "Power off container", "container-restart": "Restart container", "container-resize": "Change container",
  "container-delete": "Delete container", "move-disks": "Move disks", "pool-cache": "Pool cache", "ct-move": "Move container", "container-network": "Container network" };
const computeMembers = () => (state.cluster?.members || []).filter((m) => m.role === "compute");
const nodeLabel = (name) => state.cluster?.members.find((m) => m.name === name)?.display_name || name;
const poolText = (p) => p.display_name ? `${p.display_name} (${p.name})` : p.name;
const poolLabel = (name, node) => poolText(state.pools.find((p) => p.name === name && p.node === node) || { name });
const nodeOptionText = (name) => nodeLabel(name) === name ? name : `${nodeLabel(name)} (${name})`;

// ---------- theme and user menu ----------

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  $$("[data-theme-set]").forEach((b) => b.classList.toggle("on", (b.dataset.themeSet === "dark") === dark));
}
try {
  // Start the selected command design in its intended palette once; later choices persist.
  if (localStorage.getItem("ibhics-command-design") !== "1") {
    localStorage.setItem("ibhics-theme", "dark");
    localStorage.setItem("ibhics-rail-expanded", "false");
    localStorage.setItem("ibhics-command-design", "1");
  }
  applyTheme(localStorage.getItem("ibhics-theme") || "dark");
} catch { applyTheme("dark"); }
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-theme-set]");
  if (t) {
    applyTheme(t.dataset.themeSet);
    try { localStorage.setItem("ibhics-theme", t.dataset.themeSet); } catch { /* private mode */ }
  }
  const menu = e.target.closest("#user-menu");
  $("#user-dropdown").classList.toggle("open", Boolean(menu) && !e.target.closest(".dropdown"));
  if (e.target.closest("#tasks-menu")) location.hash = "tasks"; // the Tasks page, with every detail
  $$("details.more[open]").forEach((d) => { if (!d.contains(e.target)) d.removeAttribute("open"); });
  const link = e.target.closest("[data-tab-link]");
  if (link) setTimeout(() => selectTab("system-tabs", link.dataset.tabLink), 0);
});
// Manual popovers retain their DOM ancestry for delegated VM actions, while the
// browser top layer prevents clipping by cards, panels and scrolling tables.
function positionActionMenu(details) {
  const menu = details.querySelector(":scope > .dropdown");
  if (!menu || !details.open) return;
  const pad = 8, width = innerWidth, height = innerHeight;
  const anchor = details.querySelector("summary").getBoundingClientRect();
  Object.assign(menu.style, {position: "fixed", margin: "0", inset: "auto",
    left: "0px", top: "0px", minWidth: `${Math.min(210, width - 2 * pad)}px`,
    maxWidth: `${width - 2 * pad}px`, maxHeight: `${height - 2 * pad}px`});
  const naturalHeight = menu.scrollHeight + 2;
  const below = Math.max(0, height - anchor.bottom - pad - 4);
  const above = Math.max(0, anchor.top - pad - 4);
  const down = below >= naturalHeight || below >= above;
  const available = Math.min(height - 2 * pad, Math.max(below, above));
  menu.style.maxHeight = `${Math.max(40, available)}px`;
  const rect = menu.getBoundingClientRect();
  const rail = document.querySelector(".rail")?.getBoundingClientRect().right || 0;
  const minimumLeft = width - rail - pad >= rect.width ? rail + pad : pad;
  const left = Math.max(minimumLeft, Math.min(anchor.right - rect.width, width - rect.width - pad));
  const top = Math.max(pad, Math.min(down ? anchor.bottom + 4 : anchor.top - rect.height - 4,
    height - rect.height - pad));
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
}
document.addEventListener("toggle", (event) => {
  const details = event.target;
  if (!(details instanceof HTMLDetailsElement) || !details.classList.contains("more")) return;
  const menu = details.querySelector(":scope > .dropdown");
  if (!menu) return;
  if (!details.open) {
    if (menu.hasAttribute("popover") && menu.matches(":popover-open")) menu.hidePopover();
    return;
  }
  $$("details.more[open]").forEach((other) => { if (other !== details) other.open = false; });
  if (typeof menu.showPopover === "function") {
    menu.setAttribute("popover", "manual");
    if (!menu.matches(":popover-open")) menu.showPopover();
  }
  positionActionMenu(details);
}, true);
let actionMenuFrame = null;
function repositionActionMenus(event) {
  if (event?.target instanceof Element && event.target.closest(".dropdown")) return;
  if (actionMenuFrame !== null) return;
  actionMenuFrame = requestAnimationFrame(() => {
    actionMenuFrame = null;
    $$("details.more[open]").forEach(positionActionMenu);
  });
}
addEventListener("scroll", repositionActionMenus, {passive: true, capture: true});
addEventListener("resize", repositionActionMenus);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  $$("details.more[open]").forEach((details) => {
    details.open = false;
    details.querySelector("summary")?.focus();
  });
});
$("#logout").addEventListener("click", async () => {
  await fetch("/api/v1/auth/logout", { method: "POST" }).catch(() => {});
  location.href = "/login.html";
});

async function loadMe() {
  state.me = await api("/auth/me");
  state.security = state.me ? await api("/auth/2fa") : null;
  if (state.security?.required && !state.security.enabled) {
    location.hash = "system";
    selectTab("system-tabs", "account");
  }
  $("#whoami").textContent = state.me ? state.me.name : "local";
  document.body.classList.toggle("can-admin", isAdmin());
  document.body.classList.toggle("can-operate", canOperate());
}


// Dedicated orbital navigation button below the home logo.
const railToggle = document.getElementById("rail-toggle");
const spatialMenu = document.getElementById("menu"), spatialScrim = document.getElementById("spatial-scrim");
function setRailExpanded(expanded) {
  document.body.classList.remove("rail-expanded");
  spatialMenu.hidden = !expanded; spatialScrim.hidden = !expanded;
  if (!expanded) setOrbitExtra(false);
  railToggle.setAttribute("aria-expanded", String(expanded));
  railToggle.setAttribute("aria-label", expanded ? "Close navigation" : "Open navigation");
  const scale = Math.min(1, (innerHeight - 130) / 560, (innerWidth - (innerWidth < 700 ? 60 : 95)) / 400);
  spatialMenu.style.setProperty("--orbit-scale", String(Math.max(.45, scale)));
}
// The bouncing arrow swaps the two menu sets: pointing at it is enough with a mouse,
// a tap or a key press does it elsewhere. It sits below the first set (pointing down)
// and above the second (pointing up), so it moves away from the pointer once used.
const orbitMore = document.getElementById("orbit-more");
let orbitSwapped = 0;
function setOrbitExtra(extra) {
  spatialMenu.classList.toggle("show-extra", extra);
  orbitMore.setAttribute("aria-label", extra ? "Back to the main navigation" : "More: Containers, Templates, Activity, Terminal");
  orbitMore.title = extra ? "Main navigation" : "Containers · Templates · Activity · Terminal";
}
orbitMore.addEventListener("pointerenter", (event) => {
  if (event.pointerType !== "mouse") return;
  orbitSwapped = Date.now();
  setOrbitExtra(!spatialMenu.classList.contains("show-extra"));
});
orbitMore.addEventListener("click", () => {
  if (Date.now() - orbitSwapped < 600) return; // the pointer already swapped it
  setOrbitExtra(!spatialMenu.classList.contains("show-extra"));
});
setRailExpanded(false);
railToggle.addEventListener("click", () => setRailExpanded(spatialMenu.hidden));
spatialScrim.addEventListener("click", () => { setRailExpanded(false); railToggle.focus(); });
spatialMenu.addEventListener("click", (event) => { if (event.target.closest("a")) setRailExpanded(false); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !spatialMenu.hidden) { setRailExpanded(false); railToggle.focus(); } });
addEventListener("resize", () => { if (!spatialMenu.hidden) setRailExpanded(true); });

// ---------- navigation ----------

const PAGES = ["home", "fabric", "compute", "storage", "network", "cluster", "backup", "migrate", "activity", "system", "alerts", "tasks", "templates", "recycle", "terminal", "containers"];
const currentPage = () => {
  const hash = location.hash.slice(1);
  if (hash === "nodes") return "cluster"; // Preserve old node inventory bookmarks.
  if (hash.startsWith("alerts/")) return "alerts";
  if (hash.startsWith("storage/")) return "storage";
  if (hash.startsWith("vm/")) return "vm";
  if (hash.startsWith("node/")) return "node";
  return PAGES.includes(hash) ? hash : "home";
};
const vmFromHash = () => decodeURIComponent(location.hash.slice(4));

function show() {
  setRailExpanded(false);
  const page = state.security?.required && !state.security.enabled ? "system" : currentPage();
  document.body.classList.toggle("can-operate", canOperate());
  if (location.hash === "#nodes") history.replaceState(null, "", "#cluster");
  if (page !== "network" && ntopo.edit) {
    if (ntopo.edit.saving) { location.hash = "network"; toast("Wait for topology saving to finish.", true); return; }
    topoEndEdit(); // Leaving Network discards only the local draft.
  }
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${page}`));
  document.body.dataset.section = page;
  const sectionTitle = {fabric:"Infrastructure map",home:"Overview",compute:"Virtual machines",containers:"Containers",templates:"Templates",storage:"Storage",network:"Network",cluster:"Cluster",backup:"Data protection",migrate:"Migration",activity:"Activity",alerts:"Alerts",system:"Settings",tasks:"Jobs",recycle:"Recycle bin",terminal:"Terminal",vm:"Virtual machine",node:"Cluster node"};
  $("#workspace-section").textContent = sectionTitle[page] || page;
  const section = { vm: "compute", node: "cluster", recycle: "compute", fabric: "home" }[page] || page;
  $$(".rail a[data-page]").forEach((a) => a.classList.toggle("active", a.dataset.page === section));
  if (page === "vm") selectTab("vm-tabs", "vm-summary");
  if (page === "node") selectTab("node-tabs", "node-summary");
  refresh(page).catch(fail);
  syncJobsPoll();
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
  syncJobsPoll();
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

// Step-by-step dialogs ("wizards"): .wiz-steps on top, .wiz-page[data-page] sections, and
// Back / Next / OK buttons. check(step) throws to stop Next; show(step) fills a page.
const wizards = {};
function wizard(id, { steps = 3, check = () => {}, show = () => {}, reset = () => {} }) {
  const form = $(`#${id} form`);
  const w = { step: 1, form };
  const paint = () => {
    $$(".wiz-steps li", form).forEach((li) => {
      const n = Number(li.dataset.step);
      li.classList.toggle("on", n === w.step);
      li.classList.toggle("done", n < w.step);
    });
    $$(".wiz-page", form).forEach((p) => { p.hidden = Number(p.dataset.page) !== w.step; });
    $(".wiz-back", form).hidden = w.step === 1;
    $(".wiz-next", form).hidden = w.step === steps;
    $(".wiz-ok", form).hidden = w.step !== steps;
    show(w.step);
  };
  w.start = () => { w.step = 1; reset(); paint(); };
  w.go = (n) => { w.step = n; paint(); };
  w.check = check;
  $(".wiz-next", form).addEventListener("click", () => {
    try { check(w.step); w.step++; paint(); } catch (err) { fail(err); }
  });
  $(".wiz-back", form).addEventListener("click", () => { w.step--; paint(); });
  form.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT" && w.step < steps) { e.preventDefault(); $(".wiz-next", form).click(); }
  });
  wizards[id] = w;
  return w;
}
// Preset buttons (.chips.presets[data-target]) set a number field and show which is on.
function syncPresets(form) {
  $$(".chips.presets", form).forEach((c) => {
    const v = form.elements[c.dataset.target].value;
    $$("button", c).forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  });
}
document.addEventListener("click", (e) => {
  const b = e.target.closest(".chips.presets button[data-v]");
  if (!b) return;
  const form = b.closest("form");
  form.elements[b.closest(".chips").dataset.target].value = b.dataset.v;
  syncPresets(form);
  form.elements[b.closest(".chips").dataset.target].dispatchEvent(new Event("input", { bubbles: true }));
});
document.addEventListener("input", (e) => { if (e.target.form && $(".chips.presets", e.target.form)) syncPresets(e.target.form); });

function openDialog(id, context = null, prefill = {}) {
  const dlg = document.getElementById(id);
  const form = $("form", dlg);
  form.reset();
  fillSelects();
  for (const [k, v] of Object.entries(prefill)) if (form.elements[k] && v != null) form.elements[k].value = v;
  syncSwitch(form);
  dialogContext = context;
  $$("[data-ctx]", form).forEach((el) => { el.hidden = !context?.[el.dataset.ctx]; });
  dlg.showModal();
  // Pickers need the current pools and VMs even when the page did not load them.
  const want = [$(".pool-list", form) && "pools", $(".vm-check-list", form) && "vms",
    $(".net-list", form) && "networks", $(".iso-list", form) && "isos"].filter(Boolean);
  if (want.length) load(...want).then(() => fillSelects()).catch(() => {});
  wizards[id]?.start();
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
  const opts = online.map((m) => `<option value="${esc(m.name)}">${esc(nodeOptionText(m.name))}</option>`).join("");
  $$(".node-list, .node-list-auto").forEach((s) => {
    const was = s.value;
    s.innerHTML = `${s.matches(".node-list-auto") ? '<option value="">Automatic</option>' : ""}${opts}`;
    if ([...s.options].some((o) => o.value === was)) s.value = was;
  });
  const keep = (sel, items) => $$(sel).forEach((s) => {
    const was = s.value;
    s.innerHTML = items.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    if (items.includes(was)) s.value = was;
  });
  keep(".target-list", state.targets.map((t) => t.name));
  $$(".net-list").forEach((s) => {
    const was = s.value || "default";
    s.innerHTML = netOptions(was);
  });
  keep(".source-list", state.sources.map((s) => s.name));
  $$(".iso-list").forEach((s) => { const was = s.value; s.innerHTML = isoOptions(was); });
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
    `<option value="${esc(p.name)}">${esc(poolText(p))} (${Math.floor(p.available_gb).toLocaleString()} GB free, ${esc(p.type)})</option>`);
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
  $("#topology").textContent = c.name || TOPOLOGY[c.topology] || c.topology;
  $("#quorum").innerHTML = `<span class="dot ${c.quorum ? "ok" : "bad"}"></span> ${c.quorum ? "OK" : "Lost"}`;
  const hosts = await Promise.all(computeMembers().filter((m) => m.healthy).map((m) =>
    api(`/nodes/${enc(m.name)}/host`).then((h) => [m.name, h]).catch(() => [m.name, null])));
  state.hosts = Object.fromEntries(hosts);
}
const loaders = {
  vms: async () => {
    const [all, ha] = await Promise.all([api("/vms"), api("/ha").catch(() => null)]);
    state.clusterHA = ha;
    state.ha = new Set((ha?.vms || []).map((v) => v.uuid));
    state.vms = all.filter((v) => !v.template && !v.recycled);
    state.templates = all.filter((v) => v.template && !v.recycled);
  },
  deploys: async () => { state.deploys = await api("/deploys"); },
  activity: async () => {
    const r = await api(`/activity?limit=1000${tp.days ? `&days=${tp.days}` : ""}`);
    state.activity = r.events || [];
    state.activityRetention = r.retention_days || 0;
  },
  recycle: async () => { state.recycle = await api("/recycle-bin"); },
  edge: async () => { state.edge = await api("/edge"); },
  guests: async () => { state.guests = Object.fromEntries((await api("/vms/guests")).map((g) => [g.uuid, g])); },
  stats: async () => {
    state.stats = Object.fromEntries((await api("/vms/stats")).map((s) => [s.uuid, s]));
    const now = Date.now(); // a short trail per VM for the card waves (kept in the browser only)
    for (const [uuid, st] of Object.entries(state.stats)) {
      const h = (vxHistory[uuid] ||= []);
      if (h.length && now - h.at(-1).at < 2000) h.pop();
      h.push({ at: now, cpu: st.cpu_pct, net: (st.net_rx_bps || 0) + (st.net_tx_bps || 0) });
      if (h.length > 40) h.shift();
    }
  },
  groups: async () => { state.groups = await api("/vm-groups"); },
  pools: async () => { state.pools = await api("/pools"); },
  networks: async () => { state.networks = await api("/networks"); },
  targets: async () => { state.targets = await api("/backup/targets"); },
  policies: async () => { state.policies = await api("/backup/policies"); },
  jobs: async () => { state.jobs = await api("/backup/jobs"); },
  moves: async () => { state.moves = await api("/migrations"); },
  sources: async () => { state.sources = await api("/migrate/sources"); },
  isos: async () => { state.isos = await api("/isos"); },
  imports: async () => { state.imports = await api("/migrate/jobs"); },
  replications: async () => { state.replications = await api("/migrate/replications"); },
  containers: async () => { state.containers = await api("/containers"); },
  ctTemplates: async () => { if (!state.ctTemplates?.length) state.ctTemplates = await api("/containers/templates"); },
  summaries: async () => {
    const got = await Promise.all(computeMembers().filter((m) => m.healthy).map((m) =>
      api(`/nodes/${enc(m.name)}/summary`).then((s) => [m.name, s]).catch(() => null)));
    state.summaries = Object.fromEntries(got.filter(Boolean));
  },
  settingsTasks: async () => {
    state.applicationJobs = (await api("/settings")).jobs || [];
    state.approvalJobs = state.me ? (await api("/approvals")).requests : [];
  },
  users: async () => { state.users = canManage("users") && state.me ? await api("/users") : []; },
};
// Share in-flight reads with navigation/manual refresh and polling.
const loading = new Map();
const load = (...names) => Promise.all(names.map((n) => {
  if (!loading.has(n)) loading.set(n, Promise.resolve().then(() => loaders[n]()).finally(() => loading.delete(n)));
  return loading.get(n);
}));

// ---------- tasks ----------

// Everything long-running the system does, newest first: imports, backups and restores,
// VM moves. Running ones show their current step and how far along they are.
function allTasks() {
  const tasks = [
    ...(state.applicationJobs || []).map(j => ({kind:"Application " + j.kind, title:j.id, id:j.id, sub:(j.nodes || []).join(" → "), state:j.state, step:j.detail, created:j.created, error:j.state === "failed" || j.state === "interrupted" ? j.detail : ""})),
    ...(state.approvalJobs || []).map(j => ({kind:"Approval", title:j.path, id:j.id, sub:`Requested by ${j.requester}${j.approver ? " · reviewed by " + j.approver : ""}`, state:j.state === "pending" ? "queued" : j.state === "executed" ? "done" : j.state, step:j.state === "pending" ? "รออนุมัติจาก admin อีกคน" : j.state, created:j.created})),
    ...state.imports.map((j) => ({
      kind: "Import", title: j.name, sub: `from ${j.source} to ${j.node}`, state: j.state,
      step: j.step, percent: j.percent, created: j.created, finished: j.finished, error: j.error, log: j.id, id: j.id, result: j.result,
    })),
    ...state.jobs.map((j) => ({
      kind: JOB_KINDS[j.kind] || "Backup", title: j.vm, sub: j.kind === "rename" ? j.target : j.kind.startsWith("container") ? `container · ${j.target}` : j.kind.startsWith("snapshot") ? `snapshot ${j.target}` : `${j.kind === "restore" ? "from" : "to"} ${j.target}`,
      state: j.state, step: j.step || "", percent: j.percent ?? null, created: j.created, finished: j.finished, error: j.error, id: j.id, result: j.result,
    })),
    ...state.moves.map((m) => ({
      kind: m.live ? "Live migration" : "Move", title: m.vm, sub: `${m.source} → ${m.target || "auto"}`,
      state: m.state, step: m.step || "", percent: null, created: m.created, finished: m.finished, error: m.error, id: m.id,
    })),
    ...state.deploys.map((d) => ({
      kind: "Deploy VM", title: d.vm, sub: `from ${d.source} on ${d.node}${d.customized ? " · customized" : ""}`,
      state: d.state, step: "Copying the disks", percent: null, created: d.created, finished: d.finished,
      error: d.error, id: d.id, result: d.result ? `VM ${d.result}${d.power_on ? ", powered on" : ""}` : "",
    })),
    // Replication runs: the live one with its progress, then each finished run with its real result.
    ...(state.replications || []).flatMap((j) => [
      ...(j.operation ? [{ kind: "Replication", title: j.name, sub: `${j.source} → ${j.node}`, state: "running",
        step: replicationLive(j), percent: Number(j.percent) || 0, created: j.run_started || j.last_attempt, finished: null, error: "", id: `rep-${j.id}-live` }] : []),
      ...(j.history || []).map((h, i) => ({ kind: "Replication", title: j.name, sub: `${h.operation || "sync"} · ${j.source} → ${j.node}`,
        state: h.state === "failed" ? "failed" : "done", step: "", percent: null, created: h.at, finished: h.at, error: h.state === "failed" ? h.error || "Failed" : "",
        id: `rep-${j.id}-${i}`, result: h.state === "failed" ? "" : `${h.operation || "sync"} finished · ${Number(h.bytes_transferred || 0).toLocaleString()} bytes transferred` })),
    ]),
    ...(state.activity || []).map((a) => {
      const vm = [...(state.vms || []), ...(state.templates || [])].find((v) => v.uuid === a.object.split(" / ")[0]);
      const named = (a.note || "").startsWith("name: ") ? a.note.slice(6) : "";
      const object = vm ? a.object.replace(vm.uuid, vm.name) : a.object || named;
      return {
        kind: a.category, title: object || a.action, sub: [object ? a.action : "", a.user && a.user !== "-" ? `by ${a.user}` : ""].filter(Boolean).join(" · "),
        // 202 means the request was accepted and the work runs in the background (its own task row).
        state: a.status === 202 ? "accepted" : a.ok ? "done" : "failed", step: "", percent: null, created: a.at, finished: a.at, duration_ms: a.duration_ms,
        error: a.ok ? "" : `HTTP ${a.status}${a.note ? ` · ${a.note}` : ""}`, id: `ev${a.id}`,
        result: a.status === 202 ? "Request accepted; the result appears in its own task when the work ends" : a.ok ? `${a.method} ${a.path}${a.address ? ` from ${a.address}` : ""}` : "",
      };
    }),
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
    ${t.state === "retry_wait" ? `<div class="task-step">${esc(t.step)}</div>` : ""}
    ${t.error ? `<div class="task-error">${esc(t.error)}</div>` : ""}
    <div class="task-foot"><span>${esc(when(t.created))}</span>${t.log ? `<button class="small" data-task-log="${esc(t.log)}" data-task-title="${esc(t.title)}">Log</button>` : ""}</div>
  </div>`;
  $("#tasks-panel").innerHTML = tasks.length
    ? `<div class="tasks-title">Tasks</div>${tasks.slice(0, 15).map(row).join("")}`
    : `<p class="hint" style="padding:10px">Nothing has run yet.</p>`;
}
// ---------- Tasks page ----------

const tp = { state: "", kind: "", sel: "", days: 0 };
const taskKey = (t) => `${t.kind}|${t.id}`;
function duration(t) {
  if (t.duration_ms != null) return t.duration_ms < 1000 ? `${t.duration_ms} ms` : `${(t.duration_ms / 1000).toFixed(1)} s`;
  if (!t.created) return "";
  const s = Math.max(0, ((t.finished ? new Date(t.finished) : new Date()) - new Date(t.created)) / 1000);
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}
function renderTaskTools() {
  const keep = $("#task-retention");
  keep.hidden = !(state.me && isAdmin());
  keep.value = String(state.activityRetention || 0);
}
function renderTaskPage() {
  const q = $("#task-search").value.trim().toLowerCase();
  const all = allTasks();
  const tasks = all.filter((t) => (!tp.kind || t.kind === tp.kind)
    && (!tp.state || (tp.state === "active" ? isActive(t) : t.state === tp.state))
    && (!q || [t.title, t.sub, t.kind, t.error].join(" ").toLowerCase().includes(q)));
  const running = all.filter(isActive).length, failed = all.filter((t) => t.state === "failed").length;
  $("#tasks-sum").textContent = `${all.length} total · ${running} running · ${failed} failed`;
  $$("#task-state button").forEach((b) => b.classList.toggle("on", b.dataset.v === tp.state));
  if (!tasks.some((t) => taskKey(t) === tp.sel)) tp.sel = tasks[0] ? taskKey(tasks[0]) : "";
  $("#task-rows").innerHTML = tasks.length ? tasks.map((t) => `<tr data-key="${esc(taskKey(t))}" class="${taskKey(t) === tp.sel ? "picked" : ""}">
      <td><span class="badge plain t-kind k-${esc(t.kind.split(" ")[0].toLowerCase())}">${esc(t.kind)}</span></td>
      <td class="vm-name">${esc(t.title)}</td>
      <td class="muted">${esc(t.sub)}</td>
      <td><span class="badge ${esc(t.state)}">${JOB_STATE[t.state] || esc(t.state)}</span>
        ${isActive(t) ? `<div class="bar${t.percent == null ? " busy" : ""}" style="margin-top:6px"><i style="width:${t.percent ?? 30}%"></i></div>` : ""}</td>
      <td>${esc(when(t.created))}</td><td>${esc(duration(t))}</td></tr>`).join("")
    : empty(6, all.length ? "No tasks match" : "Nothing has run yet. Backups, restores, migrations, imports and deploys show up here.");
  return renderTaskDetail(tasks.find((t) => taskKey(t) === tp.sel));
}
async function renderTaskDetail(t) {
  const box = $("#task-detail");
  if (!t) { box.innerHTML = `<p class="hint">Pick a task to see its details.</p>`; return; }
  const step = (isActive(t) || t.state === "retry_wait") ? esc(t.step || (t.state === "queued" ? "Waiting for a free slot" : "Working")) + (t.percent != null ? ` · ${t.percent}%` : "") : "";
  const rows = [["Type", esc(t.kind)], ["Object", esc(t.title)], ["Details", esc(t.sub)],
    ["Status", `<span class="badge ${esc(t.state)}">${JOB_STATE[t.state] || esc(t.state)}</span>`],
    ["Current step", step], ["Started", esc(when(t.created))], ["Finished", t.finished ? esc(when(t.finished)) : ""],
    ["Duration", esc(duration(t))], ["Result", esc(t.result || "")], ["Task ID", `<span class="mono">${esc(t.id)}</span>`]].filter(([, v]) => v);
  const cancellable = isActive(t) && CANCEL_PATH[t.kind] && canOperate();
  box.innerHTML = `<h3>${esc(t.kind)} · ${esc(t.title)}</h3>${cancellable ? `<button type="button" class="small danger" data-task-cancel="${esc(t.kind)}|${esc(t.id)}">Cancel task</button>` : ""}<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
    ${t.error ? `<p class="wiz-label">Error</p><pre class="task-err">${esc(t.error)}</pre>` : ""}
    ${t.log ? `<p class="wiz-label">Log <button class="small" data-task-log="${esc(t.log)}" data-task-title="${esc(t.title)}">Open full log</button></p><div class="task-log import-log-mini" id="task-log">Loading…</div>` : ""}`;
  if (t.log) {
    const panel = $("#task-log");
    try {
      const data = await api(`/migrate/jobs/${enc(t.log)}/log?format=summary`);
      if (panel.isConnected) panel.innerHTML = importTimeline(data, true);
    } catch (err) { if (panel.isConnected) panel.textContent = err.message; }
  }
}
async function renderOps() {
  // Readable change history (admins also see where each change came from).
  const { events } = await api("/activity?limit=500");
  const vms = [...(state.vms || []), ...(state.templates || [])];
  const named = (e) => {
    const vm = vms.find((v) => e.object && e.object.startsWith(v.uuid));
    if (vm) return e.object.replace(vm.uuid, vm.name);
    return e.object || ((e.note || "").startsWith("name: ") ? e.note.slice(6) : "");
  };
  $("#task-ops").innerHTML = events.length ? events.map((e) => `<tr>
    <td>${esc(when(e.at))}</td><td>${esc(e.user)}</td><td><span class="badge plain">${esc(e.category)}</span> ${esc(e.action)}</td><td>${esc(named(e))}</td>
    <td><span class="badge ${e.ok ? "done" : "failed"}">${e.ok ? "OK" : `Failed (${esc(e.status)})`}</span></td><td class="muted">${esc(e.address)}</td></tr>`).join("")
    : empty(6, "No changes recorded yet");
}
{
  $("#task-rows").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-key]");
    if (tr) { tp.sel = tr.dataset.key; renderTaskPage(); }
  });
  $("#task-state").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]");
    if (b) { tp.state = b.dataset.v; renderTaskPage(); }
  });
  $("#task-kind").addEventListener("change", (e) => { tp.kind = e.target.value; renderTaskPage(); });
  $("#task-search").addEventListener("input", () => renderTaskPage());
  $("#task-detail").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-task-cancel]");
    if (!b) return;
    const [kind, id] = b.dataset.taskCancel.split("|");
    if (!confirm(`Cancel this ${kind.toLowerCase()}? The VM stays where it was.`)) return;
    b.disabled = true;
    post(CANCEL_PATH[kind](id)).then(() => { toast("Cancel requested"); return refresh("tasks"); }).catch(fail);
  });
  $("#task-days").addEventListener("change", (e) => {
    tp.days = Number(e.target.value) || 0;
    load("activity").then(renderTaskPage).catch(fail);
  });
  $("#task-csv").addEventListener("click", () => {
    const qs = new URLSearchParams();
    const categories = ["VM", "Container", "Storage", "Network", "Node", "Backup", "User", "Sign-in", "Import", "System"];
    if (categories.includes(tp.kind)) qs.set("category", tp.kind);
    if (tp.state === "done") qs.set("result", "ok");
    if (tp.state === "failed") qs.set("result", "failed");
    const q = $("#task-search").value.trim();
    if (q) qs.set("q", q);
    if (tp.days) qs.set("days", tp.days);
    location.href = `/api/v1/activity.csv?${qs}`;
  });
  $("#task-retention").addEventListener("change", (e) => {
    api("/activity/settings", { method: "PUT", body: JSON.stringify({ retention_days: Number(e.target.value) }) })
      .then((r) => toast(r.retention_days ? `Keeping ${r.retention_days} days of history` : "Keeping all history"))
      .catch(fail);
  });
}

const P2V_STATE = { waiting: "Waiting for the agent", uploading: "Copying disks", ready: "Ready to import", imported: "Imported", failed: "Failed" };
let p2vSessions = [], p2vCheckedAt = null;
async function renderP2V() {
  const local = (state.cluster?.members || []).find((m) => m.local);
  const select = $("#p2v-pool");
  const keep = select.value;
  select.innerHTML = state.pools.filter((p) => p.active && (!local || p.node === local.name) && !["lvm", "iscsi"].includes(p.type))
    .map((p) => `<option value="${esc(p.name)}">${esc(poolText(p))}</option>`).join("");
  if ([...select.options].some((o) => o.value === keep)) select.value = keep;
  const sessions = await api("/p2v/sessions");
  p2vSessions = sessions;
  p2vCheckedAt = Date.now();
  renderPhysicalSources();
  $("#p2v-rows").innerHTML = sessions.length ? sessions.map((s) => {
    const m = s.machine || {};
    const disks = (m.disks || []).map((d) => `${esc(d.name)} ${Math.ceil(d.size_bytes / 2 ** 30)} GiB`).join(", ");
    return `<tr><td class="vm-name">${esc(m.hostname || "—")}<div class="vm-sub">${esc(m.os || `session ${s.id}`)}</div></td>
      <td><span class="badge ${s.state === "ready" ? "done" : s.state === "failed" ? "failed" : "running"}">${esc(P2V_STATE[s.state] || s.state)}</span>${s.state === "uploading" ? ` ${s.percent}%` : ""}</td>
      <td>${m.cpus ? `${m.cpus} vCPU · ${Math.round(m.memory_mb / 1024)} GB` : "—"}</td><td>${disks || "—"}</td><td>${esc(s.pool)}</td><td>${esc(when(s.created))}</td>
      <td>${s.state === "uploading" || s.state === "ready" ? `<progress max="100" value="${s.state === "ready" ? 100 : Math.max(0, Math.min(100, Number(s.percent) || 0))}" aria-label="Disk copy progress"></progress>` : ""}<span class="operator-only"><button type="button" class="small danger" data-p2v-delete="${esc(s.id)}">${s.state === "ready" ? "Discard" : "Cancel"}</button></span></td></tr>`;
  }).join("") : empty(7, "No physical machines yet. Create an agent command to start.");
}
let p2vDownload = null;
let p2vBaseCommand = "", p2vOs = "apt";
function renderP2VCommand() {
  const prefixes = { apt: "sudo apt-get install -y curl gzip && ", rhel: "sudo dnf install -y curl gzip || sudo yum install -y curl gzip; ", suse: "sudo zypper -n install curl gzip && " };
  let command = p2vBaseCommand;
  // Preserve all API-issued agent arguments, including its certificate pin.
  if (p2vOs === "other") command = command.replace(/^curl -fsS (?:--pinnedpubkey '[^']+' )?-k (\S+) \|/, "wget --no-check-certificate -qO- $1 |");
  $("#p2v-code").textContent = command ? (prefixes[p2vOs] || "") + command : "";
  $("#p2v-os-hint").textContent = p2vOs === "other" ? "wget downloads the script without certificate verification. The agent still requires curl, gzip, bash, dd and lsblk to upload disks; install curl first if missing. The API-issued pin remains on agent uploads." : "Installs curl and gzip before starting the API-issued agent command.";
  const windows = p2vOs === "windows";
  // Windows gets a program to download, Linux a command to paste: say which.
  $("#p2v-open").textContent = windows ? "Create Windows agent" : "Create agent command";
  $("#p2v-step2").textContent = windows ? "② WINDOWS AGENT (.EXE)" : "② AGENT COMMAND";
  $("#p2v-linux").hidden = windows;
  $("#p2v-windows").hidden = !windows;
  $("#p2v-linux").setAttribute("aria-labelledby", $(`[data-p2v-os="${p2vOs}"]`).id);
  $$("[data-p2v-os]").forEach((tab) => tab.setAttribute("aria-selected", String(tab.dataset.p2vOs === p2vOs)));
}
$$("[data-p2v-os]").forEach((tab) => tab.addEventListener("click", () => { p2vOs = tab.dataset.p2vOs; renderP2VCommand(); }));
$("#p2v-open").addEventListener("click", async () => {
  try {
    const r = await post("/p2v/sessions", { pool: $("#p2v-pool").value });
    p2vDownload = { url: r.windows_agent_url, token: r.agent_token, sid: r.session.id };
    p2vBaseCommand = r.command;
    $("#p2v-command-meta").textContent = `valid 24 h · one machine · storage: ${r.session.pool || $("#p2v-pool").value}`;
    renderP2VCommand();
    $("#p2v-command").hidden = false;
    await renderP2V();
  } catch (err) { fail(err); }
});
$("#p2v-download").addEventListener("click", async () => {
  if (!p2vDownload) return;
  const download = p2vDownload;
  try {
    const r = await fetch(download.url, { headers: { "X-P2V-Token": download.token } });
    if (r.status === 401) return toLogin();
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `ibhics-p2v-${download.sid}.exe`;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (err) { fail(err); }
});
$("#p2v-copy").addEventListener("click", () => navigator.clipboard.writeText($("#p2v-code").textContent).then(() => toast("Command copied")).catch(fail));
$("#p2v-refresh").addEventListener("click", () => renderP2V().catch(fail));
[$("#p2v-rows"), $("#source-physical-machines")].forEach((container) => container.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-p2v-delete]");
  if (!b || !confirm("Remove this session and any disk data it received?")) return;
  del(`/p2v/sessions/${enc(b.dataset.p2vDelete)}`).then(renderP2V).catch(fail);
}));

function logTime(seconds) {
  const n = Math.max(0, Math.floor(seconds || 0));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
}
function importTimeline(data, compact = false) {
  const steps = data.steps || [], job = data.job;
  const active = job.state === "running" || job.state === "queued";
  const shown = compact ? steps.slice(-5) : steps;
  const items = shown.map((step, i) => {
    const index = steps.length - shown.length + i, next = steps[index + 1];
    const current = !next && active;
    const finished = !!next || job.state === "done";
    const elapsed = next ? next.t - step.t : Math.max(0, data.duration - step.t);
    const progress = current && step.kind === "copy" && data.progress?.step === step.text ? data.progress.percent : null;
    return `<li class="${current ? "current" : ""}"><span class="mono">${logTime(step.t)}</span>
      <span>${finished ? "✓ " : (job.state === "failed" && !next ? "✕ " : "")}${esc(step.text)}
      <small class="muted"> · ${logTime(elapsed)}${current ? " elapsed" : ""}</small>
      ${current && step.kind === "copy" ? `<div class="bar${progress == null ? " busy" : ""}" role="progressbar" aria-label="Disk copy" aria-valuemin="0" aria-valuemax="100" ${progress == null ? "" : `aria-valuenow="${progress}"`}><i style="width:${progress ?? 30}%"></i></div><small>${progress == null ? "Waiting for copy progress" : `${progress}%`}</small>` : ""}</span></li>`;
  }).join("");
  return items ? `<ol class="import-timeline">${items}</ol>` : `<p class="hint">${esc(job.step || "No conversion output yet.")}</p>`;
}
function importLogSummary(data) {
  const j = data.job;
  const rows = [["VM", `${j.vm} → ${j.name}`], ["Source", j.source], ["Node / pool", `${j.node} / ${j.pool}`],
    ["Status", JOB_STATE[j.state] || j.state], ["Started", when(j.created)], ["Elapsed", logTime(data.duration)],
    ["Finished", j.finished ? when(j.finished) : "—"], ["Detected OS", data.os || "—"], ["Disks", data.disks || "—"], ["Disk targets", j.disks?.map((d, i) => `Disk ${i + 1}: ${d.pool || j.pool} / ${d.size_gb || "Original"} GiB`).join(", ") || "Original"]];
  const messages = (key, title) => data[key]?.length ? `<section class="import-${key}"><h4>${title}</h4><ul>${data[key].map((v) => `<li>${esc(v)}</li>`).join("")}</ul></section>` : "";
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <h4>Timeline</h4>${importTimeline(data)}${messages("warnings", "Warnings")}${messages("errors", "Errors")}${messages("notes", "Notes")}`;
}
let logTimer = null, logSession = 0;
async function showLog(id, title) {
  const session = ++logSession;
  clearTimeout(logTimer);
  $("#log-title").textContent = `Import log: ${title}`;
  const dlg = $("#log-dialog"), body = $("#import-log-body"), pre = $("#log-text");
  dlg.classList.toggle("mg-dialog", currentPage() === "migrate");
  $("#log-summary").textContent = "Loading…";
  $("#log-fetch-error").textContent = "";
  $("#log-raw").open = false;
  $("#log-all").checked = false;
  pre.textContent = "";
  $("#log-download").href = `/api/v1/migrate/jobs/${enc(id)}/log?download=1`;
  let latest = null, full = null;
  const setRaw = async () => {
    if (!latest) return;
    try {
      if ($("#log-all").checked && full == null) full = (await api(`/migrate/jobs/${enc(id)}/log`)).log;
      if (session !== logSession || !dlg.open) return;
      const top = pre.scrollTop;
      pre.textContent = ($("#log-all").checked ? full : latest.raw) || "No output yet.";
      pre.scrollTop = top;
    } catch (err) { if (session === logSession) $("#log-fetch-error").textContent = err.message; }
  };
  $("#log-all").onchange = setRaw;
  $("#log-raw").ontoggle = () => { if ($("#log-raw").open) setRaw(); };
  const pull = async () => {
    try {
      const data = await api(`/migrate/jobs/${enc(id)}/log?format=summary`);
      if (session !== logSession || !dlg.open) return;
      latest = data; full = null;
      const top = body.scrollTop;
      const html = importLogSummary(data);
      if ($("#log-summary").innerHTML !== html) $("#log-summary").innerHTML = html;
      if ($("#log-raw").open) await setRaw();
      body.scrollTop = top;
      $("#log-fetch-error").textContent = "";
      if (["running", "queued"].includes(data.job.state)) logTimer = setTimeout(pull, 3000);
    } catch (err) {
      if (session !== logSession || !dlg.open) return;
      $("#log-fetch-error").textContent = err.message;
      logTimer = setTimeout(pull, 3000);
    }
  };
  if (!dlg.open) dlg.showModal();
  body.scrollTop = 0;
  pull();
}
$("#log-dialog").addEventListener("close", () => { ++logSession; clearTimeout(logTimer); });
document.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-task-log]");
  if (b) showLog(b.dataset.taskLog, b.dataset.taskTitle);
});
// Poll only the visible Jobs view. Schedule after completion so slow reads never overlap.
let jobsPollTimer = null, jobsPollBusy = false;
function jobsView() {
  if (document.hidden) return "";
  const page = document.body.dataset.section;
  const tab = { tasks: "task-jobs", backup: "jobs", migrate: "imports" }[page];
  return tab && $(`#tab-${tab}`).classList.contains("active") ? page : "";
}
function syncJobsPoll(immediate = false) {
  clearTimeout(jobsPollTimer);
  jobsPollTimer = null;
  if (!jobsView() || jobsPollBusy) return;
  const active = allTasks().some(t => isActive(t) || t.state === "retry_wait");
  jobsPollTimer = setTimeout(pollJobs, immediate ? 0 : active ? 3000 : 20000);
}
async function pollJobs() {
  const page = jobsView();
  if (!page || jobsPollBusy) return;
  jobsPollBusy = true;
  try {
    const names = ["imports", "jobs", "moves", "deploys", "settingsTasks", "replications"];
    if (page === "tasks") names.push("activity");
    // Wait for every read even on partial failure before scheduling another round.
    const results = await Promise.allSettled(names.map(n => load(n)));
    if (jobsView() !== page || results.some(r => r.status === "rejected")) return;
    renderTasks();
    const selectors = [`#page-${page} .table-wrap`, "#task-detail", "#task-log"];
    const scroll = selectors.flatMap(s => $$(s).map((el, i) => [s, i, el.scrollTop, el.scrollLeft]));
    if (page === "tasks") await renderTaskPage();
    if (page === "backup") renderJobs();
    if (page === "migrate") renderImports();
    for (const [s, i, top, left] of scroll) {
      const el = $$(s)[i];
      if (el) { el.scrollTop = top; el.scrollLeft = left; }
    }
  } catch { /* retry next round */ }
  finally { jobsPollBusy = false; syncJobsPoll(); }
}
document.addEventListener("visibilitychange", () => syncJobsPoll(true));
// Elsewhere, keep the task tray fresh while something runs or the tray is open.
setInterval(async () => {
  if (jobsView() || document.hidden) return;
  if (!allTasks().some(isActive) && !$("#tasks-panel").classList.contains("open")) return;
  try { await load("imports", "jobs", "moves", "deploys", "settingsTasks"); renderTasks(); } catch { /* next tick */ }
}, 3000);

async function refresh(page, auto = false) {
  if ($("#twofa-recovery") && !$("#twofa-recovery").hidden) return;
  if (auto && document.querySelector("details.more[open]")) return;
  if (page === "network" && auto && (nw.busy || nwPointerFrom || window.Network3D?.dragging())) return;
  if (page === "network" && ((ntopo.edit && (auto || ntopo.edit.saving)) || (auto && (ntopo.drag || ntopo.changing)))) return;
  if (state.security?.required && !state.security.enabled) {
    selectTab("system-tabs", "account");
    await renderAccountSecurity();
    return;
  }
  await loadCluster();
  await load("imports", "jobs", "moves", "deploys", "settingsTasks").then(renderTasks).catch(() => {});
  if (page === "fabric") { await load("vms", "pools", "networks", "stats", "summaries", "groups"); window.FabricMap?.render(); }
  if (page === "home") { await load("vms", "pools", "targets", "policies", "jobs", "moves", "imports", "users", "summaries"); await loadHomeHistory(); renderHome(); }
  if (page === "compute") { await load("vms", "guests", "stats", "groups", "networks", "pools", "targets", "moves", "recycle"); if (!auto || !document.querySelector("details.more[open]")) { renderVMs(); renderMoves(); } }
  if (page === "vm") { await load("vms", "groups", "pools", "targets", "jobs", "moves", "imports", "guests", "stats", "recycle"); await renderVMDetail(); }
  if (page === "storage") { await load("pools", "vms", "isos"); renderPools(); renderIsos(); await renderVirtualStorage(); }
  if (page === "network") { await load("vms", "networks", "edge", "groups", "guests", "stats"); renderNetworks(); renderEdge(); await renderTopology(); window.Network3D?.update(); }
  if (page === "cluster") { await load("vms", "summaries", "pools"); renderCluster(); }
  if (page === "node") { await load("vms", "pools"); await renderNodeDetail(auto); }
  if (page === "backup") { await load("vms", "targets", "policies", "jobs"); fillSelects(); renderPolicies(); renderJobs(); await protectionOverview(); await renderBackups(); await renderAllSnapshots(); }
  if (page === "migrate") { await load("sources", "imports", "replications", "pools"); fillSelects(); renderSources(); renderImports(); renderReplications(); renderP2V().catch(() => {}); if (!auto || !sourceVMsFor) await renderSourceVMs(); }
  if (page === "templates") { await load("vms", "pools", "deploys", "recycle"); renderTemplates(); }
  if (page === "recycle") { await load("recycle"); renderRecycle(); }
  if (page === "terminal") renderTerminal();
  if (page === "containers") { await load("containers", "ctTemplates", "pools", "networks"); renderContainers(); }
  if (page === "tasks") { await load("imports", "jobs", "moves", "deploys", "settingsTasks", "activity", "vms", "replications"); renderTaskPage(); renderTaskTools(); if (state.me && isAdmin()) renderOps().catch(() => {}); }
  if (page === "activity") { await load("jobs", "moves", "imports", "deploys"); $("#activity-feed").innerHTML = feed(100) || `<li class="hint">Nothing has happened yet.</li>`; }
  if (page === "system") { await load("users"); await renderSystem(); }
  if (page === "alerts") await window.Alerts.refresh();
  fillSelects();
  syncJobsPoll();
}

// ---------- home ----------

function totals() {
  let cpus = 0, mem = 0, sockets = 0, cores = 0, memUsed = 0, cpuBusy = 0, measured = 0, memMeasured = 0;
  for (const m of computeMembers()) {
    const h = state.hosts[m.name], s = state.summaries[m.name];
    if (h) { cpus += h.cpus; mem += h.memory_mb; sockets += h.sockets || 0; cores += h.cores || 0; }
    if (s) {
      if (Number.isFinite(s.memory_used_mb) && s.memory_mb > 0) { memUsed += s.memory_used_mb; memMeasured += s.memory_mb; }
      if (Number.isFinite(s.cpu_percent)) { cpuBusy += (s.cpu_percent / 100) * (h?.cpus || s.threads); measured += h?.cpus || s.threads; }
    }
  }
  const live = state.vms.filter((v) => v.state !== "stopped");
  return {
    cpus, mem, sockets, cores, memUsed, measured, memMeasured,
    cpuPct: measured ? Math.round((cpuBusy / measured) * 100) : null,
    vdisk: [...state.vms, ...(state.templates || [])].reduce((s, v) => s + (v.disks || []).reduce((a, d) => a + (d.size_gb || 0), 0), 0),
    vcpus: live.reduce((s, v) => s + v.vcpus, 0),
    vmem: live.reduce((s, v) => s + v.memory_mb, 0),
    cap: StorageView.pools(state.pools).reduce((s, p) => s + p.capacity_gb, 0),
    used: StorageView.pools(state.pools).reduce((s, p) => s + p.allocation_gb, 0),
  };
}

// History is collected on the server even when no browser is open.
const HISTORY_RANGES = HistoryTime.profiles;
let homeRange = "now", homeMetrics = null, homeHistoryError = "", homeHistoryRequest = 0;
try { const saved = localStorage.getItem("ibhics-history-range"); if (HISTORY_RANGES[saved]) homeRange = saved; } catch { /* private mode */ }
const allocationPct = (used, capacity) => capacity > 0 ? used / capacity * 100 : null;
const percentText = (value) => value == null ? "–" : `${Number(value.toFixed(1))}%`;
async function loadHomeHistory() {
  const range = homeRange, request = ++homeHistoryRequest;
  try {
    const data = await api(`/usage/history?resolution=${enc(range)}&utc_offset=${-new Date().getTimezoneOffset()}`);
    if (request === homeHistoryRequest && range === homeRange) { homeMetrics = data; homeHistoryError = data.note || ""; }
  } catch (err) {
    if (request === homeHistoryRequest) homeHistoryError = `History unavailable: ${err.message}`;
  }
}
$("#usage").addEventListener("change", async (event) => {
  if (event.target.id !== "home-history-range" || !HISTORY_RANGES[event.target.value]) return;
  homeRange = event.target.value;
  try { localStorage.setItem("ibhics-history-range", homeRange); } catch { /* private mode */ }
  await loadHomeHistory();
  if (currentPage() === "home") renderHome();
});

function allocationMeter(label, used, capacity, detail) {
  const value = allocationPct(used, capacity), over = value != null && value > 100;
  return `<div class="allocation-meter${over ? " overallocated" : ""}">
    <div class="allocation-top"><b>${esc(label)}</b><strong>${percentText(value)}</strong></div>
    <span class="allocation-detail">${detail}</span>
    ${over ? `<span class="allocation-warning"><svg aria-hidden="true"><use href="#i-alert"/></svg>Overallocated by ${percentText(value - 100)}</span>` : ""}
    ${value == null ? '<span class="allocation-detail">Capacity unavailable</span>' : ""}</div>`;
}

function resourceActivity(t) {
  const history = homeMetrics?.resolution === homeRange ? homeMetrics : null;
  const end = history?.end || Date.now();
  const start = history?.start || end - HISTORY_RANGES[homeRange].windowMs;
  const duration = Math.max(1, end - start), step = history?.step_ms || 10000;
  const samples = (history?.points || []).filter(s => Number.isFinite(s.at) && s.at >= start && s.at <= end).slice().sort((a,b) => a.at - b.at);
  const ticks = HistoryTime.ticks(start, end, history?.tick_ms || HISTORY_RANGES[homeRange].tickMs, history?.utc_offset ?? -new Date().getTimezoneOffset());
  const timeLabel = at => ["1d", "7d"].includes(homeRange)
    ? new Date(at).toLocaleDateString(undefined, {month:"short",day:"numeric"})
    : new Date(at).toLocaleTimeString(undefined, {hour:"2-digit",minute:"2-digit",hour12:false});
  const x = at => (at - start) / duration * 1000;
  const y = value => 240 - Math.min(100, Math.max(0, value)) * 2.4;
  const series = [
    {key:"cpu",label:"Compute",value:t.measured ? t.cpuPct : null},
    {key:"memory",label:"Memory",value:allocationPct(t.memUsed,t.memMeasured)},
  ];
  const paths = series.map(({key,label}) => {
    const groups = []; let group = null;
    for (const sample of samples) {
      if (!Number.isFinite(sample[key])) { group = null; continue; }
      if (!group || sample.at - group.at(-1).at > step * 2.5) { group = []; groups.push(group); }
      group.push(sample);
    }
    const line = g => g.map((s,i) => `${i ? "L" : "M"}${x(s.at).toFixed(2)},${y(s[key]).toFixed(2)}`).join(" ");
    return `<g class="activity-series activity-${key}">${groups.map(g => {
      const last = g.at(-1);
      return (g.length > 1 ? `<path class="activity-area" d="${line(g)} L${x(last.at).toFixed(2)},240 L${x(g[0].at).toFixed(2)},240 Z" fill="url(#activity-fill-${key})"/><path class="activity-line" d="${line(g)}"/>` : "") + `<circle class="activity-end" cx="${x(last.at).toFixed(2)}" cy="${y(last[key]).toFixed(2)}" r="3.5"/>`;
    }).join("")}${samples.filter(s => Number.isFinite(s[key])).map(s => `<circle class="activity-hit" cx="${x(s.at).toFixed(2)}" cy="${y(s[key]).toFixed(2)}" r="7"><title>${esc(label)} · ${esc(new Date(s.first_sample ?? s.at).toLocaleString())}${s.samples > 1 ? ` – ${esc(new Date(s.last_sample ?? s.at).toLocaleTimeString())} · average` : ""}: ${percentText(s[key])}</title></circle>`).join("")}</g>`;
  }).join("");
  const count = samples.filter(s => Number.isFinite(s.cpu) || Number.isFinite(s.memory)).length;
  return `<article class="resource-activity" aria-labelledby="resource-activity-title">
    <header class="activity-heading"><h3 id="resource-activity-title">Resource activity</h3><span>${esc(HISTORY_RANGES[homeRange].windowLabel)}</span></header>
    <div class="activity-legend">${series.map(s => `<span class="activity-${s.key}"><i aria-hidden="true"></i>${s.label}<b>${percentText(s.value)}</b><small>${samples.some(p => Number.isFinite(p[s.key])) ? "" : "No history"}</small></span>`).join("")}<span class="activity-unit">Usage (%)${step > 10000 ? " · average" : ""}</span></div>
    <div class="activity-chart-wrap"><div class="activity-y-axis" aria-hidden="true">${[100,75,50,25,0].map(p => `<span style="top:${100-p}%">${p}%</span>`).join("")}</div>
      <div class="activity-plot"><svg viewBox="0 0 1000 240" preserveAspectRatio="none" role="img" aria-label="Compute and memory usage from 0 to 100 percent over ${esc(HISTORY_RANGES[homeRange].windowLabel)}. Gaps indicate missing measurements.">
        <defs>${series.map(s => `<linearGradient id="activity-fill-${s.key}" x1="0" y1="0" x2="0" y2="1"><stop stop-color="${s.key === "cpu" ? "#54e8f0" : "#ed99da"}" stop-opacity="${s.key === "cpu" ? ".19" : ".07"}"/><stop offset="1" stop-color="${s.key === "cpu" ? "#54e8f0" : "#ed99da"}" stop-opacity="0"/></linearGradient>`).join("")}</defs>
        <g class="activity-grid">${[0,25,50,75,100].map(p => `<path d="M0 ${y(p)}H1000"/>`).join("")}${ticks.map(at => `<path d="M${x(at).toFixed(2)} 0V240"/>`).join("")}</g>${paths}
      </svg>${count < 2 ? `<div class="activity-empty">${homeHistoryError ? "History unavailable" : count ? "First recorded reading" : "No recorded data in this period"}</div>` : ""}
      <div class="activity-times" aria-hidden="true">${ticks.map(at => `<span style="--tick:${(x(at)/10).toFixed(3)}%" data-at="${at}">${esc(timeLabel(at))}</span>`).join("")}</div>
      </div>
    </div>
    <footer class="activity-footer"><span>Saved on server · 30-day retention · ${step > 10000 ? `Averaged samples` : "Sample every 10 sec"}</span><a href="#storage">Storage <b>${percentText(allocationPct(t.used,t.cap))}</b> <span>${t.cap ? `${num(t.used)} / ${num(t.cap)} GB` : "Capacity unavailable"}</span></a></footer>
  </article>`;
}

function activities() {
  const items = [
    ...state.jobs.map((j) => ({ kind: JOB_KINDS[j.kind] || "Backup", icon: "i-backup", what: j.vm, where: j.target, state: j.state, created: j.created, error: j.error })),
    ...state.moves.map((m) => ({ kind: "Live migration", icon: "i-migrate", what: m.vm, where: `${m.source} → ${m.target || "?"}`, state: m.state, created: m.created, error: m.error })),
    ...state.imports.map((j) => ({ kind: "Import", icon: "i-compute", what: j.name, where: j.source, state: j.state, created: j.created, error: j.error })),
    ...state.deploys.map((d) => ({ kind: "Deploy VM", icon: "i-template", what: d.vm, where: `from ${d.source}`, state: d.state, created: d.created, error: d.error })),
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
  $("#command-health").innerHTML = `<span class="dot ${mood[0]}"></span>${esc(mood[1] + mood[2])}`;
  const metric = (label, value, unit, note, href) => `<a class="command-kpi" href="#${href}"><span>${esc(label)}</span><strong>${esc(value)}<small>${esc(unit)}</small></strong><em>${esc(note)}</em></a>`;
  $("#command-kpis").innerHTML = [
    metric("Virtual machines", state.vms.length, "VMs", `${running} running · ${state.vms.length - running} stopped`, "compute"),
    metric("CPU usage", t.measured ? num(t.cpuPct) : "—", "%", t.measured ? `${t.measured} hardware threads measured` : "Waiting for node metrics", "cluster"),
    metric("Memory usage", t.memMeasured ? num(allocationPct(t.memUsed, t.memMeasured)) : "—", "%", t.memMeasured ? `${gbText(t.memUsed)} / ${gbText(t.memMeasured)}` : "Waiting for node metrics", "cluster"),
    metric("Storage usage", t.cap ? num(storagePct) : "—", "%", t.cap ? `${num(t.used)} / ${num(t.cap)} GB` : "No storage capacity reported", "storage"),
  ].join("");
  if ($("#health")) $("#health").innerHTML = `${esc(mood[1])}<em class="${mood[0]}">${esc(mood[2])}</em>`;

  $("#home-cluster-name").textContent = c.name || "Standalone server";
  const cs = $("#cluster-state");
  cs.textContent = c.quorum ? (c.ha_capable ? "Highly available" : "Healthy") : "No quorum";
  cs.className = `val ${c.quorum ? "ok" : "bad"}`;
  const row = (k, v) => `<div class="row"><span>${esc(k)}</span><b>${v}</b></div>`;
  $("#cluster-box").innerHTML = [
    row("Deployment", c.cluster_id ? `Cluster · ${esc(c.name)} (${c.members.length} node${c.members.length === 1 ? "" : "s"})` : esc(TOPOLOGY[c.topology] || c.topology)),
    row("High availability", c.ha_capable ? "Yes" : "No"),
    row("Members online", `${members.length - offline.length} / ${members.length}`),
    row("Quorum", `<span class="dot ${c.quorum ? "ok" : "bad"}"></span> ${c.quorum ? "OK" : "Lost"}`),
    row("Processors", t.cores ? `${t.sockets} socket${t.sockets === 1 ? "" : "s"} · ${t.cores} cores · ${t.cpus} threads` : `${t.cpus} threads`),
    row("Physical memory", gbText(t.mem)),
    `<div class="hint">${c.ha_capable ? "VMs can be moved between nodes while running." : "Add a second compute node and a witness for high availability."}</div>`,
  ].join("");

  const infra = $("#infra-state");
  const infraLevel = offline.length || storagePct >= 90 ? "bad" : storagePct >= 75 || failed.length ? "warn" : "ok";
  infra.textContent = { ok: "Excellent", warn: "Fair", bad: "Poor" }[infraLevel];
  infra.className = `val ${infraLevel}`;
  const computes = computeMembers();
  $("#topology-strip").innerHTML = [
    topo("t-node", computes.length, "Nodes", `${computes.filter((m) => m.healthy).length} online`, "cluster"),
    topo("t-storage", StorageView.pools(state.pools).length, "Storage pools", `${storagePct}% used`, "storage", true),
    topo("t-vm", state.vms.length, "Virtual machines", `${running} running`, "compute"),
  ].join("");

  const partial = t.measured < t.cpus || t.memMeasured < t.mem;
  $("#usage").innerHTML = `<div class="usage-row"><h4>Allocated to running VMs</h4>${[
    allocationMeter("vCPU", t.vcpus, t.cpus, `${t.vcpus} vCPU / ${t.cpus} hardware threads`),
    allocationMeter("Memory", t.vmem, t.mem, `${gbText(t.vmem)} / ${gbText(t.mem)}`),
    allocationMeter("VM disks", t.vdisk, t.cap, `${num(t.vdisk)} / ${num(t.cap)} GB · all VMs &amp; templates, thin provisioned`),
  ].join("")}</div><div class="usage-row actual-usage"><h4>Actual use now <span>Physical resources in use${partial ? " · Some node metrics unavailable" : ""}</span></h4><div class="history-controls"><label for="home-history-range">Resolution</label><select id="home-history-range">${Object.entries(HISTORY_RANGES).map(([key, {label}]) => `<option value="${key}" ${homeRange === key ? "selected" : ""}>${esc(label)}</option>`).join("")}</select><span class="hint">${esc(HISTORY_RANGES[homeRange].windowLabel)} · Time grid: every ${homeRange === "now" || homeRange === "5m" ? "5 min" : homeRange === "30m" ? "30 min" : homeRange === "1d" ? "day" : "7 days"} · Kept for 30 days${homeMetrics?.storage_bytes ? ` · ${homeMetrics.storage_bytes < 1048576 ? `${(homeMetrics.storage_bytes / 1024).toFixed(0)} KB` : `${(homeMetrics.storage_bytes / 1048576).toFixed(1)} MB`} stored` : ""}${homeMetrics?.recording_since ? ` · Recording since ${esc(new Date(homeMetrics.recording_since).toLocaleString())}` : ""}${homeHistoryError ? ` · ${esc(homeHistoryError)}` : ""}</span></div>${resourceActivity(t)}</div>`;

  $("#home-nodes").innerHTML = members.map((m) => {
    const h = state.hosts[m.name];
    const vms = state.vms.filter((v) => v.node === m.name && v.state !== "stopped");
    // Actual use when the node reports it; otherwise what its VMs are given.
    const s = state.summaries[m.name];
    const cpu = s ? Math.round(s.cpu_percent) : h ? pct(vms.reduce((a, v) => a + v.vcpus, 0), h.cpus) : 0;
    const mem = s ? pct(s.memory_used_mb, s.memory_mb) : h ? pct(vms.reduce((a, v) => a + v.memory_mb, 0), h.memory_mb) : 0;
    const usage = m.role === "compute" && h
      ? `<div class="mini">CPU ${cpu}%${bar(cpu)}</div><div class="mini">Memory ${mem}%${bar(mem)}</div>`
      : `<div class="mini">${m.role === "witness" ? "Quorum vote only" : "No data"}</div><div></div>`;
    return `<div class="node-row">${avatar("i-nodes")}
      <div><a class="vm-name vm-link" href="#node/${enc(m.name)}">${esc(nodeLabel(m.name))}</a> <span class="dot ${m.healthy ? "ok" : "bad"}"></span>
      <span class="vm-sub">${esc(m.role)}${m.local ? " · manager" : ""} · ${esc(m.address || "local")}</span></div>${usage}</div>`;
  }).join("");
  $("#home-feed").innerHTML = feed(6) || `<li class="hint">No jobs yet. Backups, migrations and imports show up here.</li>`;
}

// ---------- compute ----------

// What the guest says about itself wins over the label the admin typed in.
const osName = (vm, g) => (g?.agent && g.os_name) || vm.os_type || "";
const osKind = (name) => (/windows|^win/i.test(name) ? "Windows" : name ? "Linux" : "");
const ipList = (g) => g?.ips || [];
const num = (gb) => (gb >= 100 ? Math.round(gb) : Math.round(gb * 10) / 10).toLocaleString();

// Space really in use. The guest's own filesystems are the truth when its agent runs;
// otherwise the pool's allocation, which only tells us anything for thin disks.
function diskUsage(vm, g) {
  const total = vm.disks.reduce((sum, d) => sum + d.size_gb, 0);
  if (g?.agent && g.fs_used_gb != null) return { used: g.fs_used_gb, total, from: "inside the guest" };
  const thin = vm.disks.filter((d) => d.used_gb != null && d.used_gb < d.size_gb);
  if (thin.length === vm.disks.length && thin.length) {
    return { used: thin.reduce((sum, d) => sum + d.used_gb, 0), total, from: "allocated in the pool" };
  }
  return { used: null, total, from: "" };
}
function diskCell(vm, g) {
  const u = diskUsage(vm, g), pools = [...new Set(vm.disks.map((d) => d.pool))].join(", ");
  if (u.used == null) return `${num(u.total)} GB<span class="vm-sub">${esc(pools)}</span>`;
  return `${num(u.used)} <span class="muted">/ ${num(u.total)} GB</span>${usageBar(u)}<span class="vm-sub">${esc(pools)}</span>`;
}
const usageBar = (u) => {
  const pct = Math.min(100, Math.round((u.used / u.total) * 100));
  return `<div class="bar${pct > 90 ? " bad" : pct > 75 ? " warn" : ""}" title="${pct}% used, ${esc(u.from)}"><i style="width:${pct}%"></i></div>`;
};

function vmMenu(vm, { summary = false, compact = false } = {}) {
  if (!canOperate()) return summary && !compact ? `<a class="btn small" href="#vm/${vm.uuid}">Summary</a>` : "";
  const multi = computeMembers().length > 1;
  return `
    ${compact ? "" : summary ? `<a class="btn small" href="#vm/${vm.uuid}">Summary</a>` : `<button class="small" data-vm="edit" data-id="${vm.uuid}">Settings</button>`}
    ${vm.state === "running"
      ? `<button class="small${compact ? " sq" : ""}" data-vm="console" data-id="${vm.uuid}" data-name="${esc(vm.name)}" title="Open console">${compact ? `<svg><use href="#i-compute"/></svg>` : "Console"}</button>`
      : `<button class="small${compact ? " sq" : ""}" data-vm="start" data-id="${vm.uuid}" title="${vm.state === "paused" ? "Resume after storage recovery" : "Power on"}">${compact ? `<svg><use href="#i-power"/></svg>` : vm.state === "paused" ? "Resume" : "Power on"}</button>`}
    ${compact || vm.template ? "" : `<button class="small" data-vm="snapshot" data-id="${vm.uuid}" title="Take a snapshot">Snapshot</button>`}
    <details class="more"><summary class="small" title="More actions">⋯</summary><div class="dropdown open">
      ${vm.state === "running" ? `<button data-vm="stop" data-id="${vm.uuid}">Shut down</button>
        <button data-vm="restart" data-id="${vm.uuid}">Restart</button>
        <button data-vm="poweroff" data-id="${vm.uuid}">Power off</button>` : ""}
      ${vm.state === "paused" ? `<button data-vm="console" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Console</button><button data-vm="poweroff" data-id="${vm.uuid}">Power off</button>` : ""}
      ${vm.template ? "" : `<button data-vm="migrate" data-id="${vm.uuid}" data-name="${esc(vm.name)}" data-node="${esc(vm.node)}">Migrate… (host or storage)</button>`}
      ${multi && !vm.template ? `<button data-vm="ha" data-id="${vm.uuid}" data-name="${esc(vm.name)}" title="Start this VM on another node if its node fails (shared storage only)">${state.ha?.has(vm.uuid) ? "Turn off HA restart" : "Turn on HA restart"}</button>` : ""}
      ${summary ? `<button data-vm="edit" data-id="${vm.uuid}">Settings</button>` : ""}
      <button data-vm="snapshot" data-id="${vm.uuid}">Take snapshot…</button>
      <button data-vm="snapshots" data-id="${vm.uuid}">Restore snapshot…</button>
      <button data-vm="backup" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Back up now</button>
      <button data-vm="tools" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Insert guest tools CD</button>
      ${vm.iso ? `<button data-vm="eject" data-id="${vm.uuid}" title="Eject ${esc(vm.iso.split("/").pop())}">Eject CD (${esc(vm.iso.split("/").pop())})</button>` : ""}
      <button data-vm="rename" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Rename…</button>
      ${(vm.disks || []).some((d) => d.bus === "sata") && /windows/i.test(vm.os_type || "") ? `<button data-vm="tovirtio" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Switch disks to VirtIO…</button>` : ""}
      ${vm.state === "running" && /windows/i.test(vm.os_type || "") ? `<button data-vm="sysprep" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Generalize (sysprep) for a template…</button>` : ""}
      ${vm.state === "stopped" ? `<button data-vm="clone" data-id="${vm.uuid}">Clone…</button>
        <button data-vm="totemplate" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Convert to template</button>` : ""}
      ${vm.state === "stopped" ? `<button class="danger" data-vm="delete" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Delete</button>` : ""}
    </div></details>`;
}

// ---------- VM list: groups, metric views, batch actions, alerts ----------

const vmp = { group: "all", metric: "resources", view: "list", picked: new Set(), closed: new Set(), sort: { key: "", dir: 1 } };
try {
  const saved = JSON.parse(localStorage.getItem("ibhics.vmpage") || "{}");
  if (["list", "panel"].includes(saved.view)) vmp.view = saved.view;
  if (saved.metric) vmp.metric = saved.metric;
} catch { /* private window */ }
const vmpSave = () => { try { localStorage.setItem("ibhics.vmpage", JSON.stringify({ view: vmp.view, metric: vmp.metric })); } catch { /* ignore */ } };

const groupList = () => state.groups.groups || [];
const groupById = (id) => groupList().find((g) => g.id === id);
const groupOf = (vm) => { const id = (state.groups.members || {})[vm.uuid]; return id && groupById(id) ? id : ""; };
const childGroups = (parent) => groupList().filter((g) => g.parent === parent).sort((a, b) => a.name.localeCompare(b.name));
function groupAndBelow(id) {
  const out = new Set([id]);
  for (const g of childGroups(id)) groupAndBelow(g.id).forEach((x) => out.add(x));
  return out;
}
function groupPath(id) {
  const names = [];
  for (let g = groupById(id); g; g = groupById(g.parent)) names.unshift(g.name);
  return names.join(" / ");
}
function inGroup(vm, group) {
  if (group === "all") return true;
  if (group === "none") return !groupOf(vm);
  return groupAndBelow(group).has(groupOf(vm));
}

// What needs a look, like a monitoring view would flag it.
function vmAlerts(vm) {
  const s = state.stats[vm.uuid], g = state.guests[vm.uuid], out = [];
  if (vm.state === "running" && s) {
    if (s.cpu_pct != null && s.cpu_pct >= 90) out.push(`CPU ${Math.round(s.cpu_pct)}%`);
    if (s.mem_used_mb != null && pct(s.mem_used_mb, vm.memory_mb) >= 90) out.push(`Memory ${pct(s.mem_used_mb, vm.memory_mb)}%`);
  }
  const u = diskUsage(vm, g);
  if (u.used != null && pct(u.used, u.total) >= 90) out.push(`Disk ${pct(u.used, u.total)}% full`);
  if (vm.state === "running" && g && !g.agent) out.push("No guest agent");
  if (vm.pause_reason === "storage_io") out.push("Paused: storage I/O interrupted. Check storage before Resume.");
  if (vm.storage_error_protection === "pending_restart") out.push("Storage error protection needs power off/on");
  else if (vm.storage_error_protection === "not_configured") out.push("Storage error protection not configured");
  if (vm.restart_needed) out.push("Restart to apply changes");
  return out;
}

const rate = (bps) => (bps ? `${size(bps)}/s` : `<span class="muted">0</span>`);
const usageCell = (used, total, unit = "") => {
  if (used == null) return `<span class="muted">–</span>`;
  const p = pct(used, total);
  return `<div class="u-cell"><span>${p}%${unit ? ` <span class="muted">${unit}</span>` : ""}</span>${bar(p)}</div>`;
};
// VM card visuals: three concentric light rings (CPU outside, memory, disk inside),
// a stat list beside them, and a wave of the VM's recent CPU and network activity.
const vxHistory = {};
const vxLevel = (p) => (p > 90 ? "bad" : p > 75 ? "warn" : "");
function vxRings(cpu, mem, disk) {
  const ring = (r, value, cls) => {
    const c = 2 * Math.PI * r, p = Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
    return `<circle class="vx-ring-track" cx="60" cy="60" r="${r}"/><circle class="vx-ring ${cls} ${vxLevel(p)}" cx="60" cy="60" r="${r}" stroke-dasharray="${((p / 100) * c).toFixed(1)} ${c.toFixed(1)}"/>`;
  };
  const ticks = Array.from({ length: 36 }, (_, i) => {
    const a = (i / 36) * Math.PI * 2, r1 = 57, r2 = i % 3 ? 55 : 52;
    return `<line x1="${(60 + Math.cos(a) * r1).toFixed(1)}" y1="${(60 + Math.sin(a) * r1).toFixed(1)}" x2="${(60 + Math.cos(a) * r2).toFixed(1)}" y2="${(60 + Math.sin(a) * r2).toFixed(1)}"/>`;
  }).join("");
  const big = Number.isFinite(cpu) ? Math.round(cpu) : "–";
  return `<div class="vx-rings"><svg viewBox="0 0 120 120" aria-hidden="true"><g class="vx-ticks">${ticks}</g><g transform="rotate(-90 60 60)">${ring(46, cpu, "cpu")}${ring(36, mem, "mem")}${ring(26, disk, "disk")}</g></svg>
    <div class="vx-core"><b>${big}${Number.isFinite(cpu) ? "<small>%</small>" : ""}</b><span>CPU</span></div></div>`;
}
const vxStat = (label, value, size, cls) => {
  const known = Number.isFinite(value), p = known ? Math.max(0, Math.min(100, value)) : 0;
  return `<div class="vx-stat ${cls} ${vxLevel(p)}"><i></i><span>${esc(label)}</span><b>${known ? `${Math.round(p)}%` : "–"}</b><em>${esc(size)}</em><div class="vx-meter"><u style="width:${p}%"></u></div></div>`;
};
function vxWave(vm) {
  const h = vxHistory[vm.uuid] || [];
  if (vm.state !== "running" || h.length < 2) return `<div class="vx-wave empty"><span>${vm.state === "running" ? "Collecting activity…" : "No activity while off"}</span></div>`;
  const W = 300, H = 46, n = h.length, maxNet = Math.max(1, ...h.map((p) => p.net || 0));
  const pts = (key, scale) => h.map((p, i) => `${((i / (n - 1)) * W).toFixed(1)},${(H - 3 - Math.min(1, (p[key] || 0) / scale) * (H - 8)).toFixed(1)}`);
  const cpuPts = pts("cpu", 100), netPts = pts("net", maxNet);
  return `<div class="vx-wave"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id="vxwave-${vm.uuid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#54e8f0" stop-opacity=".45"/><stop offset="1" stop-color="#54e8f0" stop-opacity="0"/></linearGradient></defs>
    <path class="vx-wave-area" d="M0,${H} L${cpuPts.join(" L")} L${W},${H} Z" fill="url(#vxwave-${vm.uuid})"/>
    <polyline class="vx-wave-cpu" points="${cpuPts.join(" ")}"/><polyline class="vx-wave-net" points="${netPts.join(" ")}"/></svg>
    <span class="vx-wave-key"><i class="c"></i>CPU <i class="n"></i>Network · last ${Math.round((h.at(-1).at - h[0].at) / 1000)} s</span></div>`;
}
// OS badge for the VM cards and list: Windows, Linux or unknown look clearly different.
const VX_OS_ART = { // inline styles: the console's global svg rules must not repaint them
  Windows: `<svg viewBox="0 0 32 32" aria-hidden="true"><path style="fill:#8fdcff;stroke:none" d="M4 7.2 14 5.8v9.4H4z"/><path style="fill:#4aa8ff;stroke:none" d="M15.6 5.6 28 3.8v11.4H15.6z"/><path style="fill:#4aa8ff;stroke:none" d="M4 16.8h10v9.4L4 24.8z"/><path style="fill:#2f7dff;stroke:none" d="M15.6 16.8H28v11.4l-12.4-1.8z"/></svg>`,
  Linux: `<svg viewBox="0 0 32 32" aria-hidden="true"><ellipse style="fill:#16182b;stroke:none" cx="16" cy="17.5" rx="9.5" ry="11.5"/><ellipse style="fill:#f4f6ff;stroke:none" cx="16" cy="20" rx="6.2" ry="8"/><circle style="fill:#fff;stroke:none" cx="12.8" cy="10.4" r="2.2"/><circle style="fill:#fff;stroke:none" cx="19.2" cy="10.4" r="2.2"/><circle style="fill:#16182b;stroke:none" cx="13.3" cy="10.6" r="1.05"/><circle style="fill:#16182b;stroke:none" cx="18.7" cy="10.6" r="1.05"/><path style="fill:#ffc23d;stroke:none" d="M12.9 13.4h6.2L16 16.2z"/><path style="fill:#ffc23d;stroke:none" d="M7.4 29.2c1.3-3 4.8-2.8 6-.4-1.6 1.6-4.4 1.8-6 .4zm17.2 0c-1.3-3-4.8-2.8-6-.4 1.6 1.6 4.4 1.8 6 .4z"/></svg>`,
  "": `<svg viewBox="0 0 32 32" aria-hidden="true"><rect style="fill:none;stroke:#9fb0d6;stroke-width:2" x="5" y="6" width="22" height="15" rx="2.5"/><path style="fill:none;stroke:#9fb0d6;stroke-width:2;stroke-linecap:round" d="M12 26h8M16 21v5"/></svg>`,
};
const vxOs = (vm) => {
  const kind = osKind(osName(vm, state.guests[vm.uuid]));
  return `<span class="vx-os os-${kind ? kind.toLowerCase() : "other"} ${esc(vm.state)}" title="${esc(kind || "Operating system not reported")}">${VX_OS_ART[kind] || VX_OS_ART[""]}</span>`;
};
const osIcon = (vm) => { const k = osKind(osName(vm, state.guests[vm.uuid])); return k === "Windows" ? "i-os-win" : k === "Linux" ? "i-os-linux" : "i-compute"; };
const ipCell = (vm) => {
  const ips = ipList(state.guests[vm.uuid]);
  return ips.length ? `<span class="mono">${esc(ips[0])}</span>${ips.length > 1 ? ` <span class="muted" title="${esc(ips.join(", "))}">+${ips.length - 1}</span>` : ""}` : `<span class="muted">–</span>`;
};
const cpuUsed = (vm) => (vm.state === "running" ? state.stats[vm.uuid]?.cpu_pct : null);
const memUsed = (vm) => (vm.state === "running" ? state.stats[vm.uuid]?.mem_used_mb : null);
const diskTotal = (vm) => vm.disks.reduce((s, d) => s + d.size_gb, 0);

// Columns of each metric view: header, sort key, cell.
const VM_COLUMNS = {
  resources: [
    ["IP address", (v) => ipList(state.guests[v.uuid])[0] || "", ipCell],
    ["vCPU", (v) => v.vcpus, (v) => String(v.vcpus)],
    ["CPU usage", (v) => cpuUsed(v) ?? -1, (v) => usageCell(cpuUsed(v), 100)],
    ["Memory", (v) => v.memory_mb, (v) => gbText(v.memory_mb)],
    ["Memory used", (v) => (memUsed(v) ?? -1) / v.memory_mb, (v) => usageCell(memUsed(v), v.memory_mb, memUsed(v) != null ? gbText(memUsed(v)) : "")],
    ["Storage", diskTotal, (v) => `${num(diskTotal(v))} GB`],
    ["Storage used", (v) => { const u = diskUsage(v, state.guests[v.uuid]); return u.used == null ? -1 : u.used / u.total; },
      (v) => { const u = diskUsage(v, state.guests[v.uuid]); return usageCell(u.used, u.total, u.used != null ? `${num(u.used)} GB` : ""); }],
  ],
  basics: [
    ["Operating system", (v) => osName(v, state.guests[v.uuid]), (v) => { const os = osName(v, state.guests[v.uuid]); return os ? esc(os) : `<span class="muted">Not set</span>`; }],
    ["IP address", (v) => ipList(state.guests[v.uuid])[0] || "", ipCell],
    ["Hostname", (v) => state.guests[v.uuid]?.hostname || "", (v) => esc(state.guests[v.uuid]?.hostname || "–")],
    ["Group", (v) => groupPath(groupOf(v)), (v) => (groupOf(v) ? `<span class="g-tag">${esc(groupPath(groupOf(v)))}</span>` : `<span class="muted">Ungrouped</span>`)],
    ["Guest agent", (v) => (state.guests[v.uuid]?.agent ? 1 : 0), (v) => (state.guests[v.uuid]?.agent ? "Running" : `<span class="muted">${v.state === "running" ? "Not installed" : "–"}</span>`)],
    ["Description", (v) => v.description || "", (v) => `<span class="clip" title="${esc(v.description)}">${esc(v.description || "–")}</span>`],
  ],
  node: [
    ["Node", (v) => nodeLabel(v.node || ""), (v) => `<a href="#node/${enc(v.node || "")}" title="Node ID: ${esc(v.node || "")}">${esc(nodeLabel(v.node || ""))}</a>`],
    ["Node load", (v) => v.node || "", (v) => {
      const h = state.hosts[v.node];
      if (!h) return `<span class="muted">–</span>`;
      const busy = state.vms.filter((x) => x.node === v.node && x.state === "running").reduce((s, x) => s + x.vcpus, 0);
      return `<span class="vm-sub" style="display:block">${busy} of ${h.cpus} vCPU in use</span>`;
    }],
    ["Storage pools", (v) => v.disks.map((d) => d.pool).join(), (v) => esc([...new Set(v.disks.map((d) => d.pool))].join(", ") || "–")],
    ["Networks", (v) => (v.nics || []).map((n) => n.network).join(), (v) => esc((v.nics || []).map((n) => netName(n.network)).join(", ") || "–")],
    ["CD/DVD", (v) => v.iso || "", (v) => (v.iso ? esc(v.iso.split("/").pop()) : `<span class="muted">Empty</span>`)],
  ],
  throughput: [
    ["IP address", (v) => ipList(state.guests[v.uuid])[0] || "", ipCell],
    ["Networks", (v) => (v.nics || []).map((n) => n.network).join(), (v) => esc((v.nics || []).map((n) => netName(n.network)).join(", ") || "–")],
    ["Received", (v) => state.stats[v.uuid]?.net_rx_bps || 0, (v) => rate(state.stats[v.uuid]?.net_rx_bps)],
    ["Sent", (v) => state.stats[v.uuid]?.net_tx_bps || 0, (v) => rate(state.stats[v.uuid]?.net_tx_bps)],
  ],
  io: [
    ["Disks", (v) => v.disks.length, (v) => `${v.disks.length} · ${num(diskTotal(v))} GB`],
    ["Storage pools", (v) => v.disks.map((d) => d.pool).join(), (v) => esc([...new Set(v.disks.map((d) => d.pool))].join(", ") || "–")],
    ["Read", (v) => state.stats[v.uuid]?.disk_read_bps || 0, (v) => rate(state.stats[v.uuid]?.disk_read_bps)],
    ["Write", (v) => state.stats[v.uuid]?.disk_write_bps || 0, (v) => rate(state.stats[v.uuid]?.disk_write_bps)],
  ],
};
// The network a NIC is on, by its console name (VLAN N), whatever libvirt calls it.
function netName(id) {
  const n = state.networks.find((x) => x.name === id);
  return n ? `${n.name} (VLAN ${vlanId(n)})` : id;
}

function visibleVMs() {
  const q = $("#vm-search").value.trim().toLowerCase();
  const f = $("#vm-filter").value;
  const hay = (v) => [v.name, v.description, osName(v, state.guests[v.uuid]), state.guests[v.uuid]?.hostname, ...ipList(state.guests[v.uuid]), groupPath(groupOf(v))].join(" ").toLowerCase();
  let vms = state.vms.filter((v) => inGroup(v, vmp.group) && (!q || hay(v).includes(q))
    && (!f || (f === "alert" ? vmAlerts(v).length : v.state === f)));
  const { key, dir } = vmp.sort;
  const col = (VM_COLUMNS[vmp.metric] || []).find((c) => c[0] === key);
  const by = key === "Name" ? (v) => v.name.toLowerCase() : key === "Status" ? (v) => v.state : col ? col[1] : null;
  if (by) vms = [...vms].sort((a, b) => { const x = by(a), y = by(b); return (x > y ? 1 : x < y ? -1 : 0) * dir; });
  return vms;
}

function renderGroupTree() {
  if ($("#vg-tree details[open]")) return; // keep an open menu open across refreshes
  const count = (g) => state.vms.filter((v) => inGroup(v, g)).length;
  const op = canOperate();
  const row = (id, name, icon, depth, extra = "") => {
    const kids = id !== "all" && id !== "none" ? childGroups(id) : [];
    const caret = kids.length ? `<span class="vg-caret${vmp.closed.has(id) ? " shut" : ""}" data-toggle="${esc(id)}"><svg><use href="#i-chev"/></svg></span>` : `<span class="vg-caret none"></span>`;
    return `<li><div class="vg-row${vmp.group === id ? " active" : ""}" data-group="${esc(id)}" style="--d:${depth}">
      ${caret}<svg class="vg-icon"><use href="#${icon}"/></svg><span class="vg-name" title="${esc(name)}">${esc(name)}</span><span class="vg-n">${count(id)}</span>${extra}</div></li>`;
  };
  const menu = (g) => (op ? `<details class="more vg-menu"><summary title="Group actions">⋯</summary><div class="dropdown open">
      <button type="button" data-gact="sub" data-id="${esc(g.id)}">New group inside</button>
      <button type="button" data-gact="edit" data-id="${esc(g.id)}">Rename or move</button>
      <button type="button" class="danger" data-gact="del" data-id="${esc(g.id)}">Delete group</button></div></details>` : "");
  const branch = (parent, depth) => childGroups(parent).map((g) =>
    row(g.id, g.name, "i-folder", depth, menu(g)) + (vmp.closed.has(g.id) ? "" : branch(g.id, depth + 1))).join("");
  $("#vg-tree").innerHTML = row("all", "All VMs", "i-compute", 0) + branch("", 0) +
    (groupList().length ? row("none", "Ungrouped", "i-folder", 0) : "") +
    (!groupList().length && op ? `<li class="vg-empty">Sort VMs into folders, such as Production or Branch offices. <a href="#" id="vg-first">New group</a></li>` : "");
}

function renderVMAlert() {
  const bad = state.vms.filter((v) => inGroup(v, vmp.group) && vmAlerts(v).length);
  const box = $("#vm-alert"), only = $("#vm-filter").value === "alert";
  box.hidden = !bad.length && !only;
  box.innerHTML = only
    ? `<svg><use href="#i-alert"/></svg><span>Showing the ${bad.length} VM${bad.length === 1 ? " that needs" : "s that need"} attention</span><a href="#" data-alert="all">Show all VMs</a>`
    : `<svg><use href="#i-alert"/></svg><span><b>${bad.length}</b> virtual machine${bad.length === 1 ? " needs" : "s need"} attention: ${esc([...new Set(bad.flatMap(vmAlerts).map((a) => a.replace(/ \d+%.*/, "")))].join(", "))}</span><a href="#" data-alert="view">View</a>`;
}

function renderVMs() {
  for (const id of [...vmp.picked]) if (!state.vms.some((v) => v.uuid === id)) vmp.picked.delete(id);
  if (vmp.group !== "all" && vmp.group !== "none" && !groupById(vmp.group)) vmp.group = "all";
  const running = state.vms.filter((v) => v.state === "running").length;
  $("#vm-count").textContent = `${state.vms.length} total · ${running} running`;
  renderGroupTree();
  $("#vg-bin-n").textContent = state.recycle.vms.length || "";
  renderVMAlert();
  $$("#vm-view button").forEach((b) => b.classList.toggle("on", b.dataset.view === vmp.view));
  $$("#vm-metrics button").forEach((b) => b.classList.toggle("active", b.dataset.metric === vmp.metric));
  $("#vm-metrics").hidden = vmp.view === "panel";
  $("#vm-list").hidden = vmp.view === "panel";
  $("#vm-panel").hidden = vmp.view !== "panel";
  const vms = visibleVMs(), op = canOperate();
  const picked = vms.filter((v) => vmp.picked.has(v.uuid));
  $("#vm-picked").textContent = vmp.picked.size ? `${vmp.picked.size} selected` : "";
  $$("#vm-batch button[data-batch], #vm-batch summary").forEach((b) => { b.disabled = !vmp.picked.size; b.classList.toggle("off", !vmp.picked.size); });
  const none = state.vms.length ? (vmp.group === "all" ? "No VMs match" : "No VMs in this group. Select VMs, then More → Move to group, or drag them here.") : "No virtual machines yet. Create one, or bring VMs over with Migrate.";
  const where = vmp.group === "all" ? "" : vmp.group === "none" ? "Ungrouped" : groupPath(vmp.group);
  const cols = VM_COLUMNS[vmp.metric];

  if (vmp.view === "panel") {
    $("#vm-panel").innerHTML = (where ? `<p class="hint vm-where">${esc(where)}</p>` : "") + (vms.length ? `<div class="vm-cards">${vms.map((vm) => {
      const g = state.guests[vm.uuid], u = diskUsage(vm, g), alerts = vmAlerts(vm);
      const cpu = cpuUsed(vm), memPct = memUsed(vm) == null ? null : pct(memUsed(vm), vm.memory_mb);
      const diskPct = u.used == null ? null : pct(u.used, u.total), st = state.stats[vm.uuid] || {};
      const io = vm.state === "running" ? `<span title="Network received / sent">↓ ${rate(st.net_rx_bps)} · ↑ ${rate(st.net_tx_bps)}</span><span title="Disk read / write">R ${rate(st.disk_read_bps)} · W ${rate(st.disk_write_bps)}</span>` : `<span class="muted">Not running</span>`;
      return `<div class="vm-card vx-card ${esc(vm.state)}${vmp.picked.has(vm.uuid) ? " picked" : ""}${alerts.length ? " alerting" : ""}" data-uuid="${vm.uuid}" ${op ? 'draggable="true"' : ""}>
        <div class="vx-head">${op ? `<input type="checkbox" class="vm-pick" data-uuid="${vm.uuid}" aria-label="Select ${esc(vm.name)}" ${vmp.picked.has(vm.uuid) ? "checked" : ""}>` : ""}
          ${vxOs(vm)}
          <div class="vx-name"><a class="vm-name vm-link" href="#vm/${vm.uuid}">${esc(vm.name)}</a><span class="vx-ip">${ipCell(vm)}</span></div>
          <span class="vx-state ${esc(vm.state)}"><i></i>${esc(vm.state)}</span></div>
        <div class="vx-body">${vxRings(cpu, memPct, diskPct)}
          <div class="vx-stats">
            ${vxStat("CPU", cpu, `${vm.vcpus} vCPU`, "cpu")}
            ${vxStat("Memory", memPct, gbText(vm.memory_mb), "mem")}
            ${vxStat("Disk", diskPct, `${num(u.total)} GB`, "disk")}
          </div></div>
        ${vxWave(vm)}
        <div class="vx-io">${io}</div>
        <div class="vx-foot">${alerts.length ? `<span class="vc-alert" title="${esc(alerts.join(", "))}"><svg><use href="#i-alert"/></svg>${esc(alerts[0])}</span>` : `<span class="vx-node"><svg><use href="#i-server"/></svg>${esc(nodeLabel(vm.node || ""))}</span>`}
          <span class="spacer"></span><div class="actions">${vmMenu(vm, { summary: true, compact: true })}</div></div>
      </div>`;
    }).join("")}</div>` : `<p class="empty">${esc(none)}</p>`);
    return;
  }

  const sortMark = (h) => (vmp.sort.key === h ? (vmp.sort.dir > 0 ? " ▲" : " ▼") : "");
  const allOn = vms.length && picked.length === vms.length;
  $("#vm-thead").innerHTML = `<tr>${op ? `<th class="pick"><input type="checkbox" id="vm-pick-all" ${allOn ? "checked" : ""} title="Select all shown"></th>` : ""}
    <th data-sort="Status">Status${sortMark("Status")}</th><th data-sort="Name">VM name${sortMark("Name")}</th>
    ${cols.map((c) => `<th data-sort="${esc(c[0])}">${esc(c[0])}${sortMark(c[0])}</th>`).join("")}<th></th></tr>`;
  $("#vm-pick-all") && ($("#vm-pick-all").indeterminate = picked.length > 0 && !allOn);
  const span = cols.length + 3 + (op ? 1 : 0);
  $("#vms").innerHTML = vms.length ? vms.map((vm) => {
    const alerts = vmAlerts(vm);
    return `<tr data-uuid="${vm.uuid}" class="vx-row ${esc(vm.state)}${alerts.length ? " alerting" : ""}${vmp.picked.has(vm.uuid) ? " picked" : ""}" ${op ? 'draggable="true"' : ""}>
      ${op ? `<td class="pick"><input type="checkbox" class="vm-pick" data-uuid="${vm.uuid}" ${vmp.picked.has(vm.uuid) ? "checked" : ""}></td>` : ""}
      <td><span class="vx-state ${esc(vm.state)}"><i></i>${esc(vm.state)}</span></td>
      <td><div class="name-cell">${vxOs(vm)}<div><a class="vm-name vm-link" href="#vm/${vm.uuid}">${esc(vm.name)}</a>
        ${alerts.length ? `<span class="vc-alert" title="${esc(alerts.join(", "))}"><svg><use href="#i-alert"/></svg>${esc(alerts.join(" · "))}</span>` : `<span class="vm-sub clip">${esc(vmp.group === "all" && groupOf(vm) ? groupPath(groupOf(vm)) : vm.description || "")}</span>`}</div></div></td>
      ${cols.map((c) => `<td>${c[2](vm)}</td>`).join("")}
      <td><div class="actions">${vmMenu(vm, { summary: true, compact: true })}</div></td>
    </tr>`;
  }).join("") : empty(span, none);
}

{
  const rerender = () => renderVMs();
  $("#vm-search").addEventListener("input", rerender);
  $("#vm-filter").addEventListener("change", rerender);
  $("#vm-refresh").addEventListener("click", () => refresh("compute").catch(fail));
  $("#vm-view").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (b) { vmp.view = b.dataset.view; vmpSave(); renderVMs(); }
  });
  $("#vm-metrics").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-metric]");
    if (b) { vmp.metric = b.dataset.metric; vmp.sort = { key: "", dir: 1 }; vmpSave(); renderVMs(); }
  });
  $("#vm-alert").addEventListener("click", (e) => {
    const a = e.target.closest("a[data-alert]");
    if (!a) return;
    e.preventDefault();
    $("#vm-filter").value = a.dataset.alert === "view" ? "alert" : "";
    renderVMs();
  });
  $("#vm-thead").addEventListener("click", (e) => {
    if (e.target.id === "vm-pick-all") {
      const vms = visibleVMs();
      vms.forEach((v) => (e.target.checked ? vmp.picked.add(v.uuid) : vmp.picked.delete(v.uuid)));
      renderVMs();
      return;
    }
    const th = e.target.closest("th[data-sort]");
    if (!th) return;
    const key = th.dataset.sort;
    vmp.sort = { key, dir: vmp.sort.key === key ? -vmp.sort.dir : 1 };
    renderVMs();
  });
  const pick = (e) => {
    const box = e.target.closest("input.vm-pick");
    if (!box) return;
    box.checked ? vmp.picked.add(box.dataset.uuid) : vmp.picked.delete(box.dataset.uuid);
    renderVMs();
  };
  $("#vms").addEventListener("change", pick);
  $("#vm-panel").addEventListener("change", pick);

  // Drag VMs (the selected ones, or the one dragged) onto a group in the tree.
  const dragStart = (e) => {
    const item = e.target.closest("[data-uuid][draggable]");
    if (!item) return;
    const id = item.dataset.uuid;
    const ids = vmp.picked.has(id) ? [...vmp.picked] : [id];
    e.dataTransfer.setData("application/x-ibhics-vms", JSON.stringify(ids));
    e.dataTransfer.effectAllowed = "move";
  };
  $("#vms").addEventListener("dragstart", dragStart);
  $("#vm-panel").addEventListener("dragstart", dragStart);
  const tree = $("#vg-tree");
  tree.addEventListener("dragover", (e) => {
    const row = e.target.closest(".vg-row");
    if (!row || row.dataset.group === "all" || !e.dataTransfer.types.includes("application/x-ibhics-vms")) return;
    e.preventDefault();
    $$(".vg-row.drop", tree).forEach((r) => r !== row && r.classList.remove("drop"));
    row.classList.add("drop");
  });
  tree.addEventListener("dragleave", (e) => e.target.closest(".vg-row")?.classList.remove("drop"));
  tree.addEventListener("drop", async (e) => {
    const row = e.target.closest(".vg-row");
    if (!row) return;
    e.preventDefault();
    row.classList.remove("drop");
    const ids = JSON.parse(e.dataTransfer.getData("application/x-ibhics-vms") || "[]");
    if (!ids.length) return;
    await moveToGroup(ids, row.dataset.group === "none" ? "" : row.dataset.group);
  });

  tree.addEventListener("click", async (e) => {
    const toggle = e.target.closest("[data-toggle]");
    if (toggle) {
      const id = toggle.dataset.toggle;
      vmp.closed.has(id) ? vmp.closed.delete(id) : vmp.closed.add(id);
      renderGroupTree();
      return;
    }
    if (e.target.id === "vg-first") { e.preventDefault(); openGroupDialog(); return; }
    const act = e.target.closest("button[data-gact]");
    if (act) {
      act.closest("details").removeAttribute("open");
      const g = groupById(act.dataset.id);
      if (act.dataset.gact === "sub") openGroupDialog(null, g.id);
      if (act.dataset.gact === "edit") openGroupDialog(g);
      if (act.dataset.gact === "del") {
        const n = state.vms.filter((v) => groupOf(v) === g.id).length;
        const up = g.parent ? `"${groupById(g.parent)?.name}"` : "Ungrouped";
        if (!confirm(`Delete group "${g.name}"? Its ${n} VM${n === 1 ? "" : "s"} and subgroups move to ${up}. No VM is deleted.`)) return;
        try { await del(`/vm-groups/${enc(g.id)}`); } catch (err) { fail(err); }
        await load("groups");
        renderVMs();
      }
      return;
    }
    if (e.target.closest("details")) return;
    const row = e.target.closest(".vg-row");
    if (row) { vmp.group = row.dataset.group; renderVMs(); }
  });
  $("#vg-add").addEventListener("click", () => openGroupDialog(null, groupById(vmp.group) ? vmp.group : ""));

  $("#vm-batch").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-batch]");
    if (!b) return;
    b.closest("details")?.removeAttribute("open");
    batch(b.dataset.batch).catch(fail);
  });
}

function groupOptions(skip = "") {
  const out = [];
  const walk = (parent, depth) => childGroups(parent).forEach((g) => {
    if (g.id === skip) return;
    out.push(`<option value="${esc(g.id)}">${"   ".repeat(depth)}${esc(g.name)}</option>`);
    walk(g.id, depth + 1);
  });
  walk("", 0);
  return out.join("");
}

function openGroupDialog(group = null, parent = "") {
  const dlg = $("#group-dialog");
  $(".gd-title", dlg).textContent = group ? `Group ${group.name}` : "New group";
  $(".gd-parent", dlg).innerHTML = `<option value="">Top level</option>${groupOptions(group?.id)}`;
  openDialog("group-dialog", { id: group?.id || "" },
    group ? { name: group.name, parent: group.parent, description: group.description } : { parent });
}
handlers["group-dialog"] = async (f, ctx) => {
  const body = { name: f.get("name"), parent: f.get("parent"), description: f.get("description") };
  if (ctx.id) await patch(`/vm-groups/${enc(ctx.id)}`, body);
  else vmp.group = (await post("/vm-groups", body)).id;
  await load("groups");
  renderVMs();
};

async function moveToGroup(ids, group) {
  try {
    await api("/vm-groups/members", { method: "PUT", body: JSON.stringify({ vms: ids, group }) });
    toast(`${ids.length} VM${ids.length === 1 ? "" : "s"} moved to ${group ? groupPath(group) : "Ungrouped"}`);
  } catch (err) { fail(err); }
  await load("groups");
  renderVMs();
}
handlers["move-dialog"] = async (f, ctx) => { await moveToGroup(ctx.ids, f.get("group")); };

// Run one power action on every selected VM that it applies to.
async function batch(act) {
  const vms = state.vms.filter((v) => vmp.picked.has(v.uuid));
  if (!vms.length) return;
  if (act === "group") {
    if (!groupList().length) { openGroupDialog(); toast("Make a group first, then move the VMs into it"); return; }
    const dlg = $("#move-dialog");
    $(".md-group", dlg).innerHTML = `<option value="">Ungrouped</option>${groupOptions()}`;
    $(".md-what", dlg).textContent = `${vms.length} VM${vms.length === 1 ? "" : "s"}: ${vms.map((v) => v.name).join(", ")}`;
    openDialog("move-dialog", { ids: vms.map((v) => v.uuid) }, { group: groupById(vmp.group) ? vmp.group : "" });
    return;
  }
  if (act === "ungroup") { await moveToGroup(vms.map((v) => v.uuid), ""); return; }
  const fits = {
    start: (v) => v.state !== "running", stop: (v) => v.state === "running", poweroff: (v) => ["running", "paused"].includes(v.state),
    restart: (v) => v.state === "running", delete: (v) => v.state === "stopped",
  }[act];
  const todo = vms.filter(fits), skipped = vms.length - todo.length;
  const label = { start: "Power on", stop: "Shut down", poweroff: "Power off", restart: "Restart", delete: "Delete" }[act];
  if (!todo.length) { toast(`${label}: none of the selected VMs ${act === "start" ? "is stopped" : act === "delete" ? "is stopped (shut down first)" : "is running"}`, true); return; }
  const names = todo.map((v) => v.name).join(", ");
  if (act === "delete" && !confirm(`Delete ${todo.length} VM${todo.length === 1 ? "" : "s"}?${binNote()}\n\n${names}`)) return;
  if (["poweroff", "restart"].includes(act) && !confirm(`${label} ${names}?${act === "poweroff" ? " Unsaved work in the guests is lost." : ""}`)) return;
  const call = (v) => ({
    start: () => post(`/vms/${v.uuid}/start`), stop: () => post(`/vms/${v.uuid}/stop`),
    poweroff: () => post(`/vms/${v.uuid}/stop`, { force: true }), restart: () => post(`/vms/${v.uuid}/restart`),
    delete: () => del(`/vms/${v.uuid}`),
  }[act])();
  const results = await Promise.allSettled(todo.map(call));
  const failed = results.map((r, i) => [r, todo[i]]).filter(([r]) => r.status === "rejected");
  if (act === "delete") todo.forEach((v, i) => results[i].status === "fulfilled" && vmp.picked.delete(v.uuid));
  toast(`${label}: ${todo.length - failed.length} done${failed.length ? `, ${failed.length} failed (${failed.map(([r, v]) => `${v.name}: ${r.reason.message}`).join("; ")})` : ""}${skipped ? `, ${skipped} skipped` : ""}`, failed.length > 0);
  await refresh("compute");
}

function renderMoves() {
  $("#moves-panel").hidden = !state.moves.length;
  $("#moves").innerHTML = state.moves.slice(0, 20).map((m) => `<tr>
    <td class="vm-name">${esc(m.vm)}</td><td>${esc(m.source)}</td><td>${esc(m.target || "–")}</td>
    <td>${m.live ? "Live" : "Offline"}${m.copy_storage ? " + disks" : ""}</td>
    <td>${esc(when(m.created))}</td>
    <td><span class="badge ${esc(m.state)}">${JOB_STATE[m.state] || esc(m.state)}</span></td>
    <td class="vm-sub" style="display:table-cell">${esc(m.error || "")}</td></tr>`).join("");
}

// Rename a VM (Thai names too), even while it runs; backup policies follow the new name.
async function renameVM(uuid, old) {
  const name = (prompt(`New name for ${old}`, old) || "").trim();
  if (!name || name === old) return;
  if (!/^[a-zA-Z0-9\u0E00-\u0E7F][a-zA-Z0-9\u0E00-\u0E7F_.-]{0,62}$/.test(name)) throw new Error("Use letters (Thai too), digits and _ . - (no spaces)");
  await post(`/vms/${uuid}/rename`, { name });
  toast(`${old} is now ${name}`);
  await refresh(currentPage());
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
  if (act === "edit") { openEdit(id).catch(fail); return; }
  if (act === "snapshot") { openSnapshot(id); return; }
  if (act === "snapshots") { location.hash = `vm/${id}`; setTimeout(() => selectTab("vm-tabs", "vm-snapshots"), 400); return; }
  if (act === "clone") { load("pools").then(() => openDeploy(id)).catch(fail); return; }
  if (act === "rename") { renameVM(id, name).catch(fail); return; }
  if (act === "ha") {
    const on = !state.ha?.has(id);
    api(`/vms/${enc(id)}/ha`, { method: "PUT", body: JSON.stringify({ enabled: on }) })
      .then(() => { toast(`HA restart ${on ? "on" : "off"} for ${name}`); return refresh(currentPage()); })
      .catch(fail);
    return;
  }
  if (act === "backup") {
    backupNow(id, name).catch(fail);
    return;
  }
  if (act === "migrate") { openMigrate(id, name, btn.dataset.node); return; }
  try {
    if (act === "start") await post(`/vms/${id}/start`);
    if (act === "stop") await post(`/vms/${id}/stop`);
    if (act === "poweroff") await post(`/vms/${id}/stop`, { force: true });
    if (act === "restart") await post(`/vms/${id}/restart`);
    if (act === "tools") {
      if (!canOperate()) return;
      await post(`/vms/${id}/guest-tools`);
      toast(`Guest tools CD is in ${name}'s drive. Windows: run virtio-win-guest-tools.exe from it. Linux: run sh linux/install.sh from it as root.`);
    }
    if (act === "eject") await post(`/vms/${id}/cdrom`, { iso: null });
    if (act === "tovirtio") {
      if (!confirm(`Switch ${name}'s disks to VirtIO for speed? (The network card stays as it is, so its IP settings stay.)\n\nInstall the guest tools in Windows first (⋯ → Insert guest tools CD, run the installer for this Windows). ibHiCS then shuts ${name} down, starts it once with a small VirtIO disk so Windows loads the driver, shuts it down again and moves the disks over. Takes a few minutes; follow it in Tasks.`)) return;
      toast(`Switching ${name} to VirtIO… (see Tasks)`);
      setTimeout(() => load("jobs").catch(() => {}), 800);
      await post(`/vms/${id}/to-virtio`);
      toast(`${name} now uses VirtIO disks`);
      await refresh(currentPage());
      return;
    }
    if (act === "sysprep") {
      if (!confirm(`Run sysprep /generalize /oobe /shutdown in ${name}?\n\nWindows removes its SID, computer name and drivers' hardware ties, then shuts down. Convert it to a template after that: every VM deployed from it gets its own SID. Needs the guest tools (guest agent) in the VM. Do this on a copy if you still need this VM as it is.`)) return;
      await post(`/vms/${id}/sysprep`);
      toast(`Sysprep started in ${name}: it shuts down when done, then convert it to a template`);
      return;
    }
    if (act === "totemplate") {
      await load("pools");
      return openTemplateStorage(id);
    }
    if (act === "delete") {
      if (!confirm(`Delete ${name}?${binNote()}`)) return;
      await del(`/vms/${id}`);
      toast(state.recycle.days ? `${name} moved to the recycle bin` : `${name} deleted`);
      if (currentPage() === "vm") { location.hash = "compute"; return; }
    }
    await refresh(currentPage());
  } catch (err) { fail(err); }
}
$("#vms").addEventListener("click", vmAction);
$("#vm-actions").addEventListener("click", vmAction);
$("#vm-panel").addEventListener("click", vmAction);

// ---------- templates ----------
function openTemplateStorage(id) {
  const vm = [...state.vms, ...state.templates].find((v) => v.uuid === id);
  if (!vm) throw new Error("VM no longer exists. Refresh and try again.");
  openDialog("template-storage-dialog", { id, name: vm.name });
  $("#template-storage-name").textContent = vm.name;
  $("#template-storage-form").elements.pool.innerHTML = `<option value="">Keep current storage</option>`
    + state.pools.filter((p) => p.node === vm.node && p.active).map((p) =>
      `<option value="${esc(p.name)}">${esc(poolText(p))} · ${num(p.available_gb)} GB free</option>`).join("");
  $("#template-storage-windows").hidden = !/windows/i.test(vm.os_type || "");
}
handlers["template-storage-dialog"] = async (f, ctx) => {
  const body = { template: true };
  if (f.get("pool")) body.pool = f.get("pool");
  toast(`Preparing ${ctx.name}… storage moves appear in Tasks`);
  await post(`/vms/${ctx.id}/template`, body);
  toast(`${ctx.name} is ready as a template`);
  if (currentPage() === "templates") await refresh("templates");
  else location.hash = "templates";
};
// A stopped VM becomes a template that cannot run; deployed VMs get their own
// copies of its disks and can optionally be customized.
function renderTemplates() {
  const q = $("#tpl-search").value.trim().toLowerCase();
  const list = [...state.templates].sort((a, b) => a.name.localeCompare(b.name))
    .filter((t) => !q || [t.name, t.os_type, t.description, t.node].join(" ").toLowerCase().includes(q));
  $("#tpl-count").textContent = `${state.templates.length} template${state.templates.length === 1 ? "" : "s"}`;
  const busy = new Set(state.deploys.filter(isActive).map((j) => j.source_uuid));
  $("#tpl-grid").innerHTML = list.length ? list.map((t) => {
    const disk = t.disks.reduce((s, d) => s + d.size_gb, 0);
    const win = /windows/i.test(t.os_type);
    return `<article class="tpl-card">
      <div class="tpl-top"><span class="tpl-icon ${win ? "win" : "lin"}"><svg><use href="#i-os-${win ? "win" : "linux"}"/></svg></span>
        <div class="tpl-name"><b>${esc(t.name)}</b><span class="vm-sub">${esc(t.os_type || "OS not set")}</span></div>
        ${busy.has(t.uuid) ? `<span class="badge running">Deploying</span>` : ""}</div>
      ${t.description ? `<p class="tpl-desc">${esc(t.description)}</p>` : ""}
      <div class="tpl-specs">
        <span><svg><use href="#i-cpu"/></svg>${t.vcpus} vCPU</span>
        <span><svg><use href="#i-mem"/></svg>${num(t.memory_mb / 1024)} GB</span>
        <span><svg><use href="#i-hdd"/></svg>${num(disk)} GB · ${esc([...new Set(t.disks.map((d) => d.pool))].join(", ") || "no disk")}</span>
        <span><svg><use href="#i-nodes"/></svg>${esc(t.node || "")}</span>
      </div>
      <div class="tpl-foot">
        <span class="spacer"></span>
        ${canOperate() ? `<button class="primary small" data-tpl="deploy" data-id="${t.uuid}">Deploy VM</button>
        <details class="more"><summary class="small" title="More actions">⋯</summary><div class="dropdown open">
          <button data-tpl="edit" data-id="${t.uuid}">Settings</button>
          <button data-tpl="storage" data-id="${t.uuid}">Change storage</button>
          <button data-tpl="tovm" data-id="${t.uuid}" data-name="${esc(t.name)}">Convert to VM</button>
          <button class="danger" data-tpl="delete" data-id="${t.uuid}" data-name="${esc(t.name)}">Delete</button>
        </div></details>` : ""}
      </div>
    </article>`;
  }).join("") : `<div class="empty-card"><svg><use href="#i-template"/></svg><p>${state.templates.length ? "No template matches." : "No templates yet. In Virtual Machines, shut a VM down and choose <b>Convert to template</b> from its ⋯ menu."}</p></div>`;
}
$("#tpl-search").addEventListener("input", renderTemplates);
$("#tpl-grid").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-tpl]");
  if (!b) return;
  b.closest("details")?.removeAttribute("open");
  const { tpl: act, id, name } = b.dataset;
  try {
    if (act === "deploy") return openDeploy(id);
    if (act === "edit") return openEdit(id);
    if (act === "storage") return openTemplateStorage(id);
    if (act === "tovm") {
      if (!confirm(`Convert ${name} back to a VM? It can then be powered on and changed.`)) return;
      await post(`/vms/${id}/template`, { template: false });
      toast(`${name} is a VM again`);
    }
    if (act === "delete") {
      if (!confirm(`Delete template ${name}?${binNote()}`)) return;
      await del(`/vms/${id}`);
      toast(`${name} deleted`);
    }
    await refresh(currentPage());
  } catch (err) { fail(err); }
});

function openDeploy(uuid) {
  const src = [...state.templates, ...state.vms].find((v) => v.uuid === uuid);
  if (!src) return;
  const isTpl = src.template;
  let name = src.name.replace(/[-_.]?(tpl|template)$/i, "") || src.name;
  const taken = new Set([...state.vms, ...state.templates].map((v) => v.name));
  let n = 1; while (taken.has(`${name}-${String(n).padStart(2, "0")}`)) n++;
  const win = /windows/i.test(src.os_type);
  openDialog("deploy-dialog", { uuid, name: src.name }, {
    name: `${name}-${String(n).padStart(2, "0")}`,
    custom: isTpl ? (win ? "windows" : "linux") : "none",
  });
  const form = $("#deploy-form");
  $("#deploy-title").textContent = isTpl ? "Deploy VM from template" : "Clone VM";
  form.querySelector('button[value="ok"]').textContent = isTpl ? "Deploy VM" : "Clone VM";
  $("#deploy-src").innerHTML = `<svg><use href="#i-${isTpl ? "template" : "compute"}"/></svg><div><b>${esc(src.name)}</b>
    <span class="vm-sub">${src.vcpus} vCPU · ${num(src.memory_mb / 1024)} GB · ${src.disks.length} disk${src.disks.length === 1 ? "" : "s"} · ${esc(src.node || "")}</span></div>`;
  const pools = state.pools.filter((p) => p.node === src.node && p.active);
  form.elements.pool.innerHTML = `<option value="">Same pool as the ${isTpl ? "template" : "VM"}</option>`
    + pools.map((p) => `<option value="${esc(p.name)}">${esc(poolText(p))} · ${num(p.available_gb)} GB free</option>`).join("");
  syncSwitch(form);
}
handlers["deploy-dialog"] = async (f, ctx) => {
  const custom = f.get("custom");
  const body = { name: f.get("name"), power_on: f.get("power_on") === "on", reset_identity: f.get("reset_identity") === "on" };
  if (f.get("description")) body.description = f.get("description");
  if (f.get("pool")) body.pool = f.get("pool");
  if (custom !== "none") {
    body.customization = {
      os: custom, hostname: f.get("hostname"), username: f.get("username"), password: f.get("password"),
      timezone: f.get("timezone"),
      ssh_keys: custom === "linux" ? f.get("ssh_keys").split("\n").map((k) => k.trim()).filter(Boolean) : [],
      user_data: custom === "linux" ? f.get("user_data") : "",
    };
  }
  await post(`/vms/${ctx.uuid}/clone`, body);
  toast(`Deploying ${body.name} from ${ctx.name}. Follow it in Tasks.`);
  await load("deploys").then(renderTasks).catch(() => {});
  if (currentPage() === "templates") renderTemplates();
};

// ---------- edit VM ----------
// A list of the VM's parts on the left, the selected part's settings on the right.
const VM_ADVANCED_DEFAULTS = { cpu_mode: "default", mouse: "tablet", keymap: "default", boot_menu_seconds: 0, restart_on_crash: true, ballooning: true, serial_port: true, usb_controller: false, tpm: false };
function hardwareFormValues(f) {
  return Object.fromEntries(Object.entries(VM_ADVANCED_DEFAULTS).map(([key, value]) => [key,
    typeof value === "boolean" ? f.get(key) === "on" : typeof value === "number" ? Number(f.get(key)) : f.get(key) || value]));
}
let editing = null;
const BOOT_NAMES = { hd: "Hard disk", cdrom: "CD/DVD", network: "Network (PXE)" };

async function openEdit(uuid) {
  const [vm] = await Promise.all([api(`/vms/${uuid}`), load("pools", "networks", "isos")]);
  editing = {
    vm,
    vcpus: vm.vcpus,
    cpu_sockets: vm.cpu_sockets || 1,
    cpu_cores: vm.cpu_cores || vm.vcpus,
    topologyChanged: false,
    memory_mb: vm.memory_mb,
    disks: vm.disks.map((d, i) => ({ index: i, size_gb: d.size_gb, min: d.size_gb, pool: d.pool, bus: d.bus })),
    nics: vm.nics.map((n) => ({ mac: n.mac, network: n.network, model: n.model || "virtio" })),
    iso: vm.iso || "",
    boot: vm.boot || ["hd"],
    video: vm.video || "virtio",
    removedDisks: 0,
    selected: "cpu",
  };
  openDialog("edit-dialog", { uuid }, { os_type: vm.os_type, description: vm.description });
  $("#edit-form").elements.autostart.checked = vm.autostart;
  for (const [key, fallback] of Object.entries(VM_ADVANCED_DEFAULTS)) {
    const input = $("#edit-form").elements[key];
    if (typeof fallback === "boolean") input.checked = vm[key] ?? fallback;
    else input.value = vm[key] ?? fallback;
  }
  $("#edit-machine").textContent = vm.machine_type || "q35";
  await updateHardwareCapabilities($("#edit-form"), vm.node);
  $("#edit-title").textContent = `Edit virtual machine (${vm.name})`;
  $("#edit-firmware").textContent = (vm.firmware === "uefi" ? "UEFI" : "BIOS") + (vm.secure_boot ? " · Secure Boot" : "");
  const note = $("#edit-note");
  note.hidden = vm.state !== "running";
  note.textContent = "This VM is running. CPU topology and advanced hardware require shutdown. Disks, network adapters, the CD and more vCPUs without a fixed topology (up to 32) or memory (in 128 MB steps, up to 512 GB) change now. Fewer vCPUs or less memory and the boot order apply after it is shut down and powered on again. A VM made before hot-add support needs one restart after its first CPU or memory change.";
  selectTab("edit-tabs", "edit-hw");
  renderEdit();
}

function editItems() {
  const e = editing;
  return [
    { key: "cpu", icon: "i-cpu", label: "Processor", value: `${e.vcpus} vCPU` },
    { key: "mem", icon: "i-mem", label: "Memory", value: gbText(e.memory_mb) },
    ...e.disks.map((d, i) => ({ key: `disk${i}`, icon: "i-hdd", label: `Hard disk ${i + 1}`, value: `${d.size_gb} GB${d.pool ? ` · ${esc(d.pool)}` : ""}${d.index == null ? " · new" : ""}` })),
    ...e.nics.map((n, i) => ({ key: `nic${i}`, icon: "i-nic", label: `Network adapter ${i + 1}`, value: `${esc(n.network)}${n.mac ? "" : " · new"}` })),
    { key: "cd", icon: "i-cd", label: "CD/DVD drive", value: e.iso ? esc(e.iso.split("/").pop()) : "Empty" },
    { key: "video", icon: "i-compute", label: "Display", value: VIDEO_NAMES[e.video] || e.video },
  ];
}
const VIDEO_NAMES = { virtio: "VirtIO GPU", vga: "Standard VGA", qxl: "QXL", cirrus: "Cirrus" };

function renderEdit() {
  const e = editing;
  $("#edit-items").innerHTML = editItems().map((it) => `<li><button type="button" data-item="${it.key}" class="${it.key === e.selected ? "active" : ""}">
    <svg><use href="#${it.icon}"/></svg><span>${it.label}</span><em>${it.value}</em></button></li>`).join("");
  const pools = state.pools.filter((p) => p.node === e.vm.node || !p.node);
  const poolOpts = (cur) => (pools.length ? pools : [{ name: cur }]).map((p) => `<option value="${esc(p.name)}" ${p.name === cur ? "selected" : ""}>${esc(poolText(p))}${p.available_gb != null ? ` (${Math.round(p.available_gb).toLocaleString()} GB free)` : ""}</option>`).join("");
  const pane = $("#edit-pane");
  const s = e.selected;
  if (s === "video") {
    pane.innerHTML = `<h4>Display</h4><label>Display adapter <select data-set="video">${Object.entries(VIDEO_NAMES).map(([k, v]) => `<option value="${k}" ${k === e.video ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <p class="hint">VirtIO GPU suits Linux and Windows 10/Server 2016 and later. Older Windows (Server 2012 R2 and before, 7, 8.1) shows a black or slow screen with it: use Standard VGA. Applies after a restart.</p>`;
  } else if (s === "cpu") {
    pane.innerHTML = `<h4>Processor</h4>
      <label>Sockets <input type="number" min="1" max="256" value="${e.cpu_sockets}" data-set="cpu_sockets"></label>
      <label>Cores per socket <input type="number" min="1" max="256" value="${e.cpu_cores}" data-set="cpu_cores"></label>
      <label>Total vCPU <input value="${e.vcpus}" readonly></label>
      <p class="hint">Topology changes require a stopped VM. A fixed topology uses all configured CPUs; CPU hot-add remains available for existing VMs without a fixed topology.</p>`;
  } else if (s === "mem") {
    pane.innerHTML = `<h4>Memory</h4><label>Memory (GB) <input type="number" min="0.25" step="0.25" value="${e.memory_mb / 1024}" data-set="memory_gb"></label>`;
  } else if (s.startsWith("disk")) {
    const i = Number(s.slice(4)), d = e.disks[i];
    pane.innerHTML = `<h4>Hard disk ${i + 1}</h4>
      <label>Size (GB) <input type="number" min="${d.min || 1}" value="${d.size_gb}" data-disk="${i}" data-field="size_gb"></label>
      ${d.index == null ? `<label>Storage pool <select data-disk="${i}" data-field="pool">${poolOpts(d.pool)}</select></label>`
        : `<p class="hint">Storage pool: <b>${esc(d.pool || "unknown (outside ibHiCS pools)")}</b>${d.bus ? ` · ${esc(d.bus === "sata" ? "SATA" : "VirtIO")}` : ""}. To move it to another pool use ⋯ → Migrate… → Change storage. A disk can grow but not shrink; grow the partition inside the guest afterwards.</p>`}
      ${e.disks.length > 1 ? `<button type="button" class="danger small" data-remove="disk" data-i="${i}">Remove this disk</button>` : ""}`;
  } else if (s.startsWith("nic")) {
    const i = Number(s.slice(3)), n = e.nics[i];
    pane.innerHTML = `<h4>Network adapter ${i + 1}</h4>
      <label>Network <select data-nic="${i}" data-field="network">${netOptions(n.network)}</select></label>
      <label>Adapter type <select data-nic="${i}" data-field="model" ${n.mac ? "disabled" : ""}>${["virtio", "e1000e", "e1000", "rtl8139"].map((m) => `<option ${m === n.model ? "selected" : ""}>${m}</option>`).join("")}</select></label>
      <p class="hint">MAC address <span class="mono">${esc(n.mac || "assigned when saved")}</span></p>
      <button type="button" class="danger small" data-remove="nic" data-i="${i}">Remove this adapter</button>`;
  } else {
    pane.innerHTML = `<h4>CD/DVD drive</h4><label>ISO <select data-set="iso" class="iso-list">${isoOptions(e.iso, "Empty")}</select></label>
      <div class="row-btns"><label class="btn small operator-only">Upload ISO<input type="file" class="iso-upload" accept=".iso" hidden></label></div>
      <div class="iso-progress"></div>
      <p class="hint">Upload an ISO here or under Storage → ISO library; it then shows in this list.</p>`;
  }
  const boot = $("#edit-boot");
  boot.innerHTML = [0, 1, 2].map((slot) => `<label class="boot-slot"><b>${slot + 1}</b><select data-boot="${slot}">
    ${slot ? `<option value="">None</option>` : ""}${Object.entries(BOOT_NAMES).map(([k, v]) => `<option value="${k}" ${e.boot[slot] === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>`).join("");
}

{
  const form = $("#edit-form");
  form.addEventListener("click", (ev) => {
    const item = ev.target.closest("[data-item]");
    if (item) { editing.selected = item.dataset.item; renderEdit(); return; }
    const add = ev.target.closest("[data-add]");
    if (add) {
      add.closest("details").removeAttribute("open");
      if (add.dataset.add === "disk") {
        editing.disks.push({ index: null, size_gb: 20, pool: editing.vm.disks[0]?.pool || "default" });
        editing.selected = `disk${editing.disks.length - 1}`;
      } else {
        editing.nics.push({ mac: null, network: editing.nics[0]?.network || "default", model: "virtio" });
        editing.selected = `nic${editing.nics.length - 1}`;
      }
      renderEdit();
      return;
    }
    const rm = ev.target.closest("[data-remove]");
    if (rm) {
      const list = rm.dataset.remove === "disk" ? editing.disks : editing.nics;
      const [gone] = list.splice(Number(rm.dataset.i), 1);
      if (rm.dataset.remove === "disk" && gone.index != null) editing.removedDisks += 1;
      editing.selected = "cpu";
      renderEdit();
      return;
    }
  });
  form.addEventListener("change", (ev) => {
    const t = ev.target, e = editing;
    if (!e) return;
    if (["cpu_sockets", "cpu_cores"].includes(t.dataset.set)) {
      e[t.dataset.set] = Number(t.value);
      e.vcpus = e.cpu_sockets * e.cpu_cores;
      e.topologyChanged = true;
    }
    if (t.dataset.set === "memory_gb") e.memory_mb = Math.round(Number(t.value) * 1024);
    if (t.dataset.set === "iso") e.iso = t.value;
    if (t.dataset.set === "video") e.video = t.value;
    if (t.dataset.disk) e.disks[Number(t.dataset.disk)][t.dataset.field] = t.dataset.field === "size_gb" ? Number(t.value) : t.value;
    if (t.dataset.nic) e.nics[Number(t.dataset.nic)][t.dataset.field] = t.value;
    if (t.dataset.boot) {
      e.boot = $$("#edit-boot select").map((sel) => sel.value).filter(Boolean).filter((v, i, a) => a.indexOf(v) === i);
    }
    if (!t.name && !t.dataset.boot && t.name !== "autostart" && t.name !== "os_type" && t.name !== "description") renderEdit();
  });
}

handlers["edit-dialog"] = async (f, { uuid }) => {
  const e = editing;
  let deleteDisks = false;
  if (e.removedDisks) {
    deleteDisks = confirm(`Also delete the data of the ${e.removedDisks} removed disk(s)?\n\nOK deletes it for good. Cancel only detaches the disk and keeps its data in the pool.`);
  }
  const body = {
    os_type: f.get("os_type"), description: f.get("description"),
    vcpus: e.vcpus, memory_mb: e.memory_mb, boot: e.boot.length ? e.boot : ["hd"],
    autostart: f.get("autostart") === "on", video: e.video,
    ...hardwareFormValues(f),
    ...($("#edit-form").elements.tpm.disabled ? { tpm: e.vm.tpm || false } : {}),
    ...(e.topologyChanged ? { cpu_sockets: e.cpu_sockets, cpu_cores: e.cpu_cores } : {}),
    disks: e.disks.map((d) => (d.index == null ? { size_gb: d.size_gb, pool: d.pool } : { index: d.index, size_gb: d.size_gb })),
    nics: e.nics.map((n) => (n.mac ? { mac: n.mac, network: n.network, model: n.model } : { network: n.network, model: n.model })),
    delete_removed_disks: deleteDisks,
  };
  const vm = await patch(`/vms/${uuid}`, body);
  if ((e.iso || null) !== (e.vm.iso || null)) await post(`/vms/${uuid}/cdrom`, { iso: e.iso || null });
  toast(vm.restart_needed ? `${vm.name} saved. Restart it to apply the processor, memory and boot order: ibHiCS shuts it down and powers it on again.` : `${vm.name} saved`);
  await refresh(currentPage());
};

// New VM: basics (name, OS), hardware (CPU, memory, disks, network), then install media
// and a summary. Windows and recent OS versions default to UEFI.
const vmWiz = wizard("vm-dialog", {
  reset() {
    const f = $("#vm-form");
    $$(".vm-disk:not(:first-child)", f).forEach((d) => d.parentNode.removeChild(d));
    f.elements.vcpus.value = 2;
    f.elements.cpu_sockets.value = 1;
    f.elements.cpu_cores.value = 2;
    f.elements.memory_gb.value = 4;
    f.elements.disk_gb.value = 60;
    vmWizOs(f);
    updateHardwareCapabilities(f, f.elements.node.value);
    syncPresets(f);
  },
  check(step) {
    const f = $("#vm-form").elements;
    if (step === 1 && !/^[a-zA-Z0-9\u0E00-\u0E7F][a-zA-Z0-9\u0E00-\u0E7F_.-]{0,62}$/.test(f.name.value)) throw new Error("Name the VM with letters (English or Thai), digits, '-', '_' or '.' (no spaces).");
    if (step === 1 && state.vms.some((v) => v.name === f.name.value)) throw new Error(`A VM named ${f.name.value} already exists.`);
    if (step === 2) {
      f.vcpus.value = Number(f.cpu_sockets.value) * Number(f.cpu_cores.value);
      if (!Number.isInteger(Number(f.vcpus.value)) || Number(f.vcpus.value) > 256) throw new Error("CPU total must be an integer between 1 and 256.");
      if (!(Number(f.vcpus.value) >= 1)) throw new Error("Give the VM at least 1 vCPU.");
      if (!(Number(f.memory_gb.value) >= 0.25)) throw new Error("Give the VM at least 0.25 GB of memory.");
      if ($$(".vm-disk", $("#vm-form")).some((d) => !(Number($("input", d).value) >= 1) || !$("select", d).value)) throw new Error("Each disk needs a size and a storage pool.");
    }
  },
  show(step) {
    const form = $("#vm-form"), f = form.elements;
    if (step === 3) {
      const disks = $$(".vm-disk", form).map((d) => `${esc($("input", d).value)} GB on ${esc($("select", d).value)}`);
      const rows = [["Name", esc(f.name.value)], ["Node", esc(f.node.value || "Automatic")],
        ["OS", esc(f.os_type.value || { windows: "Windows", linux: "Linux", other: "Other" }[f.os_family.value])],
        ["CPU / memory", `${esc(f.vcpus.value)} vCPU · ${esc(f.memory_gb.value)} GB`], ["Disks", disks.join("<br>")],
        ["Network", esc(f.network.value)], ["Firmware", esc(f.firmware.value.toUpperCase())]];
      $(".wiz-summary", form).innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    }
  },
});
// Firmware and devices that suit a guest OS. Windows with no driver in the bundled virtio-win
// (Server 2012 non-R2, Windows 8, Windows 7 / Server 2008 R2 and older) get SATA disks and
// an Intel NIC that Windows has a driver for (e1000e from Windows 8 / 2012, e1000 before).
function osProfile(family, ver) {
  // Up to Server 2012 R2 / Windows 8.1: Setup refuses the bundled virtio drivers ("No
  // signed device drivers were found"), so these install on SATA and an Intel NIC.
  const legacy = family === "windows" && /2008|2003|windows 7|vista|xp|server 2012|windows 8/.test(ver);
  const nic = !legacy ? "virtio" : /2012|windows 8/.test(ver) ? "e1000e" : "e1000";
  const uefi = !legacy && ((family === "windows" && !/2012|2016|windows 8/.test(ver))
    || /2025|enterprise linux (9|10)|rocky linux (9|10)|almalinux (9|10)|oracle linux 9|stream 9|2[2-4]\.04|debian 1[23]|leap 15|suse linux enterprise 15|fedora|freebsd 1[34]/.test(ver));
  return { uefi, legacy, nic, folder: virtioFolder(family, ver) };
}
// The folder for this Windows on the virtio driver CD (only these are bundled).
function virtioFolder(family, ver) {
  if (family !== "windows") return "";
  const map = [[/2025/, "2k25"], [/2022/, "2k22"], [/2019/, "2k19"], [/2016/, "2k16"], [/2012 r2|windows 8\.1/, "2k12R2"], [/windows 11/, "w11"], [/windows 10/, "w10"]];
  return (map.find(([re]) => re.test(ver)) || [null, ""])[1];
}
function vmWizOs(form) {
  const f = form.elements, family = f.os_family.value, ver = f.os_type.value.toLowerCase();
  const { uefi, legacy, nic, folder } = osProfile(family, ver);
  f.firmware.value = uefi ? "uefi" : "bios";
  $(".fw-hint", form).textContent = legacy ? `Windows Setup of this version does not take the virtio drivers on the CD, so it gets SATA disks and an Intel ${nic} network card: Setup sees the disk without loading a driver.`
    : uefi ? "UEFI suits this OS (needed for Windows 11 and Server 2022+)." : "BIOS suits this OS.";
  $(".win-driver-note", form).innerHTML = legacy ? "No driver CD needed: Windows has its own SATA and Intel network drivers."
    : `Windows VMs get a second CD with the virtio drivers: Setup finds the disk and network drivers on it by itself. If it still shows no disk, click <b>Load driver</b> and pick <code>viostor\\${folder || "&lt;version&gt;"}\\amd64</code> on that CD; after Windows is installed, run <code>virtio-win-guest-tools.exe</code> from it for the network and other drivers.`;
  f.os_type.placeholder = { windows: "Windows Server 2022", linux: "Ubuntu 24.04 LTS", other: "e.g. pfSense" }[family];
  $(".win-driver-note", form).hidden = family !== "windows";
}
{
  const form = $("#vm-form");
  $$('input[name="os_family"]', form).forEach((r) => r.addEventListener("change", () => vmWizOs(form)));
  form.elements.os_type.addEventListener("change", () => {
    const v = form.elements.os_type.value.toLowerCase();
    const fam = /windows/.test(v) ? "windows" : /ubuntu|debian|linux|rhel|rocky|alma|suse|centos|fedora/.test(v) ? "linux" : /bsd|pfsense|opnsense|esxi|other/.test(v) ? "other" : null;
    if (fam) form.elements.os_family.value = fam;
    vmWizOs(form);
  });
  $(".add-disk", form).addEventListener("click", () => {
    const first = $(".vm-disk", form);
    const row = document.createElement("div");
    row.className = "vm-disk";
    row.innerHTML = `<input type="number" min="1" value="100" aria-label="Size (GB)"><span class="muted">GB on</span><select>${$("select", first).innerHTML}</select><button type="button" class="ghost" title="Remove disk">×</button>`;
    $("select", row).value = $("select", first).value;
    $(".vm-disks", form).append(row);
  });
  $(".vm-disks", form).addEventListener("click", (e) => {
    const row = e.target.closest("button") && e.target.closest(".vm-disk");
    if (row) row.parentNode.removeChild(row);
  });
}

handlers["vm-dialog"] = async (f) => {
  const form = $("#vm-form");
  vmWiz.check(1); vmWiz.check(2);
  const { legacy, nic } = osProfile(f.get("os_family"), (f.get("os_type") || "").toLowerCase());
  const body = {
    name: f.get("name"),
    vcpus: Number(f.get("cpu_sockets")) * Number(f.get("cpu_cores")),
    cpu_sockets: Number(f.get("cpu_sockets")), cpu_cores: Number(f.get("cpu_cores")),
    ...hardwareFormValues(f),
    machine_type: f.get("machine_type") || "q35",
    secure_boot: f.get("secure_boot") === "on",
    autostart: f.get("autostart") === "on",
    boot: f.get("boot_first") === "auto" ? null : [f.get("boot_first"), ...["hd", "cdrom", "network"].filter((d) => d !== f.get("boot_first"))],
    memory_mb: Math.round(Number(f.get("memory_gb")) * 1024),
    firmware: f.get("firmware"),
    os_type: f.get("os_type").trim() || (f.get("os_family") === "windows" ? "Windows" : ""),
    description: f.get("description").trim(),
    disks: $$(".vm-disk", form).map((d) => ({ size_gb: Number($("input", d).value), pool: $("select", d).value || "default", bus: legacy ? "sata" : "virtio" })),
    nics: [{ network: f.get("network") || "default", model: nic }],
    node: f.get("node") || null,
    video: f.get("video") && f.get("video") !== "auto" ? f.get("video") : legacy ? "vga" : "virtio",
  };
  if (f.get("iso")) body.iso = f.get("iso");
  const vm = await post("/vms", body);
  if (groupById(vmp.group)) await moveToGroup([vm.uuid], vmp.group).catch(() => {}); // created inside the open group
  toast(`${vm.name} created on ${vm.node}`);
  if (f.get("start") === "on") {
    try {
      await post(`/vms/${vm.uuid}/start`);
      window.open(`console.html?${new URLSearchParams({ vm: vm.uuid, name: vm.name })}`, `console-${vm.uuid}`, "width=1100,height=760");
    } catch (err) { fail(err); }
  }
  if (currentPage() === "compute") await refresh("compute"); else location.hash = "compute";
};

$("#vm-form").addEventListener("input", () => {
  const f = $("#vm-form").elements;
  f.vcpus.value = Number(f.cpu_sockets.value) * Number(f.cpu_cores.value);
});
$("#vm-form").addEventListener("change", () => {
  const f = $("#vm-form").elements;
  f.vcpus.value = Number(f.cpu_sockets.value) * Number(f.cpu_cores.value);
});

async function updateHardwareCapabilities(form, node) {
  const request = (form.hardwareCapsRequest || 0) + 1;
  form.hardwareCapsRequest = request;
  let caps = {};
  if (node) { try { caps = await api(`/nodes/${encodeURIComponent(node)}/hardware-capabilities`); } catch (_) {} }
  if (form.hardwareCapsRequest !== request) return;
  for (const key of ["tpm", "secure_boot"]) {
    const input = form.elements[key];
    if (!input) continue;
    input.disabled = !caps[key];
    // Keep an existing TPM checked so saving unrelated settings does not remove it.
    if (input.disabled && form.id !== "edit-form") input.checked = false;
  }
  const thai = form.elements.keymap.querySelector('option[value="th"]');
  thai.disabled = !(caps.keymaps || []).includes("th");
  $$(".hw-cap-hint", form).forEach((hint) => { hint.textContent = !node ? "Select a node to enable TPM or Secure Boot." : `TPM: ${caps.tpm ? "swtpm available" : "swtpm unavailable"}. Secure Boot: ${caps.secure_boot ? "firmware available" : "enrolled-key OVMF unavailable"}.`; });
}
$("#vm-form").elements.node.addEventListener("change", () => updateHardwareCapabilities($("#vm-form"), $("#vm-form").elements.node.value));

// Back up a VM now. With no backup target yet, ask where backups go first (a local
// folder on the node by default), then start the backup straight away.
async function backupNow(id, name) {
  await load("targets");
  if (state.targets.length) { openDialog("backup-dialog", { id, name }); return; }
  openDialog("target-dialog", { backupAfter: { id, name } }, { name: "backup-repo", type: "pool" });
}

handlers["backup-dialog"] = async (f, ctx) => {
  const body = { target: f.get("target"), mode: f.get("mode") || "incremental" };
  if (f.get("keep_last")) body.keep_last = Number(f.get("keep_last"));
  await post(`/vms/${ctx.id}/backup`, body);
  toast(`Backing up ${ctx.name} to ${body.target}. Progress is under Backup → Jobs.`);
};

// Migrate: another node (live migration) or other pools for the disks (storage move).
function openMigrate(id, name, node) {
  const vm = state.vms.find((v) => v.uuid === id) || { disks: [] };
  openDialog("migrate-dialog", { id, name });
  const form = $("#migrate-form"), sel = $("select[name=node]", form);
  [...sel.options].forEach((o) => { if (o.value === node) o.remove(); });
  $("#migrate-title").textContent = `Migrate ${name}`;
  const others = sel.options.length > 0, one = "Only one node in the cluster: add a node to move VMs between hosts.";
  for (const mode of ["host", "both"]) {
    const r = $(`input[name=mode][value=${mode}]`, form);
    r.disabled = !others;
    r.closest(".type-card").classList.toggle("disabled", !others);
  }
  $(".mig-host-why", form).textContent = others ? "Move the VM to another node; a running VM keeps running." : one;
  $(".mig-both-why", form).textContent = others ? "Move the VM to another node and its disks to pools you pick there." : one;
  $(`input[name=mode][value=${others ? "host" : "storage"}]`, form).checked = true;
  migState.vm = vm;
  migState.node = node;
  migShow();
}
const migState = { vm: null, node: "" };
// The disks and the pools they can go to: on the VM's node (storage), or on the chosen
// destination (host and storage), where a disk stays in the pool of the same name.
function migDisks() {
  const form = $("#migrate-form"), mode = form.elements.mode.value, vm = migState.vm || { disks: [] };
  const node = mode === "both" ? form.elements.node.value : migState.node;
  const pools = [...new Set(state.pools.filter((p) => p.node === node && p.active).map((p) => p.name))];
  $(".mig-disks", form).innerHTML = (vm.disks || []).map((d, i) => {
    const opts = [d.pool, ...pools.filter((p) => p !== d.pool)];
    return `<tr><td>Disk ${i + 1}</td><td>${num(d.size_gb)} GB</td><td>${esc(d.pool)}</td>
    <td><select data-disk="${i}" data-from="${esc(d.pool)}">${opts.map((p) => `<option value="${esc(p)}">${p === d.pool ? `${esc(poolLabel(p, node))} (same pool)` : esc(poolLabel(p, node))}</option>`).join("")}</select></td></tr>`;
  }).join("") || empty(4, "This VM has no disks");
}
function migShow() {
  const form = $("#migrate-form"), mode = form.elements.mode.value;
  $$("[data-mig]", form).forEach((el) => { el.hidden = !el.dataset.mig.split(" ").includes(mode); });
  migDisks();
}
$("#migrate-form").addEventListener("change", (e) => {
  if (e.target.name === "mode" || (e.target.name === "node" && $("#migrate-form").elements.mode.value === "both")) migShow();
});
handlers["migrate-dialog"] = async (f, ctx) => {
  if (f.get("mode") === "storage") {
    const moves = $$("#migrate-form select[data-disk]").filter((s) => s.value !== s.dataset.from).map((s) => ({ index: Number(s.dataset.disk), pool: s.value }));
    if (!moves.length) throw new Error("Pick another pool for at least one disk.");
    toast(`Moving ${ctx.name}'s disks… follow it in Tasks`);
    setTimeout(() => load("jobs").then(renderTasks).catch(() => {}), 800);
    post(`/vms/${ctx.id}/move-disks`, { moves })
      .then(() => { toast(`${ctx.name}: disks moved`); return refresh(currentPage()); })
      .catch(fail);
    return;
  }
  const body = { node: f.get("node") || null };
  if (f.get("mode") === "both") {
    body.disks = $$("#migrate-form select[data-disk]").filter((s) => s.value !== s.dataset.from).map((s) => ({ index: Number(s.dataset.disk), pool: s.value }));
  }
  if (f.get("copy_storage") === "false") body.copy_storage = false;
  if (f.get("bandwidth_mib")) body.bandwidth_mib = Number(f.get("bandwidth_mib"));
  const job = await post(`/vms/${ctx.id}/migrate`, body);
  toast(`Moving ${ctx.name} from ${job.source} to ${job.target}`);
  await refresh("compute");
};

// ---------- storage ----------
async function renderVirtualStorage() {
  if (window.VirtualStorageUI) return window.VirtualStorageUI.render();
  const box = $("#virtual-storage-status");
  try {
    const data = await api("/virtual-storage");
    const witness = data.nodes.some((n) => n.role === "witness" && n.healthy);
    box.innerHTML = `<p><span class="badge warning">${witness ? "Setup required" : "Waiting for Arbiter"}</span></p>
      <p class="hint">Install Arbiter on VMware and add it as a witness in Cluster management. Shared storage stays inactive until replication and failover have been configured and verified.</p>
      <div class="table-wrap"><table><thead><tr><th>Node</th><th>Role</th><th>Replication readiness</th></tr></thead><tbody>${data.nodes.map((n) => `<tr><td>${esc(n.display_name || n.name)}</td><td>${n.role === "witness" ? "Arbiter" : "Data node"}</td><td>${!n.healthy ? "Offline" : n.error ? esc(n.error) : `DRBD ${esc(n.kernel_version || "unknown")} · ${n.resources.length} resources`}</td></tr>`).join("")}</tbody></table></div>
      ${data.plan ? `<p>Configuration prepared for <b>${esc(data.plan.name)}</b> · Storage VIP ${esc(data.plan.virtual_ip)}. Activation has not been performed.</p><a class="btn small" href="/api/v1/virtual-storage/configuration" download>Download DRBD configuration</a>` : ""}`;
  } catch (err) { box.textContent = `Could not check storage readiness: ${err.message}`; }
}

// The LAN without a tag is the switch port's native VLAN, VLAN 1 unless the switch says otherwise.
const vlanId = (n) => n.vlan ?? 1;
const netLabel = (n) => `${n.name} (VLAN ${vlanId(n)})`;
function netOptions(selected) {
  const nets = state.networks.length ? state.networks : [{ name: "default" }];
  const names = nets.map((n) => n.name);
  const extra = selected && !names.includes(selected) ? [{ name: selected }] : [];
  return [...nets, ...extra].map((n) => `<option value="${esc(n.name)}" ${n.name === selected ? "selected" : ""}>${esc(netLabel(n))}</option>`).join("");
}

// ---------- network topology ----------
// Uplinks (each node's br0 and the ports in it) at the top, a virtual switch per network
// (VLAN) in the middle, VMs at the bottom wired to the switches of their NICs. Click
// anything to see and change it; drag a VM onto a switch to move its NIC there.

const ntopo = { sel: null, nics: {}, drag: null, edit: null };

// Pure draft/planning helpers. Drafts never mutate the live API data.
const topoClone = (value) => JSON.parse(JSON.stringify(value));
const topoNics = (nics) => nics.map((n) => ({ network: n.network, ...(n.mac ? { mac: n.mac } : {}), model: n.model || "virtio" }));
// Change exactly one adapter, preserving order and every other adapter's identity.
function topoLinkNics(nics, index, network, mac) {
  if (!Number.isInteger(index) || !nics[index] || (mac && nics[index].mac !== mac)) throw new Error("This adapter changed. Select its wire again.");
  return topoNics(nics.flatMap((nic, i) => i !== index ? [{ ...nic }] : network === null ? [] : [{ ...nic, network }]));
}
const topoSameNics = (a, b) => JSON.stringify(topoNics(a)) === JSON.stringify(topoNics(b));
function topoPlan(base, draft) {
  const creates = draft.networks.filter((n) => !base.networks.some((b) => b.name === n.name))
    .map((n) => ({ type: "create", name: n.name, body: { name: n.name, vlan: n.vlan } }));
  const patches = draft.vms.filter((v) => {
    const old = base.vms.find((b) => b.uuid === v.uuid);
    return old && !topoSameNics(old.nics, v.nics);
  }).map((v) => ({ type: "patch", uuid: v.uuid, name: v.name, body: { nics: topoNics(v.nics) } }));
  const deletes = base.networks.filter((n) => !draft.networks.some((d) => d.name === n.name))
    .map((n) => ({ type: "delete", name: n.name }));
  return [...creates, ...patches, ...deletes];
}
function topoValidateSwitch(name, vlan, networks) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$/.test(name)) return "Use 1–63 letters, numbers, dots, underscores or hyphens; start with a letter or number.";
  if (!Number.isInteger(vlan) || vlan < 2 || vlan > 4094) return "VLAN ID must be an integer from 2 to 4094.";
  if (networks.some((n) => n.name === name)) return "A switch with this name already exists.";
  if (networks.some((n) => vlanId(n) === vlan)) return "This VLAN ID is already in use.";
  return "";
}
// Rebase only unfinished operations onto freshly loaded data after a partial save.
function topoRebase(edit, live) {
  const remaining = topoPlan(edit.base, edit), draft = topoClone(live), base = topoClone(live);
  for (const step of remaining) {
    if (step.type === "create") {
      const actual = live.networks.find((n) => n.name === step.name);
      // A lost response may still have created the switch. Only accept an exact match.
      const wanted = edit.networks.find((n) => n.name === step.name);
      if (actual && actual.vlan === step.body.vlan && (wanted.nodes || []).every((n) => (actual.nodes || []).includes(n))) continue;
      base.networks = base.networks.filter((n) => n.name !== step.name);
      draft.networks = draft.networks.filter((n) => n.name !== step.name);
      draft.networks.push(topoClone(wanted));
    }
    if (step.type === "delete") draft.networks = draft.networks.filter((n) => n.name !== step.name);
    if (step.type === "patch") {
      const actual = live.vms.find((v) => v.uuid === step.uuid);
      // New adapters receive their MACs from the server; compare their other fields.
      const applied = actual && actual.nics.length === step.body.nics.length && step.body.nics.every((n, i) => {
        const a = topoNics(actual.nics)[i];
        return a.network === n.network && a.model === n.model && (!n.mac || a.mac === n.mac);
      });
      if (applied) continue;
      const old = edit.base.vms.find((v) => v.uuid === step.uuid);
      // Preserve the original baseline on conflicts, so retry cannot overwrite someone else's NICs.
      base.vms = base.vms.filter((v) => v.uuid !== step.uuid);
      base.vms.push(topoClone(old));
      draft.vms = draft.vms.filter((v) => v.uuid !== step.uuid);
      draft.vms.push({ ...topoClone(actual || old), nics: topoClone(step.body.nics) });
    }
  }
  return { ...edit, base, ...draft };
}
function topoData() { return ntopo.edit || state; }
function topoLayoutKey() {
  const c = state.cluster || {};
  return `ibhics-topology-layout:${location.host}:${c.id || c.name || (c.members || []).map((m) => m.name).sort().join(",")}`;
}
function topoReadLayout() {
  try {
    const data = JSON.parse(localStorage.getItem(topoLayoutKey()) || "{}");
    return Object.fromEntries(Object.entries(data).filter(([key, p]) => /^(cluster|node|net|vm):/.test(key) && p && Number.isFinite(p.x) && Number.isFinite(p.y) && p.x >= 85 && p.y >= 35));
  } catch { return {}; }
}
function topoLayoutChanges(edit) {
  return Object.keys({ ...edit.layoutBase, ...edit.layout }).filter((k) => JSON.stringify(edit.layoutBase[k]) !== JSON.stringify(edit.layout[k])).length;
}
function topoDirty() { return ntopo.edit && (topoPlan(ntopo.edit.base, ntopo.edit).length + topoLayoutChanges(ntopo.edit)); }
function topoToolbar() {
  const edit = ntopo.edit, busy = Boolean(edit?.saving);
  $("#topo-edit").disabled = Boolean(ntopo.changing);
  $("#topo-edit").hidden = Boolean(edit) || !canOperate();
  $("#topo-edit-tools").hidden = !edit;
  $("#topo-palette").hidden = !edit;
  $(".ntopo").classList.toggle("editing", Boolean(edit));
  $("#topo-pending").textContent = edit ? `${topoDirty()} pending change${topoDirty() === 1 ? "" : "s"}${busy ? " · Saving…" : ""}` : "";
  for (const id of ["topo-edit-save", "topo-edit-cancel", "topo-auto-layout", "topo-add-switch"]) $("#" + id).disabled = busy;
  $("#topo-add-switch").disabled = busy || !isAdmin();
  $("#topo-refresh").disabled = Boolean(edit) || Boolean(ntopo.changing);
  $('[data-open="net-dialog"]').disabled = Boolean(edit);
  $('[data-tab="net-list"]').disabled = Boolean(edit);
  $$("input, button", $("#topo-switch-form")).forEach((el) => { el.disabled = busy; });
}
function topoStartEdit() {
  if (!canOperate() || ntopo.edit || ntopo.changing) return;
  const base = topoClone({ networks: state.networks, vms: state.vms }), layout = topoReadLayout();
  ntopo.edit = { base, ...topoClone(base), layout, layoutBase: topoClone(layout), saving: false, error: "", completed: [] };
  ntopo.sel = null;
  drawTopology();
}
function topoEndEdit() {
  ntopo.edit = null; ntopo.drag = null; ntopo.sel = null;
  $("#topo-switch-form").hidden = true;
  topoToolbar();
}
async function topoChangeLink(link, network) {
  if (!canOperate() || ntopo.edit?.saving || ntopo.changing || !link) return;
  const data = topoData(), vm = data.vms.find((v) => v.uuid === link.uuid);
  if (!vm) throw new Error("VM no longer exists. Refresh the topology.");
  const nics = topoLinkNics(vm.nics, link.index, network, link.mac);
  if (network !== null && !data.networks.some((n) => n.name === network)) throw new Error("Target switch no longer exists.");
  if (topoSameNics(vm.nics, nics)) return;
  if (ntopo.edit) {
    vm.nics = nics;
    if (network === null) { ntopo.link = null; ntopo.sel = null; }
    drawTopology(); return;
  }
  const action = network === null ? `Disconnect NIC ${link.index + 1} from ${vm.name}? This removes the adapter.` : `Move ${vm.name} NIC ${link.index + 1} to ${network}?`;
  if (!confirm(`${action}\nThe network changes immediately, including while the VM is running.`)) return;
  ntopo.changing = true; topoToolbar(); topoPanel();
  try {
    await topoMove(vm.uuid, nics);
    if (ntopo.link) delete ntopo.link.target;
    if (network === null) { ntopo.link = null; ntopo.sel = null; }
  } finally {
    ntopo.changing = false; drawTopology();
  }
}
function topoDisconnect() { return topoChangeLink(ntopo.link, null); }
function topoRemoveSwitch(name) {
  const edit = ntopo.edit;
  if (!edit || edit.saving || !isAdmin() || name === "default") return;
  if (edit.vms.some((v) => v.nics.some((n) => n.network === name))) { toast("Disconnect all VM adapters from this switch first.", true); return; }
  edit.networks = edit.networks.filter((n) => n.name !== name);
  delete edit.layout[`net:${name}`];
  ntopo.sel = null; drawTopology();
}
function topoStepLabel(step) {
  if (step.type === "create") return `Create switch ${step.name} (VLAN ${step.body.vlan})`;
  if (step.type === "delete") return `Delete switch ${step.name}`;
  return `Update ${step.name}: ${step.body.nics.length} NIC(s) [${step.body.nics.map((n) => n.network).join(", ")}]`;
}
async function topoSaveEdit() {
  const edit = ntopo.edit;
  if (!edit || edit.saving || !canOperate()) return;
  const plan = topoPlan(edit.base, edit), layoutCount = topoLayoutChanges(edit);
  if (!confirm(`Save topology changes?\n${plan.map(topoStepLabel).join("\n")}\n${layoutCount} layout change(s).\nNIC changes take effect immediately, including on running VMs.`)) return;
  edit.saving = true; edit.error = ""; ntopo.drag = null; topoToolbar(); topoPanel();
  const completed = edit.completed || (edit.completed = []);
  let active = "Reload current topology";
  try {
    // Fail before writing if someone else changed an affected VM or switch.
    await load("vms", "networks");
    for (const step of plan) {
      if (step.type !== "patch" && !isAdmin()) throw new Error("Only administrators can create or delete switches.");
      if (step.type === "patch") {
        const live = state.vms.find((v) => v.uuid === step.uuid), old = edit.base.vms.find((v) => v.uuid === step.uuid);
        if (!live || !topoSameNics(live.nics, old.nics)) throw new Error(`${step.name}: adapters changed outside this draft. Cancel and edit the refreshed topology.`);
        if (step.body.nics.some((n) => !edit.networks.some((net) => net.name === n.network))) throw new Error(`${step.name}: a target switch is missing.`);
        const missing = step.body.nics.map((n) => state.networks.find((net) => net.name === n.network)).find((net) => net && net.nodes && live.node && !net.nodes.includes(live.node));
        if (missing) throw new Error(`${step.name}: ${missing.name} is not on ${live.node}. Add it to all nodes in Virtual switches first.`);
      }
      if (step.type === "create") {
        const error = topoValidateSwitch(step.name, step.body.vlan, state.networks);
        if (error) throw new Error(`${step.name}: ${error}`);
      }
      if (step.type === "delete" && (step.name === "default" || edit.vms.some((v) => v.nics.some((n) => n.network === step.name)))) throw new Error("Disconnect all adapters before deleting a non-default switch.");
    }
    for (const step of plan) {
      active = topoStepLabel(step);
      if (step.type === "create") {
        const created = await post("/networks", step.body);
        edit.base.networks.push(created);
        edit.networks = edit.networks.map((n) => n.name === step.name ? topoClone(created) : n);
      } else if (step.type === "patch") {
        const updated = await api(`/vms/${enc(step.uuid)}`, { method: "PATCH", body: JSON.stringify(step.body), signal: AbortSignal.timeout(90000) })
          .catch((err) => { throw err.name === "TimeoutError" ? new Error("the node did not answer within 90 seconds") : err; });
        edit.base.vms = edit.base.vms.map((v) => v.uuid === step.uuid ? topoClone(updated) : v);
        edit.vms = edit.vms.map((v) => v.uuid === step.uuid ? topoClone(updated) : v);
      } else {
        await del(`/networks/${enc(step.name)}`);
        edit.base.networks = edit.base.networks.filter((n) => n.name !== step.name);
      }
      completed.push(active);
    }
    active = "Save layout in this browser";
    localStorage.setItem(topoLayoutKey(), JSON.stringify(edit.layout));
    active = "Reload saved topology";
    await load("vms", "networks");
    topoEndEdit(); renderNetworks(); await renderTopology(); toast("Topology saved.");
  } catch (err) {
    edit.error = `Stopped at: ${active}. ${err.message} Completed: ${completed.join("; ") || "None"}. Unfinished changes remain pending.`;
    try {
      await load("vms", "networks");
      // Keep only unfinished operations, retaining conflict baselines for safe retries.
      ntopo.edit = topoRebase(edit, { networks: state.networks, vms: state.vms });
    } catch (reloadError) { edit.error += ` Reload failed: ${reloadError.message}. Current server state could not be verified.`; }
    ntopo.edit.error = edit.error;
    ntopo.edit.saving = false;
    fail(new Error(edit.error)); renderNetworks(); await renderTopology();
  }
}

const TOPO = { w: 150, gapY: 150, top: 40 };
const SVGNS = "http://www.w3.org/2000/svg";

async function renderTopology() {
  const nodes = computeMembers().filter((m) => m.healthy).map((m) => m.name);
  await Promise.all(nodes.map((n) => (ntopo.nics[n] ? null : api(`/nodes/${enc(n)}/nics`).then((x) => { ntopo.nics[n] = x; }).catch(() => { ntopo.nics[n] = []; }))));
  drawTopology();
}

function drawTopology() {
  const members = computeMembers(), nodes = members.map((m) => m.name);
  const clusterView = Boolean(state.cluster.cluster_id) || nodes.length > 1;
  const clusterKey = `cluster:${state.cluster.cluster_id || state.cluster.name || "local"}`;
  const networkCoverage = (net) => {
    const present = new Set(net.nodes || []), missing = nodes.filter((name) => !present.has(name));
    const variants = new Set(Object.values(net.configurations || {}).map((c) => JSON.stringify([c.bridge,c.vlan ?? 1])));
    return { missing, drift: variants.size > 1, complete: missing.length === 0 && variants.size <= 1 };
  };
  const data = topoData(), edit = ntopo.edit;
  const nets = [...data.networks].sort((a, b) => vlanId(a) - vlanId(b));
  // VMs ordered by their first network, so their wires stay short.
  const netIndex = new Map(nets.map((n, i) => [n.name, i]));
  const vms = [...data.vms].sort((a, b) => (netIndex.get(a.nics[0]?.network) ?? 99) - (netIndex.get(b.nics[0]?.network) ?? 99) || a.name.localeCompare(b.name));
  const cols = Math.max(clusterView ? 1 : nodes.length, nets.length, vms.length, 4);
  let width = cols * TOPO.w + 40;
  const rowX = (count, i) => 20 + (width - 40) * (i + 0.5) / count;
  const pos = {};
  if (clusterView) pos[clusterKey] = { x: rowX(1,0), y: TOPO.top + 30 };
  else nodes.forEach((n, i) => { pos[`node:${n}`] = { x: rowX(nodes.length, i), y: TOPO.top + 30 }; });
  nets.forEach((n, i) => { pos[`net:${n.name}`] = { x: rowX(nets.length, i), y: TOPO.top + 30 + TOPO.gapY }; });
  vms.forEach((v, i) => { pos[`vm:${v.uuid}`] = { x: rowX(vms.length, i), y: TOPO.top + 30 + TOPO.gapY * 2 }; });
  const layout = edit ? edit.layout : topoReadLayout();
  for (const key of Object.keys(pos)) if (layout[key]) pos[key] = layout[key];
  let height = TOPO.top + 30 + TOPO.gapY * 2 + 80;
  for (const p of Object.values(pos)) { width = Math.max(width, p.x + 90); height = Math.max(height, p.y + 85); }

  // A refresh can reorder adapters; never let an old index select another MAC.
  if (ntopo.sel === "link" && ntopo.link) {
    const vm = data.vms.find((v) => v.uuid === ntopo.link.uuid);
    if (ntopo.link.mac && vm) ntopo.link.index = vm.nics.findIndex((n) => n.mac === ntopo.link.mac);
    if (!vm?.nics[ntopo.link.index]) { ntopo.sel = null; ntopo.link = null; }
  }
  let grips = "";
  const wire = (a, b, cls, attrs = "", bend = 0) => {
    const p = pos[a], q = pos[b];
    if (!p || !q) return "";
    const y0 = p.y + 60; // below the parent's label
    const d = `M${p.x + bend * .35},${y0} C${p.x + bend},${(y0 + q.y - 22) / 2 + 10} ${q.x + bend},${(y0 + q.y - 22) / 2 - 10} ${q.x},${q.y - 22}`;
    return `<path ${attrs} class="wire ${cls}" d="${d}"/>${attrs ? `<path ${attrs} class="wire-hit" d="${d}"/>` : ""}`;
  };
  let wires = "";
  for (const n of nets) {
    if (clusterView) wires += wire(clusterKey, `net:${n.name}`, `${n.vlan ? "tagged" : "native"}${networkCoverage(n).complete ? "" : " coverage-warning"}`);
    else for (const name of n.nodes || []) wires += wire(`node:${name}`, `net:${n.name}`, n.vlan ? "tagged" : "native");
  }
  for (const v of vms) v.nics.forEach((nic, index) => {
    const old = edit?.base.vms.find((b) => b.uuid === v.uuid)?.nics || [];
    const pending = edit && !old.some((n) => nic.mac && n.mac === nic.mac && n.network === nic.network);
    const selected = ntopo.sel === "link" && ntopo.link?.uuid === v.uuid && ntopo.link.index === index;
    const peers = v.nics.map((n, i) => n.network === nic.network ? i : -1).filter((i) => i >= 0);
    const bend = (peers.indexOf(index) - (peers.length - 1) / 2) * 55;
    wires += wire(`net:${nic.network}`, `vm:${v.uuid}`, `${v.state === "running" ? "up" : "down"} editable${pending ? " pending" : ""}${selected ? " selected" : ""}`, `data-link-vm="${esc(v.uuid)}" data-link-index="${index}" data-link-mac="${esc(nic.mac || "")}"`, bend);
    const p = pos[`net:${nic.network}`];
    if (selected && p) grips += `<circle class="topo-link-handle" data-link-vm="${esc(v.uuid)}" data-link-index="${index}" data-link-mac="${esc(nic.mac || "")}" cx="${p.x + bend * .35}" cy="${p.y + 60}" r="7"><title>${canOperate() ? "Drag to another switch to move this NIC" : "VM connection"}</title></circle>`;
  });
  // Keep removed connections visible as non-interactive draft ghosts.
  if (edit) for (const old of edit.base.vms) {
    const v = edit.vms.find((x) => x.uuid === old.uuid);
    for (const nic of old.nics) if (nic.mac && v && !v.nics.some((n) => n.mac === nic.mac && n.network === nic.network))
      wires += wire(`net:${nic.network}`, `vm:${old.uuid}`, "removed");
  }

  const label = (x, y, text, sub) => `<text x="${x}" y="${y + 38}" class="t-name">${esc(text)}</text>${sub ? `<text x="${x}" y="${y + 53}" class="t-sub">${esc(sub)}</text>` : ""}`;
  const node = (key, x, y, icon, cls, text, sub) =>
    `<g class="tnode ${cls} ${ntopo.sel === key ? "sel" : ""} ${edit && JSON.stringify(edit.layout[key]) !== JSON.stringify(edit.layoutBase[key]) ? "pending" : ""}" data-key="${esc(key)}" transform="translate(0,0)">
      <rect x="${x - 22}" y="${y - 22}" width="44" height="44" rx="11"/><svg x="${x - (cls.startsWith("vm ") ? 24 : 30)}" y="${y - (cls.startsWith("vm ") ? 24 : 30)}" width="${cls.startsWith("vm ") ? 48 : 60}" height="${cls.startsWith("vm ") ? 48 : 60}" viewBox="0 0 64 64">${NetworkCore.deviceIcon(icon, {stopped: cls.includes("down")})}</svg>${label(x, y, text, sub)}</g>`;
  let shapes = "";
  if (clusterView) {
    const p = pos[clusterKey], online = members.filter((m) => m.healthy).length;
    shapes += node(clusterKey, p.x, p.y, "node", `uplink${online < nodes.length ? " warn" : ""}`, state.cluster.name || "Cluster", `${online}/${nodes.length} nodes online · shared networks`);
  }
  for (const n of clusterView ? [] : nodes) {
    const ports = (ntopo.nics[n] || []).filter((p) => p.master === "br0");
    const sub = ports.length ? ports.map((p) => `${p.name}${p.speed_mbps ? ` ${p.speed_mbps >= 1000 ? `${p.speed_mbps / 1000}G` : `${p.speed_mbps}M`}` : ""}`).join(", ") : "br0";
    const p = pos[`node:${n}`];
    shapes += node(`node:${n}`, p.x, p.y, "node", ports.some((x) => x.link) || !ports.length ? "uplink" : "uplink warn", `${n} · uplink`, sub);
  }
  for (const n of nets) {
    const p = pos[`net:${n.name}`];
    shapes += node(`net:${n.name}`, p.x, p.y, "switch", `switch${edit && !edit.base.networks.some((b) => b.name === n.name) ? " pending" : ""}`, n.name, `${edit && !edit.base.networks.some((b) => b.name === n.name) ? "New · " : ""}VLAN ${vlanId(n)}${n.vlan ? "" : " (native)"}${clusterView ? ` · ${(n.nodes || []).length}/${nodes.length} nodes${networkCoverage(n).drift ? " · config differs" : ""}` : ""}`);
  }
  for (const v of vms) {
    const p = pos[`vm:${v.uuid}`];
    shapes += node(`vm:${v.uuid}`, p.x, p.y, NetworkCore.detectOS(v, state.guests[v.uuid]), `vm ${v.state === "running" ? "up" : "down"}`, v.name, (state.guests[v.uuid]?.ips || v.ips || []).filter(ip => NetworkCore.ipNumber(ip) !== null)[0] || "No IP");
    if (edit) shapes += `<circle class="topo-handle" data-vm="${esc(v.uuid)}" cx="${p.x + 29}" cy="${p.y}" r="7"><title>Drag to a switch to add a NIC</title></circle>`;
  }
  if (ntopo.drag?.link && ntopo.drag.at) {
    const a = pos[ntopo.drag.key], b = ntopo.drag.at;
    if (a) wires += `<path class="wire pending preview" d="M${a.x},${a.y - 22} Q${a.x},${b.y} ${b.x},${b.y}"/>`;
  }
  if (ntopo.drag?.handle && ntopo.drag.at) {
    const a = pos[ntopo.drag.key], b = ntopo.drag.at;
    wires += `<path class="wire pending preview" d="M${a.x + 29},${a.y} L${b.x},${b.y}"/>`;
  }
  $("#topo-svg").setAttribute("viewBox", `0 0 ${width} ${height}`);
  $("#topo-svg").setAttribute("width", width);
  $("#topo-svg").setAttribute("height", height);
  $("#topo-svg").innerHTML = `${wires}${shapes}${grips}`;
  let legend = $("#topo-svg").parentElement.querySelector(".nw-device-legend");
  if (!legend) $("#topo-svg").insertAdjacentHTML("afterend", NetworkCore.deviceLegend());
  ntopo.pos = pos;
  const running = state.vms.filter((v) => v.state === "running").length;
  $("#topo-stats").innerHTML = [[clusterView ? "Cluster nodes" : "Uplinks", nodes.length], ["Virtual switches", nets.length], ["VMs running", running], ["VMs stopped", state.vms.length - running]]
    .map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
  topoToolbar();
  if (!ntopo.drag) topoPanel();
}

function topoPanel() {
  const box = $("#topo-panel"), key = ntopo.sel || "", admin = isAdmin(), op = canOperate();
  const data = topoData(), edit = ntopo.edit;
  if (key === "link" && ntopo.link) {
    const v = data.vms.find((v) => v.uuid === ntopo.link.uuid), nic = v?.nics[ntopo.link.index];
    box.innerHTML = nic ? `<h3>VM connection</h3><dl class="kv"><dt>VM</dt><dd>${esc(v.name)}</dd><dt>Adapter</dt><dd>NIC ${ntopo.link.index + 1}</dd><dt>MAC</dt><dd class="mono">${esc(nic.mac || "New adapter")}</dd><dt>Status</dt><dd><span class="badge ${esc(v.state)}">${esc(v.state)}</span></dd></dl>
      <label>Connected to<select data-topo-link-network ${!op || edit?.saving || ntopo.changing ? "disabled" : ""}>${data.networks.map((n) => `<option value="${esc(n.name)}" ${n.name === (ntopo.link.target || nic.network) ? "selected" : ""}>${esc(n.name)} · VLAN ${vlanId(n)}</option>`).join("")}</select></label>
      ${op ? `${!edit ? `<button class="primary small" data-topo-link-move ${ntopo.changing ? "disabled" : ""}>Move</button>` : ""} <button class="small danger" data-topo-disconnect ${edit?.saving || ntopo.changing ? "disabled" : ""}>Disconnect</button>` : ""}` : "";
  } else {
    const [kind, id] = [key.split(":")[0], key.slice(key.indexOf(":") + 1)];
    if (kind === "cluster") {
      const members = computeMembers();
      box.innerHTML = `<h3>${esc(state.cluster.name || "Cluster")}</h3><p class="hint">One logical cluster network view. Physical uplinks remain separate on each node. VLAN trunk configuration must also match on the upstream switches.</p><div class="topo-list">${members.map((m) => `<a href="#node/${enc(m.name)}">${esc(nodeLabel(m.name))}</a><span class="badge ${m.healthy ? "online" : "offline"}">${m.healthy ? "online" : "offline"}</span><span class="vm-sub">${esc((ntopo.nics[m.name] || []).filter((p) => p.master === "br0").map((p) => p.name).join(", ") || "br0")}</span>`).join("")}</div><a class="btn small" href="#cluster">Manage cluster</a>`;
    } else if (kind === "net") {
      const n = data.networks.find((x) => x.name === id);
      if (!n) { ntopo.sel = null; return topoPanel(); }
      const vms = data.vms.filter((v) => v.nics.some((x) => x.network === n.name));
      box.innerHTML = `<h3>${esc(n.name)}</h3><dl class="kv"><dt>VLAN ID</dt><dd>${vlanId(n)}${n.vlan ? " (tagged)" : " (native, untagged on the wire)"}</dd>
        <dt>Bridge</dt><dd class="mono">${esc(n.bridge)}</dd><dt>Available on</dt><dd>${esc(n.nodes.map(nodeLabel).join(", "))}</dd><dt>Missing on</dt><dd>${esc(computeMembers().filter((m) => !n.nodes.includes(m.name)).map((m) => nodeLabel(m.name)).join(", ")) || "None"}</dd><dt>VMs</dt><dd>${vms.length}</dd></dl>
        <p class="wiz-label">Connected VMs</p><div class="topo-list">${vms.map((v) => `<a class="vm-link" href="#vm/${esc(v.uuid)}">${esc(v.name)}</a>`).join("") || `<span class="muted">None yet. Drag a VM here.</span>`}</div>
        ${admin && n.name !== "default" ? `<button class="small danger" data-topo-del="${esc(n.name)}" style="margin-top:14px">Remove switch</button>` : ""}`;
    } else if (kind === "vm") {
      const v = data.vms.find((x) => x.uuid === id);
      if (!v) { ntopo.sel = null; return topoPanel(); }
      const opts = (cur) => data.networks.map((n) => `<option value="${esc(n.name)}" ${n.name === cur ? "selected" : ""}>${esc(netLabel(n))}</option>`).join("");
      box.innerHTML = `<div class="nw-device-heading ${v.state === "running" ? "up" : "down"}">${NetworkCore.deviceIcon(NetworkCore.detectOS(v, state.guests[v.uuid]), {stopped:v.state !== "running"})}<h3>${esc(v.name)}</h3></div><dl class="kv"><dt>Status</dt><dd><span class="badge ${esc(v.state)}">${esc(v.state)}</span></dd><dt>Node</dt><dd>${esc(v.node || "")}</dd></dl>
        <p class="wiz-label">Network adapters</p>
        ${v.nics.map((nic, i) => `<label>NIC ${i + 1}${nic.mac ? ` <span class="mono muted">${esc(nic.mac)}</span>` : ""}<select data-topo-nic="${i}" ${op && !edit?.saving ? "" : "disabled"}>${opts(nic.network)}</select></label>`).join("") || `<p class="muted">No network adapter.</p>`}
        ${op && !edit && v.nics.length ? `<button class="small primary" data-topo-save="${esc(v.uuid)}" style="margin-top:12px">Apply</button>` : ""}
        <a class="btn small" href="#vm/${esc(v.uuid)}" style="margin-top:12px">Open VM</a>`;
    } else if (kind === "node") {
      const ports = ntopo.nics[id] || [];
      box.innerHTML = `<h3>${esc(id)}</h3><p class="hint" style="padding:0">The node's LAN bridge br0. Every virtual switch leaves the node through these ports; tagged VLANs need the switch port set to trunk.</p>
        <div class="topo-list">${ports.filter((p) => p.kind === "ethernet" || p.kind === "bond").map((p) => `<div class="topo-port ${p.master === "br0" ? "in" : ""}">
          <span class="dot ${p.link ? "up" : ""}"></span><span class="mono">${esc(p.name)}</span><span class="muted">${p.master === "br0" ? "uplink" : "not used"}${p.speed_mbps ? ` · ${p.speed_mbps >= 1000 ? `${p.speed_mbps / 1000} Gb/s` : `${p.speed_mbps} Mb/s`}` : p.link ? "" : " · no cable"}</span></div>`).join("")}</div>
        ${admin ? `<a class="btn small" href="#node/${esc(id)}" style="margin-top:14px">Change uplink ports</a>` : ""}`;
    } else {
      box.innerHTML = `<h3>Topology</h3><p class="hint" style="padding:0">Click a node or wire for details. Select a wire to choose a VLAN or drag its switch-end dot. Drag a VM onto a switch to move its NIC; Edit stages changes until Save.</p>`;
    }
  }
  if (edit) {
    box.insertAdjacentHTML("afterbegin", `<p class="hint topo-edit-hint">Drag nodes to arrange; VM handles add NICs. Select a wire: choose a VLAN, drag its dot to a switch, or Delete to disconnect. Save applies pending changes.</p>${edit.error ? `<p class="topo-error" role="alert">${esc(edit.error)}</p>` : ""}`);
    $$("button, select", box).forEach((el) => { if (edit.saving) el.disabled = true; });
  }
}

// Pointer position in the diagram's own coordinates, however the SVG is scaled.
function svgPoint(svg, e) {
  const pt = svg.createSVGPoint();
  pt.x = e.clientX; pt.y = e.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}

async function topoMove(uuid, nics) {
  if (!canOperate()) return;
  const v = await patch(`/vms/${enc(uuid)}`, { nics: topoNics(nics) });
  toast(`${v.name}: ${v.nics.map((n) => n.network).join(", ") || "No network adapters"}`);
  await load("vms");
  await renderTopology();
}

{
  const svg = $("#topo-svg");
  svg.addEventListener("click", (e) => {
    if (ntopo.edit?.saving || ntopo.changing || ntopo.dragged) { ntopo.dragged = false; return; }
    const g = e.target.closest(".tnode"), link = e.target.closest("[data-link-vm]");
    ntopo.sel = link ? "link" : g ? g.dataset.key : null;
    ntopo.link = link ? { uuid: link.dataset.linkVm, index: Number(link.dataset.linkIndex), mac: link.dataset.linkMac } : null;
    drawTopology();
  });
  svg.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !canOperate() || ntopo.edit?.saving || ntopo.changing) return;
    const link = e.target.closest("[data-link-vm]");
    if (link && (link.matches(".topo-link-handle") || (ntopo.sel === "link" && ntopo.link?.uuid === link.dataset.linkVm && ntopo.link.index === Number(link.dataset.linkIndex)))) {
      const selection = { uuid: link.dataset.linkVm, index: Number(link.dataset.linkIndex), mac: link.dataset.linkMac };
      ntopo.sel = "link"; ntopo.link = selection;
      ntopo.drag = { key: `vm:${selection.uuid}`, link: { ...selection }, x0: e.clientX, y0: e.clientY };
      svg.setPointerCapture(e.pointerId); e.preventDefault(); return;
    }
    const handle = ntopo.edit && e.target.closest(".topo-handle");
    const g = e.target.closest(ntopo.edit ? ".tnode" : ".tnode.vm");
    if (!g && !handle) return;
    const key = handle ? `vm:${handle.dataset.vm}` : g.dataset.key;
    ntopo.drag = { key, handle: Boolean(handle), start: svgPoint(svg, e), origin: { ...ntopo.pos[key] }, savedLayout: ntopo.edit?.layout[key], x0: e.clientX, y0: e.clientY };
    svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  svg.addEventListener("pointermove", (e) => {
    const d = ntopo.drag;
    if (!d) return;
    if (Math.abs(e.clientX - d.x0) + Math.abs(e.clientY - d.y0) > 4) d.moved = true;
    if (!d.moved) return;
    const at = svgPoint(svg, e);
    d.at = at;
    d.over = Object.entries(ntopo.pos).find(([k, q]) => k.startsWith("net:") && k !== d.key && Math.hypot(q.x - at.x, q.y - at.y) < 45)?.[0] || null;
    if (d.link) {
      drawTopology();
    } else if (ntopo.edit) {
      if (!d.handle) ntopo.edit.layout[d.key] = { x: Math.max(85, d.origin.x + at.x - d.start.x), y: Math.max(35, d.origin.y + at.y - d.start.y) };
      drawTopology();
    } else {
      const g = $$(".tnode", svg).find((g) => g.dataset.key === d.key);
      g?.setAttribute("transform", `translate(${at.x - d.start.x},${at.y - d.start.y})`);
    }
    $$(".tnode.switch", svg).forEach((s) => s.classList.toggle("drop", s.dataset.key === d.over));
  });
  svg.addEventListener("pointerup", async (e) => {
    const d = ntopo.drag;
    ntopo.drag = null;
    if (svg.hasPointerCapture(e.pointerId)) svg.releasePointerCapture(e.pointerId);
    if (!d) return;
    ntopo.dragged = true;
    setTimeout(() => { ntopo.dragged = false; }, 0);
    if (d.link) {
      ntopo.sel = "link"; ntopo.link = d.link;
      drawTopology();
      if (d.moved && d.over) {
        try { await topoChangeLink(d.link, d.over.slice(4)); } catch (err) { fail(err); drawTopology(); }
      }
      return;
    }
    ntopo.sel = d.key;
    if (!d.moved || !d.over || !d.key.startsWith("vm:")) { drawTopology(); return; }
    const v = topoData().vms.find((x) => `vm:${x.uuid}` === d.key), net = d.over.slice(4);
    if (!v) { drawTopology(); return; }
    if (ntopo.edit) {
      if (d.handle) v.nics.push({ network: net, model: "virtio" });
      else if (v.nics.length === 1) v.nics[0].network = net;
      else toast(v.nics.length ? "This VM has several adapters: choose the NIC and network in the panel." : "Drag the VM handle to a switch to add an adapter.", true);
      // A connection gesture should keep the VM at its previous position.
      if (!d.handle) {
        if (d.savedLayout) ntopo.edit.layout[d.key] = d.savedLayout; else delete ntopo.edit.layout[d.key];
      }
      drawTopology(); return;
    }
    if (!v.nics.length) { toast(`${v.name} has no network adapter`, true); drawTopology(); return; }
    if (v.nics.length > 1) { toast("This VM has several adapters: pick the one to move on the right", true); drawTopology(); return; }
    if (v.nics[0].network === net || !confirm(`Move ${v.name} to ${net}? Its network changes at once, even while running.`)) { drawTopology(); return; }
    try { await topoMove(v.uuid, [{ ...v.nics[0], network: net }]); } catch (err) { fail(err); drawTopology(); }
  });
  const cancelDrag = () => {
    const d = ntopo.drag;
    if (d && ntopo.edit && !d.handle && !d.link) {
      if (d.savedLayout) ntopo.edit.layout[d.key] = d.savedLayout; else delete ntopo.edit.layout[d.key];
    }
    ntopo.drag = null;
    drawTopology();
  };
  svg.addEventListener("pointercancel", cancelDrag);
  svg.addEventListener("lostpointercapture", () => { if (ntopo.drag) cancelDrag(); });
  $("#topo-panel").addEventListener("change", (e) => {
    if (e.target.matches("[data-topo-link-network]")) {
      if (ntopo.edit) topoChangeLink(ntopo.link, e.target.value).catch(fail);
      else if (canOperate() && !ntopo.changing && ntopo.link) ntopo.link.target = e.target.value;
      return;
    }
    const select = e.target.closest("[data-topo-nic]");
    if (!select || !ntopo.edit || ntopo.edit.saving || !canOperate()) return;
    const vm = ntopo.edit.vms.find((v) => `vm:${v.uuid}` === ntopo.sel);
    if (!vm) return;
    vm.nics[Number(select.dataset.topoNic)].network = select.value;
    drawTopology();
  });
  $("#topo-panel").addEventListener("click", async (e) => {
    if (ntopo.edit?.saving || ntopo.changing || !canOperate()) return;
    const save = e.target.closest("[data-topo-save]");
    const delBtn = e.target.closest("[data-topo-del]");
    try {
      if (e.target.closest("[data-topo-disconnect]")) { await topoDisconnect(); return; }
      if (e.target.closest("[data-topo-link-move]")) { await topoChangeLink(ntopo.link, $("[data-topo-link-network]", $("#topo-panel")).value); return; }
      if (save) {
        const v = topoData().vms.find((x) => x.uuid === save.dataset.topoSave);
        const nics = v.nics.map((n, i) => ({ ...n, network: $(`[data-topo-nic="${i}"]`, $("#topo-panel")).value }));
        if (ntopo.edit) { v.nics = nics; drawTopology(); } else await topoMove(v.uuid, nics);
      }
      if (delBtn && isAdmin()) {
        if (ntopo.edit) { topoRemoveSwitch(delBtn.dataset.topoDel); return; }
        if (confirm(`Remove network ${delBtn.dataset.topoDel}?`)) {
          await del(`/networks/${enc(delBtn.dataset.topoDel)}`);
          ntopo.sel = null; await refresh("network");
        }
      }
    } catch (err) { fail(err); }
  });
  document.addEventListener("keydown", (e) => {
    if (!ntopo.edit || ntopo.edit.saving || currentPage() !== "network" || !["Delete", "Backspace"].includes(e.key) || e.target.closest("input, select, textarea, button, a, [contenteditable], dialog")) return;
    if (ntopo.sel === "link") { e.preventDefault(); topoDisconnect().catch(fail); }
    else if (ntopo.sel?.startsWith("net:")) { e.preventDefault(); topoRemoveSwitch(ntopo.sel.slice(4)); }
  });
  $("#topo-edit").addEventListener("click", topoStartEdit);
  $("#topo-edit-save").addEventListener("click", () => topoSaveEdit().catch(fail));
  $("#topo-edit-cancel").addEventListener("click", () => {
    if (ntopo.edit?.saving || (topoDirty() && !confirm("Discard all pending topology changes? Completed API operations cannot be undone."))) return;
    topoEndEdit(); renderTopology();
  });
  $("#topo-auto-layout").addEventListener("click", () => {
    if (!ntopo.edit || ntopo.edit.saving) return;
    ntopo.edit.layout = {}; drawTopology();
  });
  const switchForm = $("#topo-switch-form");
  const openSwitchForm = (at = null) => {
    if (!ntopo.edit || ntopo.edit.saving || !isAdmin()) return;
    ntopo.newSwitchAt = at;
    switchForm.reset(); switchForm.hidden = false;
    $("#topo-switch-error").textContent = "";
    switchForm.elements.name.focus();
  };
  $("#topo-add-switch").addEventListener("click", () => openSwitchForm());
  $("#topo-add-switch").addEventListener("dragstart", (e) => {
    if (!ntopo.edit || ntopo.edit.saving || !isAdmin()) { e.preventDefault(); return; }
    e.dataTransfer.setData("application/x-ibhics-switch", "switch");
    e.dataTransfer.effectAllowed = "copy";
  });
  svg.addEventListener("dragover", (e) => {
    if (ntopo.edit && !ntopo.edit.saving && isAdmin() && [...e.dataTransfer.types].includes("application/x-ibhics-switch")) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; }
  });
  svg.addEventListener("drop", (e) => {
    if (!ntopo.edit || !isAdmin() || e.dataTransfer.getData("application/x-ibhics-switch") !== "switch") return;
    e.preventDefault(); openSwitchForm(svgPoint(svg, e));
  });
  $("#topo-switch-cancel").addEventListener("click", () => { switchForm.hidden = true; });
  switchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const edit = ntopo.edit;
    if (!edit || edit.saving || !isAdmin() || !switchForm.reportValidity()) return;
    const name = switchForm.elements.name.value, vlan = Number(switchForm.elements.vlan.value);
    // Do not permit delete/recreate of an existing name or VLAN in the same batch.
    const error = topoValidateSwitch(name, vlan, [...edit.networks, ...edit.base.networks.filter((n) => !edit.networks.some((x) => x.name === n.name))]);
    if (error) { $("#topo-switch-error").textContent = error; return; }
    edit.networks.push({ name, vlan, nodes: computeMembers().map((n) => n.name), bridge: "Pending" });
    if (ntopo.newSwitchAt) edit.layout[`net:${name}`] = { x: Math.max(85, ntopo.newSwitchAt.x), y: Math.max(35, ntopo.newSwitchAt.y) };
    switchForm.hidden = true; ntopo.sel = `net:${name}`; drawTopology();
  });
  $("#topo-refresh").addEventListener("click", () => { if (ntopo.edit) return; ntopo.nics = {}; refresh("network").catch(fail); });
}

function renderNetworks() {
  const admin = isAdmin();
  $("#networks").innerHTML = state.networks.map((n) => {
    const vms = state.vms.filter((v) => v.nics.some((x) => x.network === n.name)).length;
    return `<tr><td><div class="name-cell">${avatar("i-nic")}<span class="vm-name">${esc(n.name)}</span></div></td>
      <td>VLAN ${vlanId(n)}${n.vlan ? "" : ` <span class="muted">(native)</span>`}</td><td class="mono">${esc(n.bridge)}</td>
      <td>${esc(n.nodes.join(", "))}</td><td>${vms}</td>
      <td><div class="actions">${admin && n.name !== "default" ? `<button class="small danger" data-net-del="${esc(n.name)}">Remove</button>` : ""}</div></td></tr>`;
  }).join("") || empty(6, "No networks");
}
$("#networks").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-net-del]");
  if (!btn || !confirm(`Remove network ${btn.dataset.netDel}?`)) return;
  try { await del(`/networks/${enc(btn.dataset.netDel)}`); await refresh("network"); } catch (err) { fail(err); }
});
handlers["net-dialog"] = async (f) => {
  await post("/networks", { name: f.get("name"), vlan: Number(f.get("vlan")) });
  toast(`Network ${f.get("name")} added on every compute node`);
  await refresh("network");
};

function renderPools() {
  const admin = isAdmin();
  const pools = StorageView.pools(state.pools).filter((p) => !window.VirtualStorageUI?.ownsPool(p));
  $("#pools").innerHTML = pools.length ? pools.map((p) => {
    const used = pct(p.allocation_gb, p.capacity_gb);
    return `<tr>
      <td data-label="Pool"><div class="name-cell pool-link" data-pool-view="${esc(p.name)}" data-node="${esc(p.node)}">${avatar("i-storage")}<span class="vm-name">${esc(p.display_name || p.name)}${p.display_name ? `<span class="vm-sub">Pool ID: ${esc(p.name)}</span>` : ""}</span></div></td>
      <td data-label="Scope / nodes">${p.shared ? `<b>Shared · ${esc(state.cluster?.name || "Cluster")}</b><span class="vm-sub">${p.members.map((m) => esc(nodeLabel(m.node))).join(", ")}</span>` : esc(nodeLabel(p.node))}</td>
      <td data-label="Type"><span class="badge info plain">${esc(poolTypeText(p))}</span></td>
      <td data-label="Source" class="vm-sub" style="display:table-cell">${esc(p.source)}</td>
      <td data-label="Usage"><div class="bar-row"><span>${p.allocation_gb} / ${p.capacity_gb} GB</span><span>${used}%</span>${bar(used)}</div></td>
      <td data-label="Actions"><div class="actions">${admin ? `<button class="small" data-pool-rename="${esc(p.name)}" data-node="${esc(p.node)}">Rename</button>${p.shared ? "" : `<button class="small danger" data-pool="${esc(p.name)}" data-node="${esc(p.node)}">Remove</button>`}` : ""}</div></td>
    </tr>`;
  }).join("") : empty(6, "No local or external storage pools");
}

async function renamePoolDisplayName(node, name) {
  const pool = state.pools.find((p) => p.name === name && p.node === node);
  if (!pool) throw new Error("Storage pool is unavailable. Refresh and try again.");
  const label = prompt(`Storage display name (${name}). Leave empty to use the Pool ID.`, pool.display_name || "");
  if (label == null) return;
  const group = StorageView.pools(state.pools).find((p) => p.members.some((m) => m.node === node && m.name === name));
  for (const member of group?.members || [pool]) {
    await patch(`/nodes/${enc(member.node)}/pools/${enc(member.name)}`, { display_name: label.trim() });
  }
  toast("Storage name updated"); await refresh("storage");
}

$("#pools").addEventListener("click", async (e) => {
  const rename = e.target.closest("[data-pool-rename]");
  if (rename) {
    const { node, poolRename: name } = rename.dataset;
    try { await renamePoolDisplayName(node, name); } catch (err) { fail(err); }
    return;
  }
  const link = e.target.closest("[data-pool-view]");
  if (link) { openPoolView(link.dataset.node, link.dataset.poolView); return; }
  const btn = e.target.closest("button[data-pool]");
  if (!btn || !confirm(`Remove pool ${btn.dataset.pool} from ${btn.dataset.node}? Data on the storage stays.`)) return;
  try {
    await del(`/nodes/${enc(btn.dataset.node)}/pools/${enc(btn.dataset.pool)}`);
    await refresh("storage");
  } catch (err) { fail(err); }
});

// ---------- storage detail: summary and files ----------

const pv = { node: "", pool: "", path: "" };
const pvDlg = $("#pool-view");
function openPoolView(node, pool) {
  Object.assign(pv, { node, pool, path: "" });
  selectTab("pv-tabs", "pv-summary");
  pvSummary();
  pvDlg.showModal();
  pvFiles();
}
function pvInfo() { return state.pools.find((p) => p.node === pv.node && p.name === pv.pool); }
function pvSummary() {
  const p = pvInfo();
  if (!p) return;
  $(".pv-title", pvDlg).textContent = `${poolText(p)} on ${nodeLabel(p.node)}`;
  const used = pct(p.allocation_gb, p.capacity_gb), free = p.available_gb ?? p.capacity_gb - p.allocation_gb;
  $(".pv-stats", pvDlg).innerHTML = `
    <div class="stat"><span>Capacity</span><b>${sizeText(p.capacity_gb)}</b><em>${esc(poolTypeText(p))}</em></div>
    <div class="stat"><span>Used</span><b>${sizeText(p.allocation_gb)} <small>${used}%</small></b>${bar(used)}</div>
    <div class="stat"><span>Free</span><b>${sizeText(free)}</b><em>${p.active ? "Online" : "Offline"}</em></div>`;
  const disks = state.vms.filter((v) => v.node === p.node).flatMap((v) => v.disks.map((d, i) => ({ vm: v, d, i }))).filter((x) => x.d.pool === p.name);
  const isos = state.isos.filter((i) => i.pool === p.name);
  $(".pv-kv", pvDlg).innerHTML = [["Node", esc(nodeOptionText(p.node))], ["Type", esc(poolTypeText(p))], ["Source", `<span class="mono">${esc(p.source)}</span>`],
    ["VM disks", `${disks.length} (${sizeText(disks.reduce((s, x) => s + x.d.size_gb, 0))} provisioned)`], ["ISOs", String(isos.length)]]
    .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  $(".pv-disks", pvDlg).innerHTML = disks.length ? disks.map((x) => `<tr><td class="vm-name"><a class="vm-link" href="#vm/${esc(x.vm.uuid)}">${esc(x.vm.name)}</a></td>
    <td>Disk ${x.i + 1}</td><td>${num(x.d.size_gb)} GB</td><td><span class="badge ${esc(x.vm.state)}">${esc(x.vm.state)}</span></td></tr>`).join("")
    : empty(4, "No VM disks here yet");
  const files = ["dir", "disk", "nfs"].includes(p.type);
  $('#pv-tabs [data-tab="pv-files"]').hidden = !files;
  pvCache(p);
}
// A hybrid pool's SSD cache: how well it works, and changing it.
function pvCache(p) {
  const box = $(".pv-cache", pvDlg), c = p.cache;
  box.hidden = !p.hybrid;
  if (!p.hybrid) return;
  const pctText = (v) => (v == null ? "–" : `${v}%`);
  box.innerHTML = `<p class="wiz-label">SSD cache</p>` + (c ? `<div class="stats" style="padding:0">
    <div class="stat"><span>Hit ratio</span><b>${pctText(c.hit_pct)}</b><em>Reads ${pctText(c.read_hit_pct)} · writes ${pctText(c.write_hit_pct)}</em></div>
    <div class="stat"><span>Cache size</span><b>${sizeText(c.size_gb)} <small>${c.used_pct}% used</small></b>${bar(c.used_pct)}</div>
    <div class="stat"><span>Mode</span><b>${esc(c.mode)}</b><em>Dirty: ${c.dirty_gb} GB not yet on the HDDs</em></div></div>
    <div class="wiz-bar operator-only" style="margin-top:10px">
      <button type="button" data-cache="mode" data-mode="${c.mode === "writeback" ? "writethrough" : "writeback"}">Switch to ${c.mode === "writeback" ? "writethrough" : "writeback"}</button>
      <button type="button" class="danger" data-cache="remove">Remove cache</button></div>`
    : `<p class="hint">No cache: the pool runs at the speed of its HDDs.</p>
    <div class="wiz-bar operator-only"><label class="inline">SSD <select class="pv-cache-dev"><option value="">Loading…</option></select></label>
      <label class="inline">Mode <select class="pv-cache-mode"><option>writethrough</option><option>writeback</option></select></label>
      <label class="inline">Size GB <input type="number" min="1" class="pv-cache-gb" placeholder="all" style="width:80px"></label>
      <button type="button" data-cache="add">Add cache</button></div>`);
  if (!c) api(`/nodes/${enc(p.node)}/disks`).then((disks) => {
    const sel = $(".pv-cache-dev", box);
    if (sel) sel.innerHTML = disks.filter((d) => d.empty).map((d) => `<option value="${esc(d.path)}">${esc(d.path)} ${sizeText(d.size_gb)} ${esc((d.media || "").toUpperCase())}</option>`).join("") || `<option value="">No empty disk</option>`;
  }).catch(() => {});
}
$(".pv-cache", pvDlg).addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-cache]");
  if (!b) return;
  const p = pvInfo(), act = b.dataset.cache, box = $(".pv-cache", pvDlg);
  const body = { action: act, mode: b.dataset.mode || "writethrough" };
  if (act === "add") {
    body.devices = [$(".pv-cache-dev", box).value].filter(Boolean);
    body.mode = $(".pv-cache-mode", box).value;
    body.size_gb = Number($(".pv-cache-gb", box).value) || null;
    body.confirm_wipe = true;
    if (!body.devices.length) { toast("Pick an empty SSD", true); return; }
    if (!confirm(`Erase ${body.devices[0]} and use it as the cache of ${p.name}?`)) return;
  }
  if (act === "remove" && !confirm(`Remove the SSD cache of ${p.name}? Dirty blocks are written to the HDDs first; VMs keep running.`)) return;
  if (body.mode === "writeback" && act !== "remove" && !confirm("Writeback keeps new writes only on the SSD until they are copied to the HDDs: use it only with a mirrored (RAID 1) SSD. Continue?")) return;
  b.disabled = true;
  try {
    await post(`/nodes/${enc(p.node)}/pools/${enc(p.name)}/cache`, body);
    toast(`${p.name}: cache ${act === "mode" ? `now ${body.mode}` : act === "add" ? "added" : "removed"}`);
    await refresh("storage");
    pvSummary();
  } catch (err) { fail(err); } finally { b.disabled = false; }
});
async function pvFiles() {
  const p = pvInfo();
  if (!p || !["dir", "disk", "nfs"].includes(p.type)) return;
  const parts = pv.path ? pv.path.split("/") : [];
  $(".pv-crumb", pvDlg).innerHTML = [`<button type="button" data-go="">${esc(pv.pool)}</button>`,
    ...parts.map((n, i) => `<svg><use href="#i-chev"/></svg><button type="button" data-go="${esc(parts.slice(0, i + 1).join("/"))}">${esc(n)}</button>`)].join("");
  $(".pv-up-btn", pvDlg).hidden = !pv.path; // uploads go into a folder, not next to the VM disks
  $(".pv-note", pvDlg).hidden = !!pv.path;
  const tbody = $(".pv-files", pvDlg);
  tbody.innerHTML = empty(4, "Loading...");
  try {
    const items = await api(`/nodes/${enc(pv.node)}/pools/${enc(pv.pool)}/files?path=${enc(pv.path)}`);
    const op = canOperate(), admin = isAdmin();
    tbody.innerHTML = items.length ? items.map((f) => `<tr ${f.dir ? `data-dir="${esc(f.path)}"` : ""}>
      <td><div class="name-cell">${avatar(f.dir ? "i-storage" : f.name.toLowerCase().endsWith(".iso") ? "i-cd" : "i-hdd")}<span class="vm-name">${esc(f.name)}</span></div></td>
      <td>${f.dir ? "Folder" : size(f.size_bytes)}</td>
      <td>${f.modified ? esc(new Date(f.modified * 1000).toLocaleString()) : ""}</td>
      <td><div class="actions">${admin && (f.dir || pv.path) ? `<button type="button" class="small danger" data-del="${esc(f.path)}">Delete</button>` : ""}${op ? "" : ""}</div></td>
    </tr>`).join("") : empty(4, pv.path ? "Empty folder. Upload files into it." : "No files yet. Make a folder such as iso.");
  } catch (err) { tbody.innerHTML = empty(4, err.message); }
}
{
  $("#pv-tabs").addEventListener("click", (e) => { if (e.target.closest('[data-tab="pv-files"]')) pvFiles(); });
  $(".pv-crumb", pvDlg).addEventListener("click", (e) => {
    const b = e.target.closest("button[data-go]");
    if (b) { pv.path = b.dataset.go; pvFiles(); }
  });
  $(".pv-files", pvDlg).addEventListener("click", async (e) => {
    const del = e.target.closest("button[data-del]");
    if (del) {
      if (!confirm(`Delete ${del.dataset.del}${e.target.closest("tr[data-dir]") ? " and everything in it" : ""}?`)) return;
      try { await del_(`/nodes/${enc(pv.node)}/pools/${enc(pv.pool)}/files?path=${enc(del.dataset.del)}`); } catch (err) { fail(err); }
      await Promise.all([pvFiles(), load("isos").catch(() => {})]);
      return;
    }
    const dir = e.target.closest("tr[data-dir]");
    if (dir) { pv.path = dir.dataset.dir; pvFiles(); }
  });
  $(".pv-refresh", pvDlg).addEventListener("click", pvFiles);
  $(".pv-mkdir", pvDlg).addEventListener("click", async () => {
    const name = prompt("Folder name", pv.path ? "" : "iso");
    if (!name) return;
    try {
      await post(`/nodes/${enc(pv.node)}/pools/${enc(pv.pool)}/folders`, { path: pv.path ? `${pv.path}/${name}` : name });
      pvFiles();
    } catch (err) { fail(err); }
  });
  $(".pv-upload", pvDlg).addEventListener("change", async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    for (const file of files) {
      const row = document.createElement("div");
      row.className = "upload-row";
      row.innerHTML = `<span class="vm-name">${esc(file.name)}</span><span class="bar"><i></i></span><span class="pct">0%</span>`;
      $(".pv-progress", pvDlg).append(row);
      try {
        await uploadFile(file, row, `/api/v1/nodes/${enc(pv.node)}/pools/${enc(pv.pool)}/files?path=${enc(`${pv.path}/${file.name}`)}`);
        $(".pct", row).textContent = "Uploaded";
        setTimeout(() => row.parentNode && row.parentNode.removeChild(row), 4000);
      } catch (err) { row.classList.add("failed"); $(".pct", row).textContent = err.message; }
    }
    await Promise.all([pvFiles(), load("isos").then(() => currentPage() === "storage" && renderIsos()).catch(() => {})]);
  });
}
const del_ = (path) => del(path);


// ---------- ISO library ----------

// Options for an ISO picker; an ISO set by hand that is not in the library stays listed.
function isoOptions(selected = "", none = "None") {
  const items = state.isos.map((i) => [i.path, `${i.name} (${gbText(Math.max(1, Math.round(i.size_bytes / 1048576)))}${i.pool ? `, ${i.pool}` : ""})`]);
  if (selected && !items.some(([p]) => p === selected)) items.push([selected, selected.split("/").pop()]);
  return [["", none], ...items].map(([p, label]) =>
    `<option value="${esc(p)}" ${p === selected ? "selected" : ""}>${esc(label)}</option>`).join("");
}

function renderIsos() {
  fillIsoDest();
  const dest = $("#iso-dest").value;
  const [node, pool] = dest.split("|");
  const isos = state.isos.filter((i) => dest ? i.pool === pool && (!i.node || i.node === node) : !i.pool);
  $("#iso-location").textContent = `Install media for new VMs and the CD/DVD drive, kept in ${dest ? `${pool} / iso on ${node}` : "/var/lib/ibhics/iso"}`;
  const users = (path) => state.vms.filter((v) => v.iso === path).map((v) => v.name);
  $("#isos").innerHTML = isos.length ? isos.map((i) => {
    const own = !i.pool && i.name !== "ibHiCS guest tools";
    const where = i.pool ? `${i.pool}${i.folder ? ` / ${i.folder}` : ""}` : own ? "System disk" : "Built in";
    return `<tr>
      <td data-label="ISO"><div class="name-cell">${avatar("i-cd")}<span class="vm-name">${esc(i.name)}</span></div></td>
      <td data-label="Stored on">${esc(where)}</td>
      <td data-label="Size">${gbText(Math.max(1, Math.round(i.size_bytes / 1048576)))}</td>
      <td data-label="In CD/DVD drive">${esc(users(i.path).join(", ") || "–")}</td>
      <td data-label="Actions"><div class="actions">${own && canOperate() ? `<button class="small danger" data-iso-del="${esc(i.name)}">Delete</button>` : ""}</div></td>
    </tr>`;
  }).join("") : empty(5, "No ISOs in this storage yet: upload one to install a new VM from it");
}

// Where "Upload ISO" saves: the system disk's ISO library, or the iso folder of a
// file-based pool (folder, thin disk, NFS), which has far more room.
function fillIsoDest() {
  const sel = $("#iso-dest");
  if (!sel) return;
  const was = sel.value;
  const initialized = sel.options.length > 0;
  const pools = state.pools.filter((p) => p.active && ["dir", "disk", "nfs"].includes(p.type));
  sel.innerHTML = `<option value="">System disk (ISO library)</option>` + pools.map((p) =>
    `<option value="${esc(`${p.node}|${p.name}`)}">${esc(poolText(p))} / iso · ${esc(nodeOptionText(p.node))} (${Math.floor(p.available_gb).toLocaleString()} GB free)</option>`).join("");
  if (initialized && [...sel.options].some((o) => o.value === was)) sel.value = was;
  else if (pools.length) sel.value = `${pools.sort((a, b) => b.available_gb - a.available_gb)[0].node}|${pools[0].name}`;
}
$("#iso-dest").addEventListener("change", () => renderIsos());
async function isoUploadUrl(file, input) {
  const dest = input.closest("#page-storage") ? $("#iso-dest").value : "";
  if (!dest) return `/api/v1/isos/${enc(file.name)}`;
  const [node, pool] = dest.split("|");
  const base = `/nodes/${enc(node)}/pools/${enc(pool)}`;
  try { await post(`${base}/folders`, { path: "iso" }); } catch { /* already there */ }
  return `/api/v1${base}/files?path=${enc(`iso/${file.name}`)}`;
}

$("#isos").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-iso-del]");
  if (!btn || !confirm(`Delete ${btn.dataset.isoDel}?`)) return;
  try { await del(`/isos/${enc(btn.dataset.isoDel)}`); await refresh("storage"); } catch (err) { fail(err); }
});

// Any "Upload ISO" button: progress shows next to it, and a picker beside it selects the
// new ISO once it is in.
document.addEventListener("change", async (e) => {
  const input = e.target.closest(".iso-upload");
  if (!input) return;
  const files = [...input.files];
  input.value = "";
  const scope = input.closest("form, .sheet") || document;
  const box = $(".iso-progress", scope);
  let last = null;
  for (const file of files) {
    const row = document.createElement("div");
    row.className = "upload-row";
    row.innerHTML = `<span class="vm-name">${esc(file.name)}</span><span class="bar"><i></i></span><span class="pct">0%</span>`;
    box.append(row);
    try {
      await uploadFile(file, row, await isoUploadUrl(file, input));
      $(".pct", row).textContent = "Uploaded";
      setTimeout(() => row.remove(), 5000);
      last = file.name;
    } catch (err) {
      row.classList.add("failed");
      $(".pct", row).textContent = err.message;
    }
  }
  await load("isos");
  fillSelects();
  const picker = $(".iso-list", scope);
  const added = state.isos.find((i) => i.name === last);
  if (picker && added) {
    picker.value = added.path;
    picker.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (currentPage() === "storage") renderIsos();
});

// ---------- Add storage wizard ----------
// Three steps: what kind of storage, which disks (or where the share is), then a name
// and a clear warning about what gets erased. Local disks are picked from a table with
// SSD/HDD shown, and several disks make one pool.

const poolWiz = {
  form: $("#pool-form"),
  step: 1,
  disks: {}, // node -> [disk] (null while loading)
  picked: new Set(),
  media: "",
  nameTouched: false,
};
const KIND_LABEL = { local: "Local disks", hybrid: "Hybrid (HDD + SSD cache)", fc: "Fibre Channel", nfs: "NFS", iscsi: "iSCSI", dir: "Folder" };
const DISK_KINDS = ["local", "fc", "hybrid"];
const poolTypeText = (p) => (p.hybrid ? (p.cache ? "Hybrid · SSD cache" : "Hybrid · no cache") : POOL_TYPES[p.type] || p.type);
// Hybrid pools: SSD/NVMe disks cache, the rest (HDD, md RAID) hold data, unless changed.
const wizIsCache = (d) => (poolWiz.role[d.path] ?? (d.media === "ssd" || d.media === "nvme")) === true;
const sizeText = (gb) => (gb >= 1000 ? `${(gb / 1024).toFixed(gb >= 10240 ? 1 : 2)} TB` : `${Math.round(gb)} GB`);
const isFcDisk = (d) => d.transport === "fc" || d.path.startsWith("/dev/mapper/");

function wizKind() { return poolWiz.form.elements.kind.value; }
function wizNode() { return poolWiz.form.elements.node.value; }
function wizDiskList() {
  const all = poolWiz.disks[wizNode()] || [];
  return all.filter((d) => (wizKind() === "fc" ? isFcDisk(d) : !isFcDisk(d)));
}
function wizPicked() { return wizDiskList().filter((d) => poolWiz.picked.has(d.path)); }

function wizStart() {
  Object.assign(poolWiz, { step: 1, disks: {}, picked: new Set(), media: "", nameTouched: false, role: {} });
  $$(".chips button", poolWiz.form).forEach((b) => b.classList.toggle("on", b.dataset.media === ""));
  $(".disk-search", poolWiz.form).value = "";
  $(".node-check-list", poolWiz.form).innerHTML = computeMembers().filter((m) => m.healthy).map((m, i) =>
    `<label class="check"><input type="checkbox" name="nodes" value="${esc(m.name)}" ${i === 0 ? "checked" : ""}> ${esc(nodeOptionText(m.name))}</label>`).join("")
    || `<p class="hint">No node is online.</p>`;
  wizShow();
}

function wizShow() {
  const { form, step } = poolWiz, kind = wizKind();
  $$(".wiz-steps li", form).forEach((li) => {
    const n = Number(li.dataset.step);
    li.classList.toggle("on", n === step);
    li.classList.toggle("done", n < step);
  });
  $(".wiz-step2", form).textContent = DISK_KINDS.includes(kind) ? "Disks" : kind === "dir" ? "Folder" : "Connection";
  $$(".wiz-page", form).forEach((p) => {
    p.hidden = Number(p.dataset.page) !== step || (p.dataset.kinds && !p.dataset.kinds.split(" ").includes(kind));
  });
  $$("[data-kinds]:not(.wiz-page)", form).forEach((el) => { el.hidden = !el.dataset.kinds.split(" ").includes(kind); });
  $(".wiz-back", form).hidden = step === 1;
  $(".wiz-next", form).hidden = step === 3;
  $(".wiz-ok", form).hidden = step !== 3;
  if (step === 2 && DISK_KINDS.includes(kind)) wizLoadDisks();
  if (step === 3) wizReview();
}

async function wizLoadDisks(force = false) {
  const node = wizNode();
  if (!node) { wizRenderDisks(); return; }
  if (poolWiz.disks[node] !== undefined && !force) { wizRenderDisks(); return; }
  poolWiz.disks[node] = null;
  wizRenderDisks();
  try { poolWiz.disks[node] = await api(`/nodes/${enc(node)}/disks`); } catch (err) { poolWiz.disks[node] = []; fail(err); }
  if (wizNode() === node) wizRenderDisks();
}

function wizRenderDisks() {
  const { form } = poolWiz, node = wizNode(), kind = wizKind();
  const rows = $(".disk-rows", form);
  if (!node) { rows.innerHTML = empty(6, "No node is online"); return wizPickText(); }
  if (poolWiz.disks[node] === null) { rows.innerHTML = empty(6, `Looking for disks on ${node}...`); return wizPickText(); }
  const q = $(".disk-search", form).value.trim().toLowerCase();
  const list = wizDiskList().filter((d) => (!poolWiz.media || d.media === poolWiz.media)
    && (!q || [d.path, d.model, d.serial, d.contents].join(" ").toLowerCase().includes(q)));
  rows.innerHTML = list.length ? list.map((d) => {
    const on = poolWiz.picked.has(d.path);
    return `<tr data-path="${esc(d.path)}" class="${on ? "sel" : ""}">
      <td><input type="checkbox" ${on ? "checked" : ""} aria-label="Select ${esc(d.path)}"></td>
      <td>${d.empty ? `<span class="badge empty-disk">Empty</span>` : `<span class="badge has-data">Has data</span>`}</td>
      <td><span class="vm-name mono">${esc(d.path)}</span><span class="vm-sub">${esc([d.model, d.transport.toUpperCase()].filter(Boolean).join(" · "))}</span></td>
      <td>${d.media ? `<span class="media ${esc(d.media)}">${esc(d.media.toUpperCase())}</span>` : "–"}</td>
      <td style="white-space:nowrap">${sizeText(d.size_gb)}</td>
      <td class="vm-sub" style="display:table-cell">${esc(d.empty ? (d.serial ? `S/N ${d.serial}` : "") : d.contents)}</td>
    </tr>`;
  }).join("") : empty(6, kind === "fc"
    ? `No FC LUN found on ${node}. Zone the LUN to this node on the SAN, then Rescan.`
    : poolWiz.media || q ? "No disk matches the filter" : `No free disk on ${node}. Insert a disk into the node, then Rescan.`);
  const shown = list.map((d) => d.path);
  const all = $(".disk-all", form);
  all.checked = shown.length > 0 && shown.every((p) => poolWiz.picked.has(p));
  all.indeterminate = !all.checked && shown.some((p) => poolWiz.picked.has(p));
  wizPickText();
}

function wizMixNote(picked) {
  const kinds = new Set(picked.map((d) => d.media).filter(Boolean));
  return kinds.size > 1
    ? `<div class="wiz-note warn">SSD and HDD are mixed in one pool. The pool runs only as fast as its HDDs; separate pools are better.</div>` : "";
}

function wizPickText() {
  const picked = wizPicked();
  const box = $(".wiz-pick", poolWiz.form);
  if (!picked.length) { box.innerHTML = `<span class="muted">Tick the disks for the new pool.</span>`; return; }
  const total = picked.reduce((s, d) => s + d.size_gb, 0);
  if (wizKind() === "hybrid") {
    const sum = (cache) => sizeText(picked.filter((d) => wizIsCache(d) === cache).reduce((s, d) => s + d.size_gb, 0));
    box.innerHTML = `<b>Data ${sum(false)} · Cache ${sum(true)}</b><div class="hybrid-roles">${picked.map((d) => `<label class="inline"><span class="mono">${esc(d.path)}</span>
      <select data-role="${esc(d.path)}"><option value="data" ${wizIsCache(d) ? "" : "selected"}>Data (HDD / RAID)</option><option value="cache" ${wizIsCache(d) ? "selected" : ""}>SSD cache</option></select></label>`).join("")}</div>`;
    return;
  }
  box.innerHTML = `<b>${picked.length} disk${picked.length > 1 ? "s" : ""} selected</b> · ${sizeText(total)} in total${wizMixNote(picked)}`;
}

function wizSuggestName() {
  const el = poolWiz.form.elements.name;
  if (poolWiz.nameTouched && el.value) return;
  const kind = wizKind(), picked = wizPicked(), f = poolWiz.form.elements;
  const media = new Set(picked.map((d) => d.media));
  let base = { nfs: "nas", iscsi: "iscsi", fc: "san", dir: "local", hybrid: "hybrid" }[kind] || (media.size === 1 ? `${[...media][0] || "data"}` : "data");
  if (kind === "nfs" && f.host.value) base = `nas-${f.host.value.split(".").pop()}`;
  if (kind === "local" || kind === "hybrid") base += `-${wizNode()}`;
  base = base.replace(/[^a-zA-Z0-9_.-]/g, "-").replace(/^[^a-zA-Z0-9]+/, "") || "pool";
  const taken = new Set(state.pools.map((p) => p.name));
  let name = base;
  for (let i = 2; taken.has(name); i++) name = `${base}-${i}`;
  el.value = name;
}

function wizReview() {
  const { form } = poolWiz, f = form.elements, kind = wizKind(), picked = wizPicked();
  wizSuggestName();
  const fmt = $$('input[name="format"]', form);
  if (kind === "local") {
    const thin = fmt.find((r) => r.value === "disk");
    thin.disabled = picked.length > 1;
    if (!$('input[name="format"]:checked', form) || thin.disabled) fmt.find((r) => r.value === (picked.length > 1 ? "lvm" : "disk")).checked = true;
  }
  const rows = [["Type", KIND_LABEL[kind]]];
  if (kind === "hybrid") {
    const line = (d) => `${esc(d.path)} (${sizeText(d.size_gb)}${d.media ? ` ${d.media.toUpperCase()}` : ""})`;
    const data = picked.filter((d) => !wizIsCache(d)), cache = picked.filter(wizIsCache);
    rows.push(["Node", wizNode()], ["Data disks", data.map(line).join("<br>")], ["Cache disks", cache.map(line).join("<br>")],
      ["Capacity", `About ${sizeText(data.reduce((s, d) => s + d.size_gb, 0))}`]);
  } else if (DISK_KINDS.includes(kind)) {
    rows.push(["Node", wizNode()], ["Disks", picked.map((d) => `${d.path} (${sizeText(d.size_gb)}${d.media ? ` ${d.media.toUpperCase()}` : ""})`).join("<br>")],
      ["Capacity", `About ${sizeText(picked.reduce((s, d) => s + d.size_gb, 0))}`]);
  } else {
    const nodes = $$('input[name="nodes"]:checked', form).map((c) => c.value);
    if (kind === "nfs") rows.push(["Share", esc(`${f.host.value}:${f.export.value}`)]);
    if (kind === "iscsi") rows.push(["Target", esc(`${f.host.value} · ${f.iqn.value} · LUN ${f.lun.value}`)]);
    if (kind === "dir") rows.push(["Folder", esc(f.path.value || "Default (/var/lib/ibhics/pools/<name>)")]);
    rows.push(["Nodes", esc(nodes.join(", "))]);
  }
  $(".wiz-summary", form).innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${k === "Node" || k === "Type" ? esc(v) : v}</dd>`).join("");
  const withData = picked.filter((d) => !d.empty);
  const erases = ["local", "fc", "iscsi", "hybrid"].includes(kind);
  $(".wiz-warn", form).innerHTML = (withData.length ? `<div class="wiz-note bad"><div><b>These disks still hold data, which will be erased:</b><ul>${withData.map((d) =>
    `<li><span class="mono">${esc(d.path)}</span>: ${esc(d.contents)}</li>`).join("")}</ul><p>Move the VMs on them to another pool first.</p></div></div>` : "") + (kind === "hybrid" ? "" : wizMixNote(picked));
  $(".wiz-confirm", form).hidden = !erases;
  $(".wiz-confirm span", form).textContent = kind === "iscsi"
    ? "I understand everything on this LUN will be erased"
    : `I understand everything on ${picked.length > 1 ? `these ${picked.length} disks` : "this disk"} will be erased`;
}

function wizCheck(step) {
  const f = poolWiz.form.elements, kind = wizKind();
  if (step === 2 && DISK_KINDS.includes(kind) && !wizPicked().length) throw new Error("Tick at least one disk.");
  if (step === 2 && kind === "hybrid") {
    const cache = wizPicked().filter(wizIsCache).length;
    if (!cache || cache === wizPicked().length) throw new Error("A hybrid pool needs at least one data disk (HDD or RAID) and one SSD for the cache.");
  }
  if (step === 3 && kind === "hybrid" && f.cache_mode.value === "writeback" && !confirm("Writeback keeps new writes only on the SSD until they are copied to the HDDs. If that SSD fails, VMs lose data. Use it only with a mirrored (RAID 1) SSD. Continue?")) throw new Error("Pick writethrough, or confirm writeback.");
  if (step === 2 && !DISK_KINDS.includes(kind)) {
    if (["nfs", "iscsi"].includes(kind) && !f.host.value.trim()) throw new Error("Enter the server address.");
    if (kind === "nfs" && !f.export.value.trim().startsWith("/")) throw new Error("Enter the export path, starting with /.");
    if (kind === "iscsi" && !f.iqn.value.trim()) throw new Error("Enter the target IQN.");
    if (!$$('input[name="nodes"]:checked', poolWiz.form).length) throw new Error("Pick at least one node.");
  }
  if (step === 3) {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$/.test(f.name.value)) throw new Error("Name the pool with letters, digits, '-', '_' or '.'.");
    if (!$(".wiz-confirm", poolWiz.form).hidden && !f.confirm_wipe.checked) throw new Error("Tick the box to confirm the disks may be erased.");
  }
}

{
  const { form } = poolWiz;
  $(".wiz-next", form).addEventListener("click", () => {
    try { wizCheck(poolWiz.step); poolWiz.step++; wizShow(); } catch (err) { fail(err); }
  });
  $(".wiz-back", form).addEventListener("click", () => { poolWiz.step--; wizShow(); });
  $$(".type-card input[name=kind]", form).forEach((r) => r.addEventListener("change", () => { poolWiz.picked.clear(); poolWiz.nameTouched = false; }));
  $$(".type-cards .type-card", form).forEach((c) => c.addEventListener("dblclick", () => $(".wiz-next", form).click()));
  form.elements.node.addEventListener("change", () => { poolWiz.picked.clear(); wizLoadDisks(); });
  form.elements.name.addEventListener("input", () => { poolWiz.nameTouched = true; });
  $(".disk-rescan", form).addEventListener("click", () => wizLoadDisks(true));
  $(".disk-search", form).addEventListener("input", wizRenderDisks);
  $(".chips", form).addEventListener("click", (e) => {
    const b = e.target.closest("button[data-media]");
    if (!b) return;
    poolWiz.media = b.dataset.media;
    $$(".chips button", form).forEach((x) => x.classList.toggle("on", x === b));
    wizRenderDisks();
  });
  $(".disk-rows", form).addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-path]");
    if (!tr) return;
    const p = tr.dataset.path;
    if (poolWiz.picked.has(p)) poolWiz.picked.delete(p); else poolWiz.picked.add(p);
    wizRenderDisks();
  });
  $(".wiz-pick", form).addEventListener("change", (e) => {
    const sel = e.target.closest("select[data-role]");
    if (sel) { poolWiz.role[sel.dataset.role] = sel.value === "cache"; wizPickText(); }
  });
  $(".disk-all", form).addEventListener("change", (e) => {
    $$(".disk-rows tr[data-path]", form).forEach((tr) => {
      if (e.target.checked) poolWiz.picked.add(tr.dataset.path); else poolWiz.picked.delete(tr.dataset.path);
    });
    wizRenderDisks();
  });
  // Enter in a field moves on instead of submitting from an earlier step.
  form.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT" && poolWiz.step < 3) { e.preventDefault(); $(".wiz-next", form).click(); }
  });
}

wizards["pool-dialog"] = { start: wizStart };

handlers["pool-dialog"] = async (f) => {
  wizCheck(3);
  const kind = wizKind(), name = f.get("name"), confirm_wipe = f.get("confirm_wipe") === "on";
  if (kind === "hybrid") {
    const picked = wizPicked(), gb = Number(f.get("cache_gb"));
    await post(`/nodes/${enc(wizNode())}/pools`, {
      name, type: "disk", confirm_wipe, devices: picked.filter((d) => !wizIsCache(d)).map((d) => d.path),
      cache_devices: picked.filter(wizIsCache).map((d) => d.path), cache_mode: f.get("cache_mode"), cache_gb: gb > 0 ? gb : null,
    });
    toast(`Hybrid storage ${name} added on ${wizNode()}`);
  } else if (DISK_KINDS.includes(kind)) {
    const devices = wizPicked().map((d) => d.path);
    const type = kind === "local" ? f.get("format") : "lvm";
    const body = type === "disk" ? { name, type, device: devices[0], confirm_wipe } : { name, type, devices, confirm_wipe };
    await post(`/nodes/${enc(wizNode())}/pools`, body);
    toast(`Storage ${name} added on ${wizNode()}`);
  } else {
    const body = { name, type: kind, confirm_wipe };
    if (kind === "dir" && f.get("path")) body.path = f.get("path");
    if (kind === "nfs" || kind === "iscsi") body.host = f.get("host").trim();
    if (kind === "nfs") body.export = f.get("export").trim();
    if (kind === "iscsi") { body.iqn = f.get("iqn").trim(); body.lun = Number(f.get("lun")); }
    const nodes = f.getAll("nodes"), done = [];
    for (const node of nodes) {
      try { await post(`/nodes/${enc(node)}/pools`, body); done.push(node); } catch (err) {
        if (done.length) toast(`Storage ${name} added on ${done.join(", ")}`);
        await refresh("storage");
        throw new Error(`${node}: ${err.message}`);
      }
    }
    toast(`Storage ${name} added on ${done.join(", ")}`);
  }
  await refresh("storage");
};

// ---------- nodes ----------

function ipmiText(info = {}) {
  info = info || {};
  const ip = info.ip_address || "";
  const valid = /^(\d{1,3}\.){3}\d{1,3}$/.test(ip) && ip.split(".").every((n) => Number(n) <= 255);
  if (valid) return `<a href="https://${esc(ip)}/" target="_blank" rel="noopener noreferrer" title="Open BMC web console">${esc(ip)}</a>${info.source ? `<span class="vm-sub">${esc(info.source)}</span>` : ""}`;
  return `<span class="muted">${esc(info.note || "Unavailable")}</span>`;
}

// HA's vms array contains the VMs explicitly protected by the restart service.
function clusterHAStatus(ha) {
  if (!ha || typeof ha.available !== "boolean" || !Array.isArray(ha.vms)) return "—";
  return ha.available ? `On · ${ha.vms.length} protected VMs` : "Unavailable on this manager";
}

// Isometric 3D server (or witness shield on a pedestal) for the heartbeat map.
// Cluster ID as shown and licensed: IBHX- and the UUID in upper-case groups of four.
const formatClusterId = (id) => `IBHX-${String(id || "").replace(/-/g, "").toUpperCase().match(/.{1,4}/g)?.join("-") || ""}`;

function clusterIcon3d(m, i) {
  const up = m.healthy, g = `cl3d${i}`;
  const [hi, lo, side, dark, glow] = up ? ["#9ffaff", "#2bb6d6", "#0f5a78", "#08304a", "#3ef0ff"] : ["#ffb3bd", "#c8485c", "#6a2232", "#3a1220", "#ff6b81"];
  const defs = `<defs><linearGradient id="${g}t" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${hi}"/><stop offset="1" stop-color="${lo}"/></linearGradient><linearGradient id="${g}l" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${side}"/><stop offset="1" stop-color="${dark}"/></linearGradient><linearGradient id="${g}r" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${dark}"/><stop offset="1" stop-color="#030c16"/></linearGradient></defs>`;
  const edge = `stroke="${glow}" stroke-width="1" stroke-linejoin="round"`;
  if (m.role === "compute") {
    const slots = [0, 1, 2, 3].map((k) => `<path d="M45 ${46 + k * 10}l21-9.8" stroke="${glow}" stroke-opacity=".55" stroke-width="2" stroke-linecap="round"/><circle class="cl-led" style="animation-delay:${(k * 0.35).toFixed(2)}s" cx="16" cy="${31 + k * 10}" r="1.8" fill="${up ? "#5ff7d2" : "#ff6b81"}"/>`).join("");
    return `<svg class="cl-3d" viewBox="0 0 80 90" aria-hidden="true">${defs}<polygon points="10,20 40,34 40,84 10,70" fill="url(#${g}l)" ${edge}/><polygon points="40,34 70,20 70,70 40,84" fill="url(#${g}r)" ${edge}/><polygon points="40,6 70,20 40,34 10,20" fill="url(#${g}t)" ${edge}/>${slots}</svg>`;
  }
  return `<svg class="cl-3d" viewBox="0 0 80 90" aria-hidden="true">${defs}<polygon points="12,66 40,79 40,87 12,74" fill="url(#${g}l)" ${edge}/><polygon points="40,79 68,66 68,74 40,87" fill="url(#${g}r)" ${edge}/><polygon points="40,53 68,66 40,79 12,66" fill="url(#${g}t)" fill-opacity=".55" ${edge}/><g class="cl-float-shield"><path d="M40 6l20 8-3 20-17 14-17-14-3-20z" fill="url(#${g}t)" ${edge}/><path d="M40 6l20 8-3 20-17 14z" fill="#000" fill-opacity=".25"/><path d="m31 25 6 6 12-12" fill="none" stroke="#04202e" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/></g></svg>`;
}

function clusterIcon(m) {
  if (m.role === "compute" && typeof NetworkCore !== "undefined") return NetworkCore.deviceIcon("node", { stopped: !m.healthy });
  return `<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">${m.role === "compute" ? '<rect x="17" y="6" width="30" height="50" rx="3"/><path d="M23 17h18M23 28h18M23 39h18M23 50h18"/>' : '<path d="m32 6 22 10-4 25-18 17-18-17-4-25Z"/><path d="m22 31 7 7 14-15"/>'}</svg>`;
}

function clusterMetric(label, value, capacity, percent) {
  const measured = Number.isFinite(percent);
  const p = measured ? Math.max(0, Math.min(100, percent)) : 0;
  return `<div class="cl-metric"><span>${label}<b>${esc(value)}</b></span>${measured ? `<meter min="0" max="100" value="${p}" aria-label="${label} used">${Math.round(p)}%</meter>` : ""}<small>${esc(capacity)}</small></div>`;
}

function nodeRows(c, admin) {
  return c.members.map((m) => {
    const h = state.hosts[m.name], s = m.healthy ? state.summaries[m.name] : null;
    const compute = m.role === "compute", vms = state.vms.filter((v) => v.node === m.name).length;
    const buttons = [
      admin ? `<button class="small" data-node-label="${esc(m.name)}">Rename</button>` : "",
      admin && compute ? `<button class="small" data-maint="${esc(m.name)}" data-on="${m.maintenance ? "" : "1"}">${m.maintenance ? "Exit maintenance" : "Maintenance"}</button>` : "",
      admin && compute && vms && computeMembers().length > 1 ? `<button class="small" data-evacuate="${esc(m.name)}">Evacuate</button>` : "",
      admin && !m.local ? `<button class="small danger" data-node="${esc(m.name)}">Remove</button>` : "",
    ].join("");
    const cpu = Number.isFinite(s?.cpu_percent) ? s.cpu_percent : null;
    const memory = Number.isFinite(s?.memory_used_mb) && s.memory_mb > 0 ? 100 * s.memory_used_mb / s.memory_mb : null;
    return `<article class="cl-node ${m.healthy ? "" : "cl-offline"}">
      <div class="cl-node-heading"><span class="cl-node-icon">${clusterIcon(m)}</span><div class="cl-node-name"><span class="badge plain">${compute ? "Compute" : "Arbiter"}</span><h3>${compute ? `<a href="#node/${enc(m.name)}">${esc(nodeLabel(m.name))}</a>` : esc(nodeLabel(m.name))}</h3><small>${esc(m.name)}${m.local ? " · Management node" : ""}</small>${m.maintenance ? '<span class="badge plain">In maintenance</span>' : ""}</div><span class="cl-status ${m.healthy ? "up" : "down"}">${m.healthy ? "Online" : "Offline"}</span></div>
      <div class="cl-resources">${compute ? clusterMetric("CPU", cpu === null ? (h ? `${h.cpus} cores` : "—") : `${Math.round(cpu)}% used`, cpu === null ? "Capacity · live usage unavailable" : `${h?.cpus || s.threads || "—"} cores / threads`, cpu) + clusterMetric("Memory", memory === null ? (h ? gbText(h.memory_mb) : "—") : `${Math.round(memory)}% used`, memory === null ? "Capacity · live usage unavailable" : `${gbText(s.memory_used_mb)} / ${gbText(s.memory_mb)}`, memory) : '<p class="hint">Quorum witness<br>No compute workloads</p>'}</div>
      <dl class="cl-node-facts"><div><dt>VMs</dt><dd>${compute ? vms : "—"}</dd></div><div><dt>Address</dt><dd>${esc(m.address || "Local server")}</dd></div><div><dt>IPMI / BMC</dt><dd>${compute ? ipmiText(s?.ipmi) : "Not applicable"}</dd></div><div><dt>Device health</dt><dd data-hardware-node="${esc(m.name)}">${compute ? '<span class="muted">Checking…</span>' : "Not applicable"}</dd></div></dl>
      <div class="cl-node-actions">${compute ? `<a class="btn small primary" href="#node/${enc(m.name)}">Summary</a>` : '<span class="hint">Quorum only</span>'}<span class="spacer"></span>${buttons ? `<details class="more"><summary class="btn small" aria-label="Actions for ${esc(nodeLabel(m.name))}">⋯</summary><div class="dropdown open">${buttons}</div></details>` : ""}</div>
    </article>`;
  }).join("");
}

// HA restart, chosen on the Cluster page with one switch per VM.
let clusterHAOpen = false;
const clusterHAVMs = () => (state.vms || []).filter((v) => !v.template && !v.recycled);
function renderClusterHA() {
  const box = $("#cluster-ha");
  if (!box) return;
  const ha = state.clusterHA, on = new Set((ha?.vms || []).map((v) => v.uuid)), vms = clusterHAVMs();
  box.hidden = !clusterHAOpen || !ha?.available;
  if (box.hidden) return;
  box.innerHTML = `<div class="cl-ha-head"><b>Auto restart (HA)</b><span>Switch on the VMs that must keep running. If their server fails, they start on another server.</span><button type="button" data-ha-all="1">Protect all</button><button type="button" data-ha-all="0">Turn all off</button></div><div class="cl-ha-list">${vms.map((v) => `<label class="cl-ha-vm ${on.has(v.uuid) ? "on" : ""}"><input type="checkbox" data-ha-vm="${esc(v.uuid)}" ${on.has(v.uuid) ? "checked" : ""}><i></i><span><b>${esc(v.name)}</b><small>${esc(nodeLabel(v.node || ""))} · ${gbText(v.memory_mb || 0)}</small></span></label>`).join("") || '<p class="hint">No VMs yet.</p>'}</div>`;
}
async function setClusterHA(uuids, enabled) {
  for (const uuid of uuids) await api(`/vms/${enc(uuid)}/ha`, { method: "PUT", body: JSON.stringify({ enabled }) });
  state.clusterHA = await api("/ha");
  state.ha = new Set((state.clusterHA?.vms || []).map((v) => v.uuid));
  toast(`HA restart ${enabled ? "on" : "off"} for ${uuids.length} VM${uuids.length === 1 ? "" : "s"}`);
  renderCluster();
}

// How many servers may fail while the cluster keeps running its VMs: the smallest of
// what quorum, the number of data copies and the spare RAM allow.
function clusterFaultTolerance(c, pools, vms, hosts) {
  const compute = c.members.filter((m) => m.role === "compute"), voters = c.members.length;
  const limits = [];
  const quorum = Math.floor((voters - 1) / 2);
  limits.push({ key: "quorum", n: quorum, text: voters === 2 ? "2 votes: losing either one stops quorum. Add a witness." : `${voters} votes: a majority (${Math.floor(voters / 2) + 1}) must stay online` });
  limits.push({ key: "nodes", n: Math.max(0, compute.length - 1), text: `${compute.length} compute node${compute.length === 1 ? "" : "s"}: one must keep running the VMs` });
  // /pools lists each node's pools; a pool is shared when every compute node has it.
  const onAll = (p) => compute.every((m) => pools.some((q) => q.name === p.name && q.node === m.name));
  const vstore = pools.some((p) => p.name === "virtualstorage" || /gateway-exports/.test(p.source || "") || /virtual storage/i.test(p.display_name || ""));
  const shared = pools.some((p) => p.shared || (compute.length > 1 && ["nfs", "iscsi"].includes(p.type) && onAll(p)));
  const copies = vstore ? 2 : shared ? Infinity : 1;
  limits.push({ key: "data", n: copies === Infinity ? Infinity : copies - 1, text: vstore ? "Virtual Storage keeps 2 copies of every disk" : shared ? "Shared storage (outside the nodes) holds the disks" : "Local disks only: VMs on a failed node stop until it returns" });
  const sizes = compute.map((m) => Number(hosts[m.name]?.memory_mb) || 0);
  const known = sizes.filter(Boolean);
  if (known.length) {
    const avg = known.reduce((a, b) => a + b, 0) / known.length;
    const ram = sizes.map((x) => x || avg).sort((a, b) => b - a);
    const need = vms.filter((v) => v.state === "running").reduce((a, v) => a + (Number(v.memory_mb) || 0), 0);
    let fit = 0;
    while (fit < ram.length - 1 && ram.slice(fit + 1).reduce((a, b) => a + b, 0) >= need) fit += 1;
    limits.push({ key: "ram", n: fit, text: `Running VMs need ${gbText(need)} RAM; the nodes left after ${fit === 1 ? "the largest fails" : fit ? `the ${fit} largest fail` : "a failure"} ${fit ? "can hold it" : "cannot hold it"}` });
  }
  const n = Math.min(...limits.map((l) => l.n));
  return { n, limits, limiting: limits.filter((l) => l.n === n) };
}

function renderCluster() {
  const c = state.cluster, local = c.members.find((m) => m.local), created = Boolean(c.cluster_id) || c.members.length > 1;
  const online = c.members.filter((m) => m.healthy).length, compute = computeMembers().length;
  const witnesses = c.members.length - compute;
  const topology = created && c.members.length === 1 ? "Single-node cluster" : TOPOLOGY[c.topology] || c.topology;
  $("#cluster-page-create").hidden = created;
  $("#cluster-page-add").hidden = !created;
  $("#cluster-empty").hidden = created;
  $("#cluster-hero").hidden = !created;
  $("#cluster-overview").hidden = !created;
  $("#cluster-guide").textContent = !created ? "Start here, or join an existing cluster below." : compute === 2 && !witnesses ? "2 nodes need an independent witness for resilient quorum. Add a node and choose Witness." : "All servers in this cluster at a glance.";
  const rows = Math.ceil(c.members.length / 3), height = rows * 170 + 25;
  const links = c.members.map((m, i) => {
    const row = Math.floor(i / 3), count = Math.min(3, c.members.length - row * 3);
    const x = (i % 3 + .5) * 900 / count, y = row * 170 + 65;
    const d = `M450 12 V${row * 170 + 25} H${x} V${y}`;
    // Online links carry a heartbeat pulse from the cluster core to the node.
    return `<path class="cl-link ${m.healthy ? "up" : "down"}" d="${d}"/>${m.healthy ? `<circle class="cl-pulse" r="3"><animateMotion dur="2.4s" begin="${(i * 0.4).toFixed(1)}s" repeatCount="indefinite" path="${d}"/></circle>` : ""}`;
  }).join("") + `<circle class="cl-core" cx="450" cy="12" r="5"/>`;
  $("#cluster-hero").innerHTML = `<div class="cl-quorum ${c.quorum ? "up" : "down"}"><span class="cl-eyebrow">CLUSTER HEARTBEAT</span><h2>${c.quorum ? "✓ Working" : "! Stopped"}</h2><p>${online} of ${c.members.length} online</p>${c.cluster_id ? `<div class="cl-id"><small>CLUSTER ID</small><code>${esc(formatClusterId(c.cluster_id))}</code><button id="cluster-copy-id" type="button" title="Copy cluster ID" aria-label="Copy cluster ID">⧉ Copy</button></div>` : ""}</div><div class="cl-map" style="--cl-rows:${rows}"><svg class="cl-links" viewBox="0 0 900 ${height}" preserveAspectRatio="none" aria-hidden="true">${links}</svg>${c.members.map((m, i) => {
    const row = Math.floor(i / 3), count = Math.min(3, c.members.length - row * 3);
    return `<div class="cl-map-node ${m.healthy ? "up" : "down"}" style="grid-row:${row + 1};grid-column:span ${6 / count}">${clusterIcon3d(m, i)}<b title="${esc(nodeLabel(m.name))}">${esc(nodeLabel(m.name))}</b><small>${m.healthy ? (m.role === "compute" ? "Server" : "Witness") : "Offline"}</small></div>`;
  }).join("")}</div>`;
  const ft = clusterFaultTolerance(c, state.pools || [], state.vms || [], state.hosts || {});
  const label = { quorum: "Votes", nodes: "Servers", data: "Data copies", ram: "Memory" };
  const down = c.members.length - online, left = Math.max(0, ft.n - down);
  const ha = state.clusterHA, haOn = ha && ha.available;
  const tile = (cls, title, value, note) => `<div class="${cls}"><small>${title}</small><b>${value}</b><span>${note}</span></div>`;
  $("#cluster-overview").innerHTML = [
    tile(c.active_manager ? "ok" : "warn", "Active manager", c.active_manager ? esc(nodeLabel(c.active_manager)) : "Unconfirmed", c.active_manager ? esc(c.active_manager) : "No elected manager reported"),
    tile(online === c.members.length ? "ok" : "warn", "Online", `${online} / ${c.members.length}`, online === c.members.length ? "All servers are up" : `${c.members.length - online} offline`),
    `<div class="cl-ft ${left ? "ok" : "none"}"><small>Can go down</small><b>${left} ${left === 1 ? "server" : "servers"}</b><span>${down ? `${down} already down · design allows ${ft.n}` : ft.n ? "VMs keep running" : `Limited by ${ft.limiting.map((l) => label[l.key].toLowerCase()).join(", ")}`}</span><details><summary>Why?</summary><ul>${ft.limits.map((l) => `<li class="${ft.limiting.includes(l) ? "limit" : ""}"><b>${label[l.key]} · ${l.n === Infinity ? "∞" : l.n}</b> ${esc(l.text)}</li>`).join("")}</ul></details></div>`,
    tile(haOn && ha.vms.length ? "ok" : "warn", "Auto restart (HA)", haOn ? `${ha.vms.length} / ${clusterHAVMs().length}` : "Off", haOn ? `VMs restart on another server if theirs fails${typeof canOperate !== "function" || canOperate() ? '<button class="cl-ha-open" type="button">Choose VMs</button>' : ""}` : "Unavailable on this manager"),
  ].join("");
  renderClusterHA();
  $("#cluster-pairing").hidden = created;
  $("#cluster-pairing-address").textContent = `Node ID: ${local?.name || ""} · Agent: http://${location.hostname}:8007`;
  $("#cluster-members").innerHTML = nodeRows(c, isAdmin()) + (created && isAdmin() ? '<button class="cl-add" data-open="node-dialog"><span>+</span><b>Add a node</b><small>Connect a compute server or witness</small></button>' : "");
  for (const m of c.members.filter((m) => m.role === "compute")) void loadHardwareBadge(m.name);
}

$("#page-cluster").addEventListener("change", (e) => {
  const box = e.target.closest("[data-ha-vm]");
  if (box) setClusterHA([box.dataset.haVm], box.checked).catch((err) => { box.checked = !box.checked; fail(err); });
});
$("#page-cluster").addEventListener("click", async (e) => {
  if (e.target.closest(".cl-ha-open")) { clusterHAOpen = !clusterHAOpen; renderClusterHA(); return; }
  const all = e.target.closest("[data-ha-all]");
  if (all) {
    const want = all.dataset.haAll === "1", on = new Set((state.clusterHA?.vms || []).map((v) => v.uuid));
    setClusterHA(clusterHAVMs().filter((v) => on.has(v.uuid) !== want).map((v) => v.uuid), want).catch(fail);
    return;
  }
  const view = e.target.closest("[data-cl-view]");
  if (view) {
    $("#cluster-members").classList.toggle("cl-list", view.dataset.clView === "list");
    $$("[data-cl-view]").forEach((b) => b.setAttribute("aria-pressed", String(b === view)));
  }
  if (e.target.closest("#cluster-copy-id")) {
    try { await navigator.clipboard.writeText(formatClusterId(state.cluster.cluster_id)); toast("Cluster ID copied"); } catch (err) { fail(err); }
  }
});

$("#cluster-copy-key").addEventListener("click", async () => {
  try {
    const data = await api("/cluster/pairing");
    await navigator.clipboard.writeText(data.agent_token);
    toast("Pairing key copied. Paste it into Add node on the cluster manager.");
  } catch (err) { fail(err); }
});

async function loadHardwareBadge(name) {
  const targets = [...document.querySelectorAll('[data-hardware-node]')].filter((e) => e.dataset.hardwareNode === name);
  if (!targets.length) return;
  try {
    const data = await api(`/nodes/${enc(name)}/hardware`), items = data.items || [];
    const faults = items.filter((x) => x.status === "critical"), warnings = items.filter((x) => x.status === "warning");
    const unknown = items.filter((x) => x.status === "unknown").length;
    const status = faults.length ? "critical" : warnings.length ? "warning" : unknown || !items.length ? "unknown" : "ok";
    let html = hardwareBadge(status) + `<span class="vm-sub">${esc(faults[0]?.name || warnings[0]?.name || (data.collecting ? "Collecting readings…" : unknown ? `${unknown} unavailable readings` : data.note || "Supported readings within limits"))}</span>`;
    if (!items.length && !data.collecting && !data.note) html = hardwareBadge("unknown");
    targets.forEach((target) => { if (target.isConnected) target.innerHTML = html; });
  } catch {
    targets.forEach((target) => { if (target.isConnected) target.innerHTML = hardwareBadge("unknown") + '<span class="vm-sub">Status unavailable</span>'; });
  }
}

const handleNodeAction = async (e) => {
  const label = e.target.closest("button[data-node-label]");
  const ev = e.target.closest("button[data-evacuate]");
  const rm = e.target.closest("button[data-node]");
  const mt = e.target.closest("button[data-maint]");
  if (!label && !ev && !rm && !mt) return;
  try {
    if (label) {
      const name = label.dataset.nodeLabel;
      const value = prompt(`Display name for ${name} (leave blank to use the Node ID):`, nodeLabel(name));
      if (value == null) return;
      await patch(`/cluster/nodes/${enc(name)}`, { display_name: value.trim() });
      toast("Node display name saved");
    } else if (mt) {
      const on = Boolean(mt.dataset.on), n = mt.dataset.maint;
      const others = computeMembers().filter((x) => x.name !== n && !x.maintenance).length;
      const vmsHere = state.vms.filter((v) => v.node === n).length;
      if (on && !confirm(`Put ${n} into maintenance? No VM will be placed, started or moved onto it.${vmsHere && others ? ` Its ${vmsHere} VM(s) move to other nodes first.` : vmsHere ? " There is no other node to move its VMs to: they stay as they are." : ""}`)) return;
      const r = await post(`/nodes/${enc(n)}/maintenance`, { on, evacuate: on && others > 0 });
      const failed = r.moves.filter((j) => j.state === "failed");
      toast(on ? `${n} is in maintenance${r.moves.length ? `; moving ${r.moves.length - failed.length} VM(s)` : ""}${failed.length ? `; ${failed.length} cannot move: ${failed[0].error}` : ""}` : `${n} is back in service`, failed.length > 0);
    } else if (ev) {
      if (!confirm(`Move every VM off ${ev.dataset.evacuate}?`)) return;
      const jobs = await post(`/nodes/${enc(ev.dataset.evacuate)}/evacuate`);
      const failed = jobs.filter((j) => j.state === "failed");
      toast(`Moving ${jobs.length - failed.length} VM(s)${failed.length ? `; ${failed.length} cannot move: ${failed[0].error}` : ""}`, failed.length > 0);
    } else if (rm) {
      if (!confirm(`Remove ${rm.dataset.node} from the cluster?`)) return;
      await del(`/cluster/nodes/${enc(rm.dataset.node)}`);
    } else return;
    await refresh("cluster");
  } catch (err) { fail(err); }
};
$("#cluster-members").addEventListener("click", handleNodeAction);

handlers["cluster-create-dialog"] = async (f) => {
  await post("/cluster/create", { name: f.get("name").trim() });
  toast("Cluster created. You can now add other nodes.");
  if (currentPage() === "cluster") await refresh("cluster");
  else location.hash = "cluster";
};

handlers["node-dialog"] = async (f) => {
  await post("/cluster/nodes", { name: f.get("name"), role: f.get("role"), address: f.get("address"), agent_token: f.get("agent_token") || null });
  toast(`${f.get("name")} joined the cluster`);
  await refresh("cluster");
};

// ---------- one node: Summary, Network, Settings ----------

const nodeFromHash = () => decodeURIComponent(location.hash.slice(6));
const node = { name: "", summary: null, nics: [], net: null, netState: null, settings: null, timezones: [], moved: "" };

let hardwareRequest = 0;
const hardwareStatus = {
  ok: ["running", "Normal"], warning: ["waiting", "Warning"],
  critical: ["failed", "Fault reported"], unknown: ["plain", "Unknown"],
};
function hardwareBadge(status) {
  const [style, label] = hardwareStatus[status] || hardwareStatus.unknown;
  return `<span class="badge ${style}">${label}</span>`;
}
function renderHardware(data) {
  const box = $("#node-health-content");
  const expanded = new Map([...box.querySelectorAll('details[data-health-group]')].map((d) => [d.dataset.healthGroup, d.open]));
  const items = data.items || [], events = data.events || [];
  const count = (s) => items.filter((x) => x.status === s).length;
  const stamp = data.collected_at ? new Date(data.collected_at).toLocaleString() : "Not collected yet";
  const summary = Object.keys(hardwareStatus).map((s) =>
    `<span>${hardwareBadge(s)} <b>${count(s)}</b></span>`).join("");
  const categories = [...new Set(items.map((x) => x.category))];
  const order = { critical: 0, warning: 1, unknown: 2, ok: 3 };
  const groups = categories.map((category) => {
    const rows = items.filter((x) => x.category === category).sort((a,b) => (order[a.status] ?? 2) - (order[b.status] ?? 2));
    const faults = rows.filter((x) => ["critical", "warning"].includes(x.status)).length;
    const status = rows.some((x) => x.status === "critical") ? "critical" : faults ? "warning" : rows.some((x) => x.status === "unknown") ? "unknown" : "ok";
    return `<details class="health-group" data-health-group="${esc(category)}" ${(expanded.get(category) ?? !!faults) ? "open" : ""}><summary>${esc(category)} ${hardwareBadge(status)} <span class="muted">${rows.length} readings</span></summary>
      <div class="table-wrap"><table><thead><tr><th>Device / sensor</th><th>Status</th><th>Reading / details</th><th>Source</th></tr></thead><tbody>${rows.map((x) =>
        `<tr><td>${esc(x.name)}</td><td>${hardwareBadge(x.status)}</td><td>${esc(x.detail)}</td><td>${esc(x.source)}</td></tr>`).join("")}</tbody></table></div></details>`;
  }).join("");
  box.innerHTML = `<div class="health-counts">${summary}</div>
    <p class="hint">${data.collecting ? "Refreshing hardware readings… · " : ""}Last collected: ${esc(stamp)} · Refresh every 60 seconds.</p>
    ${data.error ? `<p class="health-error">${esc(data.error)}</p>` : ""}
    ${data.note ? `<p class="hint">${esc(data.note)}</p>` : ""}
    ${groups}
    <p class="hint">Normal means the reported reading is within limits. Missing sensors and empty slots can show Unknown. DIMM temperature does not confirm RAM integrity; RAID controllers may hide physical disks.</p>
    <details class="health-group" data-health-group="history" ${expanded.get("history") ? "open" : ""}><summary>BMC hardware event history <span class="muted">${events.length} matching recent events</span></summary>
      <p class="hint">Historical events may have been repaired. They do not confirm a current fault. Dates come from the BMC clock. Only the latest 30 BMC records are checked.</p>
      ${events.map((e) => `<p class="health-event mono">${esc(e.detail)}</p>`).join("") || `<p class="hint">${data.events_status === "available" ? "No matching hardware events in the recent records." : "Event history unavailable."}</p>`}</details>`;
}
async function loadNodeHardware(name, fresh) {
  const request = ++hardwareRequest;
  if (fresh) $("#node-health-content").innerHTML = '<p class="hint">Collecting device health…</p>';
  try {
    const data = await api(`/nodes/${enc(name)}/hardware`);
    if (request === hardwareRequest && nodeFromHash() === name) renderHardware(data);
  } catch (err) {
    if (request === hardwareRequest && nodeFromHash() === name) renderHardware({
      items: [], events: [], error: `Hardware status unavailable: ${err.message}`,
    });
  }
}
let netTick = null;

const uptimeText = (s) => {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
};
const speedText = (mbps) => (mbps == null ? "–" : mbps >= 1000 ? `${mbps / 1000} Gb/s` : `${mbps} Mb/s`);
const KIND = { ethernet: "Ethernet", bond: "Bond", bridge: "Bridge" };
const ifaceOf = (name) => node.net?.interfaces.find((i) => i.name === name);
const ownerOf = (name) => node.net?.interfaces.find((i) => i.ports.includes(name));
// Every port on the node plus whatever the config names, ports first, then bonds, bridges.
function ifaceNames() {
  const names = new Set([...node.nics.map((n) => n.name), ...(node.net?.interfaces || []).map((i) => i.name)]);
  const kind = (n) => ifaceOf(n)?.kind || node.nics.find((x) => x.name === n)?.kind || "ethernet";
  const order = { ethernet: 0, bond: 1, bridge: 2 };
  return [...names].sort((a, b) => order[kind(a)] - order[kind(b)] || a.localeCompare(b, undefined, { numeric: true }));
}

function systemDiskText(d) {
  if (!d.device) return "–";
  const dot = d.health === "ok" ? "ok" : d.health === "failing" ? "bad" : "";
  const bits = [
    d.health === "ok" ? "SMART OK" : d.health === "failing" ? "SMART FAILING: replace it" : (d.note || "health unknown"),
    d.wear_percent != null ? `${d.wear_percent}% worn` : "",
    d.temperature_c != null ? `${d.temperature_c} °C` : "",
    d.power_on_hours != null ? `${Math.round(d.power_on_hours / 24)} days on` : "",
    d.reallocated_sectors ? `${d.reallocated_sectors} reallocated sectors` : "",
    d.root_used_percent != null ? `/ ${d.root_used_percent}% used (${d.root_free_gb} GB free)` : "",
  ].filter(Boolean);
  return `${dot ? `<span class="dot ${dot}"></span> ` : ""}<span class="mono">${esc(d.device)}</span> ${esc(d.model || "")}<br><span class="muted">${esc(bits.join(" · "))}</span>`;
}
async function renderNodeDetail(auto = false) {
  const name = nodeFromHash();
  const m = state.cluster.members.find((x) => x.name === name);
  $("#node-crumb").textContent = nodeLabel(name);
  const clear = () => ["#node-stats", "#node-hw", "#node-sys", "#node-ports", "#node-pools", "#node-vms", "#node-ifaces", "#node-net-banner", "#node-health-content"].forEach((id) => { $(id).innerHTML = ""; });
  if (!m || m.role !== "compute") {
    $("#node-title").textContent = m ? name : "Node not found";
    $("#node-subtitle").textContent = m ? "A witness only votes for quorum; it has no host details to show." : "It may have been removed from the cluster.";
    clear();
    return;
  }
  const base = `/nodes/${enc(name)}`;
  const fresh = !auto || node.name !== name;
  const [summary, nics, net, netState] = await Promise.all([
    api(`${base}/summary`), api(`${base}/nics`), api(`${base}/network`), api(`${base}/network/state`),
  ]);
  Object.assign(node, { name, summary, nics, net, netState });
  void loadNodeHardware(name, fresh);
  if (fresh) {
    const [settings, timezones] = await Promise.all([api(`${base}/settings`), api(`${base}/timezones`)]);
    Object.assign(node, { settings, timezones, moved: "" });
    fillNodeSettings();
  }
  const s = summary;
  $("#node-title").innerHTML = `${esc(nodeLabel(name))} <span class="badge ${m.healthy ? "online" : "offline"}">${m.healthy ? "online" : "offline"}</span>`;
  $("#node-subtitle").textContent = [[s.vendor, s.model].filter(Boolean).join(" "), m.local ? "runs the manager" : m.address, `ibHiCS ${s.version}`].filter(Boolean).join(" · ");

  const vms = state.vms.filter((v) => v.node === name);
  const stat = (label, value, note = "", pct = null) => `<div class="stat">${pct == null ? "" : `<i class="nd-ring" style="--p:${Math.max(0, Math.min(100, Math.round(pct)))}"><b>${Math.round(pct)}%</b></i>`}<span>${esc(label)}</span><b>${value}</b><em>${note}</em></div>`;
  const avatar = $("#page-node .vm-head .avatar");
  if (avatar && typeof clusterIcon3d === "function") avatar.innerHTML = clusterIcon3d({ role: "compute", healthy: m.healthy }, "node");
  const bar = (used, total) => usageBar({ used, total, from: "of this node" });
  $("#node-stats").innerHTML = [
    stat("CPU", `${s.cpu_percent}%`, `${s.threads} threads · load ${s.load.map((l) => l.toFixed(2)).join(" ")}`, s.cpu_percent),
    stat("Memory", `${gbText(s.memory_used_mb)} <small>/ ${gbText(s.memory_mb)}</small>`, "Used by VMs and the system", s.memory_mb ? s.memory_used_mb / s.memory_mb * 100 : 0),
    stat("Virtual machines", `${vms.filter((v) => v.state === "running").length} <small>running of ${vms.length}</small>`,
      `${vms.reduce((n, v) => n + (v.state === "running" ? v.vcpus : 0), 0)} vCPU in use`),
    stat("Uptime", uptimeText(s.uptime_s), `Since ${new Date(Date.now() - s.uptime_s * 1000).toLocaleString()}`),
  ].join("");

  const kv = (rows) => rows.filter((r) => r).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v || '<span class="muted">–</span>'}</dd>`).join("");
  $("#node-hw").innerHTML = kv([
    ["Manufacturer", esc(s.vendor)],
    ["Model", esc(s.model)],
    ["Serial number", s.serial ? `<span class="mono">${esc(s.serial)}</span>` : ""],
    ["BIOS", esc(s.bios)],
    ["Processor", esc(s.cpu_model)],
    ["Sockets / cores / threads", `${s.sockets} / ${s.cores} / ${s.threads}`],
    ["Virtualization", s.virtualization ? `<span class="dot ok"></span> ${esc(s.virtualization)}` : `<span class="dot bad"></span> Not available`],
    ["Memory", gbText(s.memory_mb)],
  ]);
  const mgmt = nics.flatMap((n) => n.addresses.map((a) => [n.name, a]));
  $("#node-sys").innerHTML = kv([
    ["Hostname", esc(s.hostname)],
    ["IPMI / BMC IPv4", ipmiText(s.ipmi)],
    ["Role", m.local ? "Compute, runs the manager" : esc(m.role)],
    ["Management address", mgmt.map(([n, a]) => `<span class="mono">${esc(a)}</span> <span class="muted">${esc(n)}</span>`).join("<br>")],
    ["ibHiCS", esc(s.version)],
    ["Operating system", esc(s.os)],
    ["Kernel", `<span class="mono">${esc(s.kernel)}</span>`],
    ["Time zone", esc(s.timezone)],
    ["Uptime", uptimeText(s.uptime_s)],
    ["Maintenance", m.maintenance ? `<span class="dot bad"></span> In maintenance: no VMs are placed here` : `<span class="dot ok"></span> In service`],
    ["System disk", systemDiskText(s.system_disk || {})],
  ]);

  const used = (n) => n.master ? `Part of ${esc(n.master)}` : ifaceOf(n.name)?.method !== "manual" && ifaceOf(n.name) ? "Own address" : '<span class="muted">Not used</span>';
  const ports = nics.filter((n) => n.kind === "ethernet");
  $("#node-ports").innerHTML = ports.length ? ports.map((n) => `<tr>
    <td><span class="vm-name">${esc(n.name)}</span><span class="vm-sub clip" title="${esc(n.model)}">${esc(n.model || n.driver || n.mac)}</span></td>
    <td>${n.link ? '<span class="dot ok"></span> Connected' : '<span class="dot"></span> No link'}</td>
    <td>${speedText(n.speed_mbps)}</td><td>${used(n)}</td>
    <td>${n.addresses.map((a) => `<span class="mono">${esc(a)}</span>`).join("<br>") || '<span class="muted">–</span>'}</td></tr>`).join("") : empty(5, "No network ports found");
  const pools = state.pools.filter((p) => p.node === name);
  $("#node-vms").innerHTML = vms.length ? [...vms].sort((a, b) => a.name.localeCompare(b.name)).map((v) => `<tr>
    <td><a class="vm-link" href="#vm/${esc(v.uuid)}">${esc(v.name)}</a></td>
    <td><span class="badge ${v.state === "running" ? "running" : "plain"}">${esc(v.state)}</span></td>
    <td>${v.vcpus}</td><td>${gbText(v.memory_mb)}</td><td>${esc(v.os_type || "")}</td>
  </tr>`).join("") : empty(5, "No VMs on this node.");
  $("#node-pools").innerHTML = pools.length ? pools.map((p) => `<tr>
    <td><span class="vm-name">${esc(p.name)}</span></td><td>${esc(p.type)}</td>
    <td>${num(p.allocation_gb)} GB / ${num(p.capacity_gb)} GB ${usageBar({ used: p.allocation_gb, total: p.capacity_gb || 1, from: "of the pool" })}</td>
    <td>${num(p.available_gb ?? p.capacity_gb - p.allocation_gb)} GB</td></tr>`).join("") : empty(4, "No storage pools");

  renderIfaces();
}

function renderIfaces() {
  const admin = isAdmin();
  $("#node-ifaces").innerHTML = ifaceNames().map((name) => {
    const cfg = ifaceOf(name), nic = node.nics.find((n) => n.name === name);
    const kind = cfg?.kind || nic?.kind || "ethernet";
    const owner = ownerOf(name);
    const status = !nic ? '<span class="dot warn"></span> Not running' : nic.link ? '<span class="dot ok"></span> Connected' : nic.up ? '<span class="dot warn"></span> No link' : '<span class="dot"></span> Down';
    const live = nic?.addresses || [];
    const ip = !cfg || cfg.method === "manual" ? '<span class="muted">No address</span>'
      : cfg.method === "dhcp" ? `DHCP${live.length ? `<span class="vm-sub mono">${live.map(esc).join(", ")}</span>` : ""}`
        : `<span class="mono">${esc(cfg.address)}</span>${cfg.gateway ? `<span class="vm-sub">gateway <span class="mono">${esc(cfg.gateway)}</span></span>` : ""}`;
    const rel = cfg?.ports.length ? cfg.ports.map(esc).join(", ") : owner ? `Part of ${esc(owner.name)}` : kind === "ethernet" ? '<span class="muted">Not used</span>' : '<span class="muted">No ports</span>';
    const note = name === "br0" ? "VM LAN and management" : kind === "bond" ? esc(cfg?.bond_mode || "") : esc(nic?.model || "");
    return `<tr>
      <td><div class="name-cell">${avatar("i-nic")}<div><span class="vm-name">${esc(name)}</span><span class="vm-sub clip" title="${note}">${note || esc(nic?.mac || "")}</span></div></div></td>
      <td>${KIND[kind]}</td><td>${status}</td><td>${speedText(nic?.speed_mbps)}</td><td>${ip}</td><td>${rel}</td>
      <td>${cfg?.mtu || nic?.mtu || 1500}</td>
      <td><div class="actions">${admin ? `<button class="small" data-iface="${esc(name)}">Edit</button>` : ""}</div></td></tr>`;
  }).join("");
  renderNetBanner();
}

function renderNetBanner() {
  const st = node.netState || {};
  clearInterval(netTick);
  if (!st.pending) {
    const show = st.message && !["confirmed", "nothing changed"].includes(st.message);
    $("#node-net-banner").innerHTML = show ? `<div class="net-banner info">${esc(st.message)}</div>` : "";
    return;
  }
  let left = st.seconds_left;
  const draw = () => {
    $("#node-net-banner").innerHTML = `<div class="net-banner warn">
      <div><b>Network change applied.</b> Check that everything still works, then confirm. Without confirming it is put back in <b>${left} s</b>.
      ${node.moved ? `<br>This page uses an address the change removed: open <a href="${esc(node.moved)}">${esc(node.moved)}</a> and confirm there.` : ""}</div>
      <span class="spacer"></span><button data-net="undo">Undo now</button><button class="primary" data-net="confirm">Keep this change</button></div>`;
  };
  draw();
  netTick = setInterval(async () => {
    left -= 1;
    if (left > 0) { if (currentPage() === "node") draw(); return; }
    clearInterval(netTick);
    try { node.netState = await api(`/nodes/${enc(node.name)}/network/state`); } catch { /* node may be away */ }
    if (currentPage() === "node") refresh("node").catch(() => {});
  }, 1000);
}

$("#node-net-banner").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-net]");
  if (!b) return;
  try {
    node.netState = await post(`/nodes/${enc(node.name)}/network/${b.dataset.net}`);
    toast(b.dataset.net === "confirm" ? "Network change kept" : "Network change put back");
    node.moved = "";
    await refresh("node");
  } catch (err) { fail(err); }
});

async function applyNetwork(interfaces, what) {
  const before = node.net.interfaces.filter((i) => i.method === "static").map((i) => i.address.split("/")[0]);
  const after = interfaces.filter((i) => i.method === "static").map((i) => i.address.split("/")[0]);
  const host = location.hostname;
  node.moved = before.includes(host) && !after.includes(host) && after.length
    ? `${location.protocol}//${after[0]}${location.port ? `:${location.port}` : ""}/#node/${enc(node.name)}` : "";
  node.netState = await api(`/nodes/${enc(node.name)}/network`, { method: "PUT", body: JSON.stringify({ interfaces }) });
  toast(node.netState.pending ? `${what}: confirm within ${node.netState.seconds_left} s` : node.netState.message || what);
  await refresh("node");
}

let ifaceCtx = null;
function openIface(name, kind) {
  const isNew = !name;
  const cfg = name ? ifaceOf(name) : null;
  kind = cfg?.kind || kind || node.nics.find((n) => n.name === name)?.kind || "ethernet";
  if (isNew) {
    const taken = new Set(ifaceNames());
    const prefix = kind === "bond" ? "bond" : "br";
    let i = kind === "bond" ? 0 : 1;
    while (taken.has(`${prefix}${i}`)) i += 1;
    name = `${prefix}${i}`;
  }
  ifaceCtx = { name, kind, isNew };
  const form = $("#iface-form");
  form.reset();
  $("#iface-title").textContent = isNew ? `Add ${kind}` : `${KIND[kind]} ${name}`;
  $$("[data-kind]", form).forEach((el) => { el.style.display = el.dataset.kind.split(" ").includes(kind) ? "" : "none"; });
  form.elements.name.value = name;
  form.elements.name.readOnly = !isNew;
  form.elements.method.value = cfg?.method || "manual";
  form.elements.address.value = cfg?.address || "";
  form.elements.gateway.value = cfg?.gateway || "";
  const mtu = String(cfg?.mtu || "");
  if (mtu && ![...form.elements.mtu.options].some((o) => o.value === mtu)) form.elements.mtu.add(new Option(mtu, mtu));
  form.elements.mtu.value = mtu;
  form.elements.auto.checked = cfg ? cfg.auto : true;
  $$('input[name="roles"]', form).forEach((c) => { c.checked = (cfg?.roles || []).includes(c.value); });
  form.elements.routes.value = (cfg?.routes || []).join("\n");
  form.elements.flow_control.value = cfg?.flow_control || "";
  form.elements.bond_mode.value = cfg?.bond_mode || "active-backup";
  // Ports this one may use: free ones, its own, and for a bridge also bonds.
  const own = cfg?.ports || [];
  const free = ifaceNames().filter((n) => {
    const k = ifaceOf(n)?.kind || node.nics.find((x) => x.name === n)?.kind || "ethernet";
    const owner = ownerOf(n);
    return n !== name && (k === "ethernet" || (kind === "bridge" && k === "bond")) && (!owner || owner.name === name);
  });
  $("#iface-ports").innerHTML = free.length ? free.map((n) => {
    const nic = node.nics.find((x) => x.name === n);
    return `<label class="check"><input type="checkbox" name="ports" value="${esc(n)}"${own.includes(n) ? " checked" : ""}> ${esc(n)} <span class="muted">${nic ? (nic.link ? speedText(nic.speed_mbps) : "no link") : ""}</span></label>`;
  }).join("") : `<p class="hint">No free ports.</p>`;
  const owner = ownerOf(name);
  $("#iface-note").textContent = name === "br0"
    ? "br0 carries the node's management address, the default VM network and the VLAN networks. Its port is the uplink to your switch."
    : owner ? `${name} is a port of ${owner.name}, so it has no address of its own. Edit ${owner.name} to change that.`
      : kind === "bridge" ? "A bridge lets VMs reach the network through its ports, for example a second LAN or a storage network."
        : kind === "bond" ? "A bond joins ports into one link for redundancy or speed. Put it on a bridge to use it for VMs." : "";
  $("#iface-delete").hidden = isNew || kind === "ethernet" || name === "br0";
  syncSwitch(form);
  $("#iface-dialog").showModal();
}

document.addEventListener("click", (e) => {
  const edit = e.target.closest("button[data-iface]");
  const add = e.target.closest("button[data-iface-new]");
  if (edit) openIface(edit.dataset.iface);
  if (add) openIface(null, add.dataset.ifaceNew);
});

handlers["iface-dialog"] = async (f) => {
  const { kind, isNew } = ifaceCtx;
  const name = f.get("name");
  const interfaces = structuredClone(node.net.interfaces);
  const old = interfaces.find((i) => i.name === ifaceCtx.name);
  const cfg = old || { name, kind, method: "manual", address: "", gateway: "", mtu: null, auto: true, ports: [], bond_mode: "", options: [] };
  if (!old) interfaces.push(cfg);
  Object.assign(cfg, {
    name, kind, method: f.get("method"), auto: f.get("auto") === "on",
    address: f.get("method") === "static" ? f.get("address").trim() : "",
    gateway: f.get("method") === "static" ? f.get("gateway").trim() : "",
    mtu: f.get("mtu") ? Number(f.get("mtu")) : null,
    roles: f.getAll("roles"),
    flow_control: kind === "ethernet" ? f.get("flow_control") || "" : "",
    routes: f.get("routes").split("\n").map((r) => r.trim().replace(/\s+/g, " ")).filter(Boolean),
  });
  if (kind !== "ethernet") {
    cfg.ports = f.getAll("ports");
    if (kind === "bond") cfg.bond_mode = f.get("bond_mode");
    if (!cfg.ports.length && kind === "bond") throw new Error("Pick at least one port for the bond.");
    for (const p of cfg.ports) {  // ports carry no address of their own
      if (!interfaces.some((i) => i.name === p)) interfaces.push({ name: p, kind: "ethernet", method: "manual", address: "", gateway: "", mtu: null, auto: false, ports: [], bond_mode: "", options: [] });
    }
  }
  await applyNetwork(interfaces, isNew ? `${name} added` : `${name} changed`);
};

$("#iface-delete").addEventListener("click", async () => {
  const { name } = ifaceCtx;
  if (!confirm(`Delete ${name}? Its ports become free.`)) return;
  try {
    await applyNetwork(node.net.interfaces.filter((i) => i.name !== name), `${name} deleted`);
    $("#iface-dialog").close();
  } catch (err) { fail(err); }
});

function fillNodeSettings() {
  const s = node.settings, form = $("#node-settings-form");
  $("#node-label-form").elements.display_name.value = nodeLabel(node.name);
  $("#node-label-form").elements.display_name.disabled = !isAdmin();
  $("#node-stable-id").textContent = node.name;
  form.elements.hostname.value = s.hostname;
  $("#node-tz").innerHTML = node.timezones.map((z) => `<option${z === s.timezone ? " selected" : ""}>${esc(z)}</option>`).join("");
  form.elements.ntp.value = s.ntp.join(", ");
  form.elements.dns.value = s.dns.join(", ");
  const sync = s.ntp_synced == null ? "" : s.ntp_synced ? " · clock in sync with NTP" : " · clock not in sync with NTP yet";
  $("#node-time").textContent = `Node time: ${new Date(s.time).toLocaleString()}${sync}`;
  $$("input, select", form).forEach((el) => { if (el.name !== "hostname") el.disabled = !isAdmin(); });
}

$("#node-label-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = $("button[type=submit]", e.target);
  button.disabled = true;
  try {
    await patch(`/cluster/nodes/${enc(node.name)}`, { display_name: e.target.elements.display_name.value.trim() });
    await loadCluster();
    await renderNodeDetail(true);
    e.target.elements.display_name.value = nodeLabel(node.name);
    fillSelects();
    toast("Node display name saved");
  } catch (err) { fail(err); }
  finally { button.disabled = false; }
});

$("#node-settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const list = (v) => v.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
  try {
    node.settings = await api(`/nodes/${enc(node.name)}/settings`, {
      method: "PUT", body: JSON.stringify({ timezone: f.get("timezone"), ntp: list(f.get("ntp")), dns: list(f.get("dns")) }),
    });
    fillNodeSettings();
    toast("Settings saved");
  } catch (err) { fail(err); }
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-node-power]");
  if (!b) return;
  const action = b.dataset.nodePower, word = action === "reboot" ? "Reboot" : "Shut down";
  if (!confirm(`${word} ${node.name}?`)) return;
  try {
    try {
      await post(`/nodes/${enc(node.name)}/power`, { action });
    } catch (err) {
      if (!/still run/.test(err.message) || !confirm(`${err.message}.\n\n${word} anyway? Those VMs are cut off as if their power failed.`)) throw err;
      await post(`/nodes/${enc(node.name)}/power`, { action, force: true });
    }
    toast(`${node.name}: ${action === "reboot" ? "rebooting" : "shutting down"} in a few seconds`);
  } catch (err) { fail(err); }
});

// ---------- one VM ----------

function guestToolsPlan(osText, selectedPm = null) {
  const text = String(osText || "");
  if (/windows/i.test(text)) return { family: "windows", pm: null, install: null, remove: null };
  let pm = null;
  if (/ubuntu|debian/i.test(text)) pm = "apt";
  else if (/suse/i.test(text)) pm = "zypper";
  else if (/rhel|red\s*hat|alma\s*linux|rocky|centos|oracle|fedora/i.test(text)) {
    const legacy = text.match(/(?:centos|rhel|red\s*hat)[^\d]*(\d+)/i);
    pm = legacy && Number(legacy[1]) <= 7 ? "yum" : "dnf";
  }
  if (["dnf", "yum", "apt", "zypper"].includes(selectedPm)) pm = selectedPm;
  const install = {
    dnf: "dnf install -y qemu-guest-agent",
    yum: "yum install -y qemu-guest-agent",
    apt: "apt-get update && apt-get install -y qemu-guest-agent",
    zypper: "zypper --non-interactive install qemu-guest-agent",
  }[pm];
  const remove = {
    dnf: "dnf remove -y open-vm-tools", yum: "yum remove -y open-vm-tools",
    apt: "apt-get remove -y open-vm-tools", zypper: "zypper --non-interactive remove open-vm-tools",
  }[pm] || null;
  // AlmaLinux 8 changed its signing key in 2024: older installs (e.g. 8.8 appliances)
  // reject current packages with "GPG check FAILED" until the new key is imported.
  const almaKey = pm === "dnf" && /alma\s*linux[^\d]*8/i.test(text)
    ? "rpm --import https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux && " : "";
  return { family: "linux", pm, install: install ? `${almaKey}${install} && systemctl enable --now qemu-guest-agent` : null, remove };
}

function renderGuestTools(vm, g, selectedPm = null) {
  const card = $("#vm-guest-tools");
  card.hidden = vm.state !== "running" || Boolean(g?.agent);
  card.innerHTML = "";
  if (card.hidden) return;
  const osText = g?.os_name || state.guests[vm.uuid]?.os_name || vm.os_type;
  const detected = guestToolsPlan(osText);
  const insert = canOperate() ? `<button class="small" data-vm="tools" data-id="${esc(vm.uuid)}" data-name="${esc(vm.name)}">Insert guest tools CD</button>` : "";
  const check = `<button class="small" id="guest-tools-check">Check again</button>`;
  const code = (id, command) => `<div class="guest-tools-command"><pre><code id="${id}">${esc(command)}</code></pre><button class="small" data-guest-tools-copy="${id}">Copy</button></div>`;
  let body;
  if (detected.family === "windows") {
    body = `<ol><li>Insert the ibHiCS guest tools CD using the button below or the ⋯ menu.</li>
      <li>Open the CD drive inside Windows.</li><li>Run <code>virtio-win-guest-tools.exe</code> and complete the installer to install the QEMU guest agent and virtio drivers.</li></ol>
      ${insert}<p class="hint">After installation, check again to detect the guest agent.</p>${check}`;
  } else {
    const plan = guestToolsPlan(osText, detected.pm || selectedPm || "dnf");
    body = `<p class="hint">Run these commands as root inside the guest VM.</p>
      ${!detected.pm ? `<p class="hint">Distribution not detected. Choose the guest's package manager:</p><div class="tabs guest-tools-tabs" aria-label="Package manager">${["dnf", "yum", "apt", "zypper"].map((pm) => `<button class="${pm === plan.pm ? "active" : ""}" aria-pressed="${pm === plan.pm}" data-guest-tools-pm="${pm}">${pm}</button>`).join("")}</div>` : ""}
      <h4>From the internet</h4>${code("guest-tools-install", plan.install)}
      <h4>From the ibHiCS guest tools CD</h4>${insert}
      ${code("guest-tools-cd", "mount /dev/sr0 /mnt && sh /mnt/linux/install.sh; umount /mnt")}
      <p class="hint">The CD installer still needs access to your distribution's package repositories.</p>
      <p class="hint guest-tools-vmware">Moved from VMware? Remove VMware Tools: <code>${esc(plan.remove)}</code></p>
      <div class="guest-tools-check"><span>Check: <code>systemctl status qemu-guest-agent</code></span>${check}</div>`;
  }
  card.innerHTML = `<h3>Install guest tools</h3><div class="guest-tools-body">${body}</div>`;
}

async function copyGuestToolsCommand(code) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(code.textContent);
  } catch {
    const range = document.createRange();
    range.selectNodeContents(code);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    // Leave the command selected for manual copying if the legacy API is unavailable.
    try { document.execCommand("copy"); } catch { /* selection remains available */ }
  }
  toast("Copied");
}

$("#vm-guest").addEventListener("click", (e) => {
  if (!e.target.closest("[data-guest-tools-link]")) return;
  e.preventDefault();
  const card = $("#vm-guest-tools");
  card.focus({ preventScroll: true });
  card.scrollIntoView({ behavior: "smooth", block: "start" });
});
$("#vm-guest-tools").addEventListener("click", async (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  try {
    if (button.dataset.guestToolsCopy) await copyGuestToolsCommand(document.getElementById(button.dataset.guestToolsCopy));
    if (button.dataset.guestToolsPm) {
      const vm = state.vms.find((v) => v.uuid === vmFromHash());
      if (vm) renderGuestTools(vm, state.guests[vm.uuid], button.dataset.guestToolsPm);
    }
    if (button.id === "guest-tools-check") {
      button.disabled = true;
      try { await load("guests"); await renderVMDetail(); }
      finally { button.disabled = false; }
    }
    if (button.dataset.vm === "tools" && canOperate()) await vmAction(e);
  } catch (err) { fail(err); }
});

async function renderVMDetail() {
  const uuid = vmFromHash();
  const vm = state.vms.find((v) => v.uuid === uuid);
  if (!vm) {
    $("#vm-guest-tools").hidden = true;
    $("#vm-guest-tools").innerHTML = "";
    $("#vm-title").textContent = "VM not found";
    $("#vm-crumb").textContent = uuid;
    $("#vm-subtitle").textContent = "It may have been deleted, or moved and renamed.";
    ["#vm-actions", "#vm-stats", "#vm-general", "#vm-guest", "#vm-net", "#vm-hw", "#vm-backups", "#vm-feed", "#vm-snaps", "#vm-alerts"].forEach((id) => { $(id).innerHTML = ""; });
    return;
  }
  let g = null;
  if (vm.state === "running") g = await api(`/vms/${uuid}/guest`).catch(() => null);
  if (g) state.guests[uuid] = g;
  const os = osName(vm, g), ips = ipList(g);
  const host = state.hosts[vm.node];
  const usage = diskUsage(vm, g);

  $("#vm-crumb").textContent = vm.name;
  $("#vm-title").innerHTML = `${esc(vm.name)} <span class="badge ${esc(vm.state)}">${esc(vm.state)}</span>`
    + (vm.restart_needed ? ` <span class="badge" title="Shut down and power on to apply the saved processor, memory or boot order">restart needed</span>` : "");
  $("#vm-subtitle").textContent = [os || "Operating system not set", vm.node && `on ${vm.node}`, ips[0]].filter(Boolean).join(" · ");
  $("#vm-actions").innerHTML = vmMenu(vm);

  const stat = (label, value, note = "") => `<div class="stat"><span>${esc(label)}</span><b>${value}</b><em>${note}</em></div>`;
  $("#vm-stats").innerHTML = [
    stat("vCPU", vm.vcpus, host ? `of ${host.cpus} cores on ${esc(vm.node)}` : ""),
    stat("Memory", gbText(vm.memory_mb), host ? `of ${gbText(host.memory_mb)}` : ""),
    stat("Disk", usage.used != null ? `${num(usage.used)} GB <small>/ ${num(usage.total)} GB</small>` : `${num(usage.total)} GB`,
      usage.used != null ? `${usageBar(usage)}Used, ${esc(usage.from)}`
        : `${vm.disks.length} disk${vm.disks.length === 1 ? "" : "s"}${vm.state === "running" ? " · install guest tools to see usage" : ""}`),
    stat("Network", `${vm.nics.length} adapter${vm.nics.length === 1 ? "" : "s"}`, esc(ips.join(", ") || "No address reported")),
  ].join("");

  const kv = (rows) => rows.filter((r) => r).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
  const dash = `<span class="muted">–</span>`;
  $("#vm-general").innerHTML = kv([
    ["Name", esc(vm.name)],
    ["Status", `<span class="badge ${esc(vm.state)}">${esc(vm.state)}</span>`],
    ["Node", esc(nodeOptionText(vm.node || "–"))],
    ["Group", groupOf(vm) ? `<span class="g-tag">${esc(groupPath(groupOf(vm)))}</span>` : `<span class="muted">Ungrouped</span>`],
    ["Operating system", vm.os_type ? esc(vm.os_type) : `<span class="muted">Not set</span>`],
    ["Firmware", vm.firmware === "uefi" ? "UEFI" : "BIOS"],
    ["CD/DVD", vm.iso ? `<span class="mono">${esc(vm.iso)}</span>` : "Empty"],
    vm.driver_iso && ["Driver CD", `<span class="mono">${esc(vm.driver_iso)}</span>`],
    ["Description", vm.description ? esc(vm.description) : dash],
    ["UUID", `<span class="mono">${esc(vm.uuid)}</span>`],
  ]);

  const agentText = g?.agent
    ? `<span class="dot ok"></span> Running`
    : vm.state === "running"
      ? `<span class="dot warn"></span> Not detected <a class="vm-sub" href="#vm-guest-tools" data-guest-tools-link>Install guest tools</a>`
      : `<span class="dot"></span> VM is powered off`;
  $("#vm-guest").innerHTML = kv([
    ["Guest agent", agentText],
    ["Reported OS", g?.os_name ? esc(g.os_name) : dash],
    ["Version", g?.os_version ? esc(g.os_version) : dash],
    ["Kernel", g?.kernel ? `<span class="mono">${esc(g.kernel)}</span>` : dash],
    ["Hostname", g?.hostname ? esc(g.hostname) : dash],
    ["IP addresses", ips.length ? ips.map((ip) => `<span class="mono">${esc(ip)}</span>`).join("<br>") : dash],
  ]);
  renderGuestTools(vm, g);

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
    ...vm.disks.map((d, i) => ["i-disk", `Hard disk ${i + 1}`,
      d.used_gb != null && d.used_gb < d.size_gb ? `${num(d.used_gb)} GB / ${d.size_gb} GB` : `${d.size_gb} GB`,
      `Pool <b>${esc(d.pool || "unknown")}</b> · ${esc(d.backend)}${d.bus === "sata" ? " · SATA" : ""}`]),
    ...vm.nics.map((n, i) => ["i-migrate", `Network adapter ${i + 1}`, esc(n.network), `${esc(n.model)} · <span class="mono">${esc(n.mac || "auto")}</span>`]),
    ["i-disk", "CD/DVD drive", vm.iso ? "ISO attached" : "Empty", vm.iso ? `<span class="mono">${esc(vm.iso)}</span>` : ""],
    ...(vm.driver_iso ? [["i-disk", "CD/DVD drive 2", "Drivers (virtio)", `<span class="mono">${esc(vm.driver_iso)}</span>`]] : []),
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

  renderVMAlerts(vm);
  await Promise.all([renderVMSnapshots(vm), renderVMBackups(vm)]);
}

async function renderVMBackups(vm) {
  const tbody = $("#vm-backups");
  if (!state.targets.length) { tbody.innerHTML = empty(6, "Add a backup repository first (Backup → Repositories)"); return; }
  const lists = await Promise.all(state.targets.map((t) =>
    api(`/backup/targets/${enc(t.name)}/backups`).then((bs) => bs.map((b) => ({ ...b, target: t.name }))).catch(() => [])));
  const points = lists.flat().filter((b) => b.vm_name === vm.name).sort((a, b) => b.created.localeCompare(a.created));
  const op = canOperate();
  tbody.innerHTML = points.length ? points.map((b) => `<tr>
    <td>${esc(when(b.created))}</td><td>${esc(b.target)}</td><td>${esc(b.node || "shared")}</td>
    <td>${size(b.size_bytes)}</td><td title="${esc(`Read from VM: ${size(b.read_bytes || 0)}; duplicate bytes avoided: ${size(b.dedup_saved_bytes || 0)}; codec: ${b.compression || 'legacy'}`)}">${size(b.stored_bytes)}${b.new_data_bytes && b.stored_bytes ? `<span class="backup-kind">${(b.new_data_bytes / b.stored_bytes).toFixed(2)}× compression of new data</span>` : '<span class="backup-kind">No new compressed data</span>'}</td>
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
  backupNow(vm.uuid, vm.name).catch(fail);
});

// ---------- VM snapshots and alerts ----------

async function renderVMSnapshots(vm) {
  const box = $("#vm-snaps");
  const snaps = await api(`/vms/${vm.uuid}/snapshots`).catch((err) => { box.innerHTML = `<li class="hint">${esc(err.message)}</li>`; return null; });
  if (!snaps) return;
  $("#vm-snap-n").textContent = snaps.length || "";
  const block = vm.disks.some((d) => d.backend === "block");
  const why = vm.template ? "Templates do not take snapshots." : block ? "This VM has a disk on block storage (LVM/iSCSI): snapshots need qcow2 disks. Use Back up now instead." : "";
  $("#vm-snap-take").disabled = Boolean(why);
  $("#vm-snap-take").title = why;
  const op = canOperate() && !vm.template;
  const here = `<li class="snap here"><span class="snap-dot"></span><div><b>You are here</b><span class="vm-sub">The VM as it is now${snaps.some((s) => s.current) ? `, changed since ${esc(snaps.find((s) => s.current).name)}` : ""}</span></div></li>`;
  box.innerHTML = (snaps.length ? [...snaps].reverse().map((s) => `<li class="snap${s.current ? " current" : ""}">
      <span class="snap-dot"><svg><use href="#i-camera"/></svg></span>
      <div class="snap-body">
        <div class="snap-head"><b>${esc(s.name)}</b>
          <span class="badge ${s.vm_state === "running" ? "running" : "plain"}">${s.vm_state === "running" ? "with memory" : "disks only"}</span>
          ${s.current ? `<span class="badge plain">current</span>` : ""}</div>
        <span class="vm-sub">${esc(when(s.created))}${s.parent ? ` · after ${esc(s.parent)}` : ""} · stored inside the VM's disks in ${esc(snapPools(vm))}</span>
        ${s.description ? `<p>${esc(s.description)}</p>` : ""}
      </div>
      ${op ? `<div class="actions"><button class="small" data-snap="revert" data-name="${esc(s.name)}" data-state="${esc(s.vm_state)}">Restore</button>
        <button class="small danger" data-snap="delete" data-name="${esc(s.name)}">Delete</button></div>` : ""}
    </li>`).join("") : `<li class="hint snap-empty">No snapshots yet.${why ? " " + esc(why) : " Take one before an upgrade or a risky change."}</li>`) ;
  if (snaps.length) box.insertAdjacentHTML("afterbegin", here);
}
$("#vm-snap-take").addEventListener("click", () => openSnapshot(vmFromHash()));
function openSnapshot(uuid) {
  const vm = state.vms.find((v) => v.uuid === uuid);
  if (!vm) return;
  openDialog("snap-dialog", { uuid: vm.uuid }, { name: snapshotName(vm.name) });
  const uefiOn = vm.state === "running" && vm.firmware === "uefi";
  $("#snap-form .snap-restart").hidden = !uefiOn;
  $("#snap-form .snap-mem").textContent = vm.state === "running"
    ? "The VM is running: its memory is saved too, so it pauses for a moment. Reverting brings it back running."
    : "The VM is off: the snapshot holds its disks.";
}
// Default snapshot name: the VM's name and when it was taken, e.g. web01-20261106-1530.
function snapshotName(vmName, d = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
  const base = String(vmName || "").replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 48).replace(/-+$/, "");
  return `${base || "snap"}-${stamp}`;
}
handlers["snap-dialog"] = async (f, ctx) => {
  const restart = !$("#snap-form .snap-restart").hidden && f.get("restart") === "on";
  toast(restart ? "Taking the snapshot: the VM may shut down and start again first… (see Tasks)" : "Taking the snapshot… (see Tasks)");
  setTimeout(() => load("jobs").catch(() => {}), 800);
  await post(`/vms/${ctx.uuid}/snapshots`, { name: f.get("name").trim(), description: f.get("description"), restart });
  toast(`Snapshot taken: ${f.get("name").trim()}`);
  if (currentPage() !== "vm") return;
  selectTab("vm-tabs", "vm-snapshots");
  await refresh("vm");
  selectTab("vm-tabs", "vm-snapshots");
};
const snapPools = (vm) => [...new Set((vm.disks || []).map((d) => d.pool).filter(Boolean))].join(", ") || "its pool";
// Restore or delete a snapshot; true when done. Shared by the VM page and Backup → Snapshots.
async function snapAction(b, uuid, vmName) {
  const name = b.dataset.name;
  if (b.dataset.snap === "revert") {
    const lose = `Everything that changed in ${vmName} since then is lost.`;
    if (!confirm(`Restore ${vmName} to snapshot ${name}? ${lose}${b.dataset.state === "running" ? " The VM will be running, as it was." : " The VM will be off."}`)) return false;
    b.disabled = true;
    await post(`/vms/${uuid}/snapshots/${enc(name)}/revert`);
    toast(`${vmName} is back at ${name}`);
  } else {
    if (!confirm(`Delete snapshot ${name} of ${vmName}? The VM itself does not change.`)) return false;
    b.disabled = true;
    await del(`/vms/${uuid}/snapshots/${enc(name)}`);
    toast(`Snapshot ${name} deleted`);
  }
  return true;
}
$("#vm-snaps").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-snap]");
  if (!b) return;
  const uuid = vmFromHash(), vm = state.vms.find((v) => v.uuid === uuid);
  try {
    if (!(await snapAction(b, uuid, vm.name))) return;
    await refresh("vm");
    selectTab("vm-tabs", "vm-snapshots");
  } catch (err) { b.disabled = false; fail(err); }
});
async function renderAllSnapshots() {
  const box = $("#all-snaps");
  const snaps = await api("/snapshots").catch((err) => { box.innerHTML = empty(6, esc(err.message)); return null; });
  if (!snaps) return;
  const op = canOperate();
  box.innerHTML = snaps.length ? snaps.sort((a, b) => a.vm_name.localeCompare(b.vm_name) || b.created.localeCompare(a.created)).map((s) => `<tr>
    <td><a href="#vm/${esc(s.vm_uuid)}">${esc(s.vm_name)}</a></td>
    <td><b>${esc(s.name)}</b>${s.current ? ` <span class="badge plain">current</span>` : ""}${s.description ? `<div class="vm-sub">${esc(s.description)}</div>` : ""}</td>
    <td>${esc(when(s.created))}</td>
    <td>${s.vm_state === "running" ? "with memory" : "disks only"}</td>
    <td>${esc(s.node)} · ${esc(s.stored_in || "-")}</td>
    <td><div class="actions">${op ? `<button class="small" data-snap="revert" data-uuid="${esc(s.vm_uuid)}" data-vm="${esc(s.vm_name)}" data-name="${esc(s.name)}" data-state="${esc(s.vm_state)}">Restore</button>
      <button class="small danger" data-snap="delete" data-uuid="${esc(s.vm_uuid)}" data-vm="${esc(s.vm_name)}" data-name="${esc(s.name)}">Delete</button>` : ""}</div></td>
  </tr>`).join("") : empty(6, "No snapshots. Take one from a VM's menu (Snapshot).");
}
$("#all-snaps").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-snap]");
  if (!b) return;
  try {
    if (await snapAction(b, b.dataset.uuid, b.dataset.vm)) await renderAllSnapshots();
  } catch (err) { b.disabled = false; fail(err); }
});

const ALERT_HELP = [
  [/^CPU/, "i-cpu", "The guest uses almost all its processors. Add vCPUs in Settings, or find what keeps it busy."],
  [/^Memory/, "i-mem", "The guest uses almost all its memory. Add memory in Settings before it starts swapping."],
  [/^Disk/, "i-hdd", "The disks are nearly full. Enlarge a disk in Settings, then extend the partition in the guest."],
  [/^No guest agent/, "i-alert", "Install the guest tools (⋯ → Insert guest tools CD) so ibHiCS sees IPs, disk use and can shut it down cleanly."],
  [/^Restart/, "i-refresh", "Processor, memory or boot order changes wait for the next power off and on."],
];
function renderVMAlerts(vm) {
  const alerts = vmAlerts(vm);
  $("#vm-alert-n").textContent = alerts.length || "";
  $("#vm-alerts").innerHTML = alerts.length ? alerts.map((a) => {
    const [, icon, help] = ALERT_HELP.find(([re]) => re.test(a)) || [null, "i-alert", ""];
    return `<li><span class="al-icon"><svg><use href="#${icon}"/></svg></span><div><b>${esc(a)}</b><span class="vm-sub">${esc(help)}</span></div></li>`;
  }).join("") : `<li class="ok"><span class="al-icon"><svg><use href="#i-power"/></svg></span><div><b>No alerts</b><span class="vm-sub">CPU, memory, disks and the guest agent look fine.</span></div></li>`;
}

// ---------- import a VM from files (the uploads source of ibHiCS Migrate) ----------

let uploadedVMs = [];
async function listUploaded() {
  const tbody = $("#vmimport-vms");
  uploadedVMs = await api("/migrate/sources/uploads/vms").catch((err) => { tbody.innerHTML = empty(6, err.message); return null; });
  if (!uploadedVMs) return;
  tbody.innerHTML = uploadedVMs.length ? uploadedVMs.map((vm, i) => `<tr>
    <td><div class="name-cell">${avatar("i-compute")}<div><span class="vm-name">${esc(vm.name)}</span><span class="vm-sub clip">${esc(vm.ref.split("/").pop())}</span></div></div></td>
    <td>${vm.vcpus}</td><td>${gbText(vm.memory_mb)}</td><td>${vm.disks_gb.map((g) => (g ? `${g} GB` : "?")).join(", ")}</td>
    <td>${esc(vm.firmware.toUpperCase())}</td>
    <td><div class="actions"><button type="button" class="small primary" data-upimport="${i}">Import</button>
      <button type="button" class="small danger" data-updel="${esc(vm.ref.split("/").pop())}">Delete file</button></div></td>
  </tr>`).join("") : empty(6, "Nothing uploaded yet");
}
$("#vm-import-open").addEventListener("click", () => {
  openDialog("vmimport-dialog");
  $("#vmimport-progress").innerHTML = "";
  listUploaded();
});
$("#vmimport-files").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  for (const file of files) {
    const row = document.createElement("div");
    row.className = "upload-row";
    row.innerHTML = `<span class="vm-name">${esc(file.name)}</span><span class="bar"><i></i></span><span class="pct">0%</span>`;
    $("#vmimport-progress").append(row);
    try {
      await uploadFile(file, row);
      $(".pct", row).textContent = "Uploaded";
    } catch (err) {
      row.classList.add("failed");
      $(".pct", row).textContent = err.message;
    }
  }
  await listUploaded();
});
$("#vmimport-vms").addEventListener("click", async (e) => {
  const imp = e.target.closest("button[data-upimport]"), rm = e.target.closest("button[data-updel]");
  if (rm) {
    if (!confirm(`Delete the uploaded file ${rm.dataset.updel}?`)) return;
    try { await del(`/migrate/uploads/${enc(rm.dataset.updel)}`); await listUploaded(); } catch (err) { fail(err); }
  }
  if (imp) {
    $("#vmimport-dialog").close();
    // The migration wizard, for the one uploaded VM picked here.
    sourceVMs = [...uploadedVMs]; sourceVMsFor = "uploads";
    await openImportWizard([Number(imp.dataset.upimport)]).catch(fail);
  }
});

// ---------- recycle bin ----------

function renderRecycle() {
  const { days, vms } = state.recycle;
  $("#rb-sub").textContent = days ? `Deleted VMs are kept ${days} days with their disks, then deleted for good` : "Off: deleting a VM removes it at once";
  $("#rb-empty").disabled = !vms.length;
  $("#rb-rows").innerHTML = vms.length ? vms.map(({ vm, expires }) => `<tr>
    <td><div class="name-cell">${avatar(vm.template ? "i-template" : osIcon(vm))}<div><span class="vm-name">${esc(vm.name)}</span><span class="vm-sub">${esc(vm.os_type || (vm.template ? "Template" : ""))}</span></div></div></td>
    <td>${esc(vm.node || "")}</td>
    <td>${vm.vcpus} vCPU · ${gbText(vm.memory_mb)}</td>
    <td>${num(diskTotal(vm))} GB <span class="vm-sub">${esc([...new Set(vm.disks.map((d) => d.pool))].join(", "))}</span></td>
    <td>${esc(when(vm.recycled))}</td>
    <td>${expires ? `${esc(when(expires))}<span class="vm-sub">${esc(leftUntil(expires))}</span>` : "–"}</td>
    <td><div class="actions">${canOperate() ? `<button class="small" data-rb="restore" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Restore</button>
      <button class="small danger" data-rb="purge" data-id="${vm.uuid}" data-name="${esc(vm.name)}">Delete now</button>` : ""}</div></td>
  </tr>`).join("") : empty(7, "The recycle bin is empty.");
}
$("#rb-rows").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-rb]");
  if (!b) return;
  const { rb, id, name } = b.dataset;
  try {
    if (rb === "restore") {
      await post(`/recycle-bin/${id}/restore`);
      toast(`${name} restored. It is off; power it on when you need it.`);
    } else {
      if (!confirm(`Delete ${name} and its disks for good? This cannot be undone.`)) return;
      await del(`/recycle-bin/${id}`);
      toast(`${name} deleted for good`);
    }
    await refresh("recycle");
  } catch (err) { fail(err); }
});
$("#rb-empty").addEventListener("click", async () => {
  const vms = state.recycle.vms;
  if (!vms.length || !confirm(`Delete ${vms.length} VM${vms.length === 1 ? "" : "s"} and their disks for good? This cannot be undone.\n\n${vms.map((r) => r.vm.name).join(", ")}`)) return;
  const results = await Promise.allSettled(vms.map((r) => del(`/recycle-bin/${r.vm.uuid}`)));
  const failed = results.filter((r) => r.status === "rejected");
  toast(failed.length ? `${failed.length} could not be deleted: ${failed.map((r) => r.reason.message).join("; ")}` : "Recycle bin emptied", failed.length > 0);
  await refresh("recycle");
});
function leftUntil(iso) {
  const h = (new Date(iso) - Date.now()) / 3600000;
  if (h <= 1) return "within the hour";
  return h < 48 ? `in ${Math.round(h)} hours` : `in ${Math.round(h / 24)} days`;
}
const binNote = () => (state.recycle.days ? ` It goes to the recycle bin for ${state.recycle.days} days and can be restored.` : " This cannot be undone.");

// ---------- backup ----------

const protection = { points: [], usage: new Map(), errors: [], view: "timeline", selected: null, request: 0 };
const pointLocked = b => b.immutable_until && new Date(b.immutable_until) > new Date();
function protectionBadges(b) {
  return `<div class="bp-badges"><span class="badge ${b.verified === "passed" ? "done" : b.verified === "failed" ? "failed" : "plain"}">${b.verified === "passed" ? "✓ Boot verified" : b.verified === "failed" ? "Boot check failed" : "Boot not verified"}</span>${pointLocked(b) ? `<span class="badge waiting" title="${esc(when(b.immutable_until))}">🔒 Immutable · ${esc(b.lock_status === "native" ? "native" : "soft lock")}</span>` : ""}${b.suspicious ? `<span class="badge failed" title="${esc(b.anomaly_reason)}">⚠ Suspicious changes</span>` : ""}</div>`;
}
function protectionNext(p) {
  if (!p.enabled) return null;
  const now = new Date();
  if (p.every_hours) return p.last_run ? new Date(Math.max(+now, +new Date(p.last_run) + p.every_hours * 3600000)) : now;
  const next = new Date(), [h, m] = p.at.split(":").map(Number);
  next.setHours(h, m, 0, 0);
  if (+next < +now) next.setDate(next.getDate() + 1);
  return next;
}
function protectionFreeSpace(usage) {
  const scopes = new Map();
  for (const u of usage) {
    const key = `${u.node}/${u.capacity_scope}`;
    const group = scopes.get(key) || {physical:Infinity, quota:0, unbounded:false};
    group.physical = Math.min(group.physical, u.filesystem_free_bytes ?? u.free_bytes ?? 0);
    group.quota += u.free_bytes || 0;
    group.unbounded ||= !u.quota_bytes;
    scopes.set(key, group);
  }
  return [...scopes.values()].reduce((sum,g)=>sum+Math.min(g.physical,g.unbounded ? Infinity : g.quota),0);
}
async function protectionOverview() {
  const rows = await Promise.all(state.targets.map(async t => {
    const [points, usage] = await Promise.allSettled([api(`/backup/targets/${enc(t.name)}/backups`), api(`/backup/targets/${enc(t.name)}/usage`)]);
    return { t, points, usage };
  }));
  protection.points = rows.flatMap(r => r.points.status === "fulfilled" ? r.points.value.map(b => ({...b, target:r.t.name})) : []);
  protection.usage = new Map(rows.map(r => [r.t.name, r.usage.status === "fulfilled" ? r.usage.value : [{error:r.usage.reason.message}]]));
  protection.errors = rows.filter(r => r.points.status === "rejected").map(r => r.t.name);
  const vms = state.vms.filter(v => !v.recycled && !v.template), policies = state.policies.filter(p => p.enabled);
  const covered = vms.filter(v => policies.some(p => p.entity_type !== "container" && (!p.vms.length || p.vms.includes(v.name)))).length;
  const pct = vms.length ? Math.round(covered / vms.length * 100) : 0;
  const jobs = state.jobs.filter(j => j.kind === "backup" && ["done", "failed"].includes(j.state)).sort((a,b) => String(b.finished).localeCompare(String(a.finished)));
  const next = policies.map(protectionNext).filter(Boolean).sort((a,b) => a-b)[0];
  const usage = [...protection.usage.values()].flat(), used = usage.reduce((n,u) => n + (u.used_bytes || 0), 0), free = protectionFreeSpace(usage);
  const known = usage.length && usage.every(u => u.used_bytes != null && u.free_bytes != null && !u.error);
  const pts = protection.points;
  const last = jobs[0], lastOk = last && last.state === "done";
  const verified = pts.filter(b => b.verified === "passed").length, locked = pts.filter(pointLocked).length, alerts = pts.filter(b => b.suspicious).length;
  const cap = used + (free || 0), usedPct = known && cap ? Math.round(used / cap * 100) : 0;
  const icon = (d) => `<svg class="bp-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>`;
  const tile = (cls, title, ico, body) => `<div class="bp-holo ${cls}"><i class="bp-corner"></i><small>${ico}${title}</small>${body}</div>`;
  $("#protection-overview").innerHTML = [
    tile(pct === 100 ? "ok" : "warn", "Protected VMs", icon("M12 2l8 3v6c0 5-3.4 9.4-8 11-4.6-1.6-8-6-8-11V5z"),
      `<div class="bp-ring" style="--coverage:${pct}%" role="img" aria-label="${pct}% of VMs have an enabled schedule"><b>${pct}%</b></div><span>${covered} of ${vms.length} VMs</span>`),
    tile(!last ? "idle" : lastOk ? "ok" : "bad", "Last backup", icon("M12 4a8 8 0 1 0 8 8M12 8v4l3 2"),
      `<b class="bp-big">${!last ? "—" : lastOk ? "✓ OK" : "✕ Failed"}</b><span>${esc(last ? when(last.finished) : "No runs yet")}</span><span class="bp-dim">Next: ${next ? esc(when(next.toISOString())) : "not scheduled"}</span>`),
    tile(alerts ? "bad" : "ok", "Recovery proof", icon("M4 12l5 5L20 6"),
      `<div class="bp-trio"><span><b>${verified}</b>Boot tested</span><span><b>${locked}</b>Locked</span><span class="${alerts ? "bp-alert" : ""}"><b>${alerts}</b>Alerts</span></div><span class="bp-dim">${pts.length} restore points${protection.errors.length ? " · some repositories unavailable" : ""}</span>`),
    tile(known ? "ok" : "idle", "Repository space", icon("M4 6h16v4H4zM4 14h16v4H4z"),
      `<b class="bp-big">${known ? size(free) : "—"}</b><span>${known ? "free" : "Unknown"}</span><div class="bp-bar"><i style="width:${usedPct}%"></i></div>`),
  ].join("");
  renderTargets();
}
function renderTargets() {
  $("#targets").innerHTML = state.targets.map(t => {
    const usage = protection.usage.get(t.name) || [], known = usage.length && usage.every(u => u.used_bytes != null && u.free_bytes != null && !u.error);
    const used = usage.reduce((n,u)=>n+(u.used_bytes||0),0), free = usage.reduce((n,u)=>n+(u.free_bytes||0),0), pct = used+free ? Math.round(used/(used+free)*100) : 0;
    return `<article class="bp-card"><div class="bp-card-head"><h3>${esc(t.name)}</h3><span class="badge info">${esc(TARGET_TYPES[t.type] || t.type)}</span></div><p>${esc(t.source)}</p><div class="bp-meter"><span style="width:${known ? pct : 0}%"></span></div><p>${known ? `${size(used)} used · ${size(free)} free` : "Capacity unavailable"}</p><p>🔒 ${t.settings.immutability_days || 0} days immutability</p><small>Immutability locks each new restore point for this many days. A locked point cannot be deleted or changed by anyone, admins and ransomware included. ${t.settings.immutability_days ? "" : "0 = not locked."}</small><button class="small operator-only" data-immutability="${esc(t.name)}" data-days="${Number(t.settings.immutability_days || 0)}">Edit lock days</button><p class="${usage.some(u=>u.error) ? "bp-alert" : ""}">${esc(usage.find(u=>u.error)?.error || (t.on_demand ? "Attached during jobs" : "Always attached"))}</p><div class="actions">${isAdmin() ? `<button class="small danger" data-target="${esc(t.name)}">Remove</button>` : ""}</div></article>`;
  }).join("") || `<p class="hint">Add a repository to start protecting VMs.</p>`;
}
function renderProtectionTimeline(backups) {
  const groups = new Map();
  backups.forEach(b => { if (!groups.has(b.vm_uuid)) groups.set(b.vm_uuid, []); groups.get(b.vm_uuid).push(b); });
  $("#backup-timeline").innerHTML = [...groups.values()].map(points => `<article class="bp-lane"><div class="bp-card-head"><h3>${esc(points[0].vm_name)}</h3><small>${points.length} restore points</small></div><div class="bp-track">${points.map(b=>`<button class="bp-point ${b.suspicious ? "suspicious" : b.verified === "passed" ? "verified" : ""}" data-bp-point="${esc(b.id)}" data-node="${esc(b.node || "")}" aria-label="${esc(`${b.vm_name}, ${when(b.created)}, ${b.verified}, ${pointLocked(b) ? "immutable" : "unlocked"}, ${b.suspicious ? "suspicious" : "no anomaly flag"}`)}"><span class="bp-orb">${b.suspicious ? "⚠" : b.verified === "passed" ? "✓" : "●"}</span><time>${esc(when(b.created))}</time><small>${pointLocked(b) ? "🔒 " : ""}${esc(b.node || "shared")} · ${size(b.size_bytes)}</small></button>`).join("")}</div></article>`).join("") || `<p class="hint">No restore points on this repository yet.</p>`;
}
function protectionDetail(b) {
  protection.selected = b;
  const el = $("#backup-detail"); el.hidden = false;
  el.innerHTML = `<div class="bp-card-head"><h3>${esc(b.vm_name)}</h3><button data-bp-close aria-label="Close restore point details">×</button></div><p>${esc(when(b.created))}</p>${protectionBadges(b)}<dl><dt>Logical size</dt><dd>${size(b.size_bytes)}</dd><dt>New stored data</dt><dd>${size(b.stored_bytes)}</dd><dt>Verification</dt><dd>${esc(b.entity_type === "container" ? "Boot verification is not available for container filesystem backups" : b.verification?.reason || "No boot verification has run")}</dd>${b.verification?.screenshot_error ? `<dt>Screenshot</dt><dd>${esc(b.verification.screenshot_error)}</dd>` : ""}<dt>Locked until</dt><dd>${esc(when(b.immutable_until))}</dd><dt>Change analysis</dt><dd>${esc(b.anomaly_reason || "No anomaly flag. This does not prove the data is safe.")}</dd></dl>${pointLocked(b) && b.lock_status !== "native" ? '<p class="bp-alert">Soft lock: API deletion is blocked. Host root and storage administrators can still alter data.</p>' : ""}<div class="actions">${canOperate() ? `<button class="primary" data-restore="${esc(b.id)}" data-vm="${esc(b.vm_name)}" data-node="${esc(b.node || "")}">Restore</button>${b.entity_type === "container" ? "" : `<button data-bp-recover>Recover &amp; power on</button>`}` : ""}${b.verification?.screenshot_path ? '<button data-bp-evidence>View boot screenshot</button>' : ""}</div><p class="hint">${b.entity_type === "container" ? "Container filesystem backup. Restore creates a new, stopped container; review its network before starting it." : "Instant recovery is unavailable for dedup chunks. Recovery reconstructs all disks into the pool, then powers on. Actual duration appears in Jobs. No live finalize is needed."}</p><div id="bp-evidence"></div>`;
}

async function renderBackups() {
  const target = $("#browse-target").value || state.targets[0]?.name;
  const tbody = $("#backups");
  if (!target) { tbody.innerHTML = empty(6, "Add a backup repository first"); $("#backup-timeline").textContent = "Add a backup repository first"; $("#backup-detail").hidden = true; return; }
  const op = canOperate(), admin = isAdmin();
  const request = ++protection.request;
  try {
    const backups = await api(`/backup/targets/${enc(target)}/backups`);
    if (request !== protection.request) return;
    protection.current = backups;
    renderProtectionTimeline(backups);
    tbody.innerHTML = backups.length ? backups.slice().reverse().map((b) => `<tr>
      <td class="vm-name">${esc(b.vm_name)}<span class="backup-kind" title="${esc(b.note || "")}">${b.entity_type === "container" ? "Container · " : ""}${esc(({full:"Full",incremental:"Incremental (CBT)",synthetic_full:"Synthetic full",copy:"Backup copy",archive:"Archive"})[b.kind] || "Full")}</span></td>
      <td><button class="small" data-bp-point="${esc(b.id)}" data-node="${esc(b.node || "")}">${esc(when(b.created))}</button>${protectionBadges(b)}</td>
      <td>${esc(b.node || "shared")}</td>
      <td>${size(b.size_bytes)}</td>
      <td>${size(b.stored_bytes)}</td>
      <td><div class="actions">
        ${op ? `<button class="small" data-restore="${esc(b.id)}" data-vm="${esc(b.vm_name)}" data-node="${esc(b.node || "")}">Restore</button>` : ""}
        ${op ? `<button class="small" data-backup-op="${esc(b.id)}" data-node="${esc(b.node || "")}">Synthetic / Copy</button>` : ""}
        ${admin ? `<button ${pointLocked(b) ? 'disabled title="Immutable points cannot be deleted during retention"' : ""} class="small danger" data-backup-del="${esc(b.id)}" data-node="${esc(b.node || "")}">Delete</button>` : ""}
      </div></td>
    </tr>`).join("") : empty(6, "No restore points on this target yet");
  } catch (err) {
    if (request !== protection.request) return;
    $("#backup-detail").hidden = true;
    protection.current = [];
    tbody.innerHTML = empty(6, err.message);
    $("#backup-timeline").textContent = err.message;
  }
}

function renderPolicies() {
  $("#policies").innerHTML = state.policies.map(p => {
    const jobs = state.jobs.filter(j => j.policy === p.name && j.kind === "backup").sort((a,b)=>String(a.created).localeCompare(String(b.created))).slice(-14);
    const dots = Array.from({length:14-jobs.length},()=>'<span class="bp-history-dot empty" title="No recorded run" aria-label="No recorded run"></span>').join("") + jobs.map(j => `<span class="bp-history-dot ${esc(j.state)}" title="${esc(`${j.vm} · ${when(j.created)} · ${j.state}${j.error ? ` · ${j.error}` : ""}`)}" aria-label="${esc(`${j.vm}: ${j.state}`)}">${j.report?.verification?.verified === "passed" ? "✓" : ""}</span>`).join("");
    return `<article class="bp-card"><div class="bp-card-head"><h3>${p.entity_type === "container" ? "Container · " : ""}${esc(p.name)}</h3><span class="badge ${p.enabled ? "online" : "plain"}">${p.enabled ? "Enabled" : "Disabled"}</span></div><p class="bp-schedule-time">${p.at ? `Daily · ${esc(p.at)}` : `Every ${p.every_hours} hours`}</p><p>${p.vms.length ? esc(p.vms.join(", ")) : "Every VM, including new ones"}</p><small>${esc(p.target)} · Keep ${p.keep_last} recent + GFS</small><div class="bp-history" aria-label="Last 14 VM backup jobs">${dots}</div><small>Last 14 runs · green success / red failed / grey no run</small><p>${p.entity_type === "container" ? (p.allow_stop ? "Full filesystem · temporary stop allowed" : "Full filesystem · stopped containers only") : p.verify_after_backup ? `✓ Verify after backup · ${esc(p.verify_pool)} · ${p.verify_timeout}s` : "Boot verification is off"}</p><div class="actions">${canOperate() ? `<button data-policy-run="${esc(p.name)}">Run now</button>` : ""}${isAdmin() ? `<button data-policy-edit="${esc(p.name)}">Edit</button><button class="danger" data-policy-del="${esc(p.name)}">Delete</button>` : ""}</div></article>`;
  }).join("") || `<p class="hint">No schedules yet. Create one to protect VMs automatically.</p>`;
}

function backupLiveDetails(j) {
  if (j.kind !== "backup" || !["queued", "running", "retry_wait"].includes(j.state)) return "";
  const p = j.progress || {}, duration = Math.max(0, (Date.now() - Date.parse(j.created)) / 1000);
  const size = n => `${(Number(n) / 2 ** 30).toFixed(2)} GiB`;
  const parts = [`Elapsed ${Math.floor(duration / 60)}m ${Math.floor(duration % 60)}s`];
  if (p.mode) parts.push(p.mode === "incremental" ? "Incremental CBT" : "Full scan");
  if (p.read_bytes != null) parts.push(`Read ${size(p.read_bytes)}`);
  if (p.mode === "full" && p.total_bytes && p.scanned_bytes != null) parts.push(`Disk scan ${size(p.scanned_bytes)} / ${size(p.total_bytes)}`);
  if (p.stored_bytes != null) parts.push(`New data stored ${size(p.stored_bytes)}`);
  if (p.read_mib_per_second > 0) parts.push(`Average ${Number(p.read_mib_per_second).toFixed(1)} MiB/s`);
  const age = p.updated_at ? Math.max(0, Date.now() / 1000 - p.updated_at) : null;
  if (age != null) parts.push(`Last progress ${Math.floor(age)}s ago`);
  const bar = j.percent != null ? `<progress max="100" value="${Math.max(0, Math.min(100, Number(j.percent)))}"></progress> ${Number(j.percent).toFixed(1)}%` : `<progress max="100" aria-label="Backup in progress"></progress> ${esc(p.phase === "waiting" ? "Waiting for repository" : p.phase === "exporting" ? "Exporting disk image; total progress not yet available" : p.phase === "reading" ? "Reading changed blocks; total size not yet known" : "Preparing backup")}`;
  const waiting = age > 120 && p.phase !== "waiting" ? '<span class="badge warning">No new progress reported for over 2 minutes; check storage/node</span>' : "";
  return `<div class="backup-kind">${parts.map(esc).join(" · ")}</div>${bar}${waiting}`;
}

function renderJobs() {
  const jobs = state.jobs.slice(0, 50);
  $("#jobs").innerHTML = jobs.length ? jobs.map((j) => `<tr>
    <td>${esc(JOB_KINDS[j.kind] || j.kind)}${j.policy ? ` <span class="vm-sub" style="display:inline">(${esc(j.policy)})</span>` : ""}</td>
    <td class="vm-name">${esc(j.vm)}</td>
    <td>${esc(j.target)}</td>
    <td>${esc(j.node || "")}</td>
    <td>${esc(when(j.created))}</td>
    <td><span class="badge ${esc(j.state)}">${JOB_STATE[j.state] || esc(j.state)}</span></td>
    <td class="vm-sub" style="display:table-cell">${j.state === "done" && j.report?.recovery_mode ? `Recovered in ${j.report.elapsed_seconds}s · full restore before boot` : j.kind === "verify" ? esc(j.report?.reason || j.error || j.step) : j.state === "done" && j.report && Object.keys(j.report).length ? `<button class="small" data-backup-report="${esc(j.id)}">Report / Export</button><span class="backup-kind">${j.report.new_data_bytes && j.report.stored_bytes ? `Compression ${(j.report.new_data_bytes / j.report.stored_bytes).toFixed(2)}×` : 'No new compressed data'} · ${j.report.new_data_bytes ? `Dedup ${((j.report.new_data_bytes + (j.report.dedup_saved_bytes || 0)) / j.report.new_data_bytes).toFixed(2)}×` : (j.report.dedup_saved_bytes ? 'All data reused' : 'No dedup input')}</span>` : esc(j.state === "retry_wait" ? j.step : j.error || j.step || j.result || "")}${backupLiveDetails(j)}${j.kind === "backup" && ["queued", "running", "retry_wait"].includes(j.state) && canOperate() && !Object.keys(j.report || {}).length && !["committing", "complete"].includes(j.progress?.phase) ? `<button class="small danger" data-backup-cancel="${esc(j.id)}" ${j.cancel_requested ? "disabled" : ""}>${j.cancel_requested ? "Cancelling…" : "Cancel job"}</button>` : ""}${j.report?.suspicious ? '<span class="badge failed">⚠ Suspicious data change</span>' : ""}</td>
  </tr>`).join("") : empty(7, "No jobs yet");
}

$("#jobs").addEventListener("click", async e => {
  const b = e.target.closest("[data-backup-cancel]");
  if (!b || !confirm("Cancel this backup job? The VM stays running and previous restore points are kept.")) return;
  b.disabled = true;
  try {
    await post(`/backup/jobs/${enc(b.dataset.backupCancel)}/cancel`);
    await load("jobs"); renderJobs();
    toast("Cancellation requested; waiting for safe cleanup.");
  } catch (err) { fail(err); b.disabled = false; }
});

$("#browse-target").addEventListener("change", () => { protection.selected = null; $("#backup-detail").hidden = true; renderBackups(); });
$("#refresh-backups").addEventListener("click", async () => { await protectionOverview(); await renderBackups(); });

$("#page-backup").addEventListener("click", async e => {
  const btn = e.target.closest("button"); if (!btn) return;
  const d = btn.dataset;
  if (d.bpView) {
    protection.view = d.bpView;
    $("#backup-list").hidden = d.bpView !== "list";
    $("#backup-timeline").hidden = d.bpView !== "timeline";
    $$('[data-bp-view]').forEach(b=>b.setAttribute("aria-pressed",String(b === btn)));
  }
  if (d.bpClose != null) $("#backup-detail").hidden = true;
  if (d.bpPoint) { const b = protection.current?.find(p=>p.id === d.bpPoint && (p.node || "") === (d.node || "")); if (b) protectionDetail(b); }
  if (d.bpRecover != null && protection.selected) {
    const b = protection.selected;
    openDialog("restore-dialog", {target:$("#browse-target").value, id:b.id, sourceNode:b.node, recovery:true}, {name:`${b.vm_name}-recovery`});
    toast("Recovery will fully restore disks, then power on with the saved network configuration.");
  }
  if (d.bpEvidence != null && protection.selected) {
    try {
      const b = protection.selected, target = $("#browse-target").value;
      const data = await api(`/backup/targets/${enc(target)}/backups/${enc(b.id)}/evidence${b.node ? `?node=${enc(b.node)}` : ""}`);
      if (protection.selected !== b) return;
      if (data.mime === "image/png") $("#bp-evidence").innerHTML = `<img alt="Console captured during isolated boot verification" src="data:image/png;base64,${esc(data.data)}">`;
      else $("#bp-evidence").innerHTML = `<a download="boot-console.ppm" href="data:image/x-portable-pixmap;base64,${esc(data.data)}">Download console screenshot (PPM)</a>`;
    } catch (err) { fail(err); }
  }
});

$("#page-backup").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-target], button[data-policy-run], button[data-policy-edit], button[data-policy-del], button[data-restore], button[data-backup-del], button[data-backup-op]");
  if (!btn) return;
  const d = btn.dataset;
  const target = $("#browse-target").value;
  try {
    if (d.target) {
      if (!confirm(`Remove target ${d.target}? Backup files on it stay.`)) return;
      await del(`/backup/targets/${enc(d.target)}`);
    } else if (d.policyEdit) {
      await load("vms", "targets", "policies");
      if (!state.policies.some(p => p.name === d.policyEdit)) throw new Error("This schedule no longer exists. Refresh the list.");
      const policy = state.policies.find(p => p.name === d.policyEdit);
      if (policy.entity_type === "container") await openCtSchedule(policy.vms[0], policy);
      else openDialog("policy-dialog", {policyName:d.policyEdit});
      return;
    } else if (d.policyRun) {
      const jobs = await post(`/backup/policies/${enc(d.policyRun)}/run`);
      toast(`Started ${jobs.length} backup job${jobs.length === 1 ? "" : "s"}`);
    } else if (d.policyDel) {
      if (!confirm(`Delete schedule ${d.policyDel}?`)) return;
      await del(`/backup/policies/${enc(d.policyDel)}`);
    } else if (d.restore) {
      const point = protection.current.find(p => p.id === d.restore && (p.node || "") === (d.node || ""));
      if (point?.entity_type === "container") await openCtRestore(point, target);
      else openDialog("restore-dialog", { target, id: d.restore, sourceNode: d.node }, { name: `${d.vm}-restore` });
      return;
    } else if (d.backupOp) {
      const select = $("#backup-operation-form select[name=destination]");
      select.innerHTML = '<option value="">Choose a repository</option>' + state.targets.filter(t => t.name !== target).map(t => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
      openDialog("backup-operation-dialog", { target, id: d.backupOp, node: d.node });
      return;
    } else if (d.backupDel) {
      if (!confirm(`Delete restore point ${d.backupDel}?`)) return;
      const q = d.node ? `?node=${enc(d.node)}` : "";
      await del(`/backup/targets/${enc(target)}/backups/${enc(d.backupDel)}${q}`);
    }
    await refresh("backup");
  } catch (err) { fail(err); }
});

handlers["target-dialog"] = async (f, ctx) => {
  const FIELDS = {
    pool: [], local: ["path"], nfs: ["host", "export"], smb: ["host", "share", "username", "password"],
    usb: ["device"], iscsi: ["host", "iqn"], fc: ["device"], s3: ["endpoint", "bucket", "access_key", "secret_key", "storage_class"],
  };
  const body = { name: f.get("name"), type: f.get("type"), compression: f.get("compression") || "balanced", immutability_days: Number(f.get("immutability_days")) };
  for (const key of FIELDS[body.type]) if (f.get(key)) body[key] = f.get(key);
  if (body.type === "pool") Object.assign(body, { type: "local", pool: f.get("pool"), quota_gb: Number(f.get("quota_gb")) });
  if (body.type === "iscsi") body.lun = Number(f.get("lun"));
  if (body.type === "iscsi" || body.type === "fc") {
    body.confirm_wipe = f.get("confirm_wipe") === "on";
    if (body.confirm_wipe && !confirm("If the LUN has no filesystem it will be formatted. Continue?")) return;
  }
  await post("/backup/targets", body);
  const after = ctx?.backupAfter;
  if (after) {
    await post(`/vms/${after.id}/backup`, { target: body.name });
    toast(`Repository ${body.name} added. Backing up ${after.name} now; progress is under Backup → Jobs.`);
    await load("targets");
    return;
  }
  toast(`Repository ${body.name} added`);
  await refresh("backup");
};

// New backup schedule: which VMs, when and how many to keep, where to, then a name.
const polWiz = { picked: new Set(), nameTouched: false, editing: null, target: null };
const polForm = $("#policy-form");
function polScope() { return polForm.elements.scope.value; }
function polRender() {
  const pick = polScope() === "pick";
  $(".pol-vms", polForm).hidden = !pick;
  if (!pick) return;
  const q = $(".pol-search", polForm).value.trim().toLowerCase();
  const vms = [...state.vms].sort((a, b) => a.name.localeCompare(b.name)).filter((v) => !q || v.name.toLowerCase().includes(q));
  $(".pol-rows", polForm).innerHTML = vms.length ? vms.map((v) => {
    const on = polWiz.picked.has(v.name);
    return `<tr data-vm="${esc(v.name)}" class="${on ? "sel" : ""}"><td><input type="checkbox" ${on ? "checked" : ""} aria-label="Back up ${esc(v.name)}"></td>
      <td class="vm-name">${esc(v.name)}</td><td>${esc(v.node || "")}</td><td><span class="badge ${esc(v.state)}">${esc(v.state)}</span></td>
      <td>${v.disks.reduce((s, d) => s + d.size_gb, 0).toLocaleString()} GB</td></tr>`;
  }).join("") : empty(5, state.vms.length ? "No VM matches" : "No VMs yet");
  $(".pol-count", polForm).textContent = `${polWiz.picked.size} of ${state.vms.length} VMs ticked`;
}
function polSchedule() {
  const f = polForm.elements;
  $(".pol-at", polForm).hidden = f.mode.value !== "at";
  $(".pol-every", polForm).hidden = f.mode.value !== "every";
  const keep = Number(f.keep_last.value) || 0;
  const hours = f.mode.value === "at" ? 24 : Number(f.every_hours.value) || 0;
  const span = Math.max(0, keep - 1) * hours;
  $(".pol-keep", polForm).textContent = keep && hours
    ? `Recent points span about ${span >= 48 ? `${Math.round(span / 24)} days` : `${span} hours`}. Older points are removed unless retained by GFS.` : "";
}
function polText() {
  const f = polForm.elements;
  return f.mode.value === "at" ? `Every day at ${f.at.value}` : `Every ${f.every_hours.value} hours`;
}
const policyWiz = wizard("policy-dialog", {
  steps: 4,
  reset() {
    const policy = state.policies.find(p => p.name === dialogContext?.policyName);
    const f = polForm.elements;
    polWiz.editing = policy?.name || null;
    polWiz.target = policy?.target || null;
    polWiz.picked = new Set(policy?.vms || []);
    polWiz.nameTouched = Boolean(policy);
    f.name.readOnly = Boolean(policy);
    $("#policy-dialog header h3").textContent = policy ? "Edit backup schedule" : "New backup schedule";
    $(".wiz-ok", polForm).textContent = policy ? "Save changes" : "Create schedule";
    $(".pol-edit-note", polForm).hidden = !policy;
    $(".pol-search", polForm).value = "";
    $(".pol-targets", polForm).innerHTML = "";
    $$('.secondary-repos', polForm).forEach(select => {
      select.innerHTML = '<option value="">Disabled</option>' + state.targets.map(t => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
    });
    if (policy) {
      f.scope.value = policy.vms.length ? "pick" : "all";
      f.mode.value = policy.at != null ? "at" : "every";
      f.backup_mode.value = policy.mode;
      for (const key of ["retries", "retry_delay_minutes", "rpo_hours", "name", "at", "every_hours", "keep_last", "full_weekday", "synthetic_weekday", "keep_weekly", "keep_monthly", "keep_yearly", "copy_target", "copy_keep_last", "archive_target", "archive_weekday", "archive_keep_weekly", "archive_keep_monthly", "archive_keep_yearly"]) {
        if (policy[key] != null) f[key].value = policy[key];
      }
      f.enabled.checked = policy.enabled;
      f.verify_after_backup.checked = policy.verify_after_backup;
      f.verify_pool.value = policy.verify_pool || "";
      f.verify_timeout.value = policy.verify_timeout || 300;
    }
    syncPresets(polForm);
    polSchedule();
    polRender();
    if (!policy) load("vms", "targets", "policies").then(polRender).catch(fail);
  },
  check(step) {
    const f = polForm.elements;
    if (step === 1 && polScope() === "pick" && !polWiz.picked.size) throw new Error("Tick at least one VM, or choose Every VM.");
    if (step === 2) {
      if (f.mode.value === "at" && !/^\d\d:\d\d$/.test(f.at.value)) throw new Error("Pick a time of day.");
      if (f.mode.value === "every" && !(Number(f.every_hours.value) >= 1 && Number(f.every_hours.value) <= 168)) throw new Error("Hours must be 1 to 168.");
      if (f.verify_after_backup.checked && !f.verify_pool.value.trim()) throw new Error("Choose a pool on the backup node for isolated verification.");
      if (!(Number(f.keep_last.value) >= 1)) throw new Error("Keep at least 1 backup.");
    }
    if (step === 3) {
      const names = [$('input[name="target"]:checked', polForm)?.value, f.copy_target.value, f.archive_target.value].filter(Boolean);
      if (new Set(names).size !== names.length) throw new Error("Choose different primary, copy and archive repositories.");
    }
    if (step === 3 && !$('input[name="target"]:checked', polForm)) throw new Error(state.targets.length ? "Pick where the backups go." : "Add a backup repository first (Backup → Repositories).");
    if (step === 4 && !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$/.test(f.name.value)) throw new Error("Name the schedule with letters, digits, '-', '_' or '.'.");
    if (step === 4 && state.policies.some((p) => p.name === f.name.value && p.name !== polWiz.editing)) throw new Error(`A schedule named ${f.name.value} already exists.`);
  },
  show(step) {
    const f = polForm.elements;
    if (step === 1) polRender();
    if (step === 2) polSchedule();
    if (step === 3) {
      $$('.secondary-repos', polForm).forEach(select => {
        const selected = select.value;
        select.innerHTML = '<option value="">Disabled</option>' + state.targets.map(t => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
        select.value = selected;
      });
      const was = $('input[name="target"]:checked', polForm)?.value || polWiz.target;
      $(".pol-targets", polForm).innerHTML = state.targets.map((t, i) => `<label class="type-card small"><input type="radio" name="target" value="${esc(t.name)}" ${(was ? t.name === was : i === 0) ? "checked" : ""}>
        <svg><use href="#i-backup"/></svg><span><b>${esc(t.name)}</b><em>${esc(TARGET_TYPES[t.type] || t.type)} · ${esc(t.source)}</em></span></label>`).join("")
        || `<p class="hint" style="padding:0">No backup target yet.</p>`;
    }
    if (step === 4) {
      if (!polWiz.nameTouched || !f.name.value) {
        let base = f.mode.value === "at" ? (f.at.value < "06:00" || f.at.value >= "20:00" ? "nightly" : "daily") : `every-${f.every_hours.value}h`;
        if (polScope() === "pick" && polWiz.picked.size === 1) base = `${[...polWiz.picked][0]}-${base}`;
        let name = base;
        for (let i = 2; state.policies.some((p) => p.name === name); i++) name = `${base}-${i}`;
        f.name.value = name;
      }
      const vms = polScope() === "all" ? "Every VM, including new ones" : esc([...polWiz.picked].join(", "));
      const rows = [["VMs", vms], ["Status", f.enabled.checked ? "Enabled" : "Disabled"], ["When", esc(polText())], ["Keep", `${esc(f.keep_last.value)} latest of each VM`],
        ["Target", esc($('input[name="target"]:checked', polForm)?.value || "")],
        ["Method", f.backup_mode.value === "active_full" ? "Active full" : "Incremental with safe full fallback"],
        ["Boot verification", f.verify_after_backup.checked ? `Isolated boot on ${esc(f.verify_pool.value)} · ${esc(f.verify_timeout.value)} seconds` : "Disabled"],
        ["Synthetic full", f.synthetic_weekday.selectedOptions[0].textContent],
        ["Long-term", `${esc(f.keep_weekly.value)} weekly / ${esc(f.keep_monthly.value)} monthly / ${esc(f.keep_yearly.value)} yearly`],
        ["Secondary copy", esc(f.copy_target.value || "Disabled")], ["Archive", esc(f.archive_target.value || "Disabled")]];
      $(".wiz-summary", polForm).innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    }
  },
});
{
  $$('input[name="scope"]', polForm).forEach((r) => r.addEventListener("change", polRender));
  $$('input[name="mode"]', polForm).forEach((r) => r.addEventListener("change", polSchedule));
  ["every_hours", "keep_last"].forEach((n) => polForm.elements[n].addEventListener("input", polSchedule));
  $(".chips.presets", polForm).addEventListener("click", () => setTimeout(polSchedule));
  $(".pol-search", polForm).addEventListener("input", polRender);
  polForm.elements.name.addEventListener("input", () => { polWiz.nameTouched = true; });
  polForm.elements.enabled.addEventListener("change", () => { if (policyWiz.step === 4) policyWiz.go(4); });
  $(".pol-rows", polForm).addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-vm]");
    if (!tr) return;
    if (polWiz.picked.has(tr.dataset.vm)) polWiz.picked.delete(tr.dataset.vm); else polWiz.picked.add(tr.dataset.vm);
    polRender();
  });
}

handlers["policy-dialog"] = async (f, ctx) => {
  [1, 2, 3, 4].forEach((n) => policyWiz.check(n));
  const body = {
    name: f.get("name"),
    target: f.get("target"),
    enabled: f.get("enabled") === "on",
    vms: polScope() === "all" ? [] : [...polWiz.picked],
    keep_last: Number(f.get("keep_last")),
  };
  body.verify_after_backup = f.get("verify_after_backup") === "on";
  body.verify_pool = f.get("verify_pool") || null;
  body.verify_timeout = Number(f.get("verify_timeout"));
  body.retries = Number(f.get("retries"));
  body.retry_delay_minutes = Number(f.get("retry_delay_minutes"));
  body.rpo_hours = f.get("rpo_hours") ? Number(f.get("rpo_hours")) : null;
  body.mode = f.get("backup_mode");
  for (const key of ["full_weekday", "synthetic_weekday"]) body[key] = f.get(key) === "" ? null : Number(f.get(key));
  for (const key of ["keep_weekly", "keep_monthly", "keep_yearly", "copy_keep_last", "archive_weekday", "archive_keep_weekly", "archive_keep_monthly", "archive_keep_yearly"]) body[key] = Number(f.get(key));
  body.copy_target = f.get("copy_target") || null;
  body.archive_target = f.get("archive_target") || null;
  if (f.get("mode") === "at") body.at = f.get("at"); else body.every_hours = Number(f.get("every_hours"));
  if (ctx?.policyName) {
    await api(`/backup/policies/${enc(ctx.policyName)}`, {method:"PUT", body:JSON.stringify(body)});
  } else {
    await post("/backup/policies", body);
  }
  toast(`Schedule ${body.name} ${ctx?.policyName ? "updated" : "created"}: ${polText()}`);
  await refresh("backup");
};

// Restore: pick the VM, then the restore point, then restore it as a new VM or under
// its own name (only when the original is gone). Restore buttons on a restore point
// open it at the last step with that point chosen.
const rsForm = $("#restore-form");
const rs = { points: [], vm: null, point: null, nameTouched: false, recovery: false };
const rsKey = (p) => `${p.target}/${p.id}`;
function rsVmGone(name) { return !state.vms.some((v) => v.name === name); }
async function rsLoad() {
  const lists = await Promise.all(state.targets.map((t) =>
    api(`/backup/targets/${enc(t.name)}/backups`).then((bs) => bs.map((b) => ({ ...b, target: t.name }))).catch(() => [])));
  rs.points = lists.flat().filter(p => p.entity_type !== "container").sort((a, b) => String(b.created).localeCompare(String(a.created)));
}
function rsRenderVms() {
  const q = $(".rs-search", rsForm).value.trim().toLowerCase();
  const byVm = new Map();
  for (const p of rs.points) (byVm.get(p.vm_name) || byVm.set(p.vm_name, []).get(p.vm_name)).push(p);
  const names = [...byVm.keys()].sort().filter((n) => !q || n.toLowerCase().includes(q));
  $(".rs-vms", rsForm).innerHTML = names.length ? names.map((n) => {
    const on = rs.vm === n, pts = byVm.get(n);
    return `<tr data-vm="${esc(n)}" class="${on ? "sel" : ""}"><td><input type="radio" ${on ? "checked" : ""} aria-label="${esc(n)}"></td>
      <td class="vm-name">${esc(n)}</td><td>${pts.length}</td><td>${esc(when(pts[0].created))}</td>
      <td>${rsVmGone(n) ? `<span class="badge failed">Deleted</span>` : `<span class="badge online">Yes</span>`}</td></tr>`;
  }).join("") : empty(5, state.targets.length ? (rs.points.length ? "No VM matches" : "No restore points yet") : "Add a backup repository first");
}
function rsRenderPoints() {
  $(".rs-vm-title", rsForm).textContent = `Restore points of ${rs.vm}, newest first`;
  const pts = rs.points.filter((p) => p.vm_name === rs.vm);
  $(".rs-points", rsForm).innerHTML = pts.map((p) => {
    const on = rs.point && rsKey(p) === rsKey(rs.point);
    return `<tr data-key="${esc(rsKey(p))}" class="${on ? "sel" : ""}"><td><input type="radio" ${on ? "checked" : ""} aria-label="${esc(when(p.created))}"></td>
      <td>${esc(when(p.created))}</td><td>${esc(p.target)}</td><td>${esc(p.node || "shared")}</td><td>${size(p.size_bytes)}</td></tr>`;
  }).join("");
}
function rsAs() {
  const f = rsForm.elements, gone = rsVmGone(rs.vm);
  const orig = $('input[name="as"][value="original"]', rsForm);
  orig.disabled = !gone;
  $(".rs-orig-note", rsForm).textContent = gone ? `${rs.vm} was deleted, so it can come back under its own name.` : `${rs.vm} still exists. Delete it first to restore over it.`;
  if (!gone && f.as.value === "original") f.as.value = "new";
  if (f.as.value === "original") { f.name.value = rs.vm; f.name.readOnly = true; }
  else {
    f.name.readOnly = false;
    if (!rs.nameTouched || !f.name.value || f.name.value === rs.vm) {
      let name = `${rs.vm}-restore`;
      for (let i = 2; state.vms.some((v) => v.name === name); i++) name = `${rs.vm}-restore-${i}`;
      f.name.value = name;
    }
  }
  const rows = [["VM", esc(rs.vm)], ["Restore point", `${esc(when(rs.point.created))} on ${esc(rs.point.target)}`],
    ["Restore as", esc(f.name.value)], ["Node", esc(f.node.value || "Automatic")], ["Storage", esc(f.pool.value || "Same as the original VM")]];
  if (rs.recovery) rows.push(["Recovery method", "Full disk restore before power on. Saved network adapters are connected. Actual duration is recorded in Jobs."]);
  $(".wiz-summary", rsForm).innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
}
const restoreWiz = wizard("restore-dialog", {
  reset() {
    const ctx = dialogContext || {};
    Object.assign(rs, { vm: null, point: null, nameTouched: false, recovery: Boolean(ctx.recovery) });
    $("#restore-dialog header h3").textContent = rs.recovery ? "Recover and power on" : "Restore VM";
    $(".wiz-ok", rsForm).textContent = rs.recovery ? "Recover and power on" : "Restore";
    $(".rs-search", rsForm).value = "";
    $(".rs-vms", rsForm).innerHTML = empty(5, "Looking for restore points...");
    Promise.all([load("vms", "targets"), rsLoad()]).then(() => {
      if (ctx.id) {
        const p = rs.points.find((x) => x.id === ctx.id && (!ctx.target || x.target === ctx.target) && (!ctx.sourceNode || x.node === ctx.sourceNode));
        if (p) { rs.vm = p.vm_name; rs.point = p; restoreWiz.go(3); return; }
      }
      rsRenderVms();
    }).catch(fail);
  },
  check(step) {
    if (step === 1 && !rs.vm) throw new Error("Pick the VM to restore.");
    if (step === 2 && !rs.point) throw new Error("Pick a restore point.");
    if (step === 3) {
      const n = rsForm.elements.name.value;
      if (!/^[a-zA-Z0-9\u0E00-\u0E7F][a-zA-Z0-9\u0E00-\u0E7F_.-]{0,62}$/.test(n)) throw new Error("Name the VM with letters (English or Thai), digits, '-', '_' or '.'.");
      if (state.vms.some((v) => v.name === n)) throw new Error(`A VM named ${n} already exists.`);
    }
  },
  show(step) {
    if (step === 1) rsRenderVms();
    if (step === 2) {
      if (!rs.point || rs.point.vm_name !== rs.vm) rs.point = rs.points.find((p) => p.vm_name === rs.vm) || null;
      rsRenderPoints();
    }
    if (step === 3) rsAs();
  },
});
{
  $(".rs-search", rsForm).addEventListener("input", rsRenderVms);
  $(".rs-vms", rsForm).addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-vm]");
    if (tr) { rs.vm = tr.dataset.vm; rsRenderVms(); }
  });
  $(".rs-vms", rsForm).addEventListener("dblclick", (e) => { if (e.target.closest("tr[data-vm]")) $(".wiz-next", rsForm).click(); });
  $(".rs-points", rsForm).addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-key]");
    if (tr) { rs.point = rs.points.find((p) => rsKey(p) === tr.dataset.key); rsRenderPoints(); }
  });
  $$('input[name="as"]', rsForm).forEach((r) => r.addEventListener("change", rsAs));
  ["node", "pool"].forEach((n) => rsForm.elements[n].addEventListener("change", rsAs));
  rsForm.elements.name.addEventListener("input", () => { rs.nameTouched = true; rsAs(); });
}

handlers["restore-dialog"] = async (f, ctx) => {
  restoreWiz.check(3);
  const p = rs.point;
  const body = { name: f.get("name") || null, node: f.get("node") || null, pool: f.get("pool") || null, source_node: p.node || null };
  await post(`/backup/targets/${enc(p.target)}/backups/${enc(p.id)}/${ctx?.recovery ? "recover" : "restore"}`, body);
  toast(`Restoring ${body.name}. Progress is under Backup → Jobs.`);
  await refresh(currentPage());
};

// ---------- migrate ----------

// Status is based only on an observed inventory read or connection test this visit.
const migrateSourceStatus = new Map();
// Keep the destination line under the Migrate button in step with the wizard.
$("#import-form").addEventListener("change", () => renderMigrateSummary());
$("#import-dialog").addEventListener("close", () => renderMigrateSummary());
function renderMigrateSummary() {
  const name = $("#browse-source").value;
  const source = state.sources.find((s) => s.name === name);
  $("#source-chips").innerHTML = state.sources.map((s) => {
    const status = migrateSourceStatus.get(s.name);
    return `<button type="button" class="mg-chip ${status?.ok === true ? "ok" : status?.ok === false ? "bad" : "unknown"}" data-browse-source="${esc(s.name)}" aria-pressed="${s.name === name}" title="${esc(status?.label || "Not tested this visit")}"><i aria-hidden="true"></i>${esc(s.name)} · ${esc(SOURCE_TYPES[s.type] || s.type)}<small>${esc(status?.label || "Not tested")}</small></button>`;
  }).join("") || '<p class="hint">Add a source in the Sources tab to begin.</p>';
  $("#mg-source-meta").textContent = source ? `${source.name} · ${SOURCE_TYPES[source.type] || source.type}` : "Select a source";
  $("#mg-vm-count").textContent = sourceVMsFor === name ? `${sourceVMs.length} VMs` : "— VMs";
  $("#mg-running").textContent = `${state.imports.filter((j) => ["queued", "running"].includes(j.state)).length} active`;
  const members = state.cluster?.members || [];
  $("#mg-nodes").textContent = members.map((m) => m.name).join(" · ") || "No cluster nodes available";
  $("#mg-node-count").textContent = `${members.length} nodes`;
  const destinations = importChosen().map((vm) => importBatch.targets.get(vm.ref)).filter(Boolean);
  const pools = [...new Set(destinations.flatMap((t) => t.disks.length ? t.disks.map((d) => d.pool) : [t.pool]))];
  $("#mg-write").textContent = `WRITE · ${pools.join(" · ") || "select storage"}`;
  $("#mg-destination").textContent = destinations.length
    ? `to ${[...new Set(destinations.map((t) => t.node))].join(" · ")} · ${pools.join(" · ")}`
    : "Choose destination in the import wizard";
}
$("#source-chips").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-browse-source]");
  if (!chip) return;
  $("#browse-source").value = chip.dataset.browseSource;
  $("#browse-source").dispatchEvent(new Event("change"));
});

// Original platform silhouettes, not vendor logos.
const migrateSourceKinds = {
  vmware: { label: "VMware ESXi / vCenter", art: '<rect x="4" y="5" width="20" height="13" rx="2"/><rect x="12" y="22" width="20" height="9" rx="2"/><path d="M14 18v8H8m16-15h7v11"/>' },
  kvm: { label: "Proxmox / KVM", art: '<path d="m18 4 13 7-13 7L5 11Zm-13 14 13 7 13-7M5 25l13 7 13-7"/>' },
  sangfor: { label: "Sangfor HCI", art: '<path d="m18 3 12 6v10c0 7-12 14-12 14S6 26 6 19V9Z"/><path d="M11 17h14m-14 6h14m-7-12v17"/>' },
  nutanix: { label: "Nutanix AHV / Prism", art: '<path d="m18 3 14 8v14l-14 8-14-8V11Zm0 0v15m14-7-14 7-14-7m14 7v15"/><circle cx="18" cy="18" r="4"/>' },
  ova: { label: "OVA folder", art: '<path d="M3 11V6h12l4 5h14v20H3Zm0 6h30M12 23l6-3 6 3-6 4Zm0 0v5l6 3 6-3v-5m-6 4v4"/>' },
  disk: { label: "Disk image folder", art: '<rect x="5" y="4" width="26" height="28" rx="3"/><circle cx="18" cy="16" r="8"/><circle cx="18" cy="16" r="2"/><path d="m20 18 6 9M9 28h5"/>' },
};
function migrateSourceIcon(type) {
  return `<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true">${(migrateSourceKinds[type] || migrateSourceKinds.ova).art}</svg>`;
}
function userMigrateSources() {
  return state.sources.filter((s) => !["uploads", "physical"].includes(s.name) && !["upload", "p2v"].includes(s.type));
}
// Source categories share the workload view controls and table styles.
let sourceSection = "connections";
const sourceViews = { connections: "cards", physical: "cards" };
try {
  if (localStorage.getItem("ibhics.migrate.sources.section") === "physical") sourceSection = "physical";
  for (const section of Object.keys(sourceViews)) {
    if (localStorage.getItem(`ibhics.migrate.sources.${section}.view`) === "list") sourceViews[section] = "list";
  }
} catch { /* private mode */ }
function syncSourceSections() {
  $$("[data-source-section]").forEach((tab) => tab.setAttribute("aria-selected", String(tab.dataset.sourceSection === sourceSection)));
  for (const section of Object.keys(sourceViews)) $("#source-" + section).hidden = section !== sourceSection;
  $$("[data-source-view]").forEach((button) => button.setAttribute("aria-pressed", String(sourceViews[button.dataset.sourceView] === button.dataset.view)));
}
syncSourceSections();
function renderSourceList(sources, admin, op) {
  return `<table class="mg-vm-table" aria-label="Source connections"><thead><tr>${["Type", "Name", "Host / path", "User", "VMs", "Status", "Last test", "Actions"].map((label) => `<th scope="col">${label}</th>`).join("")}</tr></thead><tbody>${sources.map((s) => {
    const st = s.settings || {}, status = migrateSourceStatus.get(s.name);
    const where = [st.host, st.path].filter(Boolean).join(" / ");
    const label = status?.testing ? "Testing…" : status?.ok === true ? "Connected" : status?.ok === false ? "Failed" : "Not tested";
    return `<tr aria-busy="${!!status?.testing}"><td><span class="mg-source-list-icon kind-${esc(s.type)}" title="${esc(migrateSourceKinds[s.type]?.label || s.type)}">${migrateSourceIcon(s.type)}</span></td><td>${esc(s.name)}</td><td><span class="mg-source-list-path" title="${esc(where)}">${esc(where || "Not provided")}</span></td><td>${esc(st.username || (s.type === "kvm" ? "root (SSH)" : "Not required"))}</td><td>${status?.vmCount ?? "Not loaded"}</td><td><span class="badge ${status?.ok === true ? "done" : status?.ok === false ? "failed" : "plain"}" title="${esc(status?.failure || status?.label || label)}">${label}</span></td><td>${status?.testedAt ? `${esc(new Date(status.testedAt).toLocaleString())} · ${status.testOK ? "Passed" : "Failed"}` : "Not tested"}</td><td><div class="actions"><button type="button" data-source-browse="${esc(s.name)}">Browse</button>${op ? `<button type="button" data-source-test="${esc(s.name)}" ${status?.testing || sourceTestAllRunning ? "disabled" : ""}>Test</button>` : ""}${admin ? `<button type="button" class="danger" data-source-del="${esc(s.name)}">Remove</button>` : ""}</div></td></tr>`;
  }).join("")}</tbody></table>`;
}
function renderPhysicalSources() {
  const container = $("#source-physical-machines"), sessions = p2vSessions.filter((s) => s.machine?.hostname);
  const list = sourceViews.physical === "list";
  container.classList.toggle("mg-source-list", list && !!sessions.length);
  if (!sessions.length) {
    container.innerHTML = `<div class="mg-source-empty mg-panel"><span class="mg-eyebrow">PHYSICAL LINK / AWAITING AGENT</span><div class="mg-agent-hologram" aria-hidden="true">${migrateSourceIcon("disk")}</div><h4>No physical machines connected</h4><p>Create an agent and run it on a physical machine. It appears here once its inventory arrives.</p><button type="button" data-source-tab="p2v">PHYSICAL MACHINES →</button></div>`;
    return;
  }
  const rows = sessions.map((s) => {
    const m = s.machine, disks = m.disks || [], percent = Math.max(0, Math.min(100, Number(s.percent) || 0));
    const badge = `<span class="badge ${s.state === "ready" ? "done" : s.state === "imported" ? "mg-imported" : s.state === "failed" ? "failed" : "running"}" title="${esc(s.error || "")}">${esc(s.state === "uploading" ? `Copying · ${percent}%` : { ready: "Ready", imported: "Imported", failed: "Failed" }[s.state] || P2V_STATE[s.state] || s.state)}</span>`;
    const progress = s.state === "uploading" ? `<progress max="100" value="${percent}" aria-label="Disk copy progress"></progress>` : "";
    const specs = [m.hostname, m.os || "—", (m.firmware || "—").toUpperCase(), `${m.cpus || "—"} CPU · ${gbText(m.memory_mb || 0)}`, `${disks.length} · ${(disks.reduce((sum, d) => sum + (Number(d.size_bytes) || 0), 0) / 2 ** 30).toFixed(1)} GiB`, s.pool || "—"];
    // The existing API exposes creation time, but no agent heartbeat timestamp.
    const checked = p2vCheckedAt ? new Date(p2vCheckedAt).toLocaleString() : "—";
    const actions = canOperate() ? `<button type="button" data-physical-import="${esc(s.id)}" ${s.state === "ready" ? "" : "disabled"}>Import</button><button type="button" class="danger" data-p2v-delete="${esc(s.id)}">${["uploading", "waiting"].includes(s.state) ? "Cancel" : "Remove"}</button>` : "";
    if (list) return `<tr>${specs.map((value) => `<td>${esc(value)}</td>`).join("")}<td>${badge}${progress}</td><td title="Time this session was fetched; agent report time is unavailable">${esc(checked)}</td><td><div class="actions">${actions}</div></td></tr>`;
    return `<article class="mg-source-card mg-panel"><header><div class="mg-source-badge">${migrateSourceIcon("disk")}</div><div class="mg-source-name"><h4>${esc(m.hostname)}</h4><span>${esc(m.os || "OS not reported")}</span></div></header><div>${badge}${progress}</div><dl class="mg-source-data">${["FIRMWARE", "CPU / RAM", "DISKS", "DESTINATION STORAGE"].map((label, i) => `<div><dt>${label}</dt><dd title="${esc(specs[i + 2])}">${esc(specs[i + 2])}</dd></div>`).join("")}<div><dt>LAST CHECKED</dt><dd title="Agent report time is unavailable">${esc(checked)}</dd></div></dl>${s.error ? `<div class="mg-source-failure">${esc(s.error)}</div>` : ""}<footer>${actions}</footer></article>`;
  }).join("");
  container.innerHTML = list ? `<table class="mg-vm-table" aria-label="Physical agent machines"><thead><tr>${["Machine", "OS", "Firmware", "CPU / RAM", "Disks · total", "Destination storage", "Status", "Last checked", "Actions"].map((label) => `<th scope="col">${label}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table>` : rows;
}

let sourceTestAllRunning = false;
function renderSources() {
  renderMigrateSummary();
  const sources = userMigrateSources(), admin = isAdmin(), op = canOperate();
  const connected = sources.filter((s) => migrateSourceStatus.get(s.name)?.ok === true).length;
  const failed = sources.filter((s) => migrateSourceStatus.get(s.name)?.ok === false).length;
  $("#source-summary").innerHTML = `<span><b>${sources.length}</b> SOURCES</span><span class="ok"><b>${connected}</b> CONNECTED</span><span class="bad"><b>${failed}</b> FAILED</span><span><b>${sources.length - connected - failed}</b> UNTESTED</span>`;
  $("#test-all-sources").hidden = !op;
  $("#test-all-sources").disabled = sourceTestAllRunning || !sources.length || sources.some((s) => migrateSourceStatus.get(s.name)?.testing);
  $("#test-all-sources").textContent = sourceTestAllRunning ? "Testing sources…" : "Test all";
  $("#sources").classList.toggle("mg-source-list", sourceViews.connections === "list");
  $("#sources").innerHTML = sources.length && sourceViews.connections === "list" ? renderSourceList(sources, admin, op) : sources.length ? sources.map((s) => {
    const st = s.settings || {}, status = migrateSourceStatus.get(s.name);
    const where = [st.host, st.path].filter(Boolean).join(" / ");
    const test = status?.testedAt ? `${new Date(status.testedAt).toLocaleString()} · ${status.testOK ? "Passed" : "Failed"}` : "Not tested";
    return `<article class="mg-source-card mg-panel ${status?.ok === true ? "ok" : status?.ok === false ? "bad" : "unknown"} ${status?.testing ? "testing" : ""}" aria-busy="${!!status?.testing}">
      <header><div class="mg-source-badge kind-${esc(s.type)}">${migrateSourceIcon(s.type)}</div><div class="mg-source-name"><h4>${esc(s.name)}</h4><span>${esc(migrateSourceKinds[s.type]?.label || s.type)}</span></div></header>
      <div class="mg-source-state" title="${esc(status?.label || "Not tested this visit")}">${status?.testing ? "Testing…" : status?.ok === true ? "Connected" : status?.ok === false ? "Failed" : "Not tested"}</div>
      <dl class="mg-source-data"><div><dt>HOST / PATH</dt><dd class="mg-source-path" title="${esc(where)}">${esc(where || "Not provided")}</dd></div><div><dt>USER</dt><dd title="${esc(st.username || "")}">${esc(st.username || (s.type === "kvm" ? "root (SSH)" : "Not required"))}</dd></div><div><dt>VMs</dt><dd>${status?.vmCount ?? "Not loaded"}</dd></div><div><dt>LAST TEST</dt><dd>${esc(test)}</dd></div></dl>
      ${status?.ok === false ? `<div class="mg-source-failure" title="${esc(status.failure || "Connection failed")}">${esc(status.failure || "Connection failed")}${status.node ? ` · Node: ${esc(status.node)}` : ""}</div>` : ""}
      <footer><button type="button" data-source-browse="${esc(s.name)}">Browse VMs →</button>${op ? `<button type="button" data-source-test="${esc(s.name)}" ${status?.testing || sourceTestAllRunning ? "disabled" : ""}>Test</button>` : ""}${admin ? `<button type="button" class="danger" data-source-del="${esc(s.name)}">Remove</button>` : ""}</footer>
    </article>`;
  }).join("") : `<div class="mg-source-empty mg-panel"><span class="mg-eyebrow">SOURCE LINK / AWAITING CONNECTION</span><h4>Connect your first source</h4><p>Link a hypervisor or export folder to discover workloads for migration.</p>${admin ? '<button type="button" class="primary" data-open="source-dialog">+ ADD SOURCE</button>' : '<p>Ask an administrator to add a source.</p>'}<div class="mg-source-supported">${Object.entries(migrateSourceKinds).map(([type, kind]) => `<span class="kind-${type}" title="${kind.label}">${migrateSourceIcon(type)}<small>${kind.label}</small></span>`).join("")}</div></div>`;
}
$("#tab-sources").addEventListener("click", async (e) => {
  const section = e.target.closest("[data-source-section]");
  const view = e.target.closest("[data-source-view]");
  if (section) {
    sourceSection = section.dataset.sourceSection;
    try { localStorage.setItem("ibhics.migrate.sources.section", sourceSection); } catch { /* private mode */ }
    syncSourceSections();
  }
  if (view) {
    sourceViews[view.dataset.sourceView] = view.dataset.view;
    try { localStorage.setItem(`ibhics.migrate.sources.${view.dataset.sourceView}.view`, view.dataset.view); } catch { /* private mode */ }
    syncSourceSections(); renderSources(); renderPhysicalSources();
  }
  const physicalImport = e.target.closest("[data-physical-import]");
  if (physicalImport && canOperate() && p2vSessions.some((s) => s.id === physicalImport.dataset.physicalImport && s.state === "ready")) {
    physicalImport.disabled = true;
    try {
      selectTab("migrate-tabs", "import");
      $("#browse-source").value = "physical";
      await renderSourceVMs();
      if (sourceVMsFor !== "physical") return;
      const index = sourceVMs.findIndex((vm) => vm.ref === physicalImport.dataset.physicalImport);
      if (index < 0) { toast("This machine is no longer ready. Refresh the physical sessions."); return; }
      migratePicked.clear(); migratePicked.add(index);
      renderMigrateInventory();
    } catch (err) { fail(err); } finally { physicalImport.disabled = false; }
  }
  const browse = e.target.closest("[data-source-browse]");
  if (browse) {
    selectTab("migrate-tabs", "import");
    $("#browse-source").value = browse.dataset.sourceBrowse;
    $("#browse-source").dispatchEvent(new Event("change"));
  }
  const tab = e.target.closest("[data-source-tab]");
  if (tab) selectTab("migrate-tabs", tab.dataset.sourceTab);
});
$("#test-all-sources").addEventListener("click", async () => {
  if (!canOperate() || sourceTestAllRunning) return;
  sourceTestAllRunning = true;
  renderSources();
  try {
    for (const source of userMigrateSources()) {
      await runSourceTest(source.name, document.createElement("div"));
    }
  } finally { sourceTestAllRunning = false; renderSources(); }
});

let sourceVMs = [];
let sourceVMsFor = null;  // the source the list above was read from
const migratePicked = new Set();
let sourceReadGeneration = 0;
let migrateView = "cards";
try { if (localStorage.getItem("ibhics.migrate.view") === "list") migrateView = "list"; } catch { /* private mode */ }
let migrateSort = { key: null, direction: 1 };
function syncMigrateView() {
  $("#source-vms").classList.toggle("mg-list", migrateView === "list");
  $$("[data-migrate-view]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.migrateView === migrateView)));
}
syncMigrateView();
$$("[data-migrate-view]").forEach((button) => button.addEventListener("click", () => {
  migrateView = button.dataset.migrateView;
  try { localStorage.setItem("ibhics.migrate.view", migrateView); } catch { /* private mode */ }
  syncMigrateView();
  if (sourceVMsFor) renderMigrateInventory();
}));
function migrateSelection() {
  $("#migrate-selected").disabled = !canOperate() || !migratePicked.size || !sourceVMsFor;
  $("#migrate-selected").textContent = `MIGRATE ${migratePicked.size} SELECTED`;
  $$("#source-vms .mg-vm, #source-vms .mg-vm-row").forEach((card) => card.classList.toggle("selected", !!$("[data-migrate-pick]", card)?.checked));
  renderMigrateSummary();
  const all = $("#migrate-select-all");
  all.checked = sourceVMs.length > 0 && migratePicked.size === sourceVMs.length;
  all.indeterminate = migratePicked.size > 0 && !all.checked;
}
function connectionChecks(result) {
  return `<ol>${result.checks.map((c) => `<li class="${c.ok ? "check-ok" : "check-failed"}"><strong>${c.ok ? "✓" : "✗"} ${esc(c.step)}</strong><div>${esc(c.detail)}</div>${c.hint ? `<p>${esc(c.hint)}</p>` : ""}</li>`).join("")}</ol>`;
}
async function runSourceTest(name, box) {
  if (migrateSourceStatus.get(name)?.testing) return;
  box.innerHTML = "Testing connection…";
  migrateSourceStatus.set(name, { ...migrateSourceStatus.get(name), testing: true });
  renderSources();
  try {
    const result = await post(`/migrate/sources/${enc(name)}/test`, {});
    const failed = result.checks.find((c) => !c.ok && c.step !== "TCP 902");
    const ok = !failed;
    const label = `Tested ${new Date().toLocaleTimeString()}: ${ok ? "passed" : "failed"}`;
    migrateSourceStatus.set(name, { ...migrateSourceStatus.get(name), testing: false, ok, label, testLabel: label, testedAt: Date.now(), testOK: ok, failure: failed ? `${failed.step} failed` : "", node: result.node });
    renderSources();
    box.innerHTML = connectionChecks(result);
    return result;
  } catch (err) {
    migrateSourceStatus.set(name, { ...migrateSourceStatus.get(name), testing: false, ok: false, label: "Connection test failed", testLabel: "Connection test failed", testedAt: Date.now(), testOK: false, failure: "Connection test request failed", node: null });
    renderSources(); box.textContent = err.message;
  }
}
async function renderSourceVMs() {
  const name = $("#browse-source").value || state.sources[0]?.name;
  const generation = ++sourceReadGeneration;
  const tbody = $("#source-vms"), error = $("#source-error");
  sourceVMs = []; sourceVMsFor = null; migratePicked.clear(); migrateSelection();
  error.hidden = true;
  if (!name) { tbody.innerHTML = '<p class="mg-empty">Add a source first (Sources tab)</p>'; return; }
  tbody.innerHTML = '<p class="mg-empty" role="status">Reading the source…</p>';
  try {
    const found = await api(`/migrate/sources/${enc(name)}/vms`);
    if (generation !== sourceReadGeneration) return;
    sourceVMs = found; sourceVMsFor = name;
    migrateSourceStatus.set(name, { ...migrateSourceStatus.get(name), ok: true, label: "Inventory available", vmCount: found.length, failure: "" }); renderSources();
  } catch (err) {
    if (generation !== sourceReadGeneration) return;
    tbody.innerHTML = "";
    migrateSourceStatus.set(name, { ...migrateSourceStatus.get(name), ok: false, label: "Inventory failed", failure: "VM inventory failed" }); renderSources();
    error.hidden = false;
    error.innerHTML = `<strong>Could not list VMs from ${esc(name)}</strong><p>Run the connection test to identify the failed step and how to fix it.</p><details><summary>Inventory error</summary><p>${esc(err.message)}</p></details><button type="button" data-source-test="${esc(name)}">Run connection test</button><div class="migrate-checks"></div>`;
    // Diagnose separately: inventory failures may be DNS, TCP, TLS, login or listing.
    if (canOperate()) {
      const result = await runSourceTest(name, $(".migrate-checks", error));
      if (generation !== sourceReadGeneration) return;
      const failed = result?.checks.find((c) => !c.ok && c.step !== "TCP 902");
      if (failed) $("strong", error).textContent = `${name}: ${failed.step} failed`;
    }
    return;
  }
  renderMigrateInventory();
}
function renderMigrateInventory() {
  const tbody = $("#source-vms"), op = canOperate();
  if (migrateView === "list" && sourceVMs.length) {
    const columns = [["name", "VM / OS"], [null, "Power"], ["vcpus", "vCPU"], ["memory_mb", "Memory"], ["disks", "Disks"], [null, "Firmware"]];
    const diskSize = (vm) => vm.disks_gb.reduce((sum, size) => sum + (Number(size) || 0), 0);
    const rows = sourceVMs.map((vm, i) => ({ vm, i }));
    if (migrateSort.key) rows.sort((a, b) => {
      const value = (vm) => migrateSort.key === "disks" ? diskSize(vm) : migrateSort.key === "name" ? String(vm.name) : Number(vm[migrateSort.key]) || 0;
      const av = value(a.vm), bv = value(b.vm);
      return (typeof av === "string" ? av.localeCompare(bv, undefined, { numeric: true, sensitivity: "base" }) : av - bv) * migrateSort.direction || a.i - b.i;
    });
    tbody.innerHTML = `<table class="mg-vm-table" aria-label="Source VMs"><thead><tr><th scope="col" aria-label="Select"></th>${columns.map(([key, label]) => `<th scope="col"${key ? ` aria-sort="${migrateSort.key === key ? migrateSort.direction === 1 ? "ascending" : "descending" : "none"}"` : ""}>${key ? `<button type="button" data-migrate-sort="${key}">${label}${migrateSort.key === key ? migrateSort.direction === 1 ? " ↑" : " ↓" : ""}</button>` : label}</th>`).join("")}<th scope="col">Actions</th></tr></thead><tbody>${rows.map(({ vm, i }) => `<tr class="mg-vm-row">
      <td><input type="checkbox" data-migrate-pick="${i}" aria-label="Select ${esc(vm.name)}" ${migratePicked.has(i) ? "checked" : ""} ${op ? "" : "disabled"}></td>
      <td><div class="mg-vm-identity">${vxOs({ ...vm, os_type: vm.guest_os })}<span class="mg-vm-name"><b>${esc(vm.name)}</b><small>${esc(vm.guest_os || "OS not reported")}</small></span></div></td>
      <td><span class="badge ${vm.state === "running" ? "running" : ""}">${esc(vm.state || "Unknown")}</span></td>
      <td>${Number(vm.vcpus)}</td><td>${gbText(vm.memory_mb)}</td><td>${vm.disks_gb.length} · ${diskSize(vm)} GB${vm.disks_gb.some((g) => !g) ? " + ?" : ""}</td>
      <td title="${esc(vm.notes || "")}">${esc((vm.firmware || "Unknown").toUpperCase())}${vm.state === "running" ? '<small>Power off at source first</small>' : ""}</td>
      <td><div class="actions">${op ? `<button class="small" data-import="${i}">Import</button>` : ""}${op && sourceVMsFor === "uploads" ? `<button class="small danger" data-upload-del="${esc(vm.ref.split("/").pop())}">Delete file</button>` : ""}</div></td>
    </tr>`).join("")}</tbody></table>`;
    migrateSelection();
    return;
  }
  tbody.innerHTML = sourceVMs.length ? sourceVMs.map((vm, i) => {
    const kind = osKind(vm.guest_os || "");
    return `<article class="mg-vm">
      <label class="mg-vm-select"><span class="vx-os os-${kind.toLowerCase() || "other"}">${VX_OS_ART[kind] || VX_OS_ART[""]}</span>
      <span class="mg-vm-name"><b>${esc(vm.name)}</b><small>${esc(vm.guest_os || "OS not reported")}</small></span>
      <input type="checkbox" data-migrate-pick="${i}" aria-label="Select ${esc(vm.name)}" ${migratePicked.has(i) ? "checked" : ""} ${op ? "" : "disabled"}></label>
      <span class="badge ${vm.state === "running" ? "running" : ""}">${esc(vm.state || "Unknown")}</span>
      <div class="mg-spec">${Number(vm.vcpus)} vCPU · ${gbText(vm.memory_mb)} · ${vm.disks_gb.length} disks · ${vm.disks_gb.map((g) => g ? `${g} GB` : "?").join(" + ")}</div>
      <small class="mg-ref" title="${esc(vm.ref)}">${esc((vm.firmware || "").toUpperCase())} · ${esc(String(vm.ref).split("/").pop())}${vm.notes ? ` · ${esc(vm.notes)}` : ""}</small>
      ${vm.state === "running" ? '<small>Power off at source first</small>' : ""}
      <div class="actions">${op ? `<button class="small" data-import="${i}">Import</button>` : ""}${op && sourceVMsFor === "uploads" ? `<button class="small danger" data-upload-del="${esc(vm.ref.split("/").pop())}">Delete file</button>` : ""}</div>
    </article>`;
  }).join("") : '<p class="mg-empty">No VMs found on this source</p>';
  migrateSelection();
}
$("#source-vms").addEventListener("click", (e) => {
  const sort = e.target.closest("[data-migrate-sort]");
  if (sort) {
    migrateSort = { key: sort.dataset.migrateSort, direction: migrateSort.key === sort.dataset.migrateSort ? -migrateSort.direction : 1 };
    renderMigrateInventory();
    return;
  }
  const row = e.target.closest(".mg-vm-row");
  if (!row || e.target.closest("button, input, a, label")) return;
  const input = $("[data-migrate-pick]", row);
  if (!input.disabled) input.click();
});
$("#browse-source").addEventListener("change", () => renderSourceVMs());
$("#source-vms").addEventListener("change", (e) => {
  const input = e.target.closest("[data-migrate-pick]");
  if (!input) return;
  const index = Number(input.dataset.migratePick);
  if (input.checked) migratePicked.add(index); else migratePicked.delete(index);
  migrateSelection();
});
$("#migrate-select-all").addEventListener("change", (e) => {
  migratePicked.clear();
  if (canOperate() && sourceVMsFor && e.target.checked) sourceVMs.forEach((v, i) => migratePicked.add(i));
  $$("#source-vms [data-migrate-pick]").forEach((c) => { c.checked = migratePicked.has(Number(c.dataset.migratePick)); });
  migrateSelection();
});

// Uploads stream straight to the node's disk; big exports take a while, so each file
// shows its own progress bar.
function uploadFile(file, row, url = `/api/v1/migrate/uploads/${enc(file.name)}`) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
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

function importElapsed(j) {
  const seconds = Math.max(0, Math.floor(((j.finished ? Date.parse(j.finished) : Date.now()) - Date.parse(j.created)) / 1000));
  return Number.isFinite(seconds) ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : "–";
}
function renderImports() {
  renderMigrateSummary();
  $("#imports").innerHTML = state.imports.length ? state.imports.map((j) => `<tr>
    <td class="vm-sub" style="display:table-cell" title="${esc(j.vm)}">${esc(j.vm.split("/").pop())}</td><td>${esc(j.source)}</td>
    <td class="vm-name">${esc(j.name)}</td><td>${esc(j.node)}${j.disks ? `<span class="vm-sub">${j.disks.map((d, i) => `Disk ${i + 1}: ${esc(d.pool || j.pool)} / ${d.size_gb || "Original"} GiB`).join("<br>")}</span>` : ""}${j.via ? `<span class="vm-sub">via ${esc(j.via)}</span>` : ""}</td><td>${esc(when(j.created))}<span class="vm-sub">Elapsed ${importElapsed(j)}</span></td>
    <td><span class="badge ${esc(j.state)}">${JOB_STATE[j.state] || esc(j.state)}</span></td>
    <td class="vm-sub" style="display:table-cell">${esc(j.error || j.step || "")}
      ${j.percent != null ? `<progress max="100" value="${Number(j.percent)}" aria-label="Migration progress"></progress> ${Number(j.percent)}%` : ""}
      <button class="small" data-task-log="${esc(j.id)}" data-task-title="${esc(j.name)}">Log</button>${canOperate() && ["queued", "running"].includes(j.state) ? `<button class="small danger" data-import-cancel="${esc(j.id)}">Cancel</button>` : ""}</td></tr>`).join("") : empty(7, "No imports yet");
}

async function openImportWizard(indices, replication = false) {
  if (!canOperate() || !sourceVMsFor || !indices.length) return;
  const source = sourceVMsFor, vms = [...sourceVMs];
  await load("vms", "pools", "networks");
  if (replication) {
    const cap = await api(`/migrate/sources/${enc(source)}/replication-capability`);
    if (!cap.supported) throw new Error(cap.reason);
  }
  openDialog("import-dialog", { source, vms, indices, replication });
}
$("#migrate-selected").addEventListener("click", () => openImportWizard([...migratePicked]).catch(fail));
$("#page-migrate").addEventListener("click", async (e) => {
  const cancel = e.target.closest("button[data-import-cancel]");
  if (cancel && confirm("Cancel this import?")) {
    cancel.disabled = true;
    try { await post(`/migrate/jobs/${enc(cancel.dataset.importCancel)}/cancel`); await refresh("migrate"); } catch (err) { cancel.disabled = false; fail(err); }
  }
  const imp = e.target.closest("button[data-import]");
  const rm = e.target.closest("button[data-source-del]");
  const test = e.target.closest("button[data-source-test]");
  const upDel = e.target.closest("button[data-upload-del]");
  if (test) {
    test.disabled = true;
    try {
      const panel = test.closest("#source-error");
      const box = panel ? $(".migrate-checks", panel) : $("#saved-source-test-results");
      if (!panel) openDialog("source-test-dialog");
      const result = await runSourceTest(test.dataset.sourceTest, box);
      if (panel && result) {
        const failed = result.checks.find((c) => !c.ok && c.step !== "TCP 902");
        $("strong", panel).textContent = failed ? `${test.dataset.sourceTest}: ${failed.step} failed` : "Connection checks passed. Refresh the source inventory.";
      }
    } finally { test.disabled = false; }
  }
  if (upDel) {
    if (!confirm(`Delete the uploaded file ${upDel.dataset.uploadDel}?`)) return;
    try { await del(`/migrate/uploads/${enc(upDel.dataset.uploadDel)}`); renderSourceVMs(); } catch (err) { fail(err); }
  }
  if (imp) { try { await openImportWizard([Number(imp.dataset.import)]); } catch (err) { fail(err); } }
  if (rm) {
    if (!confirm(`Remove source ${rm.dataset.sourceDel}?`)) return;
    try { await del(`/migrate/sources/${enc(rm.dataset.sourceDel)}`); await refresh("migrate"); } catch (err) { fail(err); }
  }
});

function sourceFormBody(f) {
  const type = f.get("type");
  const body = { name: f.get("name"), type };
  if (type === "vmware") {
    Object.assign(body, { host: f.get("host"), username: f.get("username"), password: f.get("password"), verify_tls: f.get("verify_tls") === "on", transport: f.get("transport") });
    if (f.get("vpath")) body.path = f.get("vpath");
  } else if (type === "kvm") Object.assign(body, { host: f.get("host"), username: "root", password: f.get("password") });
  else if (type === "nutanix" || type === "sangfor") Object.assign(body, { host: f.get("host"), username: f.get("username"), password: f.get("password"), verify_tls: f.get("verify_tls") === "on" });
  else body.path = f.get("path");
  return body;
}
let sourceTestVersion = 0;
function clearSourceTest() { sourceTestVersion++; $("#source-test-results").replaceChildren(); }
$("#source-dialog").addEventListener("close", clearSourceTest);
$("#source-form").addEventListener("input", clearSourceTest);
$("#source-form").addEventListener("change", clearSourceTest);
$("#test-source-form").addEventListener("click", async (e) => {
  const form = $("#source-form"), box = $("#source-test-results");
  if (!form.reportValidity()) return;
  const version = ++sourceTestVersion;
  e.target.disabled = true; box.textContent = "Testing connection…";
  try {
    const result = await post("/migrate/sources/test", sourceFormBody(new FormData(form)));
    if (version === sourceTestVersion) box.innerHTML = connectionChecks(result);
  } catch (err) { if (version === sourceTestVersion) box.textContent = err.message; }
  finally { e.target.disabled = false; }
});

handlers["source-dialog"] = async (f) => {
  const body = sourceFormBody(f);
  await post("/migrate/sources", body);
  toast(`Source ${body.name} added`);
  await refresh("migrate");
  $("#browse-source").value = body.name;
  selectTab("migrate-tabs", "import");
  await renderSourceVMs();
};

// Map the union of source networks, then rebuild each VM's NIC array in NIC order.
// Replication controls live outside the import-wizard diagnostic test slice.
// What a running sync is doing, in plain words: amount, speed and time left.
function replicationLive(j) {
  if (!j.operation) return j.state === "failed" ? "Failed" : "";
  if (j.operation === "test-failover") return "Preparing an isolated test VM from the last committed replica…";
  if (j.operation === "test-cleanup") return "Removing the isolated test VM…";
  const verb = j.state === "seeding" ? "Seeding" : j.operation === "sync" ? "Syncing" : j.operation[0].toUpperCase() + j.operation.slice(1);
  if (!j.total_bytes) return `${verb} · preparing the source snapshot…`;
  const gb = (b) => (b / 2 ** 30).toFixed(1);
  const secs = j.run_started ? (Date.now() - Date.parse(j.run_started)) / 1000 : 0;
  const rate = secs > 5 ? j.done_bytes / secs : 0;
  const left = rate ? (j.total_bytes - j.done_bytes) / rate : 0;
  const eta = !left ? "" : left > 3600 ? ` · ~${(left / 3600).toFixed(1)} h left` : ` · ~${Math.ceil(left / 60)} min left`;
  return `${verb} ${gb(j.done_bytes)} / ${gb(j.total_bytes)} GB (${Number(j.percent).toFixed(1)}%)${rate ? ` · ${(rate / 2 ** 20).toFixed(0)} MB/s` : ""}${eta}`;
}
let replicationView = "cards";
function renderReplications() {
  const box = $("#replications");
  box.classList.toggle("is-list", replicationView === "list");
  box.innerHTML = state.replications.length ? state.replications.map((j) => {
    const busy = !!j.operation || ["seeding", "syncing", "cutting-over"].includes(j.state);
    const button = (action, label, disabled = false) => `<button class="small operator-only" data-replication-id="${esc(j.id)}" data-replication-action="${action}" ${disabled ? "disabled" : ""}>${label}</button>`;
    const cycles = (j.history || []).filter(h => h.change_tracking).slice().reverse();
    const trackingHistory = cycles.length ? `<details><summary>Sync tracking history</summary>${cycles.map(h => `<p>${esc(when(h.at))} · ${esc(h.change_tracking)}${h.fallback_reason ? ` · ${esc(h.fallback_reason)}` : ""}</p>`).join("")}</details>` : "";
    return `<article class="mg-panel mg-replica"><h3>${esc(j.name)} <span class="badge ${busy ? "running" : esc(j.state)}">${esc(j.operation || j.state)}</span></h3><p>${esc(j.source)} → ${esc(j.node)} · ${esc(j.change_tracking || j.capability.change_tracking)}</p>${j.fallback_reason ? `<p class="hint">${esc(j.fallback_reason)}</p>` : ""}${trackingHistory}<dl><dt>RPO</dt><dd>${j.rpo_seconds == null ? "Awaiting seed" : `${Math.floor(j.rpo_seconds / 60)}m ${j.rpo_seconds % 60}s`}</dd><dt>Last sync</dt><dd>${j.last_sync ? esc(when(j.last_sync)) : "Never"}</dd><dt>${j.operation ? "Read so far" : "Transferred / changed"}</dt><dd>${j.operation ? (j.total_bytes ? `${(j.done_bytes / 2 ** 30).toFixed(1)} / ${(j.total_bytes / 2 ** 30).toFixed(1)} GB` : "Starting…") : `${(Number(j.bytes_transferred) / 2 ** 30).toFixed(2)} GB / ${Number(j.changed_pct)}%`}</dd><dt>Schedule</dt><dd>${j.schedule.at ? `Daily ${esc(j.schedule.at)}` : `Every ${Number(j.schedule.interval_minutes)} minutes`}</dd></dl><progress max="100" value="${Number(j.percent)}" aria-label="Replication progress"></progress><p role="status" class="${!busy && j.state === "failed" ? "bp-alert" : ""}">${esc(j.operation ? replicationLive(j) : j.error || replicationLive(j))}</p><div class="toolbar">${button("sync", "Sync now", busy || !["new", "idle", "failed"].includes(j.state))}${button("test-failover", "Test failover", busy || !!j.test_vm || !j.last_sync || !["idle", "paused", "failed"].includes(j.state))}${button("test-cleanup", "Clean up test", busy || !j.test_vm)}${button("cutover", "Cutover", busy || !!j.test_vm || !!j.result || !["idle", "failed"].includes(j.state))}${button("pause", j.state === "paused" ? "Resume" : j.stop_requested ? "Stopping…" : "Pause", j.state === "done" || !!j.stop_requested || (busy && j.operation !== "sync"))}${button("delete", "Delete", busy || !!j.test_vm)}</div></article>`;
  }).join("") : '<p class="hint">No replications yet.</p>';
}
// New replication picks its source and VMs inside the wizard; a source already
// loaded on Import VMs (with its picked VMs) is offered first.
$("#new-replication").onclick = async () => {
  if (!canOperate()) return;
  try {
    await load("vms", "pools", "networks", "sources");
    const ready = sourceVMsFor && sourceVMs.length;
    const indices = ready ? (migratePicked.size ? [...migratePicked] : []) : [];
    openDialog("import-dialog", { source: ready ? sourceVMsFor : "", vms: ready ? [...sourceVMs] : [], indices, replication: true });
    await repSourceOptions(ready ? sourceVMsFor : "");
  } catch (err) { fail(err); }
};
async function repSourceOptions(selected) {
  const sel = $("#import-source"), live = (state.sources || []).filter((src) => !["upload", "p2v", "ova", "disk"].includes(src.type));
  $("#import-source-pick").hidden = false;
  sel.innerHTML = `<option value="">Choose a source…</option>` + live.map((src) => `<option value="${esc(src.name)}" ${src.name === selected ? "selected" : ""}>${esc(src.name)} · ${esc(src.type)}</option>`).join("");
  $("#import-source-status").textContent = live.length ? "" : "Add a VMware, KVM/Proxmox or Nutanix source on the Sources tab first.";
  if (selected) await repCheckSource(selected, false);
}
async function repCheckSource(name, reload = true) {
  const status = $("#import-source-status");
  if (!name) { Object.assign(importBatch, { source: "", vms: [], picked: new Set() }); importWiz.go(1); return; }
  status.textContent = "Checking the source…";
  try {
    const cap = await api(`/migrate/sources/${enc(name)}/replication-capability`);
    if (!cap.supported) throw new Error(cap.reason || "This source cannot replicate yet.");
    if (reload) {
      status.textContent = "Loading VMs…";
      const vms = await api(`/migrate/sources/${enc(name)}/vms`);
      Object.assign(importBatch, { source: name, vms, picked: new Set() });
    }
    status.textContent = `${importBatch.vms.length} VMs · change tracking: ${cap.change_tracking}`;
  } catch (err) {
    Object.assign(importBatch, { source: name, vms: [], picked: new Set() });
    status.textContent = err.message;
  }
  importWiz.go(1);
}
$("#import-source").onchange = (e) => repCheckSource(e.target.value);
$$("[data-replication-view]").forEach((b) => { b.onclick = () => {
  replicationView = b.dataset.replicationView;
  $$("[data-replication-view]").forEach((v) => v.setAttribute("aria-pressed", String(v === b)));
  renderReplications();
}; });
$("#replications").onclick = async (e) => {
  const b = e.target.closest("[data-replication-action]");
  if (!b || !canOperate()) return;
  const j = state.replications.find((job) => job.id === b.dataset.replicationId);
  if (!j) return;
  const action = b.dataset.replicationAction, url = `/migrate/replications/${enc(j.id)}`;
  b.disabled = true;
  try {
    if (action === "cutover") openDialog("replication-cutover-dialog", j);
    else if (action === "test-failover") {
      await load("networks");
      $("#replication-test-networks").innerHTML = Array.from({length: j.source_vm.nics}, (_, i) => `<label>NIC ${i + 1}<select name="network"><option value="">Disconnected (isolated)</option>${state.networks.filter((n) => !n.node || n.node === j.node).map((n) => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join("")}</select></label>`).join("");
      openDialog("replication-test-dialog", j);
    } else if (action === "pause") await patch(url, {paused: j.state !== "paused"});
    else if (action === "delete") { if (confirm(`Delete replica data for ${j.name}? The cutover VM is kept.`)) await del(`${url}?confirm=true`); }
    else await post(`${url}/${action}`);
    await refresh("migrate");
  } catch (err) { fail(err); } finally { b.disabled = false; }
};
handlers["replication-cutover-dialog"] = async (f, j) => {
  if (f.get("source_powered_off") !== "on") throw new Error("Confirm that the source VM is powered off.");
  await post(`/migrate/replications/${enc(j.id)}/cutover`, {source_powered_off: true});
  await refresh("migrate");
};
handlers["replication-test-dialog"] = async (f, j) => {
  const networks = f.getAll("network");
  if (networks.some(Boolean) && networks.some((n) => !n)) throw new Error("Choose all test networks or leave every NIC disconnected.");
  await post(`/migrate/replications/${enc(j.id)}/test-failover`, {networks: networks.some(Boolean) ? networks : []});
  await refresh("migrate");
};

const importForm = $("#import-form");
const importBatch = { vms: [], picked: new Set(), names: new Map(), resources: new Map(), targets: new Map(), networks: new Map(), nicSettings: new Map(), submitted: new Set() };
function importChosen() { return importBatch.vms.filter((vm) => importBatch.picked.has(vm.ref)); }
function importNetworkKeys(vm) {
  return Array.from({ length: Math.max(vm.nics ?? 1, vm.networks?.length || 0, vm.macs?.length || 0) }, (_, i) => vm.networks?.[i] || `Adapter ${i + 1} (unspecified)`);
}
function importTarget(vm) {
  if (!importBatch.targets.has(vm.ref)) importBatch.targets.set(vm.ref, {
    node: importForm.elements.node.value, originalNode: importForm.elements.node.value,
    pool: importForm.elements.pool.value, originalPool: importForm.elements.pool.value, start: false, clear_hibernation: false,
    disks: (vm.disks_gb || []).map((size) => ({ pool: importForm.elements.pool.value, size_gb: size || null })),
  });
  return importBatch.targets.get(vm.ref);
}
function importNicKey(vm, i) { return JSON.stringify([vm.ref, i]); }
function importNicNetwork(vm, i, source) {
  const nic = importNicKey(vm, i);
  return importBatch.networks.get(nic) ?? importBatch.networks.get(source) ?? importDefaultNetwork(vm, source);
}
function importAvailableNetworks(vm) {
  const node = importTarget(vm).node;
  return state.networks.filter((n) => (!n.node || n.node === node) && (!n.nodes?.length || n.nodes.includes(node)));
}
function importNetworkVlan(n, node) { return n.configurations?.[node] ? n.configurations[node].vlan : n.vlan; }
function importNetworkLabel(n, node) {
  const vlan = importNetworkVlan(n, node);
  return `${n.name} · ${vlan ? `VLAN ${vlan}` : "untagged"}`;
}
function importDefaultMac(vm, i) { return vm.macs?.[i] && vm.state === "stopped" ? "keep" : "new"; }
function importNicSettings(vm, i) {
  const key = importNicKey(vm, i);
  if (!importBatch.nicSettings.has(key)) importBatch.nicSettings.set(key, {mac_mode: importDefaultMac(vm, i), mac: "", vlan: ""});
  return importBatch.nicSettings.get(key);
}
function importNicMac(vm, i) {
  const n = importNicSettings(vm, i);
  return n.mac_mode === "keep" ? vm.macs?.[i] || "Detected during conversion" : n.mac_mode === "custom" ? n.mac : "Generated on destination";
}
function importNetworkOptions(vm, selected) {
  const nets = importAvailableNetworks(vm);
  return `<option value="">Select…</option>` + (selected && selected !== "__new_vlan__" && !nets.some((n) => n.name === selected) ? `<option selected disabled value="${esc(selected)}">${esc(selected)} (unavailable)</option>` : "") + nets.map((n) => `<option value="${esc(n.name)}" ${selected === n.name ? "selected" : ""}>${esc(importNetworkLabel(n, importTarget(vm).node))}</option>`).join("") + `<option value="__new_vlan__" ${selected === "__new_vlan__" ? "selected" : ""}>New VLAN…</option>`;
}
function importNicLabel(vm, i, src) {
  const name = importNicNetwork(vm, i, src), nic = importNicSettings(vm, i);
  return name === "__new_vlan__" ? `New VLAN ${nic.vlan}` : importNetworkLabel(importAvailableNetworks(vm).find((n) => n.name === name) || {name}, importTarget(vm).node);
}
function importValidateNics() {
  const seen = new Set((state.vms || []).flatMap((v) => (v.nics || v.spec?.nics || []).map((n) => n.mac?.toLowerCase()).filter(Boolean)));
  for (const vm of importChosen()) {
    if (importBatch.submitted.has(vm.ref)) continue;
    importNetworkKeys(vm).forEach((src, i) => {
      const n = importNicSettings(vm, i), net = importNicNetwork(vm, i, src);
      if (net === "__new_vlan__") {
        if (!Number.isInteger(Number(n.vlan)) || Number(n.vlan) < 1 || Number(n.vlan) > 4094) throw new Error("VLAN ID must be 1–4094.");
      } else if (!importAvailableNetworks(vm).some((n) => n.name === net)) throw new Error("Choose an available target network for every NIC.");
      if (n.mac_mode === "keep" && (!vm.macs?.[i] || vm.state !== "stopped")) throw new Error("Keep source MAC requires a known MAC and a stopped VM.");
      if (n.mac_mode !== "new") {
        const mac = importNicMac(vm, i).toLowerCase();
        if (!/^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/.test(mac) || (parseInt(mac.slice(0, 2), 16) & 1) || mac === "00:00:00:00:00:00") throw new Error("MAC must be a nonzero unicast address in xx:xx:xx:xx:xx:xx format.");
        if (seen.has(mac)) throw new Error("Duplicate MAC address in batch or cluster.");
        seen.add(mac);
      }
    });
  }
}
async function importResolveNetworks(vm) {
  for (const [i, src] of importNetworkKeys(vm).entries()) {
    if (importNicNetwork(vm, i, src) !== "__new_vlan__") continue;
    const vlan = Number(importNicSettings(vm, i).vlan), node = importTarget(vm).node;
    // Re-read before creating: another batch item or operator may already have made it.
    state.networks = await api("/networks");
    let existing = importAvailableNetworks(vm).find((n) => importNetworkVlan(n, node) === vlan);
    if (!existing) {
      const base = `vlan-${vlan}`;
      let name = base, suffix = 2;
      while (state.networks.some((n) => n.name === name)) name = `${base}-${suffix++}`;
      try { existing = await post("/networks", {name, vlan}); }
      catch (err) {
        state.networks = await api("/networks");
        existing = importAvailableNetworks(vm).find((n) => importNetworkVlan(n, node) === vlan);
        if (!existing) throw err;
      }
      if (!state.networks.some((n) => n.name === existing.name)) state.networks.push(existing);
    }
    importBatch.networks.set(importNicKey(vm, i), existing.name);
  }
}
function importDefaultNetwork(vm, source) {
  const nets = importAvailableNetworks(vm);
  return nets.find((n) => n.name === source)?.name || nets.find((n) => n.name === "default")?.name || nets[0]?.name || "";
}
function importOptions(items, selected) {
  return `<option value="" ${!selected ? "selected" : ""}>Select…</option>${selected && !items.includes(selected) ? `<option value="${esc(selected)}" selected disabled>${esc(selected)} (unavailable)</option>` : ""}` + items.map((name) => `<option value="${esc(name)}" ${selected === name ? "selected" : ""}>${esc(name)}</option>`).join("");
}
function renderImportTargets() {
  $("#import-bulk").hidden = importChosen().length < 2;
  $("#import-disk-targets").innerHTML = importChosen().map((vm, v) => {
    const t = importTarget(vm), locked = importBatch.submitted.has(vm.ref) ? "disabled" : "";
    const r = importBatch.resources.get(vm.ref) || { vcpus: vm.vcpus, memory_mb: vm.memory_mb };
    r.cpu_sockets ??= vm.sockets && vm.cores_per_socket && vm.sockets * vm.cores_per_socket === vm.vcpus ? vm.sockets : 1;
    r.cpu_cores ??= r.vcpus / r.cpu_sockets;
    r.vcpus = r.cpu_sockets * r.cpu_cores;
    importBatch.resources.set(vm.ref, r);
    const pools = state.pools.filter((p) => p.node === t.node && p.active);
    const field = (label, key, value, original, control, disk = null) => {
      const changed = String(value ?? "") !== String(original ?? "");
      const attrs = `data-target-vm="${v}" data-target-field="${key}" ${disk == null ? "" : `data-target-disk="${disk}"`} ${locked}`;
      return `<div class="mg-target-field ${changed ? "is-changed" : ""}"><label>${label}${control(attrs)}</label><button type="button" class="mg-reset" data-reset-vm="${v}" data-reset-field="${key}" ${disk == null ? "" : `data-reset-disk="${disk}"`} ${!changed || locked ? "disabled" : ""} aria-label="Reset ${esc(label)}">Reset</button></div>`;
    };
    const input = (label, key, value, original, type = "text", constraints = "") => field(label, key, value, original, (attrs) => `<input type="${type}" value="${esc(value)}" ${constraints} ${attrs}>`);
    const initialNode = t.originalNode ?? importForm.elements.node.value;
    const initialPool = t.originalPool ?? importForm.elements.pool.value;
    const nicKeys = importNetworkKeys(vm);
    const shrinking = t.disks.some((d, i) => d.size_gb && vm.disks_gb[i] && d.size_gb < vm.disks_gb[i]);
    return `<article class="mg-vm-target mg-transfer"><section class="mg-source-pane"><span class="mg-eyebrow">TARGET / CURRENT VM</span><h4>${esc(vm.name)}</h4><dl class="mg-source-specs"><div><dt>OS / firmware</dt><dd>${esc(vm.guest_os || "OS not reported")} · ${esc(vm.firmware || "Not reported")}</dd></div><div><dt>Compute</dt><dd>${vm.vcpus} vCPU · ${vm.memory_mb} MB memory</dd></div><div><dt>Power</dt><dd>${esc(vm.state || "Not reported")}</dd></div></dl><div class="mg-device-list">${nicKeys.map((net, i) => `<p>NIC ${i + 1} · ${esc(net)}${vm.macs?.[i] ? `<small>${esc(vm.macs[i])}</small>` : ""}</p>`).join("")}${(vm.disks_gb || []).map((size, i) => `<p>Disk ${i + 1} · ${size ? `${size} GiB` : "size detected after conversion"}</p>`).join("") || "<p>Disk size detected after conversion</p>"}</div></section>
      <div class="mg-transfer-arrow" aria-hidden="true"><div class="mg-beam"><i></i><b>›</b><b>›</b><b>›</b><em>MIGRATE</em></div></div>
      <section class="mg-target-pane"><span class="mg-eyebrow">DESTINATION / ibHiCS</span><div class="mg-target-grid">
      ${input("VM name", "name", importBatch.names.get(vm.ref) ?? vm.name, vm.name)}
      ${field("Node", "node", t.node, initialNode, (attrs) => `<select ${attrs}>${importOptions(computeMembers().filter((m) => m.healthy).map((m) => m.name), t.node)}</select>`)}
      <label>Total vCPU <input type="number" value="${r.vcpus}" readonly></label>
      ${input("Sockets", "cpu_sockets", r.cpu_sockets, vm.sockets || 1, "number", 'min="1" max="256" step="1"')}
      ${input("Cores per socket", "cpu_cores", r.cpu_cores, vm.cores_per_socket || vm.vcpus, "number", 'min="1" max="256" step="1"')}
      ${input("Memory (MB)", "memory_mb", r.memory_mb, vm.memory_mb, "number", 'min="128" step="1"')}
      </div><div class="mg-target-devices">${nicKeys.map((src, i) => {
        const n = importNicSettings(vm, i), net = importNicNetwork(vm, i, src);
        return `<div class="mg-target-nic"><h5>NIC ${i + 1} · ${esc(src)}</h5>${field("VLAN / Network", "network", net, importDefaultNetwork(vm, src), (attrs) => `<select ${attrs}>${importNetworkOptions(vm, net)}</select>`, i)}
        ${net === "__new_vlan__" ? field("VLAN ID", "vlan", n.vlan, "", (attrs) => `<input type="number" min="1" max="4094" step="1" value="${esc(n.vlan)}" ${attrs}>`, i) : ""}
        ${field("MAC", "mac_mode", n.mac_mode, importDefaultMac(vm, i), (attrs) => `<select ${attrs}><option value="keep" ${n.mac_mode === "keep" ? "selected" : ""} ${!vm.macs?.[i] || vm.state !== "stopped" ? "disabled" : ""}>Keep source MAC</option><option value="new" ${n.mac_mode === "new" ? "selected" : ""}>Generate new</option><option value="custom" ${n.mac_mode === "custom" ? "selected" : ""}>Custom</option></select>`, i)}
        ${n.mac_mode === "custom" ? field("MAC address", "mac", n.mac, "", (attrs) => `<input value="${esc(n.mac)}" placeholder="52:54:00:12:34:56" ${attrs}>`, i) : `<small>${esc(importNicMac(vm, i))}</small>`}
        <button type="button" data-random-mac-vm="${v}" data-random-mac-nic="${i}" ${locked} title="Generate and use a random MAC">🎲</button></div>`;
      }).join("")}

      ${!t.disks.length ? field("Storage pool", "pool", t.pool, initialPool, (attrs) => `<select ${attrs}>${importOptions(pools.map((p) => p.name), t.pool)}</select>`) : ""}
      ${t.disks.map((d, i) => {
        const source = vm.disks_gb[i];
        const badge = source && d.size_gb && d.size_gb !== source ? `<span class="mg-resize-badge">${d.size_gb < source ? "SHRINK" : "GROW"}</span>` : "";
        return `<div class="mg-target-disk"><h5>Disk ${i + 1} ${badge}</h5><div class="mg-target-grid">${field("Storage pool", "pool", d.pool, initialPool, (attrs) => `<select ${attrs}>${importOptions(pools.map((p) => p.name), d.pool)}</select>`, i)}${field("Size (GiB)", "size_gb", d.size_gb, source || null, (attrs) => `<input type="number" min="1" step="1" value="${d.size_gb ?? ""}" placeholder="Keep original size" ${attrs}>`, i)}</div></div>`;
      }).join("")}</div>${shrinking ? '<p class="hint mg-shrink-hint">Shrink needs used space +10% (minimum +1 GiB). Checked after conversion; filesystem support required.</p>' : ""}
      <div class="mg-target-grid">${field(importBatch.replication ? "Start after cutover" : "Start after import", "start", t.start, false, (attrs) => `<input type="checkbox" ${t.start ? "checked" : ""} ${attrs}>`)}${/windows/i.test(vm.guest_os) && !$("#import-hib").hidden ? field("Clear hibernation", "clear_hibernation", t.clear_hibernation, false, (attrs) => `<input type="checkbox" ${t.clear_hibernation ? "checked" : ""} ${attrs}>`) : ""}</div></section></article>`;
  }).join("");
  const save = (input, reset = false) => {
    const vm = importChosen()[Number(input.dataset[reset ? "resetVm" : "targetVm"])];
    if (importBatch.submitted.has(vm.ref)) return;
    const t = importTarget(vm), key = input.dataset[reset ? "resetField" : "targetField"];
    const disk = input.dataset[reset ? "resetDisk" : "targetDisk"];
    let value = input.type === "checkbox" ? input.checked : input.value;
    if (["vcpus", "cpu_sockets", "cpu_cores", "memory_mb", "size_gb"].includes(key)) value = value === "" && key === "size_gb" ? null : Number(value);
    if (reset) value = key === "name" ? vm.name : ["vcpus", "memory_mb"].includes(key) ? vm[key] : key === "node" ? t.originalNode : key === "pool" ? t.originalPool : key === "size_gb" ? vm.disks_gb[Number(disk)] || null : false;
    if (reset && key === "cpu_sockets") value = vm.sockets || 1;
    if (reset && key === "cpu_cores") value = vm.cores_per_socket || vm.vcpus;
    if (["mac", "mac_mode", "vlan"].includes(key)) {
      const n = importNicSettings(vm, Number(disk));
      n[key] = reset ? key === "mac_mode" ? importDefaultMac(vm, Number(disk)) : "" : value;
    } else if (key === "network") {
      const nic = importNicKey(vm, Number(disk));
      if (reset) importBatch.networks.delete(nic); else importBatch.networks.set(nic, value);
    } else if (key === "name") importBatch.names.set(vm.ref, value);
    else if (["vcpus", "cpu_sockets", "cpu_cores", "memory_mb"].includes(key)) importBatch.resources.get(vm.ref)[key] = value;
    else if (disk != null) t.disks[Number(disk)][key] = value;
    else t[key] = value;
    if (key === "node") {
      const pools = state.pools.filter((p) => p.node === t.node && p.active);
      t.disks.forEach((d) => { if (!pools.some((p) => p.name === d.pool)) d.pool = pools[0]?.name || ""; });
      if (!pools.some((p) => p.name === t.pool)) t.pool = pools[0]?.name || "";
    }
    renderImportTargets(); importCapacity(); renderMigrateSummary();
  };
  $$("#import-disk-targets [data-random-mac-vm]").forEach((button) => { button.onclick = () => {
    const vm = importChosen()[Number(button.dataset.randomMacVm)];
    if (importBatch.submitted.has(vm.ref)) return;
    const n = importNicSettings(vm, Number(button.dataset.randomMacNic));
    n.mac_mode = "custom";
    n.mac = "52:54:00:" + Array.from(crypto.getRandomValues(new Uint8Array(3)), (b) => b.toString(16).padStart(2, "0")).join(":");
    renderImportTargets(); renderMigrateSummary();
  }; });
  $$("#import-disk-targets [data-target-field]").forEach((input) => { input.onchange = () => save(input); });
  $$("#import-disk-targets [data-reset-field]").forEach((button) => { button.onclick = () => save(button, true); });
}
function importCapacity() {
  let invalid = false, insufficient = false;
  const demand = new Map();
  for (const vm of importChosen()) {
    const t = importTarget(vm);
    const scratch = t.disks[0]?.pool || t.pool || importForm.elements.pool.value;
    const shrinking = t.disks.some((d, i) => d.size_gb && d.size_gb < vm.disks_gb[i]);
    const add = (pool, size, unknown) => {
      const key = JSON.stringify([t.node, pool]);
      const entry = demand.get(key) || { size: 0, unknown: false };
      entry.size += size; entry.unknown ||= unknown;
      demand.set(key, entry);
    };
    if (!computeMembers().some((m) => m.healthy && m.name === t.node)) invalid = true;
    add(scratch, t.disks.reduce((sum, d, i) => sum + Math.max(d.size_gb || vm.disks_gb[i] || 0, vm.disks_gb[i] || 0), 0) * (shrinking ? 3 : 1), !t.disks.length || vm.disks_gb.some((g) => !g));
    t.disks.forEach((d, i) => {
      if (d.size_gb != null && (!Number.isInteger(d.size_gb) || d.size_gb < 1)) invalid = true;
      add(d.pool, d.size_gb || vm.disks_gb[i] || 0, !d.size_gb && !vm.disks_gb[i]);
    });
  }
  const bars = [];
  for (const [key, entry] of demand) {
    const [node, name] = JSON.parse(key), pool = state.pools.find((p) => p.node === node && p.name === name && p.active);
    if (!pool) invalid = true;
    const free = pool?.available_gb;
    const short = Number.isFinite(free) && free < entry.size;
    insufficient ||= short;
    const required = entry.size ? `${entry.unknown ? "≥ " : ""}${entry.size} GiB` : "Pending detection";
    bars.push(`<div class="mg-pool-capacity ${short || !pool ? "is-short" : ""}"><strong>${esc(node)} / ${esc(name || "Select pool")}</strong><span>${required} required · ${Number.isFinite(free) ? `${Math.floor(free)} GiB` : "Unknown"} free</span><meter min="0" max="${Math.max(free || 0, entry.size, 1)}" value="${entry.size}" aria-label="${esc(node)} / ${esc(name)} required space"></meter>${entry.unknown ? '<small>Size detected after conversion; required space may increase.</small>' : ""}</div>`);
  }
  $("#import-capacity").innerHTML = bars.join("");
  return { invalid, insufficient };
}
function importTargetPools() {
  const sel = importForm.elements.pool, was = sel.value;
  const pools = state.pools.filter((p) => p.node === importForm.elements.node.value && p.active);
  sel.innerHTML = pools.map((p) => `<option value="${esc(p.name)}">${esc(poolText(p))} (${Math.floor(p.available_gb)} GB free, ${esc(p.type)})</option>`).join("");
  if (pools.some((p) => p.name === was)) sel.value = was;
  importCapacity();
}
function importApplyAll() {
  for (const vm of importChosen()) {
    if (importBatch.submitted.has(vm.ref)) continue;
    const t = importTarget(vm);
    t.node = importForm.elements.node.value;
    t.pool = importForm.elements.pool.value;
    t.disks.forEach((d) => { d.pool = importForm.elements.pool.value; });
  }
  renderImportTargets(); importCapacity(); renderMigrateSummary();
}
const importWiz = wizard("import-dialog", {
  steps: 3,
  reset() {
    const ctx = dialogContext;
    Object.assign(importBatch, { source: ctx.source, vms: ctx.vms, picked: new Set(ctx.indices.map((i) => ctx.vms[i].ref)), names: new Map(), resources: new Map(), targets: new Map(), networks: new Map(), nicSettings: new Map(), submitted: new Set(), replication: !!ctx.replication });
    $("#import-title").textContent = importBatch.replication ? "New replication" : "Migrate VMs";
    $("#import-source-pick").hidden = !importBatch.replication;
    $("#import-final-step").textContent = importBatch.replication ? "Schedule & review" : "Summary";
    $("#replication-schedule").hidden = !importBatch.replication;
    $$("#replication-schedule input").forEach((input) => { input.disabled = !importBatch.replication; });
    $("#import-dialog .wiz-ok").textContent = importBatch.replication ? "Create replication" : "Start migration";
    $("#import-power-hint").textContent = importBatch.replication ? "Source VMs may remain running while snapshots sync." : "Power off selected VMs at the source to continue.";
    $("#import-submit-status").textContent = "";
    importForm.elements.node.innerHTML = computeMembers().filter((m) => m.healthy).map((m) => `<option value="${esc(m.name)}">${esc(nodeOptionText(m.name))}</option>`).join("");
    importTargetPools();
  },
  check(step) {
    const chosen = importChosen();
    if (!chosen.length) throw new Error("Select at least one VM.");
    if (!importBatch.replication && chosen.some((vm) => vm.state === "running")) throw new Error("Power off all selected VMs at the source, then refresh the inventory before migration.");
    if (step >= 2) {
      const { invalid, insufficient } = importCapacity();
      if (invalid) throw new Error("Choose a healthy node, an available pool, and a positive whole disk size (or leave size blank to keep the original).");
      if (insufficient) throw new Error("Insufficient space for the selected VMs and conversion files.");
      importValidateNics();
      const names = chosen.map((v) => importBatch.names.get(v.ref) ?? v.name);
      if (names.some((n) => !/^[a-zA-Z0-9\u0E00-\u0E7F][a-zA-Z0-9\u0E00-\u0E7F_.-]{0,62}$/.test(n))) throw new Error("VM names must be 1–63 letters (English or Thai), digits, '-', '_' or '.'.");
      if (new Set(names).size !== names.length) throw new Error("Each selected VM needs a distinct target name.");
      if (chosen.some((v) => {
        const r = importBatch.resources.get(v.ref) || v;
        return (r.cpu_sockets != null && (!Number.isInteger(r.cpu_sockets) || r.cpu_sockets < 1)) || (r.cpu_cores != null && (!Number.isInteger(r.cpu_cores) || r.cpu_cores < 1)) || !Number.isInteger(r.vcpus) || r.vcpus < 1 || r.vcpus > 256 || !Number.isInteger(r.memory_mb) || r.memory_mb < 128;
      })) throw new Error("Choose 1–256 vCPUs and at least 128 MB memory per VM.");
      if (chosen.some((v, i) => !importBatch.submitted.has(v.ref) && (state.vms.some((vm) => vm.name === names[i]) || state.imports.some((j) => j.name === names[i] && ["queued", "running"].includes(j.state))))) throw new Error("A target name already exists or has a pending migration. Choose another name.");
    }
  },
  show(step) {
    if (step === 1) {
      $("#import-pick-vms").innerHTML = importBatch.vms.map((vm, i) => `<tr><td><input type="checkbox" data-wizard-vm="${i}" ${importBatch.picked.has(vm.ref) ? "checked" : ""} ${importBatch.submitted.size ? "disabled" : ""} aria-label="Select ${esc(vm.name)}"></td><td>${esc(vm.name)}</td><td>${esc(vm.state || "Unknown")}${vm.state === "running" && !importBatch.replication ? " — power off first" : ""}</td><td>${(vm.disks_gb || []).map((g) => g ? `${g} GiB` : "Size detected after conversion").join(", ") || "Size detected after conversion"}</td></tr>`).join("");
    }
    if (step === 2) {
      const source = state.sources.find((s) => s.name === importBatch.source);
      $("#import-win").hidden = !importChosen().some((v) => /windows/i.test(v.guest_os));
      $("#import-hib").hidden = !(source?.type === "kvm" || (source?.type === "vmware" && (source.settings?.transport || "ssh") === "ssh"));
      renderImportTargets();
      importCapacity();
    }
    if (step === 3) {
      $("#import-summary").innerHTML = importChosen().map((vm) => {
        const t = importTarget(vm), r = importBatch.resources.get(vm.ref) || vm;
        return `<article class="mg-vm-target"><h4>${esc(vm.name)} → ${esc(importBatch.names.get(vm.ref) ?? vm.name)}</h4><p>${esc(t.node)} · vCPU ${vm.vcpus} → ${r.vcpus} (${r.cpu_sockets || 1} × ${r.cpu_cores || r.vcpus}) · Memory ${vm.memory_mb} → ${r.memory_mb} MB · Start: ${t.start ? "Yes" : "No"} · Clear hibernation: ${t.clear_hibernation ? "Yes" : "No"}</p><p>${t.disks.map((d, i) => `Disk ${i + 1}: ${vm.disks_gb[i] || "Size detected after conversion"} → ${d.size_gb || "Original"} GiB / ${esc(d.pool)} ${d.size_gb && vm.disks_gb[i] && d.size_gb < vm.disks_gb[i] ? "SHRINK" : vm.disks_gb[i] && d.size_gb > vm.disks_gb[i] ? "GROW" : ""}`).join("<br>") || `Storage pool: ${esc(t.pool)} · size detected after conversion`}</p><p>${importNetworkKeys(vm).map((src, i) => `NIC ${i + 1}: ${esc(src)} → ${esc(importNicLabel(vm, i, src))} · MAC ${esc(importNicMac(vm, i))}`).join("<br>")}</p></article>`;
      }).join("") + `<p class="hint">${importBatch.replication ? "Sync starts immediately. Shut down the source only when ready for cutover." : "Keep source VMs powered off until migration completes."}</p>`;
    }
  },
});
$("#import-pick-vms").onchange = (e) => {
  const input = e.target.closest("[data-wizard-vm]");
  if (!input) return;
  const ref = importBatch.vms[Number(input.dataset.wizardVm)].ref;
  if (input.checked) importBatch.picked.add(ref); else importBatch.picked.delete(ref);
};
importForm.elements.node.onchange = () => { importTargetPools(); importApplyAll(); };
importForm.elements.pool.onchange = importApplyAll;

handlers["import-dialog"] = async (f) => {
  if (importWiz.step !== 3) throw new Error("Review the migration summary first.");
  importWiz.check(3);
  const chosen = importChosen();
  for (const vm of chosen) {
    if (importBatch.submitted.has(vm.ref)) continue;
    try {
      await importResolveNetworks(vm);
      const t = importBatch.targets.get(vm.ref);
      const schedule = importBatch.replication ? (f.get("replication_at") ? {at: f.get("replication_at")} : {interval_minutes: Number(f.get("replication_interval"))}) : undefined;
      await post(importBatch.replication ? "/migrate/replications" : "/migrate/jobs", {
        ...(schedule ? {schedule} : {}),
        cpu_sockets: (importBatch.resources.get(vm.ref) || {}).cpu_sockets || 1,
        cpu_cores: (importBatch.resources.get(vm.ref) || {}).cpu_cores || (importBatch.resources.get(vm.ref) || vm).vcpus,
        source: importBatch.source, vm: vm.ref, name: importBatch.names.get(vm.ref) ?? vm.name,
        node: t?.node || f.get("node"), pool: t?.disks[0]?.pool || t?.pool || f.get("pool"), disks: t?.disks.length ? t.disks.map((d) => ({pool: d.pool, size_gb: d.size_gb})) : undefined, vcpus: (importBatch.resources.get(vm.ref) || vm).vcpus, memory_mb: (importBatch.resources.get(vm.ref) || vm).memory_mb, network: importNetworkKeys(vm).map((k, i) => importNicNetwork(vm, i, k)), nics: importNetworkKeys(vm).length ? importNetworkKeys(vm).map((k, i) => ({network: importNicNetwork(vm, i, k), mac_mode: importNicSettings(vm, i).mac_mode, mac: importNicSettings(vm, i).mac_mode === "custom" ? importNicSettings(vm, i).mac.toLowerCase() : null})) : undefined, start: t ? t.start : f.get("start") === "on",
        clear_hibernation: (t ? t.clear_hibernation : f.get("clear_hibernation") === "on") && /windows/i.test(vm.guest_os) && !$("#import-hib").hidden,
      });
      importBatch.submitted.add(vm.ref);
      $("#import-submit-status").textContent = `${importBatch.submitted.size} of ${chosen.length} jobs created.`;
    } catch (err) {
      $("#import-submit-status").textContent = `${importBatch.submitted.size} of ${chosen.length} jobs created. ${vm.name}: ${err.message}. Review Jobs before retrying; accepted jobs will be skipped in this wizard.`;
      throw err;
    }
  }
  toast(`${chosen.length} ${importBatch.replication ? "replication" : "migration"} jobs created.`);
  await refresh("migrate");
  selectTab("migrate-tabs", importBatch.replication ? "replications" : "imports");
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
  await renderSettings();
  renderUsers();
  if (isAdmin() && state.me) await renderAudit();
}

function renderUsers() {
  const me = state.me?.name;
  $("#users").innerHTML = state.users.length ? state.users.map((u) => `<tr>
    <td><div class="name-cell">${avatar("i-user")}<div><span class="vm-name">${esc(u.name)}</span>${u.name === me ? '<span class="vm-sub">you</span>' : ""}</div></div></td>
    <td><select data-role="${esc(u.name)}" style="width:auto" ${u.name === me || (u.role === "admin" && !isAdmin()) ? "disabled" : ""}>
      ${Object.keys(state.settingsRoles || {viewer:[], operator:[], admin:[]}).map((r) => `<option value="${r}" ${u.role === r ? "selected" : ""}>${r[0].toUpperCase() + r.slice(1)}</option>`).join("")}
    </select></td>
    <td><span class="badge ${u.enabled ? "online" : "disabled"}">${u.enabled ? (u.must_change_password ? "Must set password" : "Active") : "Disabled"}</span></td>
    <td>${esc(u.last_login ? when(u.last_login) : "never")}</td>
    <td>${esc(when(u.created))}</td>
    <td><div class="actions">${u.name === me || (u.role === "admin" && !isAdmin()) ? "" : `
      <button class="small" data-user-reset="${esc(u.name)}">Reset password</button>
      ${isAdmin() ? (u.totp_enabled || u.require_2fa ? `<button class="small" data-user-tfa-off="${esc(u.name)}" title="Turn 2FA off for this user; no code at sign-in">Turn off 2FA</button>` : `<button class="small" data-user-tfa-on="${esc(u.name)}" title="This user must set up 2FA at the next sign-in">Require 2FA</button>`) : ""}
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
    if (d.userTfaOn || d.userTfaOff) {
      const name = d.userTfaOn || d.userTfaOff, on = !!d.userTfaOn;
      if (!on && !confirm(`Turn off 2FA for ${name}? They will sign in with the password only.`)) return;
      await patch(`/users/${enc(name)}`, { require_2fa: on });
      toast(on ? `${name} must set up 2FA at the next sign-in` : `2FA turned off for ${name}`);
    } else if (d.userTwofa) {
      openTwofaReset(d.userTwofa);
      return;
    } else if (d.userReset) {
      const pw = await askNewPassword(d.userReset);
      if (!pw) return;
      await patch(`/users/${enc(d.userReset)}`, { password: pw });
      toast(`Password of ${d.userReset} reset; they set their own at next sign-in`);
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

window.addEventListener("DOMContentLoaded", () => loadMe().then(show).catch(fail), { once: true });
setInterval(() => {
  if (!jobsView() && !document.querySelector("dialog[open], details.more[open]") && !document.hidden) refresh(currentPage(), true).catch(() => {});
}, 10000);


// ---------- Firewall and connectivity test ----------

function renderEdge() {
  renderNetworkStudio();
}

// Network studio: firewall drafts are independent from live state and topology drafts.
const nw = { draft: null, base: null, busy: false, conflict: false, from: null, undo: null, applied: null, expired: false, deadline: 0, dragRule: null };
const nwServices = { Custom: null, Web: ['tcp', '80,443'], SSH: ['tcp', '22'], RDP: ['tcp', '3389'], DNS: ['udp', '53'], 'SQL Server': ['tcp', '1433'], MySQL: ['tcp', '3306'], PostgreSQL: ['tcp', '5432'], SMB: ['tcp', '445'], Ping: ['icmp', ''] };
// Services the user added with "+ Add service" are kept in this browser.
const nwCustomKey = 'ibhics.fw.services';
function nwLoadCustom() {
  try { Object.assign(nwServices, JSON.parse(localStorage.getItem(nwCustomKey) || '{}')); } catch { /* ignore */ }
}
function nwSaveCustom(name, proto, ports) {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(nwCustomKey) || '{}'); } catch { /* ignore */ }
  saved[name] = [proto, ports];
  localStorage.setItem(nwCustomKey, JSON.stringify(saved));
  nwServices[name] = [proto, ports];
}
function nwRenderServices() {
  const picked = new Set($$('#nw-rule-services input:checked').map(b => b.value));
  $('#nw-rule-services').innerHTML = Object.entries(nwServices).filter(([, v]) => v).map(([n, [proto, ports]]) => `<label class="nw-svc"><input type="checkbox" value="${esc(n)}" ${picked.has(n) ? 'checked' : ''}><span>${esc(n)}</span><small>${proto.toUpperCase()}${ports ? ' ' + esc(ports) : ''}</small></label>`).join('')
    + `<button type="button" class="nw-svc nw-svc-add" id="nw-svc-add">+ Add service</button>`
    + `<span class="nw-svc-new" id="nw-svc-new" hidden><input id="nw-svc-name" placeholder="Name, e.g. App" maxlength="24"><select id="nw-svc-proto"><option value="tcp">TCP</option><option value="udp">UDP</option></select><input id="nw-svc-ports" placeholder="8080 or 8000-8100"><button type="button" id="nw-svc-save">Add</button></span>`;
  $('#nw-svc-add').onclick = () => { $('#nw-svc-new').hidden = false; $('#nw-svc-name').focus(); };
  $('#nw-svc-save').onclick = () => {
    const name = $('#nw-svc-name').value.trim(), proto = $('#nw-svc-proto').value, ports = $('#nw-svc-ports').value.trim();
    try {
      if (!/^[A-Za-z0-9][A-Za-z0-9 _.-]{0,23}$/.test(name)) throw Error('Service name: letters, digits, space, _ . -');
      if (nwServices[name] !== undefined && !JSON.parse(localStorage.getItem(nwCustomKey) || '{}')[name]) throw Error('That name is a built-in service');
      NetworkCore.ports(ports);
      if (!ports) throw Error('Enter a port or a range');
      nwSaveCustom(name, proto, ports);
      nwRenderServices();
      const box = $$('#nw-rule-services input').find(b => b.value === name);
      if (box) box.checked = true;
      nwSyncServices();
      $('#nw-test-service').innerHTML = Object.keys(nwServices).map(n => `<option>${esc(n)}</option>`).join('');
    } catch (err) { toast(err.message, true); }
  };
}
// Several picked services may mix protocols; one rule is staged per protocol.
function nwServiceGroups() {
  const groups = {};
  for (const box of $$('#nw-rule-services input:checked')) {
    const [proto, ports] = nwServices[box.value];
    const set = (groups[proto] ||= new Set());
    for (const p of ports.split(',').filter(Boolean)) set.add(p);
  }
  return Object.entries(groups).map(([proto, ports]) => ({proto, ports: [...ports].join(',')}));
}
function nwSyncServices() {
  const groups = nwServiceGroups(), f = $('#fw-form').elements, note = $('#nw-rule-split');
  if (groups.length) { f.proto.value = groups[0].proto; f.ports.value = groups[0].ports; }
  f.proto.disabled = f.ports.disabled = groups.length > 1;
  note.hidden = groups.length < 2;
  note.textContent = groups.length > 1 ? `Stages ${groups.length} rules, one per protocol: ${groups.map(g => g.proto.toUpperCase() + (g.ports ? ' ' + g.ports : '')).join(' · ')}` : '';
}
const nwRule = r => Object.fromEntries(['name','action','src','dst','proto','port','ports','description','enabled'].map(k => [k, r[k] ?? ({port:null, ports:'', description:'', enabled:true}[k])]));
const nwRules = () => nw.draft || (state.edge?.rules || []).map(nwRule);
const nwInventory = () => state.vms.map(v => ({...v, ips: (state.guests[v.uuid]?.ips || []).filter(ip => NetworkCore.ipNumber(ip) !== null)}));
const nwEnds = () => [{id:'any', name:'External / Any', kind:'internet'}, ...state.networks.map(n => ({id:n.name, name:`${n.name} · VLAN ${vlanId(n)}`, kind:'switch'})), ...(state.groups.groups || []).map(g => ({id:`group:${g.id}`, name:g.name, kind:'other'})), ...state.vms.map(v => ({id:`vm:${v.name}`, name:v.name, kind:NetworkCore.detectOS(v,state.guests[v.uuid]), vm:v}))];
function nwStage(rules) {
  if (!canOperate() || nw.busy || nw.deadline) return;
  if (!nw.draft) nw.base = (state.edge?.rules || []).map(nwRule);
  nw.draft = rules.map(nwRule);
  $('#nw-test-result').textContent = 'Draft changed. Run the connection test again.';
  renderNetworkStudio(); window.Network3D?.update();
}
function nwOpenRule(src = 'any', dst = 'any', index = null) {
  if (!canOperate() || nw.busy || nw.deadline) return;
  const rule = index === null ? {name:`policy-${Date.now().toString(36)}`, action:'allow', src, dst, proto:'tcp', ports:'80,443', description:'', enabled:true} : nwRules()[index];
  openDialog('fw-dialog', {index}, {...rule, ports: [rule.port, rule.ports].filter(Boolean).join(',')});
  $('#fw-form').elements.enabled.checked = rule.enabled !== false;
  $$('#nw-rule-services input').forEach(b => { b.checked = false; }); nwSyncServices();
}
function renderNetworkStudio() {
  const live = (state.edge?.rules || []).map(nwRule);
  nw.conflict = !!nw.draft && JSON.stringify(live) !== JSON.stringify(nw.base);
  if (nw.expired && state.edge && !state.edge.pending_confirm_seconds) nw.expired=false;
  if (state.edge?.pending_confirm_seconds && !nw.deadline && !nw.expired) nw.deadline = Date.now() + state.edge.pending_confirm_seconds * 1000;
  const rules = nwRules(), op = canOperate() && !nw.busy && !nw.deadline;
  const changes = nw.draft ? Math.max(live.length, rules.length) - live.filter((r,i) => JSON.stringify(r) === JSON.stringify(rules[i])).length : 0;
  $('#nw-draft-count').textContent = nw.conflict ? 'Live policy changed. Discard and reload your draft.' : `${changes} staged change${changes === 1 ? '' : 's'}`;
  $('#nw-apply').disabled = !op || !changes || nw.conflict;
  $('#nw-discard').disabled = !op || !nw.draft;
  $('#fw-add').disabled = !op; $('#nw-template-add').disabled = !op;
  $('#fw-rules').innerHTML = rules.map((r,i) => `<article class="nw-rule ${r.action} ${r.enabled ? '' : 'disabled'}" draggable="${op}" data-rule="${i}"><div class="nw-rule-order">${NetworkCore.deviceIcon("firewall")}${i+1}<small>↕</small></div><div class="nw-rule-body"><b>${esc(r.name)}</b><div class="nw-route">${esc(r.src)} <span>→</span> ${esc(r.dst)}</div><small>${esc(r.description || 'No description')}</small></div><span class="nw-verdict">${r.action === 'allow' ? 'ALLOW' : 'BLOCK'}</span><span>${esc(r.proto.toUpperCase())} ${esc([r.port,r.ports].filter(Boolean).join(','))}</span><div class="actions">${op ? `<button data-fw-up="${i}" ${i ? '' : 'disabled'} aria-label="Move ${esc(r.name)} up">↑</button><button data-fw-down="${i}" ${i === rules.length - 1 ? 'disabled' : ''} aria-label="Move ${esc(r.name)} down">↓</button><button data-fw-toggle="${i}" aria-pressed="${r.enabled}">${r.enabled ? 'On' : 'Off'}</button><button data-fw-edit="${i}">Edit</button><button data-fw-del="${i}">Remove</button>` : ''}</div></article>`).join('') || '<div class="nw-empty">No rules · All connections are allowed. Connect two endpoints to start.</div>';
  const ends = nwEnds();
  $('#fw-ends').innerHTML = ends.map(n => `<option value="${esc(n.id)}">${esc(n.name)}</option>`).join('');
  $('#nw-test-ends').innerHTML = state.vms.map(v => `<option value="${esc(v.name)}">`).join('');
  $('#nw-policy-nodes').innerHTML = ends.map(n => `<button class="nw-end ${nw.from === n.id ? 'selected' : ''} ${n.vm ? (n.vm.state === 'running' ? 'up' : 'down') : 'equipment'}" data-end="${esc(n.id)}" ${op ? '' : 'disabled'}><span>${NetworkCore.deviceIcon(n.kind,{stopped:n.vm && n.vm.state !== "running"})}</span><b>${esc(n.name)}</b>${n.vm ? `<small>${esc((state.guests[n.vm.uuid]?.ips || n.vm.ips || []).filter(ip=>NetworkCore.ipNumber(ip)!==null)[0] || "No IP")}</small>` : ""}</button>`).join('');
  const computeCount = computeMembers().length;
  $('#nw-switch-cards').innerHTML = state.networks.map(n => {
    const vms = state.vms.filter(v => v.nics.some(nic => nic.network === n.name)).length;
    const nodes = n.nodes || [n.node || 'Local'];
    const missing = n.vlan && computeCount > nodes.length;
    const actions = isAdmin() && n.name !== 'default' ? `<div class="nw-switch-actions">
      ${missing ? `<button type="button" data-sw-sync="${esc(n.name)}" title="Create it on every compute node">Add to all nodes</button>` : ''}
      <button type="button" data-sw-edit="${esc(n.name)}" ${vms ? `disabled title="Move its ${vms} VMs to another network first"` : ''}>Edit</button>
      <button type="button" class="danger" data-sw-del="${esc(n.name)}" ${vms ? `disabled title="Move its ${vms} VMs to another network first"` : ''}>Delete</button></div>` : '';
    const form = nw.switchEdit === n.name ? `<form class="nw-switch-form" data-sw-form="${esc(n.name)}"><label>Name<input name="name" value="${esc(n.name)}" required pattern="[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}"></label><label>VLAN ID<input name="vlan" type="number" min="1" max="4094" value="${n.vlan || ''}" required></label><div class="nw-switch-actions"><button type="submit" class="primary">Save</button><button type="button" data-sw-cancel>Cancel</button></div></form>` : '';
    return `<article class="nw-panel"><h3>${esc(n.name)}</h3><div class="nw-vlan">${vlanId(n)}<small>${n.vlan ? 'TAGGED VLAN' : 'NATIVE VLAN'}</small></div><p>${vms} virtual machines</p><small>${esc(nodes.join(' · '))} · ${esc(n.bridge || 'br0')}${missing ? ` <b class="nw-warn">· not on every node</b>` : ''}</small>${form || actions}</article>`;
  }).join('');
  requestAnimationFrame(nwDrawPolicy);
  nwSafeBanner();
}
function nwDrawPolicy() {
  const host = $('#nw-policy'), rect = host.getBoundingClientRect(), svg = $('svg', host);
  if (!rect.width) return;
  svg.setAttribute('viewBox', `0 0 ${rect.width} ${host.scrollHeight}`);
  svg.style.height = `${host.scrollHeight}px`;
  const positions = new Map($$('[data-end]',host).map(el => {const r=el.getBoundingClientRect(); return [el.dataset.end, {x:r.left-rect.left+r.width/2+host.scrollLeft,y:r.top-rect.top+r.height/2+host.scrollTop}];}));
  svg.innerHTML = nwRules().map(r => {const a=positions.get(r.src), b=positions.get(r.dst); return a && b ? `<path d="M${a.x},${a.y} Q${(a.x+b.x)/2},${Math.min(a.y,b.y)-40} ${b.x},${b.y}" fill="none" stroke="${r.action === 'allow' ? '#6dffb3' : '#ff6b7a'}" stroke-width="2" opacity=".5" ${r.enabled ? '' : 'stroke-dasharray="5 6"'}/>` : '';}).join('');
}
$('#fw-add').addEventListener('click', () => nwOpenRule());
handlers['fw-dialog'] = async (f, ctx) => {
  const els = $('#fw-form').elements, groups = nwServiceGroups();
  const rule = {name:f.get('name').trim(), action:f.get('action'), src:f.get('src').trim(), dst:f.get('dst').trim(), proto:els.proto.value, ports:els.ports.value.trim(), port:null, description:f.get('description').trim(), enabled:els.enabled.checked};
  if (groups.length > 1) {
    // Mixed protocols: stage one rule per protocol, named <name>-tcp, <name>-udp, ...
    const rules = nwRules().map(nwRule);
    const made = groups.map(g => ({...rule, name: `${rule.name.slice(0, 58)}-${g.proto}`, proto: g.proto, ports: g.ports}));
    if (made.some(m => rules.some((r, i) => r.name === m.name && i !== ctx.index))) throw Error('Rule names must be unique');
    if (ctx.index === null) rules.push(...made); else rules.splice(ctx.index, 1, ...made);
    nwStage(rules);
    return;
  }
  if (rule.ports && !['tcp','udp'].includes(rule.proto)) throw Error('Ports require TCP or UDP');
  NetworkCore.ports(rule.ports);
  const validEnd = end => nwEnds().some(n => n.id === end) || NetworkCore.inCIDR('0.0.0.0', end) || (NetworkCore.ipNumber(end.split('/')[0]) !== null && /^\d+$/.test(end.split('/')[1] || '32') && +(end.split('/')[1] || 32) <= 32 && end.split('/').length <= 2);
  if (!validEnd(rule.src) || !validEnd(rule.dst)) throw Error('Choose a known VM, group, VLAN, any, or a valid IPv4 CIDR');
  const rules = nwRules().map(nwRule);
  if (rules.some((r,i) => r.name === rule.name && i !== ctx.index)) throw Error('Rule names must be unique');
  if (ctx.index === null) rules.push(rule); else rules[ctx.index] = rule;
  if (ctx.mirror) {
    const name = `${rule.name.slice(0,59)}.out`;
    if (rules.some(r => r.name === name)) throw Error('The outbound rule name already exists');
    rules.push({...rule, name, src:rule.dst, dst:rule.src});
  }
  nwStage(rules);
};
$('#fw-rules').addEventListener('click', e => {
  if (!canOperate() || nw.busy || nw.deadline) return;
  const b = e.target.closest('button'); if (!b) return;
  const rules = nwRules().map(nwRule);
  if (b.dataset.fwEdit !== undefined) {nwOpenRule('any','any',+b.dataset.fwEdit); return;}
  if (b.dataset.fwDel !== undefined) rules.splice(+b.dataset.fwDel,1);
  if (b.dataset.fwToggle !== undefined) rules[+b.dataset.fwToggle].enabled = !rules[+b.dataset.fwToggle].enabled;
  for (const [key,d] of [['fwUp',-1],['fwDown',1]]) if (b.dataset[key] !== undefined) {const i=+b.dataset[key]; if (rules[i+d]) [rules[i],rules[i+d]]=[rules[i+d],rules[i]];}
  nwStage(rules);
});
$('#fw-rules').addEventListener('dragstart', e => {const card=e.target.closest('[data-rule]'); nw.dragRule=card ? +card.dataset.rule : null; if (card) e.dataTransfer.setData('text/plain',card.dataset.rule);});
$('#fw-rules').addEventListener('dragend', () => { nw.dragRule=null; });
$('#fw-rules').addEventListener('dragover', e => e.preventDefault());
$('#fw-rules').addEventListener('drop', e => {e.preventDefault(); const card=e.target.closest('[data-rule]'); if (!card || nw.dragRule === null) return; const rules=[...nwRules()], [r]=rules.splice(nw.dragRule,1); rules.splice(+card.dataset.rule,0,r); nw.dragRule=null; nwStage(rules);});
$('#nw-rule-view').onclick = () => {const list=$('#fw-rules').classList.toggle('list'); $('#nw-rule-view').textContent=list?'Card view':'List view';};
$('#nw-switch-cards').addEventListener('click', async (e) => {
  const b = e.target.closest('button'); if (!b) return;
  try {
    if (b.dataset.swEdit) { nw.switchEdit = b.dataset.swEdit; renderNetworkStudio(); }
    else if (b.hasAttribute('data-sw-cancel')) { nw.switchEdit = ''; renderNetworkStudio(); }
    else if (b.dataset.swDel) {
      if (!confirm(`Delete virtual switch ${b.dataset.swDel} from every node? VMs must not use it.`)) return;
      await del(`/networks/${enc(b.dataset.swDel)}`); toast(`${b.dataset.swDel} deleted`); await refresh('network');
    } else if (b.dataset.swSync) {
      await post(`/networks/${enc(b.dataset.swSync)}/sync`, {}); toast(`${b.dataset.swSync} is on every node now`); await refresh('network');
    }
  } catch (err) { fail(err); }
});
$('#nw-switch-cards').addEventListener('submit', async (e) => {
  const f = e.target.closest('[data-sw-form]'); if (!f) return; e.preventDefault();
  const data = new FormData(f), name = f.dataset.swForm;
  try {
    await api(`/networks/${enc(name)}`, { method: 'PUT', body: JSON.stringify({ name: data.get('name'), vlan: Number(data.get('vlan')) }) });
    nw.switchEdit = ''; toast(`${name} updated`); await refresh('network');
  } catch (err) { fail(err); }
});
$('#nw-switch-view').onclick = () => {const list=$('#tab-net-list').classList.toggle('list'); $('#nw-switch-view').textContent=list?'Card view':'List view';};
$('#nw-discard').onclick = () => {nw.draft=null; nw.base=null; renderNetworkStudio(); window.Network3D?.update();};
nwLoadCustom();
nwRenderServices();
$('#nw-rule-services').onchange = e => { if (e.target.matches('input[type=checkbox]')) nwSyncServices(); };
for (const id of ['nw-test-service']) $('#'+id).innerHTML = Object.keys(nwServices).map(n => `<option>${n}</option>`).join('');
$('#nw-test-service').onchange = e => {const service=nwServices[e.target.value]; if (service) {const f=$('#nw-test-form'); f.elements.proto.value=service[0]; f.elements.port.value=service[1].split(',')[0];}};
$('#nw-test-form').onsubmit = async e => {
  e.preventDefault(); const f=new FormData(e.target), flow={src:f.get('src').trim(),dst:f.get('dst').trim(),proto:f.get('proto'),port:f.get('proto')==='icmp'?null:Number(f.get('port'))||null};
  const out=$('#nw-test-result');
  try {const draft=!!nw.draft, result=draft ? NetworkCore.simulate(nwRules(),flow,nwInventory(),state.groups.members) : await post('/edge/simulate',flow);
    out.className=result.allowed?'allowed':'blocked'; out.textContent=`${result.allowed?'✓ Allowed':'✕ Blocked'} · ${result.rule ? `Rule ${result.index}: ${result.rule}` : 'Default allow'} · ${draft?'Draft policy':'Live policy'} (policy simulation)`;
  } catch(err) {out.className=''; out.textContent=err.message;}
};
$('#nw-template-add').onclick = () => {
  const type=$('#nw-template').value, vm=state.vms[0], net=state.networks.find(n=>n.vlan) || state.networks[0], group=state.groups.groups[0];
  const target=type==='lock'?net?.name:vm && `vm:${vm.name}`;
  if (!target || (type==='database' && !group)) {toast('This template needs a VM / VLAN, and Database needs a VM group.',true);return;}
  // Open the populated rule so its destination can be reviewed before staging.
  nwOpenRule(type==='database'?`group:${group.id}`:'any', target);
  const f=$('#fw-form'); f.elements.action.value=['lock','isolate'].includes(type)?'deny':'allow'; f.elements.proto.value=['lock','isolate'].includes(type)?'any':'tcp'; f.elements.ports.value=type==='web'?'80,443':type==='database'?'5432,3306':''; f.elements.description.value=`${type} template · review endpoints before staging`;
  if (['isolate','lock'].includes(type)) {
    dialogContext.mirror=true;
    f.elements.description.value='Blocks both directions: stages inbound and outbound rules.';
  }
};
let nwPointerFrom=null;
$('#nw-policy').addEventListener('pointerdown',e=>{const b=e.target.closest('[data-end]'); if (b && !b.disabled) nwPointerFrom=b.dataset.end;});
$('#nw-policy').addEventListener('pointermove',e=>{if(!nwPointerFrom || !e.buttons)return; const host=$('#nw-policy'),r=host.getBoundingClientRect(),b=$$('[data-end]',host).find(n=>n.dataset.end===nwPointerFrom),a=b.getBoundingClientRect(); $('#nw-policy svg').innerHTML += `<path class="nw-preview" d="M${a.left-r.left+a.width/2},${a.top-r.top+a.height/2} L${e.clientX-r.left},${e.clientY-r.top}" stroke="#3ef0ff" stroke-dasharray="4 4"/>`; $$('.nw-preview',host).slice(0,-1).forEach(n=>n.remove());});
$('#nw-policy').addEventListener('pointerup',e=>{const b=e.target.closest('[data-end]'); if(b && nwPointerFrom && b.dataset.end!==nwPointerFrom){nwOpenRule(nwPointerFrom,b.dataset.end); nw.from=null; nwPointerFrom=null; nwDrawPolicy();}});
document.addEventListener('pointerup',()=>{nwPointerFrom=null; nwDrawPolicy();});
$('#nw-policy').addEventListener('click',e=>{const b=e.target.closest('[data-end]'); if(!b || b.disabled || $('#fw-dialog').open)return; if(nw.from && nw.from!==b.dataset.end){nwOpenRule(nw.from,b.dataset.end);nw.from=null;}else nw.from=b.dataset.end; renderNetworkStudio();});
async function nwKeep() {
  if (!canOperate() || nw.busy || !nw.deadline) return;
  nw.busy=true;
  try {state.edge=await api('/edge/confirm',{method:'POST',body:'{}',signal:AbortSignal.timeout(5000)}); nw.deadline=0; toast('Firewall changes kept');} catch(err){fail(err);} finally{nw.busy=false;renderNetworkStudio();}
}
function nwSafeBanner() {
  const active=nw.deadline>Date.now(); $('#nw-safe').hidden=!active && !nw.undo && !nw.expired && !nw.deadline;
  $('#nw-safe-text').textContent=active?`Safe apply · ${Math.max(0,Math.ceil((nw.deadline-Date.now())/1000))}s until automatic rollback`:nw.expired || nw.deadline?'Confirmation expired · Refresh to verify automatic rollback':'Connectivity verified · Changes kept';
  $('#nw-keep').hidden=!active; $('#nw-keep').disabled=nw.busy; $('#nw-revert').disabled=nw.busy || !nw.undo;
}
$('#nw-apply').onclick=async()=>{
  if(!canOperate() || nw.busy || !nw.draft || nw.conflict || nw.deadline)return;
  nw.busy=true;renderNetworkStudio();
  try {
    const fresh=await api('/edge');
    if(JSON.stringify(fresh.rules.map(nwRule))!==JSON.stringify(nw.base))throw Error('Live rules changed. Discard the draft and reload before applying.');
    const before=nw.base.map(nwRule);
    state.edge=await api('/edge/rules?safe=true',{method:'PUT',body:JSON.stringify(nw.draft)});
    nw.undo=before; nw.expired=false; nw.applied=state.edge.rules.map(nwRule); nw.draft=null; nw.base=null; nw.deadline=Date.now()+(state.edge.pending_confirm_seconds || 60)*1000;
    // A separate successful read verifies browser connectivity after applying.
    await api('/edge',{signal:AbortSignal.timeout(5000)});
    nw.busy=false; await nwKeep();
  }catch(err){fail(err);}finally{nw.busy=false;renderNetworkStudio();window.Network3D?.update();}
};
$('#nw-keep').onclick=nwKeep;
$('#nw-revert').onclick=async()=>{
  if(!canOperate() || nw.busy || !nw.undo)return;
  nw.busy=true;renderNetworkStudio();
  try{const fresh=await api('/edge');if(JSON.stringify(fresh.rules.map(nwRule))!==JSON.stringify(nw.applied))throw Error('Live policy changed after this apply. Reload and review before reverting.');state.edge=await api('/edge/rules?safe=true',{method:'PUT',body:JSON.stringify(nw.undo)}); nw.deadline=Date.now()+60000; await api('/edge',{signal:AbortSignal.timeout(5000)}); state.edge=await api('/edge/confirm',{method:'POST',body:'{}',signal:AbortSignal.timeout(5000)});nw.undo=null;nw.deadline=0;}catch(err){fail(err);}finally{nw.busy=false;renderNetworkStudio();window.Network3D?.update();}
};
setInterval(()=>{nwSafeBanner();if(nw.deadline && nw.deadline<=Date.now() && !nw.busy){nw.deadline=0;nw.expired=true;nw.undo=null;nw.applied=null;load('edge').then(()=>{renderEdge();window.Network3D?.update();}).catch(fail);}},1000);
window.addEventListener('resize',nwDrawPolicy);
window.addEventListener('beforeunload',e=>{if(nw.draft){e.preventDefault();e.returnValue='';}});
// The hologram shares the topology transaction and Policy studio draft.
window.NetworkUI={getData:()=>({
    nodes:(state.cluster?.members||[]).filter(m=>m.role==='compute').map(m=>({name:m.name,online:!!m.healthy})),
    networks:topoData().networks,
    vms:topoData().vms.map(v=>({...v,guest_info:state.guests[v.uuid],ips:(state.guests[v.uuid]?.ips||[]).filter(ip=>NetworkCore.ipNumber(ip)!==null)})),
    rules:nwRules(),groups:state.groups,stats:state.stats,key:topoLayoutKey(),
    editing:!!ntopo.edit,saving:!!ntopo.edit?.saving,canEdit:canOperate(),
    canApplyFirewall:canOperate()&&!!nw.draft&&!nw.busy&&!nw.conflict&&!nw.deadline,
    pending:ntopo.edit?topoPlan(ntopo.edit.base,ntopo.edit).filter(s=>s.type==='patch').map(s=>s.uuid):[],
    editError:ntopo.edit?.error||''
  }),
  beginEdit:topoStartEdit,
  saveEdit:async()=>{await topoSaveEdit();window.Network3D?.update();},
  discardEdit:()=>{if(ntopo.edit?.saving)return;topoEndEdit();drawTopology();window.Network3D?.update();},
  stageNic:(uuid,index,network)=>{
    if(!canOperate()||!ntopo.edit||ntopo.edit.saving)return;
    const vm=state.vms.find(v=>v.uuid===uuid), net=state.networks.find(n=>n.name===network);
    if(vm&&net&&net.nodes&&vm.node&&!net.nodes.includes(vm.node)){
      toast(`${network} is not on ${vm.node}, where ${vm.name} runs. Open Virtual switches and press "Add to all nodes" on ${network} first.`,true);
      return;
    }
    ntopo.edit.vms=NetworkCore.stageNicMove(ntopo.edit.vms,uuid,index,network,ntopo.edit.networks);
    drawTopology();
  },
  openRule:nwOpenRule,
  applyRules:()=>$('#nw-apply').click(),
  selectVM:uuid=>{
  const v=window.NetworkUI.getData().vms.find(v=>v.uuid===uuid);if(!v)return;
  $('#nw-vm-panel').innerHTML=`<div class="nw-device-heading ${v.state === "running" ? "up" : "down"}">${NetworkCore.deviceIcon(NetworkCore.detectOS(v),{stopped:v.state !== "running"})}<h3>${esc(v.name)}</h3></div><p>${esc(v.state)} · ${esc(v.node || '')}</p><p>${esc(v.nics.map(n=>n.network).join(' · '))}</p><p>${esc(v.ips.join(', ') || 'No guest IP reported')}</p><h3>Related policy</h3>${nwRules().filter(r=>NetworkCore.matches(r.src,NetworkCore.endpoint(v.name,nwInventory()),state.groups.members)||NetworkCore.matches(r.dst,NetworkCore.endpoint(v.name,nwInventory()),state.groups.members)).map(r=>`<p>${esc(r.name)} · ${esc(r.action)}</p>`).join('') || '<p>No matching endpoint rules</p>'}<button id="nw-vm-test">Test connection</button><div id="nw-layer-colors"></div>`;
  $('#nw-vm-test').onclick=()=>{selectTab('net-tabs','net-fw');$('#nw-test-form').elements.src.value=v.name;$('#nw-test-form').elements.dst.focus();};window.Network3D?.colors();
}};


// Terminal (v0.8): allowlisted diagnostic commands on a node, run over the node's agent.
const term = { history: [], at: 0, busy: false };
function renderTerminal() {
  const sel = $("#term-node"), names = computeMembers().map((m) => m.name);
  const keep = sel.value;
  sel.innerHTML = names.map((n) => `<option>${esc(n)}</option>`).join("");
  if (names.includes(keep)) sel.value = keep;
  $("#term-prompt").textContent = `${sel.value || "node"} $`;
  if (!$("#term-out").textContent) termPrint("Type a command such as  ping 8.8.8.8  or  traceroute 1.1.1.1 , or  help  for the list.\n");
  $("#term-input").focus();
}
function termPrint(text, cls = "") {
  const out = $("#term-out"), span = document.createElement("span");
  if (cls) span.className = cls;
  span.textContent = text.endsWith("\n") ? text : text + "\n";
  out.append(span);
  out.parentElement.scrollTop = out.parentElement.scrollHeight;
}
$("#term-node").addEventListener("change", () => { $("#term-prompt").textContent = `${$("#term-node").value} $`; $("#term-input").focus(); });
$("#term-chips").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-cmd]");
  if (!b) return;
  const input = $("#term-input");
  input.value = b.dataset.cmd;
  if (!b.dataset.cmd.endsWith(" ")) $("#term-form").requestSubmit(); else input.focus();
});
$("#term-input").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
  e.preventDefault();
  term.at = Math.max(0, Math.min(term.history.length, term.at + (e.key === "ArrowUp" ? -1 : 1)));
  e.target.value = term.history[term.at] || "";
});
$("#term-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#term-input"), line = input.value.trim(), node = $("#term-node").value;
  if (!line || term.busy) return;
  input.value = "";
  if (term.history.at(-1) !== line) term.history.push(line);
  term.at = term.history.length;
  if (line === "clear") { $("#term-out").textContent = ""; return; }
  termPrint(`${node} $ ${line}`, "term-cmd");
  term.busy = true; input.disabled = true;
  try {
    const r = await post(`/nodes/${enc(node)}/terminal`, { command: line });
    termPrint(r.output || "", r.code ? "term-err" : "");
    if (r.code) termPrint(`(exit ${r.code})`, "term-dim");
  } catch (err) {
    termPrint(String(err.message || err), "term-err");
  } finally {
    term.busy = false; input.disabled = false; input.focus();
  }
});

// Containers (v1.0): LXC system containers beside the VMs.
const CT_ICONS = { debian: "🌀", ubuntu: "🟠", rockylinux: "🟢", almalinux: "🔵", alpine: "🏔️" };
function renderContainers() {
  const tpls = state.ctTemplates || [];
  const q = ($("#ct-search").value || "").trim().toLowerCase();
  const card = (t) => `<button class="ct-tpl" data-tpl="${esc(t.id)}">
    <span class="ct-ico">${CT_ICONS[t.distro] || "📦"}</span><b>${esc(t.title)}</b><span class="muted">${t.kind === "app" ? `app · ${esc(t.base)}` : t.kind === "os" ? "operating system" : "image"}</span></button>`;
  const match = (t) => !q || `${t.title} ${t.distro} ${t.release} ${t.id}`.toLowerCase().includes(q);
  const groups = [["Applications", tpls.filter((t) => t.kind === "app" && match(t))], ["Popular systems", tpls.filter((t) => t.kind === "os" && match(t))]];
  const more = tpls.filter((t) => t.kind === "image" && match(t));
  for (const d of [...new Set(more.map((t) => t.distro))]) groups.push([d.charAt(0).toUpperCase() + d.slice(1), more.filter((t) => t.distro === d)]);
  $("#ct-catalog").innerHTML = groups.filter(([, list]) => list.length).map(([title, list]) =>
    `<h4 class="ct-group">${esc(title)} <span class="muted">${list.length}</span></h4>${list.map(card).join("")}`).join("") || `<p class="hint">No template matches “${esc(q)}”.</p>`;
  const cts = [...(state.containers || [])].sort((a, b) => a.name.localeCompare(b.name));
  const title = (id) => tpls.find((t) => t.id === id)?.title || id;
  $("#ct-rows").innerHTML = cts.length ? cts.map((c) => {
    const on = c.state === "running";
    return `<tr>
    <td><b>${esc(c.name)}</b>${c.ha_enabled ? ` <span class="badge plain">HA</span>` : ""}${c.description ? `<br><span class="muted">${esc(c.description)}</span>` : ""}</td>
    <td><span class="badge ${on ? "running" : "plain"}">${esc(c.state)}</span></td>
    <td>${esc(c.node)}</td><td>${esc(title(c.template))}${c.unprivileged ? "" : ` <span class="badge plain">privileged</span>`}</td>
    <td>${c.vcpus}</td><td>${on ? `${gbText(c.memory_used_mb)} / ` : ""}${gbText(c.memory_mb)}</td>
    <td>${c.disk_used_gb} / ${c.disk_gb} GB</td><td>${esc(c.pool)}</td><td class="mono">${esc((c.addresses || []).join(" ") || (c.ip === "dhcp" ? "" : c.ip))}</td>
    <td>${(c.snapshots || []).length}</td>
    <td class="actions" data-ct="${esc(c.name)}">
      ${on ? `<button class="small" data-act="stop">Stop</button><button class="small" data-act="restart">Restart</button>` : `<button class="small" data-act="start">Start</button>`}
      ${on && isAdmin() ? `<button class="small" data-act="console">Console</button>` : ""}
      ${canOperate() ? `<button class="small" data-act="backup">Backup</button><button class="small" data-act="schedule">Schedule</button><button class="small" data-act="migrate">Migrate</button>${c.migration_pending ? `<button class="small" data-act="migration-recover">Recover migration</button>` : ""}` : ""}
      <button class="small" data-act="edit">Settings</button>
      ${on ? "" : `<button class="small" data-act="rename">Rename</button>`}
      ${on ? "" : `<button class="small danger" data-act="delete">Delete</button>`}
    </td></tr>`;
  }).join("") : empty(11, "No containers yet. Pick a template above to create one in a minute.");
}
function updateCtMigrateMode(form, running) {
  const offline = form.elements.mode.value === "offline";
  $("#ct-migrate-stop").hidden = !offline;
  form.elements.allow_stop.disabled = !offline;
  form.elements.allow_stop.required = offline && running;
  $("#ct-migrate-mode-hint").textContent = offline
    ? "A running container stops and starts on the destination. Memory and live connections are not transferred."
    : "Live migration attempts to keep memory using CRIU. Selecting Live also allows a stop and restart if checkpoint or restore fails; in that case memory and live connections are lost.";
}
async function openCtMigrate(c) {
  state.cluster = await api("/cluster");
  const nodes = (state.cluster.members || []).filter(n => n.role === "compute" && n.name !== c.node && n.healthy && !n.maintenance);
  if (!nodes.length) throw new Error("No healthy destination node is available.");
  const form = $("#ct-migrate-form");
  form.elements.node.innerHTML = nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join("");
  openDialog("ct-migrate-dialog", {name:c.name});
  form.elements.mode.value = "live";
  form.elements.allow_stop.checked = false;
  form.onchange = () => updateCtMigrateMode(form, c.state === "running");
  updateCtMigrateMode(form, c.state === "running");
  $("#ct-migrate-summary").textContent = `${c.name} · ${c.node} · ${c.state}`;
}
handlers["ct-migrate-dialog"] = async (f, ctx) => {
  const result = await post(`/containers/${enc(ctx.name)}/migrate`, {node:f.get("node"), mode:f.get("mode") || "live", allow_stop:f.get("allow_stop") === "on"});
  toast(result.result || "Container migrated.");
  await refresh("containers");
  await load("jobs"); renderTasks();
};
async function openCtSchedule(name, policy = null) {
  await load("containers", "targets", "policies");
  const form = $("#ct-schedule-form");
  const names = [...new Set([...state.containers.map(c => c.name), ...(policy?.vms || [])])];
  form.elements.container.innerHTML = names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  form.elements.target.innerHTML = state.targets.map(t => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
  openDialog("ct-schedule-dialog", {policy}, {name:policy?.name || `${name.slice(0,48)}-nightly`, container:name, target:policy?.target, at:policy?.at || "02:00", frequency:policy?.every_hours ? "interval" : "daily", every_hours:policy?.every_hours || 24, keep_last:policy?.keep_last || 7});
  form.elements.name.readOnly = Boolean(policy);
  form.elements.allow_stop.checked = policy?.allow_stop || false;
  form.elements.enabled.checked = policy?.enabled ?? true;
  ctScheduleFrequency();
}
function ctScheduleFrequency() {
  const f = $("#ct-schedule-form").elements, daily = f.frequency.value === "daily";
  f.at.disabled = !daily;
  f.at.closest("label").hidden = !daily;
  f.every_hours.disabled = daily;
  f.every_hours.closest("label").hidden = daily;
}
$("#ct-schedule-form select[name=frequency]").addEventListener("change", ctScheduleFrequency);
handlers["ct-schedule-dialog"] = async (f, ctx) => {
  const body = {...(ctx.policy || {}), name:f.get("name"), target:f.get("target"), entity_type:"container", vms:[f.get("container")], mode:"active_full", verify_after_backup:false, allow_stop:f.get("allow_stop") === "on", enabled:f.get("enabled") === "on", keep_last:Number(f.get("keep_last")), at:f.get("frequency") === "daily" ? f.get("at") : null, every_hours:f.get("frequency") === "interval" ? Number(f.get("every_hours")) : null};
  if (ctx.policy) await api(`/backup/policies/${enc(ctx.policy.name)}`, {method:"PUT", body:JSON.stringify(body)});
  else await post("/backup/policies", body);
  toast("Container backup schedule saved. Manage it in Data protection → Schedules.");
  await load("policies");
};
async function openCtBackup(c) {
  await load("targets");
  if (!state.targets.length) throw new Error("Add a repository in Data protection first.");
  const form = $("#ct-backup-form");
  form.elements.target.innerHTML = state.targets.map(t => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
  openDialog("ct-backup-dialog", {name:c.name});
  form.elements.allow_stop.required = c.state === "running";
  $("#ct-backup-summary").textContent = `${c.name} · ${c.node} · ${c.state}`;
}
handlers["ct-backup-dialog"] = async (f, ctx) => {
  await post(`/containers/${enc(ctx.name)}/backup`, {target:f.get("target"), allow_stop:f.get("allow_stop") === "on"});
  toast(`Backup started for ${ctx.name}. Follow progress in Data protection → Jobs.`);
  await load("jobs");
  renderTasks();
};
function ctRestorePools() {
  const form = $("#ct-restore-form"), previous = form.elements.pool.value;
  const pools = state.pools.filter(p => p.active && p.node === form.elements.node.value);
  form.elements.pool.innerHTML = pools.map(p => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("");
  if (pools.some(p => p.name === previous)) form.elements.pool.value = previous;
}
async function openCtRestore(point, target) {
  await load("targets", "pools");
  state.cluster = await api("/cluster");
  const repository = state.targets.find(t => t.name === target);
  if (!repository) throw new Error("Repository no longer exists. Refresh and retry.");
  const members = (state.cluster?.members || []).filter(m => m.role === "compute" && m.healthy && !m.maintenance && (repository.shared || m.name === point.node));
  if (!members.length) throw new Error("The restore node is unavailable. Local repositories restore on their source node.");
  const form = $("#ct-restore-form");
  form.elements.node.innerHTML = members.map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join("");
  openDialog("ct-restore-dialog", {target, id:point.id, sourceNode:point.node}, {name:`${point.vm_name.slice(0,55)}-restore`});
  ctRestorePools();
  $("#ct-restore-summary").textContent = `${point.vm_name} · ${when(point.created)} · ${target}`;
}
$("#ct-restore-form select[name=node]").addEventListener("change", ctRestorePools);
handlers["ct-restore-dialog"] = async (f, ctx) => {
  await post(`/backup/targets/${enc(ctx.target)}/backups/${enc(ctx.id)}/restore-container`, {name:f.get("name").trim(), node:f.get("node"), pool:f.get("pool"), source_node:ctx.sourceNode || null});
  toast("Container restore started. It will remain stopped; follow progress in Jobs.");
  await load("jobs");
  renderTasks();
};
function openCtWizard(tpl = "debian-13") {
  const label = { app: "Applications", os: "Popular systems", image: "All images" };
  const opts = ["app", "os", "image"].map((k) => {
    const list = (state.ctTemplates || []).filter((t) => t.kind === k);
    return list.length ? `<optgroup label="${label[k]}">${list.map((t) => `<option value="${esc(t.id)}">${esc(t.title)}${t.kind === "app" ? ` (${esc(t.base)})` : ""}</option>`).join("")}</optgroup>` : "";
  }).join("");
  $("#ct-template").innerHTML = opts;
  openDialog("ct-dialog", null, { template: tpl });
  $("#ct-form").elements.nesting.checked = ["app:docker", "app:npm"].includes(tpl);
}
$("#ct-new").addEventListener("click", () => openCtWizard());
$("#ct-search").addEventListener("input", renderContainers);
$("#ct-catalog").addEventListener("click", (e) => { const b = e.target.closest("[data-tpl]"); if (b) openCtWizard(b.dataset.tpl); });
handlers["ct-dialog"] = async (f) => {
  const num = (k) => Number(f.get(k));
  const r = await post("/containers", {
    name: f.get("name").trim(), template: f.get("template"), node: f.get("node") || "",
    vcpus: num("vcpus"), memory_mb: num("memory_mb"), disk_gb: num("disk_gb"), pool: f.get("pool"),
    network: f.get("network"), ip: f.get("ip").trim() || "dhcp", gateway: f.get("gateway").trim(), dns: f.get("dns").trim(),
    password: f.get("password"), ssh_key: f.get("ssh_key").trim(), nesting: f.get("nesting") === "on",
    autostart: f.get("autostart") === "on", unprivileged: f.get("unprivileged") === "on",
  });
  toast(`Creating ${r.name} on ${r.node}… it appears when ready (see Tasks)`);
  setTimeout(() => load("jobs").then(renderTasks).catch(() => {}), 800);
};
let ctCurrent = null;
function ctSnaps(c) {
  $("#ct-snaps").innerHTML = (c.snapshots || []).length ? c.snapshots.map((s) => `<tr><td class="mono">${esc(s)}</td>
    <td class="actions" data-snap="${esc(s)}"><button type="button" class="small" data-sact="restore">Restore</button><button type="button" class="small danger" data-sact="delete">Delete</button></td></tr>`).join("")
    : `<tr><td class="muted">No snapshots.</td></tr>`;
}
$("#ct-rows").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const name = b.closest("[data-ct]").dataset.ct, act = b.dataset.act;
  const c = state.containers.find((x) => x.name === name);
  try {
    if (act === "migrate") { await openCtMigrate(c); return; }
    if (act === "migration-recover") {
      if (!confirm(`Recover interrupted migration for ${name}? The current owner resumes it; the other node remains fenced.`)) return;
      await post(`/containers/${enc(name)}/migration-recover`, {});
      await refresh("containers"); return;
    }
    if (act === "schedule") {
      await openCtSchedule(c.name);
      return;
    }
    if (act === "backup") {
      await openCtBackup(c);
      return;
    }
    if (act === "edit") {
      ctCurrent = name;
      openDialog("ct-edit-dialog", { name }, { vcpus: c.vcpus, memory_mb: c.memory_mb, disk_gb: c.disk_gb, description: c.description });
      $("#ct-edit-form").elements.autostart.checked = c.autostart;
      $("#ct-ha-enabled").checked = Boolean(c.ha_enabled);
      $("#ct-ha-enabled").disabled = !canManage("cluster");
      $("#ct-edit-form").elements.dns.value = c.dns || "";
      ctNics(c);
      ctStorage(c);
      $("#ct-edit-title").textContent = `Container ${name}`;
      ctSnaps(c);
      return;
    }
    if (act === "console") {
      ctCurrent = name;
      $("#ct-console-title").textContent = `Console: ${name}`;
      $("#ct-prompt").textContent = `root@${name} #`;
      $("#ct-out").textContent = "";
      $("#ct-console-dialog").showModal();
      $("#ct-input").focus();
      return;
    }
    if (act === "rename") {
      const to = (prompt(`New name for ${name} (small letters, digits and -; also its host name)`, name) || "").trim();
      if (!to || to === name) return;
      await post(`/containers/${enc(name)}/rename`, { name: to });
      toast(`${name} is now ${to}`);
      await refresh("containers");
      return;
    }
    if (act === "delete" && !confirm(`Delete container ${name} and its disk and snapshots? This cannot be undone.`)) return;
    b.disabled = true;
    if (act === "delete") await del(`/containers/${enc(name)}`);
    else await post(`/containers/${enc(name)}/action`, { action: act });
    toast(`${name}: ${act} done`);
    await refresh("containers");
  } catch (err) { fail(err); } finally { b.disabled = false; }
});
// Where a container's disk lives, and moving it to another pool (stopped only).
function ctStorage(c) {
  const pools = [...new Set(state.pools.filter((p) => p.active && p.node === c.node).map((p) => p.name))];
  $("#ct-storage").innerHTML = `<p>Pool <b>${esc(c.pool)}</b> · <span class="mono">${esc(c.path || "")}/rootfs.img</span></p>
    <label>Move to <select id="ct-move-pool">${pools.filter((p) => p !== c.pool).map((p) => `<option>${esc(p)}</option>`).join("")}</select></label>
    <button type="button" class="small" id="ct-move" ${c.state === "running" || pools.length < 2 ? "disabled" : ""}>Move</button>
    <span class="hint">${c.state === "running" ? "Stop the container to move it." : "Copies the disk, config and snapshots, then removes the old folder."}</span>`;
}
$("#ct-storage").addEventListener("click", async (e) => {
  if (!e.target.closest("#ct-move")) return;
  const name = ctCurrent, pool = $("#ct-move-pool").value;
  if (!pool || !confirm(`Move ${name} to pool ${pool}?`)) return;
  e.target.disabled = true;
  toast(`Moving ${name} to ${pool}… (see Tasks)`);
  setTimeout(() => load("jobs").catch(() => {}), 800);
  try {
    const c = await post(`/containers/${enc(name)}/move`, { pool });
    toast(`${name} is now on ${pool}`);
    await refresh("containers");
    ctStorage(c);
  } catch (err) { fail(err); } finally { e.target.disabled = false; }
});
// Network cards of a container: one row each, eth0 first.
function ctNicRow(card, i, now = "") {
  const nets = (state.networks || []).map((n) => n.name);
  if (!nets.includes(card.network)) nets.unshift(card.network);
  return `<tr data-nic>
    <td class="mono">eth${i}</td>
    <td><select data-f="network">${nets.map((n) => `<option ${n === card.network ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></td>
    <td><input data-f="ip" value="${esc(card.ip || "dhcp")}" placeholder="dhcp or 10.0.0.50/24" size="16"></td>
    <td><input data-f="gateway" value="${esc(card.gateway || "")}" placeholder="10.0.0.1" size="12"></td>
    <td class="mono"><input type="hidden" data-f="mac" value="${esc(card.mac || "")}">${esc(card.mac || "new")}</td>
    <td class="mono">${esc(now)}</td>
    <td>${i ? `<button type="button" class="small danger" data-nic-rm>Remove</button>` : ""}</td></tr>`;
}
function ctNics(c) {
  const cards = [{ network: c.network, ip: c.ip, gateway: c.gateway, mac: c.mac }, ...(c.nics || [])];
  $("#ct-nics").innerHTML = cards.map((card, i) => ctNicRow(card, i, (c.addresses || [])[i] || "")).join("");
  ctNicSnapshot = ctNicValues();
}
let ctNicSnapshot = "";
const ctNicValues = () => JSON.stringify([...$$("#ct-nics tr[data-nic]")].map((tr) =>
  Object.fromEntries($$("[data-f]", tr).map((el) => [el.dataset.f, el.value.trim()]))));
$("#ct-nic-add").addEventListener("click", () => {
  const n = $$("#ct-nics tr[data-nic]").length;
  if (n >= 8) { toast("A container has at most 8 network cards", true); return; }
  $("#ct-nics").insertAdjacentHTML("beforeend", ctNicRow({ network: "default", ip: "dhcp" }, n));
});
$("#ct-nics").addEventListener("click", (e) => {
  if (!e.target.closest("[data-nic-rm]")) return;
  e.target.closest("tr").remove();
  $$("#ct-nics tr[data-nic]").forEach((tr, i) => { tr.cells[0].textContent = `eth${i}`; });
});
handlers["ct-edit-dialog"] = async (f, ctx) => {
  const c = state.containers.find((x) => x.name === ctx.name) || {};
  const nics = ctNicValues(), dns = (f.get("dns") || "").trim();
  if (nics !== ctNicSnapshot || dns !== (c.dns || "")) {
    if (c.state === "running" && !confirm(`Apply the network to ${ctx.name}? It restarts for a few seconds.`)) return;
    await api(`/containers/${enc(ctx.name)}/network`, { method: "PUT", body: JSON.stringify({ nics: JSON.parse(nics), dns }) });
  }
  await patch(`/containers/${enc(ctx.name)}`, {
    vcpus: Number(f.get("vcpus")), memory_mb: Number(f.get("memory_mb")), disk_gb: Number(f.get("disk_gb")),
    autostart: f.get("autostart") === "on", description: f.get("description"),
  });
  const haEnabled = f.get("ha_enabled") === "on";
  if (canManage("cluster") && haEnabled !== Boolean(c.ha_enabled)) {
    await api(`/containers/${enc(ctx.name)}/ha`, {method: "PUT", body: JSON.stringify({enabled: haEnabled})});
  }
  toast(`${ctx.name} changed`);
  await refresh("containers");
};
$("#ct-snap-take").addEventListener("click", async () => {
  const name = ctCurrent, snap = prompt("Snapshot name", snapshotName(name));
  if (!snap) return;
  try { const c = await post(`/containers/${enc(name)}/snapshots`, { name: snap.trim() }); ctSnaps(c); toast(`Snapshot taken: ${snap}`); } catch (err) { fail(err); }
});
$("#ct-snaps").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-sact]");
  if (!b) return;
  const snap = b.closest("[data-snap]").dataset.snap, name = ctCurrent;
  if (!confirm(b.dataset.sact === "restore" ? `Restore ${name} to ${snap}? Changes since then are lost.` : `Delete snapshot ${snap}?`)) return;
  try {
    const c = b.dataset.sact === "restore" ? await post(`/containers/${enc(name)}/snapshots/${enc(snap)}/restore`) : await del(`/containers/${enc(name)}/snapshots/${enc(snap)}`);
    ctSnaps(c); toast(b.dataset.sact === "restore" ? `${name} restored to ${snap}` : `Snapshot ${snap} deleted`);
  } catch (err) { fail(err); }
});
$("#ct-input").addEventListener("keydown", async (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  const input = e.target, line = input.value.trim(), out = $("#ct-out");
  if (!line) return;
  input.value = "";
  if (line === "clear") { out.textContent = ""; return; }
  out.textContent += `root@${ctCurrent} # ${line}\n`;
  input.disabled = true;
  // The command runs on the node in the background (up to an hour); its output shows as
  // it comes, and Stop (or Ctrl+C) ends it.
  const name = ctCurrent, scroll = () => { out.parentElement.scrollTop = out.parentElement.scrollHeight; };
  let text = "";
  try {
    const { job } = await post(`/containers/${enc(name)}/jobs`, { command: line });
    ctJob = { name, job };
    $("#ct-stop").hidden = false;
    let offset = 0;
    for (;;) {
      const r = await api(`/containers/${enc(name)}/jobs/${job}?offset=${offset}`);
      offset = r.offset;
      if (r.output) { text += r.output; out.textContent += r.output; scroll(); }
      if (r.done) { out.textContent += (text && !text.endsWith("\n") ? "\n" : "") + (r.code ? `(exit ${r.code})\n` : ""); break; }
      if (!$("#ct-console-dialog").open) { await del(`/containers/${enc(name)}/jobs/${job}`).catch(() => {}); break; }
      await new Promise((res) => setTimeout(res, 700));
    }
  } catch (err) { out.textContent += `${err.message || err}\n`; }
  finally { ctJob = null; $("#ct-stop").hidden = true; input.disabled = false; input.focus(); scroll(); }
});
let ctJob = null;
const ctStop = () => { if (ctJob) del(`/containers/${enc(ctJob.name)}/jobs/${ctJob.job}`).catch(() => {}); };
$("#ct-stop").addEventListener("click", ctStop);
$("#ct-console-dialog").addEventListener("keydown", (e) => { if (e.ctrlKey && e.key === "c" && ctJob) { e.preventDefault(); ctStop(); } });

handlers["backup-operation-dialog"] = async (f, ctx) => {
  const operation = f.get("operation"), body = { node: ctx.node || null };
  if (operation !== "synthetic") {
    if (!f.get("destination")) throw new Error("Choose a destination repository.");
    body.destination = f.get("destination");
    body.archive = operation === "archive";
  }
  await post(`/backup/targets/${enc(ctx.target)}/backups/${enc(ctx.id)}/${operation === "synthetic" ? "synthetic" : "copy"}`, body);
  toast("Repository job started. Follow it under Backup → Jobs.");
  await refresh("backup");
};

$("#jobs").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-backup-report]");
  if (!button) return;
  try {
    const report = await api(`/backup/jobs/${enc(button.dataset.backupReport)}/report`);
    const ratios = v => v == null ? "Not applicable" : `${Number(v).toFixed(2)}×`;
    $("#backup-report-details").innerHTML = [
      ["VM", report.vm], ["Method", report.backup_method], ["Repository", report.repository],
      ["Duration", `${Number(report.duration_seconds).toFixed(1)} seconds`],
      ["Logical disks", size(report.logical_size_bytes)], ["Read from VM", size(report.source_read_bytes)],
      ["Duplicate data avoided", size(report.dedup_saved_bytes)],
      ["Deduplication", report.all_data_reused ? "All processed data reused" : ratios(report.dedup_ratio)],
      ["New unique data", size(report.new_data_bytes)], ["New stored payload", size(report.stored_bytes)],
      ["Compression", ratios(report.compression_ratio)], ["Codec", report.compression_codec],
      ["Combined data reduction", ratios(report.combined_ratio)], ["Notes", report.note || "—"],
    ].map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    $("#backup-report-basis").textContent = report.metric_basis;
    $("#backup-report-csv").href = `/api/v1/backup/jobs/${enc(report.job_id)}/report?format=csv`;
    $("#backup-report-json").href = `/api/v1/backup/jobs/${enc(report.job_id)}/report?format=json`;
    $("#backup-report-json").download = `backup-job-${report.job_id}.json`;
    $("#backup-report-dialog").showModal();
  } catch (err) { fail(err); }
});

// Backup repository: edit how long new restore points stay locked.
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-immutability]");
  if (!b) return;
  const days = prompt(`Lock new restore points on ${b.dataset.immutability} for how many days? (0 = no lock)\nPoints already locked stay locked until their date.`, b.dataset.days);
  if (days === null) return;
  try {
    await api(`/backup/targets/${enc(b.dataset.immutability)}/immutability`, { method: "PUT", body: JSON.stringify({ days: Number(days) }) });
    toast(`Immutability set to ${Number(days)} days`);
    refresh(currentPage());
  } catch (err) { fail(err); }
});

// Admin password reset: type one or generate one, see it, copy it.
function genPassword(length = 16) {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#%+=";
  const bytes = crypto.getRandomValues(new Uint32Array(length));
  return Array.from(bytes, (b) => chars[b % chars.length]).join("");
}
function askNewPassword(user) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "pw-reset";
    dlg.innerHTML = `<form method="dialog"><h3>Reset password · ${esc(user)}</h3>
      <p class="hint">Type a temporary password or generate one. ${esc(user)} must change it at the next sign-in.</p>
      <div class="pw-row"><input name="pw" type="text" minlength="8" autocomplete="off" spellcheck="false" placeholder="8+ characters" required>
      <button type="button" data-gen>Generate</button><button type="button" data-copy>Copy</button></div>
      <div class="pw-actions"><button value="cancel" formnovalidate>Cancel</button><button value="ok" class="primary">Set password</button></div></form>`;
    document.body.append(dlg);
    const input = dlg.querySelector("input");
    dlg.querySelector("[data-gen]").onclick = () => { input.value = genPassword(); input.select(); };
    dlg.querySelector("[data-copy]").onclick = async () => { try { await navigator.clipboard.writeText(input.value); toast("Password copied"); } catch { input.select(); } };
    dlg.addEventListener("close", () => { resolve(dlg.returnValue === "ok" ? input.value : ""); dlg.remove(); });
    dlg.showModal();
    input.focus();
  });
}

$("#bp-new-container-schedule").addEventListener("click", async () => {
  try {
    await load("containers");
    if (!state.containers.length) {toast("No containers are available to schedule.", true); return;}
    await openCtSchedule(state.containers[0].name);
  } catch (err) {fail(err);}
});
