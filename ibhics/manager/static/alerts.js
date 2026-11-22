/* Alert history and details. Read status never changes the underlying fault. */
window.Alerts = (() => {
  let events = [], loaded = false, pending = null;
  const selectedId = () => {
    try { return decodeURIComponent(location.hash.startsWith("#alerts/") ? location.hash.slice(8) : ""); }
    catch { return ""; }
  };
  function destination(event) {
    const kind = event.event_id || "", category = (event.category || "").toLowerCase();
    const [prefix, id] = (event.key || "").split(":");
    if ((prefix === "hardware" || prefix === "node") && id) return {hash:`node/${enc(id)}`, label:"Open node"};
    if (prefix === "vm" && id) return {hash:`vm/${enc(id)}`, label:"Open virtual machine"};
    if (kind.startsWith("replication.") || prefix === "replication") return {hash:"migrate", tabs:"migrate-tabs", tab:"replications", label:"Open replication"};
    if (kind.startsWith("import.") || kind.startsWith("p2v.")) return {hash:"migrate", label:"Open migration"};
    if (kind.startsWith("hardware.") || category.includes("hardware") || category === "node" || category === "quorum") return {hash:"cluster", label:"Open cluster"};
    if (kind.startsWith("backup.") || kind.startsWith("restore.") || ["backup", "rpo", "anomaly", "verify"].includes(category)) return {hash:"backup", label:"Open data protection"};
    if (kind.startsWith("storage.") || kind.startsWith("vstore.") || category === "capacity") return {hash:"storage/virtual/mirror", label:"Open storage"};
    if (category === "email" || kind.startsWith("email.")) return isAdmin() ? {hash:"system", tabs:"system-tabs", tab:"notifications", label:"Open email settings"} : null;
    if (kind.startsWith("subscription.") || category === "subscription") return {hash:"system", tabs:"system-tabs", tab:"about", label:"Open subscriptions"};
    if (kind.startsWith("approval.")) return {hash:"system", tabs:"system-tabs", tab:"approvals", label:"Open approvals"};
    if (kind.startsWith("auth.")) return {hash:"system", tabs:"system-tabs", tab:isAdmin() ? "audit" : "account", label:"Open account security"};
    return null;
  }
  function filtered() {
    const query = $("#alerts-search").value.trim().toLowerCase();
    const level = $("#alerts-level").value, status = $("#alerts-read-filter").value, category = $("#alerts-category").value;
    return events.filter(e => (!level || e.level === level) && (!category || e.category === category)
      && (!status || (status === "read" ? e.read : !e.read))
      && (!query || [e.message, e.category, e.event_id, e.key].some(v => String(v || "").toLowerCase().includes(query))));
  }
  function renderDetail() {
    const id = selectedId(), box = $("#alert-detail");
    box.hidden = !id;
    if (!id) { box.innerHTML = ""; return; }
    const event = events.find(e => e.id === id);
    if (!event) {
      box.innerHTML = '<h2>Alert unavailable</h2><p>This alert is no longer in the retained history.</p><a href="#alerts">Back to all alerts</a>';
      return;
    }
    const target = destination(event);
    box.innerHTML = `<div class="alerts-detail-head"><h2>Alert details</h2><a href="#alerts" aria-label="Close alert details">Close</a></div>
      <span class="alert-severity ${esc(event.level)}">${esc(event.level)}</span>
      <h3>${esc(event.category)}</h3><p class="alert-message">${esc(event.message)}</p>
      <dl><dt>Time</dt><dd>${esc(when(event.time))}</dd><dt>Event</dt><dd>${esc(event.event_id || event.category)}</dd><dt>Read status</dt><dd>${event.read ? "Read" : "Unread"}</dd></dl>
      <p class="hint">Marking an alert as read does not resolve the underlying issue. Open the related page to check its current status.</p>
      <div class="actions">${target ? '<button type="button" class="primary" data-alert-related>View related page</button>' : ""}${!event.read ? `<button type="button" data-alert-read="${esc(event.id)}">Mark as read</button>` : ""}</div>`;
    if (target) $("[data-alert-related]", box).textContent = target.label;
  }
  function render() {
    if (currentPage() !== "alerts") return;
    const category = $("#alerts-category"), previous = category.value;
    const categories = [...new Set(events.map(e => e.category))].sort();
    category.innerHTML = '<option value="">All categories</option>' + categories.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
    category.value = categories.includes(previous) ? previous : "";
    const rows = filtered(), unread = events.filter(e => !e.read).length;
    $("#alerts-summary").textContent = `${events.length} retained · ${unread} unread · ${events.filter(e => e.level === "critical").length} critical`;
    $("#alerts-result-count").textContent = `${rows.length} of ${events.length} alerts`;
    $("#alerts-read-visible").disabled = !loaded || !rows.some(e => !e.read);
    const list = $("#alerts-list"), scroll = list.scrollTop;
    list.innerHTML = rows.map(e => `<article class="alert-row ${e.read ? "is-read" : ""} ${selectedId() === e.id ? "is-selected" : ""}">
      <a href="#alerts/${enc(e.id)}" class="alert-row-link"><span class="alert-severity ${esc(e.level)}">${esc(e.level)}</span><span class="alert-row-body"><b>${esc(e.category)}</b><span>${esc(e.message)}</span><small>${esc(when(e.time))} · ${e.read ? "Read" : "Unread"}</small></span><span aria-hidden="true">→</span></a>
      ${!e.read ? `<button type="button" class="small" data-alert-read="${esc(e.id)}">Mark as read</button>` : ""}</article>`).join("")
      || `<p class="alerts-empty">${loaded ? events.length ? "No alerts match these filters." : "No alerts recorded yet." : "Loading alerts…"}</p>`;
    list.scrollTop = scroll;
    renderDetail();
  }
  function update(data) {
    events = data.events;
    loaded = true;
    $("#notification-count").textContent = data.unread;
    $("#notifications-read").disabled = !data.unread;
    $("#notification-list").innerHTML = events.slice(0, 10).map(e => `<a href="#alerts/${enc(e.id)}" class="notification-item notification-link ${e.read ? "is-read" : ""} ${esc(e.level)}"><b>${esc(e.level)} · ${esc(e.category)}</b><p>${esc(e.message)}</p><small>${esc(when(e.time))}</small><span class="notification-open">View details →</span></a>`).join("") || '<p class="hint">No notifications</p>';
    render();
  }
  async function refresh() {
    if (!pending) pending = api("/notifications").then(data => {
      update(data);
      $("#alerts-error").textContent = "";
    }).catch(err => {
      $("#alerts-error").textContent = "Alerts could not be refreshed. Showing the last available history. Try Refresh again.";
      throw err;
    }).finally(() => { pending = null; });
    return pending;
  }
  async function markRead(ids) {
    if (!ids.length) return;
    try { update(await post("/notifications/read", {ids})); }
    catch (err) { fail(err); }
  }
  $("#notifications-read").onclick = () => markRead(events.filter(e => !e.read).map(e => e.id));
  $("#alerts-read-visible").onclick = () => markRead(filtered().filter(e => !e.read).map(e => e.id));
  $("#alerts-refresh").onclick = () => refresh().catch(fail);
  for (const id of ["#alerts-search", "#alerts-level", "#alerts-read-filter", "#alerts-category"]) $(id).addEventListener(id === "#alerts-search" ? "input" : "change", render);
  $("#page-alerts").addEventListener("click", e => {
    const read = e.target.closest("[data-alert-read]");
    if (read) markRead([read.dataset.alertRead]);
    if (e.target.closest("[data-alert-related]")) {
      const target = destination(events.find(event => event.id === selectedId()));
      if (!target) return;
      location.hash = target.hash;
      if (target.tabs) selectTab(target.tabs, target.tab);
    }
  });
  $(".notification-menu").addEventListener("click", e => {
    if (e.target.closest('a[href^="#alerts"]')) $(".notification-menu").open = false;
  });
  setTimeout(() => refresh().catch(() => {}), 1000);
  setInterval(() => refresh().catch(() => {}), 30000);
  return {refresh, render};
})();
