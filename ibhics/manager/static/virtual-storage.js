/* Virtual Storage: physical disk selection, server review and durable creation jobs. */
window.VirtualStorageUI = (() => {
  const dialog = $("#virtual-storage-dialog"), form = $("#virtual-storage-form");
  let latest = null, inspected = [], receipt = null, timer = null, rendering = false;
  let desiredCacheMode = null, activeTab = "overview", replicationView = "panels";
  function selectTab(name) {
    if (!["overview", "replication", "cache", "capacity", "files", "settings"].includes(name)) name = "overview";
    activeTab = name;
    $$("[data-vs-tab]").forEach((button) => {
      const selected = button.dataset.vsTab === name;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    $$("[data-vs-panel]").forEach((panel) => { panel.hidden = panel.dataset.vsPanel !== name; });
  }
  $("#vs-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-vs-tab]");
    if (button) {
      selectTab(button.dataset.vsTab);
      history.replaceState(null, "", `#storage/virtual/${activeTab}`);
    }
  });
  $("#vs-tabs").addEventListener("keydown", (event) => {
    const tabs = $$("[data-vs-tab]");
    const index = tabs.indexOf(event.target);
    if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    selectTab(tabs[next].dataset.vsTab);
    history.replaceState(null, "", `#storage/virtual/${activeTab}`);
    tabs[next].focus();
  });
  const busy = (job) => job && ["queued", "running"].includes(job.state);
  const error = (text = "") => {
    $("#vs-form-error").textContent = text;
    $("#vs-form-error").hidden = !text;
  };
  const nodeName = (name) => latest?.nodes.find((n) => n.name === name)?.display_name || name;
  function ownsPool(pool) {
    const created = latest?.created;
    return !!created && pool.name === created.spec.name && pool.type === "nfs"
      && pool.source === `${created.spec.virtual_ip}:${created.export || `/srv/gateway-exports/${created.spec.name}`}`;
  }
  function invalidate() {
    receipt = null;
    $("#vs-review").hidden = true;
    $("#vs-review-button").hidden = false;
    $("#vs-submit").hidden = true;
    form.elements.confirm_name.required = false;
    form.elements.confirm_wipe.required = false;
    form.elements.confirm_wipe.checked = false;
    form.elements.confirm_name.value = "";
    error();
  }
  function selected(node, role) {
    return $$("select[data-vs-disk]", form)
      .filter((s) => s.dataset.vsNode === node && s.value === role).map((s) => s.dataset.vsDisk);
  }
  function capacity() {
    const nodes = inspected.map((n) => {
      const picked = selected(n.name, "data");
      const data = n.disks.filter((d) => picked.includes(d.path)).map((d) => d.size_gib);
      if (!data.length) return {raw: 0, maximum: 0};
      const raw = data.reduce((a, b) => a + b, 0);
      const meta = raw < 100 ? 128 : 1024;
      const thin = Math.floor(raw * 1024 * .9) - 2 * meta - 256;
      const replication = Math.max(1024, Math.ceil(thin / 16384) + 256);
      return {raw, headroom: raw * .10, metadata: (2 * meta + 256 + replication) / 1024,
        maximum: Math.max(0, Math.floor((thin - replication) / 1024))};
    });
    const maximum = nodes.length === 2 ? Math.min(...nodes.map((n) => n.maximum)) : 0;
    $("#vs-capacity").textContent = maximum
      ? "RF2 stores two copies across the nodes. All selected data disks contribute capacity; SSD cache is excluded. Actual free space is reported after formatting."
      : "Choose at least one data disk on each node. SSD cache is optional.";
    $("#vs-capacity-breakdown").hidden = !maximum;
    if (maximum) {
      const limiting = nodes.reduce((a, b) => a.maximum < b.maximum ? a : b);
      const raw = nodes.reduce((s, n) => s + n.raw, 0), afterRF2 = Math.min(...nodes.map((n) => n.raw));
      $("#vs-capacity-breakdown").innerHTML = breakdown({raw_data_gib: raw,
        after_rf2_gib: afterRF2, headroom_gib: limiting.headroom,
        metadata_and_service_gib: limiting.metadata, maximum_gib: maximum,
        unmatched_gib: raw / 2 - afterRF2});
      form.elements.size_gib.max = String(maximum);
      form.elements.size_gib.value = String(maximum);
    } else {
      form.elements.size_gib.removeAttribute("max");
      form.elements.size_gib.value = "";
    }
  }
  function breakdown(c) {
    const gib = (n) => `${Number(n).toLocaleString(undefined, {maximumFractionDigits: 1})} GiB`;
    return `<div class="vs-overview"><div><small>Raw data disks · both nodes</small><b>${gib(c.raw_data_gib)}</b></div>
      <div><small>After RF2 · two copies</small><b>${gib(c.after_rf2_gib)}</b></div>
      <div><small>Headroom · 10%</small><b>−${gib(c.headroom_gib)}</b></div>
      <div><small>Metadata and service reserve</small><b>−${gib(c.metadata_and_service_gib)}</b></div>
      <div><small>Estimated maximum · rounded down</small><b>${gib(c.maximum_gib)}</b></div></div>
      ${c.unmatched_gib > .1 ? `<p class="hint">Unequal node capacities leave ${gib(c.unmatched_gib)} beyond the smaller node's matching capacity.</p>` : ""}
      <p class="hint">The 10% headroom stays unallocated. It is not an automatic rebuild guarantee.</p>`;
  }
  function diskPicker() {
    $("#vs-disk-picker").innerHTML = inspected.map((n) => `<section class="vs-node">
      <div class="vs-node-head"><h3>${esc(n.display_name || n.name)}</h3>
      <span class="badge plain">RF2 data copy</span></div>
      ${n.error ? `<p class="vs-erase">${esc(n.error)}</p>` : `<div class="table-wrap"><table><thead><tr><th>Use as</th><th>Disk</th><th>Capacity</th><th>Current contents</th></tr></thead><tbody>
      ${n.disks.map((d) => `<tr class="${d.selectable ? "" : "vs-protected"}">
        <td><select data-vs-node="${esc(n.name)}" data-vs-disk="${esc(d.path)}" ${d.selectable ? "" : "disabled"} aria-label="Role for ${esc(n.name)} ${esc(d.path)}"><option value="">${d.selectable ? "Do not use" : "Protected"}</option>${d.selectable ? `<option value="data">Data</option>${d.media === "ssd" ? '<option value="cache">SSD cache</option>' : ""}` : ""}</select></td>
        <td><b>${esc(d.path)}</b><span class="vm-sub">${esc(d.model)} · ${esc(d.media.toUpperCase())}</span><span class="vm-sub">${esc(d.serial || "No serial reported")}</span></td>
        <td>${d.size_gib.toLocaleString()} GiB</td><td>${esc(d.contents || "Empty")}
          ${d.reclaim_pools.length ? `<span class="vm-sub">Replaces pool: ${esc(d.reclaim_pools.join(", "))}</span>` : ""}
          ${d.reasons.length ? `<span class="vm-sub">${esc(d.reasons.join("; "))}</span>` : ""}
          ${d.required_disks.length > 1 && d.selectable ? `<span class="vm-sub">Select the complete set: ${esc(d.required_disks.join(", "))}</span>` : ""}</td></tr>`).join("")}
      </tbody></table></div>`}</section>`).join("");
    capacity();
  }
  async function start() {
    form.reset();
    invalidate();
    inspected = [];
    form.elements.arbiter.innerHTML = (latest?.nodes || []).filter((n) => n.role === "witness" && n.healthy)
      .map((n) => `<option value="${esc(n.name)}">${esc(n.display_name || n.name)}</option>`).join("");
    $("#vs-disk-picker").textContent = "Reading physical disks and checking VM references…";
    dialog.showModal();
    $("#vs-review-button").disabled = true;
    try {
      inspected = (await api("/virtual-storage/inventory")).nodes;
      diskPicker();
      if (inspected.length !== 2 || inspected.some((n) => n.error)) {
        throw new Error("Both data nodes must return their disk inventories before storage can be created.");
      }
      $("#vs-review-button").disabled = false;
    } catch (err) { error(err.message); }
  }
  form.addEventListener("change", (event) => {
    if (["confirm_name", "confirm_wipe"].includes(event.target.name)) return;
    invalidate();
    capacity();
  });
  // Editing a reviewed text/number field invalidates its one-time server receipt.
  form.addEventListener("input", (event) => {
    if (!["confirm_name", "confirm_wipe"].includes(event.target.name)) invalidate();
  });
  $("#vs-review-button").addEventListener("click", async () => {
    if (!form.reportValidity()) return;
    const button = $("#vs-review-button");
    button.disabled = true;
    error();
    try {
      if (!Number(form.elements.size_gib.value) || inspected.some((n) => !selected(n.name, "data").length)) {
        throw new Error("Choose data disks on both nodes before reviewing the storage.");
      }
      const body = {name: form.elements.name.value, virtual_ip: form.elements.virtual_ip.value,
        prefix: Number(form.elements.prefix.value), size_gib: Number(form.elements.size_gib.value),
        replicas: 2, arbiter: form.elements.arbiter.value, nodes: inspected.map((n) => ({node: n.name,
          fingerprint: n.fingerprint, data: selected(n.name, "data"), cache: selected(n.name, "cache")}))};
      receipt = await post("/virtual-storage/review", body);
      const r = receipt.review;
      $("#vs-review-details").innerHTML = `<p><b>${esc(r.name)}</b> · RF2 · ${r.size_gib.toLocaleString()} GiB requested · VIP ${esc(r.virtual_ip)} · Arbiter ${esc(nodeName(r.arbiter))}</p>
        ${breakdown(r.capacity)}
        <div class="table-wrap"><table><thead><tr><th>Node</th><th>Data disks to erase</th><th>Cache disks to erase</th><th>Replaces pools</th></tr></thead><tbody>${r.nodes.map((n) => `<tr><td>${esc(nodeName(n.node))}</td><td>${n.data.map((d) => `${esc(d.path)} · ${esc(d.serial || d.model)}`).join("<br>")}</td><td>${n.cache.map((d) => `${esc(d.path)} · ${esc(d.serial || d.model)}`).join("<br>") || "None"}</td><td>${esc(n.reclaim_pools.join(", ") || "None")}</td></tr>`).join("")}</tbody></table></div>`;
      $("#vs-review").hidden = false;
      $("#vs-submit").hidden = false;
      button.hidden = true;
      form.elements.confirm_name.required = true;
      form.elements.confirm_wipe.required = true;
      $("#vs-review").scrollIntoView({block: "nearest", behavior: "smooth"});
    } catch (err) { error(err.message); }
    finally { button.disabled = false; }
  });
  handlers["virtual-storage-dialog"] = async () => {
    if (!receipt) throw new Error("Review the selected disks first.");
    try {
      await post("/virtual-storage/create", {token: receipt.token,
        confirm_name: form.elements.confirm_name.value,
        confirm_wipe: form.elements.confirm_wipe.checked});
      receipt = null;
      toast("Virtual Storage creation started. Progress is shown in Storage.");
      await render();
    } catch (err) {
      if (/expired|changed|no longer available/i.test(err.message)) invalidate();
      error(err.message); throw err;
    }
  };
  $("#vs-create").addEventListener("click", start);
  $("#virtual-storage-status").addEventListener("change", (event) => {
    if (event.target.id === "vs-cache-mode") desiredCacheMode = event.target.value;
  });
  $("#virtual-storage-status").addEventListener("click", async (event) => {
    const view = event.target.closest("[data-vs-view]");
    if (view) {
      replicationView = view.dataset.vsView;
      $("#vs-replicas").classList.toggle("vs-list", replicationView === "list");
      $$("[data-vs-view]").forEach((b) => b.setAttribute("aria-pressed", String(b === view)));
      return;
    }
    const browse = event.target.closest("[data-vs-browse]"), rename = event.target.closest("[data-vs-rename]");
    const pool = StorageView.pools(state.pools).find(ownsPool);
    if (browse && pool) { openPoolView(pool.node, pool.name); return; }
    if (rename && pool) {
      try { await renamePoolDisplayName(pool.node, pool.name); } catch (err) { fail(err); }
      return;
    }
    const button = event.target.closest("#vs-cache-apply");
    if (!button) return;
    button.disabled = true;
    try {
      await post("/virtual-storage/cache", {name: latest.created.spec.name,
        mode: $("#vs-cache-mode").value});
      toast("Changing SSD cache on both data nodes. Follow progress here.");
      await render();
    } catch (err) { fail(err); button.disabled = false; }
  });
  function cacheControls(data, running) {
    if (!data.created || !data.cache?.length) return "";
    const modes = [...new Set(data.cache.map((n) => n.mode || "unavailable"))];
    const mode = modes.length === 1 ? modes[0] : "mixed";
    const selected = desiredCacheMode || (mode === "writeback" ? "writeback" : "writethrough");
    return `<div class="vs-cache-panel"><b>SSD cache · ${esc(mode)}</b>
      <p class="hint">${data.cache.map((n) => `${esc(nodeName(n.node))}: ${esc(n.mode || n.error || "Not reported")}${n.dirty_bytes != null ? ` · ${(n.dirty_bytes / 1024**2).toFixed(1)} MiB pending to HDD` : ""}`).join("<br>")}</p>
      ${isAdmin() ? `<div class="actions"><select id="vs-cache-mode" aria-label="SSD cache mode" ${running ? "disabled" : ""}>
        <option value="writethrough" ${selected === "writethrough" ? "selected" : ""}>Writethrough · write to HDD before acknowledging</option>
        <option value="writeback" ${selected === "writeback" ? "selected" : ""}>Writeback · accelerate writes using SSD</option>
      </select><button id="vs-cache-apply" class="primary" ${running || data.phase !== "ready" || data.cache.some((n) => n.error) ? "disabled" : ""}>Apply to both nodes</button></div>` : ""}
      <p class="hint">Writeback acknowledges writes from SSD before destaging to HDD. Cache failure can lose pending data; RF2 does not replace SSD power-loss protection or backups. Writethrough waits for HDD. Switching back flushes pending data first. Performance still depends on the network and workload.</p></div>`;
  }
  $("#vs-prepare").addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      await post("/virtual-storage/software");
      toast("Preparing storage software. No disks are being erased.");
      await render();
    } catch (err) { fail(err); event.target.disabled = false; }
  });
  const reported = (value, suffix = "") => value == null || value === "" ? "Not reported" : `${esc(String(value))}${suffix}`;
  const metric = (label, value) => value == null || value === "Not reported" ? "" : `<div><small>${esc(label)}</small><b>${value}</b></div>`;
  function gauge(value, label, detail = "", suffix = "%") {
    const known = Number.isFinite(value);
    const percent = known && suffix === "%" ? Math.max(0, Math.min(100, value)) : 0;
    return `<div class="vs-gauge"><svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="42"/><circle class="vs-gauge-fill" cx="50" cy="50" r="42" pathLength="100" stroke-dasharray="${percent} 100" transform="rotate(-90 50 50)"/></svg><div><b>${known ? `${num(value)}${suffix}` : "—"}</b><small>${esc(label)}</small></div></div>${detail ? `<p class="hint">${esc(detail)}</p>` : ""}`;
  }
  function cacheGauges(data) {
    return `<div class="vs-replicas">${(data.cache || []).map((n) => `<article class="vs-replica"><h3>${esc(nodeName(n.node))}</h3>${gauge(n.dirty_bytes == null ? null : n.dirty_bytes / 1024**2, "Pending to HDD", n.error || n.mode || "", " MiB")}<p class="hint">${n.dirty_bytes == null ? "Pending bytes unavailable" : n.dirty_bytes === 0 ? "Cache is clean" : "Dirty data waiting to destage"}. Cache capacity is not reported; no percentage is estimated.</p></article>`).join("")}</div>`;
  }
  function replicationConnected(data) {
    const nodes = data.nodes.filter((n) => n.role === "compute");
    return nodes.length === 2 && !["degraded", "failed"].includes(data.phase) && nodes.every((n) => {
      const r = n.resources?.find((r) => r.name === data.created?.spec.name);
      return n.healthy && r?.devices?.length && r.devices.every((d) => d["disk-state"] === "UpToDate")
        && r.connections?.some((c) => c["connection-state"] === "Connected" && c.peer_devices?.length);
    });
  }
  function topologyScene(data, pool) {
    const nodes = data.nodes.filter((n) => n.role === "compute");
    const witness = data.nodes.find((n) => n.role === "witness");
    const usage = pool && Number.isFinite(pool.allocation_gb) && pool.capacity_gb > 0
      ? Math.max(0, Math.min(100, pool.allocation_gb / pool.capacity_gb * 100)) : null;
    return `<div id="vs-topology" class="vs-scene ${replicationConnected(data) ? "vs-linked" : ""}">
      <div class="vs-grid" aria-hidden="true"></div>
      <svg viewBox="0 0 900 380" role="img" aria-label="Two data replicas connected through DRBD, with an Arbiter above and Storage VIP in the center">
        <g class="vs-wires"><path d="M220 240H680"/><path d="M450 90V220"/></g>
        <g class="vs-beam"><path d="M220 240H680"/></g>
        <g class="vs-orbit"><ellipse cx="450" cy="266" rx="142" ry="51"/><ellipse cx="450" cy="278" rx="160" ry="58" stroke-dasharray="5 9"/></g>
        <g class="vs-ring" transform="translate(450 256) scale(1 .38)"><circle r="116"/><circle class="vs-ring-used" r="116" pathLength="100" stroke-dasharray="${usage ?? 0} 100" transform="rotate(-90)"/></g>
        ${nodes.slice(0, 2).map((n, i) => `<g transform="translate(${i ? 680 : 220} 228)" class="vs-tower"><path d="M-66-40 0-65 66-40 0-15Z"/><path d="M-66-40V42L0 68V-15ZM0-15 66-40V42L0 68"/>${[0, 20, 40].map((y) => `<path d="M-55 ${y - 22} -12 ${y - 5}M12 ${y - 5} 55 ${y - 22}"/>`).join("")}<text y="100">${esc(n.display_name || n.name)}</text><text y="121" class="vs-svg-dim">${n.healthy ? "Data node · online" : "Data node · offline"}</text></g>`).join("")}
        <g class="vs-arbiter"><path d="m450 26 44 24-44 24-44-24Z"/><path d="m406 50 44 42 44-42"/><text x="450" y="118">${esc(witness?.display_name || witness?.name || "No Arbiter")}</text><text x="450" y="138" class="vs-svg-dim">${witness?.healthy ? "Arbiter · online · no VM data" : "Arbiter · unavailable"}</text></g>
        <text x="450" y="211" class="vs-vip">${esc(data.created?.spec.virtual_ip || "VIP not configured")}</text><text x="450" y="235" class="vs-svg-dim">STORAGE VIP</text><text x="450" y="350">${usage == null ? "Usage not reported" : `${usage.toFixed(1)}% used`} · RF2</text>
      </svg><p>HDD + SSD cache → LVM thin → DRBD RF2 + Arbiter → XFS → NFS / Storage VIP</p></div>`;
  }
  function replicaTable(data) {
    return `<div class="vs-replica-table table-wrap"><table><thead><tr><th>Node</th><th>Role / disk</th><th>Status / quorum</th><th>Peer replication</th><th>Sync</th><th>Out of sync</th><th>Verify remaining</th></tr></thead><tbody>${data.nodes.map((n) => {
      const r = n.resources?.find((r) => r.name === data.created?.spec.name);
      const peers = (r?.connections || []).flatMap((c) => c.peer_devices || []);
      const values = (key, suffix = "") => peers.length ? peers.map((d) => reported(d[key], suffix)).join("<br>") : "Not reported";
      const disks = r?.devices || [];
      const quorum = disks.length && disks.every((d) => typeof d.quorum === "boolean")
        ? disks.every((d) => d.quorum) ? "Yes" : "No" : "Not reported";
      return `<tr><td>${esc(n.display_name || n.name)}</td><td>${esc(n.role === "witness" ? "Arbiter" : r?.role || "Data node")}<small>${reported(disks.map((d) => d["disk-state"]).filter(Boolean).join(", "))}</small></td><td>${n.healthy ? "Online" : "Offline"}<small>Quorum: ${quorum}</small></td><td>${values("replication-state")}</td><td>${values("percent-in-sync", "%")}</td><td>${values("out-of-sync", " KiB")}</td><td>${values("ov-left")}<small>Reported units</small></td></tr>`;
    }).join("")}</tbody></table></div>`;
  }
  function replication(data) {
    return `<div class="vs-section-head"><div><h3>Replica telemetry</h3><p class="hint">Arbiter votes in quorum and stores no VM data. Verify status is read only.</p></div><div class="vs-view-switch" aria-label="Replica display"><button data-vs-view="panels" aria-pressed="${replicationView === "panels"}">Panels</button><button data-vs-view="list" aria-pressed="${replicationView === "list"}">List</button></div></div>
      <div id="vs-replicas" class="vs-replicas ${replicationView === "list" ? "vs-list" : ""}">${data.nodes.map((n) => {
        const r = n.resources?.find((r) => r.name === data.created?.spec.name);
        const devices = r?.devices || [];
        const current = devices.length && devices.every((d) => d["disk-state"] === "UpToDate" && d.quorum);
        return `<article class="vs-replica"><header><h3>${esc(n.display_name || n.name)}</h3><span class="badge ${n.healthy ? "plain" : "failed"}">${!n.healthy ? "Offline" : r ? n.role === "witness" ? "Voting · no VM data" : current ? "Up to date" : "Check replication" : n.software_ready ? "Ready to create" : "Preparation required"}</span></header>
          ${n.role === "compute" ? gauge((r?.connections || []).flatMap((c) => c.peer_devices || []).map((d) => d["percent-in-sync"]).filter((v) => typeof v === "number").reduce((min, v) => min == null ? v : Math.min(min, v), null), "Replica sync") : ""}
          <div class="vs-overview">${metric("Role", reported(r?.role || (n.role === "witness" ? "Arbiter" : "Data node")))}${metric("Disk state", reported(devices.map((d) => d["disk-state"]).filter(Boolean).join(", ")))}${metric("Quorum", devices.length && devices.every((d) => typeof d.quorum === "boolean") ? devices.every((d) => d.quorum) ? "Yes" : "No" : "Not reported")}</div>
          ${(r?.connections || []).map((c) => `<div class="vs-peer"><b>${esc(c.name || "Peer")} · ${reported(c["connection-state"])}</b>${(c.peer_devices || []).map((d) => `<div class="vs-overview">${metric("Replication", reported(d["replication-state"]))}${metric("Sync", reported(d["percent-in-sync"], "%"))}${metric("Out of sync", reported(d["out-of-sync"], " KiB"))}${metric("Verify remaining (reported units)", reported(d["ov-left"]))}</div>`).join("")}</div>`).join("") || '<p class="hint">Peer sync / out-of-sync / verify: Not reported</p>'}
          ${n.error || n.missing_components?.length ? `<p class="hint">${esc(n.error || n.missing_components.join("; "))}</p>` : ""}</article>`;
      }).join("")}${replicaTable(data)}</div>`;
  }
  function workspace(data, pool, running, prepared) {
    const spec = data.created?.spec, status = data.resource?.status;
    const summary = `<div class="vs-overview">${metric("Virtual Storage", reported(pool?.display_name || spec?.name))}${metric("Storage VIP", reported(spec?.virtual_ip))}${metric("Active node", reported(status?.primary ? nodeName(status.primary) : null))}${metric("Usable capacity", spec ? `${esc(spec.size_gib.toLocaleString())} GiB` : "Not reported")}</div>`;
    const usedPercent = pool?.capacity_gb > 0 && Number.isFinite(pool.allocation_gb)
      ? pool.allocation_gb / pool.capacity_gb * 100 : null;
    const capacity = `<div class="vs-capacity-layout"><article class="vs-replica">${gauge(usedPercent, "Filesystem used")}</article><div class="vs-overview">${metric("Used", pool?.allocation_gb != null ? `${num(pool.allocation_gb)} GB` : null)}${metric("Available", pool?.available_gb != null ? `${num(pool.available_gb)} GB` : null)}${metric("Total capacity", pool?.capacity_gb != null ? `${num(pool.capacity_gb)} GB` : null)}</div></div><p class="hint">Usage is the exported filesystem capacity. Thin pool utilization and growth history are not reported by the current API.</p>`;
    const panels = {
      overview: `${topologyScene(data, pool)}${summary}${pool ? `<div class="vs-overview">${metric("Used", pool.allocation_gb != null ? `${num(pool.allocation_gb)} GB` : "Not reported")}${metric("Available", pool.available_gb != null ? `${num(pool.available_gb)} GB` : "Not reported")}${metric("Shared by", pool.members.map((m) => esc(nodeName(m.node))).join(", "))}</div>` : ""}${!spec ? `<p class="hint">${prepared ? "Choose the disks on both nodes, review what will be erased, then create your shared storage." : "Prepare the storage components first, then select the data disks and optional SSD cache yourself."}</p>` : ""}`,
      replication: replication(data),
      cache: `<h3>SSD acceleration</h3>${cacheGauges(data)}${cacheControls(data, running) || '<p class="hint">No SSD cache reported for this Virtual Storage.</p>'}`,
      capacity: `<h3>Capacity telemetry</h3>${capacity}`,
      files: `<h3>Shared filesystem</h3><p class="hint">${pool ? `Shared by ${pool.members.map((m) => esc(nodeName(m.node))).join(", ")}` : "Storage pool is not available for browsing."}</p>${pool ? '<button id="vs-browse" data-vs-browse class="primary">Browse files</button>' : ""}`,
      settings: `<h3>Storage identity</h3>${summary}${pool && isAdmin() ? '<button id="vs-rename" data-vs-rename>Rename</button>' : ""}${spec ? '<p class="hint">Additional Virtual Storage is not enabled on this installation yet.</p>' : ""}`,
    };
    return Object.entries(panels).map(([name, html]) => `<section id="vs-panel-${name}" data-vs-panel="${name}" role="tabpanel" aria-labelledby="vs-tab-${name}" tabindex="0" ${activeTab === name ? "" : "hidden"}>${html}</section>`).join("");
  }
  async function render() {
    if (rendering) return;
    rendering = true;
    clearTimeout(timer);
    const box = $("#virtual-storage-status");
    try {
      const data = await api("/virtual-storage");
      const previousName = latest?.created?.spec?.name;
      latest = data;
      if (data.created?.spec?.name && data.created.spec.name !== previousName) {
        await load("pools");
      }
      renderPools();
      const running = busy(data.job), prepared = data.nodes.length === 3 && data.nodes.every((n) => n.healthy && n.software_ready);
      const topology = data.nodes.filter((n) => n.role === "compute").length === 2 && data.nodes.filter((n) => n.role === "witness" && n.healthy).length === 1;
      $("#vs-prepare").hidden = !!data.created || prepared;
      $("#vs-prepare").disabled = running || !topology;
      $("#vs-create").hidden = false;
      $("#vs-create").disabled = !!data.created || running || !prepared || !topology;
      $("#vs-create").title = data.created
        ? "Additional Virtual Storage is not enabled on this installation yet."
        : "Create Virtual Storage";
      const label = {ready: "Healthy", syncing: "Synchronizing replicas", degraded: "Needs attention", preparing: "Preparing software", creating: "Creating storage", "changing-cache": "Changing SSD cache"}[data.phase]
        || (prepared ? "Ready to create" : topology ? "Preparation required" : "Waiting for two nodes and Arbiter");
      const pool = StorageView.pools(state.pools).find(ownsPool);
      const focus = box.contains(document.activeElement) ? document.activeElement : null;
      const focusId = focus?.id;
      const focusAction = focus?.hasAttribute("data-vs-view") ? focus.dataset.vsView : null;
      const previousScene = document.getElementById("vs-topology");
      box.innerHTML = `<div class="vs-section-head"><span class="badge ${data.phase === "ready" ? "online" : "warning"}">${esc(label)}</span><small>RF2 / SHARED STORAGE</small></div>
        ${data.phase === "syncing" ? '<p class="hint">Storage is serving data while the second copy synchronizes. Full redundancy is not ready yet.</p>' : ""}
        ${running ? `<div class="vs-job" role="status"><b>${data.job.kind === "cache-mode" ? "Changing SSD cache" : data.job.kind === "software" ? "Preparing storage software" : "Creating Virtual Storage"}</b><p>${esc(data.job.step)}</p><div class="vs-working" aria-label="Operation in progress"></div><small>This operation continues if you close the page.</small></div>` : ""}
        ${["failed", "interrupted"].includes(data.job?.state) ? `<div class="vs-erase"><b>Operation ${esc(data.job.state)}</b><p>${esc(data.job.error)}</p><small>Inspect the recorded operation before retrying. Disk erasure is never replayed automatically.</small></div>` : ""}
        ${data.error ? `<p class="vs-erase">${esc(data.error)}</p>` : ""}
        ${workspace(data, pool, running, prepared)}`;
      if (previousScene?.classList.contains("vs-has-webgl")) {
        const freshScene = document.getElementById("vs-topology");
        previousScene.querySelector("svg").replaceWith(freshScene.querySelector("svg"));
        previousScene.classList.toggle("vs-linked", replicationConnected(data));
        freshScene.replaceWith(previousScene);
      }
      mountScene();
      if (focusId) document.getElementById(focusId)?.focus({preventScroll: true});
      else if (focusAction) box.querySelector(`[data-vs-view="${focusAction}"]`)?.focus({preventScroll: true});
      if (currentPage() === "storage") timer = setTimeout(() => render().catch(fail), running ? 2500 : 10000);
    } catch (err) { box.textContent = `Could not check Virtual Storage: ${err.message}`; }
    finally { rendering = false; }
  }
  function mountScene() {
    if (latest) window.StorageScene?.mount(document.getElementById("vs-topology"), latest,
      StorageView.pools(state.pools).find(ownsPool), replicationConnected(latest));
  }
  return {render, ownsPool, selectTab, mountScene};
})();
