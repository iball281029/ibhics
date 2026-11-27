"use strict";

async function renderAccountSecurity() {
  if (!state.me) {
    $("#twofa-status").textContent = "Enable sign-in on this manager to use two-factor authentication.";
    $("#twofa-setup").hidden = true;
    return;
  }
  const security = await api("/auth/2fa");
  state.security = security;
  $("#twofa-status").textContent = `${security.enabled ? `Enabled · ${security.recovery_remaining} recovery codes remaining.` : "Not enabled."} ${security.required ? "Your administrator requires 2FA. Enroll here before using the console." : "Add a second step to protect your account."}`;
  $("#twofa-setup").hidden = security.enabled;
  $("#twofa-disable").hidden = !security.enabled || security.required;
  $("#twofa-required").checked = security.required_for_all ?? security.required;
}

async function renderSettings() {
  const [data, roleData] = await Promise.all([api("/settings"), api("/roles")]);
  state.applicationJobs = data.jobs || [];
  state.settingsRoles = roleData.roles;
  $$("[data-settings-permission]").forEach(el => { el.hidden = !canManage(el.dataset.settingsPermission); });
  $("#settings-version").textContent = $("#about dd:nth-of-type(2)")?.textContent || "—";
  $("#settings-users-tab").hidden = !canManage("users");
  $("#subscription-cards").innerHTML = [
    ["update", "Update", "New versions and security patches released on or before the expiry date are included. After expiry, your system keeps running normally. Versions released after expiry cannot be installed."],
    ["support", "Support", "Open support cases while your subscription is active. Contact channels and response times follow your contract. Without Support, opening a case is locked."],
  ].map(([key, label, terms]) => {
    const sub = data.subscription[key];
    return `<article class="st-panel"><div class="toolbar"><h3>${label}</h3><span class="spacer"></span><span class="st-status ${esc(sub.status)}">${esc(sub.status)}</span></div><b class="st-expiry">${sub.until ? esc(sub.until) : "No subscription"}</b><p class="hint">${sub.days == null ? "Upload a signed subscription to activate." : `${sub.days} days remaining · ${esc(data.subscription.customer)}`}</p><p>${terms}</p>${key === "support" ? '<button type="button" data-st-support>Open support case</button>' : '<button type="button" data-st-updates>View updates</button>'}</article>`;
  }).join("");
  const active = data.subscription.support.status === "Active";
  const permitted = canManage("subscription");
  $("#support-fields").disabled = !active || !permitted || !state.me;
  $("[data-st-support]").disabled = !active || !permitted || !state.me;
  $("#support-reason").textContent = !active ? "Locked: an active Support subscription is required." : !permitted ? "Your role cannot open support cases." : !state.me ? "Sign in to open a support case." : data.support_delivery_configured ? "Cases are saved locally, then sent to your configured support endpoint." : "Cases are saved locally. No support endpoint is configured; nothing will be sent.";
  $("#subscription-trust").textContent = data.verification_available ? "Signatures are checked with the deployment's trusted verifier." : "Signed uploads are not enabled on this installation. Ask your administrator to configure trusted verification.";
  $("#subscription-upload button").disabled = !data.verification_available;
  $("#subscription-remove").disabled = !data.subscription.customer;
  $("#update-upload button").disabled = !data.verification_available;
  $("#update-availability").textContent = data.installation_available ? "A verified update is ready for installation when its release date is covered by your Update subscription." : "Application updates are not enabled on this installation. Ask your administrator to configure application updates before installing.";
  $("#update-check-status").textContent = data.check_error || (data.last_check ? `Last checked ${data.last_check}` : "No channel check has run.");
  $("#update-install").disabled = !data.installation_available || !data.staged;
  $("#update-rollback").disabled = !data.installation_available || !data.previous_available;
  $("#update-check").disabled = !data.verification_available || !data.update_channel_configured;
  $("#update-preview").innerHTML = data.staged ? `<h3>Ready for review: ${esc(data.staged.version)}</h3><p>Released ${esc(data.staged.released)}</p><pre>${esc(data.staged.notes || "No release notes provided.")}</pre>` : '<p class="hint">No verified update is staged.</p>';
  const config = $("#settings-config");
  if (!config.contains(document.activeElement) && !config.dataset.dirty) {
    config.elements.stable.value = data.channels?.stable || "";
    config.elements.beta.value = data.channels?.beta || "";
    config.elements.channel.value = data.channel;
    config.elements.support_endpoint.value = data.support_endpoint || "";
    config.elements.auto_install.checked = data.auto_install;
    config.elements.auto_install.disabled = !data.installation_available;
  }
  $("#settings-update-jobs").innerHTML = data.jobs.length ? data.jobs.slice().reverse().map(j => `<div class="st-job"><b>${esc(j.kind)}</b><span>${esc(j.state)}</span><small>${esc(j.nodes.join(" → "))} ${esc(j.detail)}</small></div>`).join("") : '<p class="hint">No application update jobs.</p>';
  $("#settings-cases").innerHTML = (data.cases || []).map(c => `<div class="st-job"><b>${esc(c.subject)}</b><span>${esc(c.severity)}</span><small>${esc(c.id)} · ${esc(c.delivery)}</small></div>`).join("") || '<p class="hint">No saved cases.</p>';
  const categories = Object.entries(roleData.categories);
  $("#role-matrix").innerHTML = `<table><thead><tr><th>Role</th>${categories.map(([,name]) => `<th>${esc(name)}</th>`).join("")}</tr></thead><tbody>${Object.entries(roleData.roles).map(([name, permissions]) => `<tr><th>${esc(name)}</th>${categories.map(([key]) => `<td aria-label="${permissions.includes(key) ? "Allowed" : "View only"}">${permissions.includes(key) ? "✓" : "—"}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  if (!$("#role-permissions").children.length) $("#role-permissions").innerHTML = categories.map(([key,name]) => `<label class="inline"><input type="checkbox" name="permission" value="${esc(key)}">${esc(name)}</label>`).join("");
  const roleSelect = $("#user-form select[name=role]"), selected = roleSelect.value;
  roleSelect.innerHTML = Object.keys(roleData.roles).map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join("");
  roleSelect.value = selected;
  await renderNotificationConfig();
  await renderAccountSecurity();
  await renderApprovals();
}

$("#subscription-cards").addEventListener("click", e => {
  if (e.target.closest("[data-st-support]")) selectTab("system-tabs", "support");
  if (e.target.closest("[data-st-updates]")) selectTab("system-tabs", "updates");
});
function settingsForm(id, action) {
  $(id).addEventListener("submit", async e => {
    e.preventDefault();
    const button = e.submitter;
    if (button) button.disabled = true;
    try { await action(new FormData(e.target), e.target); }
    catch (err) { fail(err); }
    finally { if (button) button.disabled = false; }
  });
}
async function fileBase64(file, limit) {
  if (file.size > limit) throw new Error(`File is too large (limit ${size(limit)}).`);
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}
async function jsonFile(file, limit = 65536) {
  if (file.size > limit) throw new Error(`Signed JSON files must be under ${limit / 1024} KiB.`);
  return JSON.parse(await file.text());
}
settingsForm("#subscription-upload", async (f, form) => {
  await post("/settings/subscription", await jsonFile(f.get("license"), 128 * 1024));
  window.dispatchEvent(new Event("ibhics-license-changed"));
  form.reset(); await renderSettings(); toast("Subscription verified and saved");
});
settingsForm("#update-upload", async (f, form) => {
  await post("/settings/updates/stage", { envelope: await jsonFile(f.get("manifest")), package: await fileBase64(f.get("package"), 32 * 1024 * 1024) });
  form.reset(); await renderSettings(); toast("Package signature and checksum verified");
});
$("#settings-config").addEventListener("input", e => { e.currentTarget.dataset.dirty = "true"; });
settingsForm("#settings-config", async (f, form) => {
  await api("/settings/config", { method: "PUT", body: JSON.stringify({ channels: {stable:f.get("stable"), beta:f.get("beta")}, channel:f.get("channel"), support_endpoint:f.get("support_endpoint"), auto_install:f.has("auto_install") }) });
  delete form.dataset.dirty; toast("Preferences saved"); await renderSettings();
});
settingsForm("#support-case", async (f, form) => {
  const attachment = f.get("diagnostic");
  const result = await post("/settings/support/cases", { subject:f.get("subject"), severity:f.get("severity"), details:f.get("details"), diagnostic:attachment?.size ? await fileBase64(attachment, 4 * 1024 * 1024) : null });
  $("#support-result").textContent = `Case ${result.id}: ${result.delivery === "sent" ? "saved locally and sent to support" : result.delivery === "local" ? "saved locally; no endpoint configured" : "saved locally; sending failed"}.`;
  form.reset(); await renderSettings();
});
for (const [id, endpoint] of [["#update-check","check"],["#update-install","install"],["#update-rollback","rollback"]]) {
  $(id).addEventListener("click", async e => {
    if (endpoint !== "check" && !confirm(`${endpoint === "install" ? "Install the verified application update" : "Restore the previous application version"} on every node?`)) return;
    e.target.disabled = true;
    try { await post(`/settings/updates/${endpoint}`); await renderSettings(); }
    catch (err) { fail(err); }
    finally { await renderSettings().catch(() => {}); }
  });
}
settingsForm("#custom-role", async (f, form) => {
  const name = f.get("name");
  if (state.settingsRoles?.[name] && !confirm(`Replace the permissions of custom role ${name}?`)) return;
  await api(`/roles/${enc(name)}`, {method:"PUT", body:JSON.stringify({permissions:f.getAll("permission")})});
  form.reset(); await renderSettings(); renderUsers(); toast("Role saved");
});
settingsForm("#twofa-setup", async (f, form) => {
  const data = await post("/auth/2fa/setup", {password:f.get("password")});
  form.reset(); $("#twofa-secret").value = data.secret; $("#twofa-uri").value = data.uri;
  $("#twofa-enrollment").hidden = false;
});
settingsForm("#twofa-confirm", async (f, form) => {
  const data = await post("/auth/2fa/confirm", {code:f.get("code")});
  form.reset(); $("#twofa-secret").value = ""; $("#twofa-uri").value = "";
  $("#twofa-enrollment").hidden = true; $("#twofa-setup").hidden = true;
  $("#twofa-codes").textContent = data.recovery_codes.join("\n");
  $("#twofa-recovery").hidden = false;
  $("#twofa-status").textContent = "2FA enabled. Save your recovery codes, then sign in again.";
});
settingsForm("#twofa-disable", async (f, form) => {
  await post("/auth/2fa/disable", {password:f.get("password"),code:f.get("code")});
  form.reset(); location.href = "/login.html";
});
$("#twofa-required").addEventListener("change", async e => {
  try {
    await api("/security/2fa", {method:"PUT", body:JSON.stringify({require_2fa:e.target.checked})});
    await renderAccountSecurity();
  } catch (err) { e.target.checked = !e.target.checked; fail(err); }
});

// A password input keeps administrator reauthentication out of visible prompt text.
function openTwofaReset(name) {
  let dialog = $("#settings-2fa-reset");
  if (!dialog) {
    dialog = document.createElement("dialog"); dialog.id = "settings-2fa-reset";
    dialog.innerHTML = '<form><header><h3>Reset user 2FA</h3></header><div class="body"><p id="settings-reset-name"></p><label>Your password<input name="password" type="password" autocomplete="current-password" required></label><label>Your authentication code (if enabled)<input name="code" autocomplete="one-time-code"></label></div><footer><button type="button" data-close>Cancel</button><button class="danger">Reset 2FA</button></footer></form>';
    document.body.append(dialog);
    $("[data-close]", dialog).onclick = () => dialog.close();
    $("form", dialog).onsubmit = async e => {
      e.preventDefault(); const f = new FormData(e.target);
      try { await post(`/users/${enc(dialog.dataset.user)}/2fa/reset`, {password:f.get("password"),code:f.get("code")}); dialog.close(); await refresh("system"); }
      catch (err) { fail(err); }
      finally { e.target.reset(); }
    };
    dialog.onclose = () => $("form", dialog).reset();
  }
  dialog.dataset.user = name; $("#settings-reset-name").textContent = `Reset 2FA for ${name}? Their sessions will end. If 2FA is required, they must enroll again.`;
  dialog.showModal();
}

async function renderApprovals() {
  if (!state.me) return;
  const data = await api("/approvals");
  state.approvalJobs = data.requests;
  renderTasks();
  $("#approvals-enabled").checked = data.enabled;
  $("#approvals-list").innerHTML = data.requests.slice().reverse().map(j => `<article class="st-panel"><b>${esc(j.method)} ${esc(j.path)}</b><pre>${esc(j.query ? "?" + j.query : "")} ${esc(JSON.stringify({changes:j.changes || {}, release:j.context}))}</pre><p>${esc(j.requester)} · ${esc(j.state)}${j.approver ? " · " + esc(j.approver) : ""}</p>${j.state === "pending" ? '<span class="badge">รออนุมัติจาก admin อีกคน</span>' : ""}${isAdmin() && j.requester !== state.me.name && j.state === "pending" ? `<button data-approval="${esc(j.id)}" data-decision="true">Approve</button><button data-approval="${esc(j.id)}" data-decision="false">Reject</button>` : ""}</article>`).join("") || '<p class="hint">No approval requests.</p>';
}
$("#approvals-refresh").onclick = () => renderApprovals().catch(fail);
$("#approvals-enabled").onchange = async e => {
  try { await api("/security/approvals", {method:"PUT", body:JSON.stringify({enabled:e.target.checked})}); }
  catch (err) { fail(err); }
  finally { await renderApprovals(); }
};
$("#approvals-list").onclick = async e => {
  const button = e.target.closest("[data-approval]");
  if (!button) return;
  button.disabled = true;
  try {
    const result = await post(`/approvals/${enc(button.dataset.approval)}/decision`, {approve:button.dataset.decision === "true"});
    toast(`Approval ${result.state}`, result.state === "failed");
  } catch (err) { fail(err); }
  finally { await renderApprovals(); }
};


let notificationCatalog = [];
let notificationLineEvents = [];
function renderEventPreferences(catalog, preferences) {
  notificationCatalog = catalog;
  let category = "";
  $("#notification-events").innerHTML = catalog.map(event => {
    const pref = preferences[event.id];
    let heading = "";
    if (event.category !== category) {
      category = event.category;
      heading = `<tr><th colspan="5" scope="colgroup">${esc(category)}</th></tr>`;
    }
    return heading + `<tr data-event-id="${esc(event.id)}"><td><b>${esc(event.id)}</b>${event.hooked ? "" : ' <span class="hint">coming soon</span>'}<br><small>${esc(event.description)}</small></td><td><select data-event-level aria-label="${esc(event.id)} level">${["critical", "warning", "info"].map(level => `<option ${pref.level === level ? "selected" : ""}>${level}</option>`).join("")}</select></td>${["in_app", "email", "line"].map(channel => `<td><input type="checkbox" role="switch" data-channel="${channel}" aria-label="${esc(event.id)} ${channel}" ${(channel === "line" ? notificationLineEvents.includes(event.id) : pref[channel]) ? "checked" : ""}></td>`).join("")}</tr>`;
  }).join("");
}
function eventPreferences() {
  return Object.fromEntries(Array.from(document.querySelectorAll("#notification-events tr[data-event-id]")).map(row => [row.dataset.eventId, {
    level:row.querySelector("[data-event-level]").value,
    in_app:row.querySelector('[data-channel="in_app"]').checked,
    email:row.querySelector('[data-channel="email"]').checked,
  }]));
}
function selectedLineEvents() {
  return Array.from(document.querySelectorAll('#notification-events tr[data-event-id]'))
    .filter(row => row.querySelector('[data-channel="line"]').checked).map(row => row.dataset.eventId);
}
$("#notification-defaults").onclick = () => {
  notificationLineEvents = selectedLineEvents();
  renderEventPreferences(notificationCatalog, Object.fromEntries(notificationCatalog.map(event => [event.id, {level:event.level, in_app:event.in_app, email:event.email}])));
  $("#notification-config").dataset.dirty = "true";
};
$("#notification-critical").onclick = () => {
  notificationLineEvents = selectedLineEvents();
  const preferences = eventPreferences();
  for (const pref of Object.values(preferences)) if (pref.level === "critical") { pref.in_app = true; pref.email = true; }
  renderEventPreferences(notificationCatalog, preferences);
  $("#notification-config").dataset.dirty = "true";
};

async function renderNotificationConfig() {
  if (!state.me || !isAdmin()) return;
  const form = $("#notification-config");
  if (form.dataset.dirty || form.contains(document.activeElement)) return;
  const config = await api("/settings/notifications");
  for (const key of ["host", "port", "security", "username", "from", "summary_at"]) form.elements[key].value = config[key];
  form.elements.password.value = "";
  form.elements.password.placeholder = config.password || "No password saved";
  form.elements.recipients.value = config.recipients.join(", ");
  form.elements.daily_summary.checked = config.daily_summary;
  notificationLineEvents = config.line.events;
  form.elements.line_enabled.checked = config.line.enabled;
  form.elements.line_token.value = "";
  form.elements.line_token.placeholder = config.line.token || "No token saved";
  form.elements.line_recipient.value = config.line.recipient;
  renderEventPreferences(config.catalog, config.preferences);

}
$("#notification-config").addEventListener("input", e => { e.currentTarget.dataset.dirty = "true"; });
settingsForm("#notification-config", async (f, form) => {
  const body = Object.fromEntries(["host", "security", "username", "from", "summary_at"].map(k => [k, f.get(k)]));
  Object.assign(body, {port:Number(f.get("port")), recipients:f.get("recipients").split(/[,;\s]+/).filter(Boolean), preferences:eventPreferences(), daily_summary:f.has("daily_summary")});
  if (f.has("clear_password")) body.password = "";
  else if (f.get("password")) body.password = f.get("password");
  body.line = {
    enabled:f.has("line_enabled"),
    recipient:f.get("line_recipient").trim(),
    events:selectedLineEvents(),
  };
  if (f.has("line_clear_token")) body.line.token = "";
  else if (f.get("line_token")) body.line.token = f.get("line_token");
  await api("/settings/notifications", {method:"PUT", body:JSON.stringify(body)});
  form.elements.line_token.value = "";
  form.elements.line_clear_token.checked = false;
  form.elements.password.value = "";
  form.elements.clear_password.checked = false;
  delete form.dataset.dirty;
  $("#notification-result").textContent = "Settings saved";
  await renderNotificationConfig();
});
$("#notification-test").onclick = async e => {
  e.target.disabled = true;
  $("#notification-result").textContent = "Sending with saved settings…";
  try {
    const result = await post("/settings/notifications/test");
    $("#notification-result").textContent = result.message;
  } catch (err) { $("#notification-result").textContent = err.message; }
  finally { e.target.disabled = false; }
};

$("#notification-test-line").onclick = async e => {
  e.target.disabled = true;
  $("#notification-result").textContent = "Sending LINE with saved settings…";
  try {
    const result = await post("/settings/notifications/test-line");
    $("#notification-result").textContent = result.message;
  } catch (err) { $("#notification-result").textContent = err.message; }
  finally { e.target.disabled = false; }
};

$("#subscription-remove").addEventListener("click", async () => {
  if (!confirm("Remove this installation's license? Support and Update entitlements will be removed. Running VMs will continue. You can upload a signed license again.")) return;
  const button = $("#subscription-remove"); button.disabled = true;
  try {
    await del("/settings/subscription");
    window.dispatchEvent(new Event("ibhics-license-changed"));
    await renderSettings(); toast("License removed. You can upload a new signed license.");
  } catch (err) {fail(err); button.disabled = false;}
});
