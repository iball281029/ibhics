/* Virtual Storage: physical disk selection, server review and durable creation jobs. */
window.VirtualStorageUI = (() => {
  const dialog = $("#virtual-storage-dialog"), form = $("#virtual-storage-form");
  let latest = null, inspected = [], receipt = null, timer = null, rendering = false;
  const busy = (job) => job && ["queued", "running"].includes(job.state);
  const error = (text = "") => {
    $("#vs-form-error").textContent = text;
    $("#vs-form-error").hidden = !text;
  };
  const nodeName = (name) => latest?.nodes.find((n) => n.name === name)?.display_name || name;
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
    const sizes = inspected.map((n) => {
      const picked = selected(n.name, "data");
      const data = n.disks.filter((d) => picked.includes(d.path)).map((d) => d.size_gib);
      if (!data.length) return 0;
      const layout = $$("select[data-vs-layout]", form).find((s) => s.dataset.vsLayout === n.name).value;
      const raw = layout === "mirror" ? Math.min(...data) : data.reduce((a, b) => a + b, 0);
      const meta = raw < 100 ? 128 : 1024;
      return Math.max(0, Math.floor((Math.floor(raw * 1024 * .9) - 2 * meta - 512) / 1024));
    });
    const maximum = sizes.length === 2 ? Math.min(...sizes) : 0;
    $("#vs-capacity").textContent = maximum
      ? `Up to approximately ${maximum.toLocaleString()} GiB usable, with a copy on each node. SSD cache does not add storage capacity.`
      : "Choose at least one data disk on each node. SSD cache is optional.";
    if (maximum) {
      form.elements.size_gib.max = String(maximum);
      if (!form.elements.size_gib.value || Number(form.elements.size_gib.value) > maximum) {
        form.elements.size_gib.value = String(maximum);
      }
    } else form.elements.size_gib.removeAttribute("max");
  }
  function diskPicker() {
    $("#vs-disk-picker").innerHTML = inspected.map((n) => `<section class="vs-node">
      <div class="vs-node-head"><h3>${esc(n.display_name || n.name)}</h3>
      <label>Data layout<select data-vs-layout="${esc(n.name)}"><option value="mirror">Mirror data disks</option><option value="stripe">Combine data disks</option></select></label></div>
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
      const body = {name: form.elements.name.value, virtual_ip: form.elements.virtual_ip.value,
        prefix: Number(form.elements.prefix.value), size_gib: Number(form.elements.size_gib.value),
        arbiter: form.elements.arbiter.value, nodes: inspected.map((n) => ({node: n.name,
          fingerprint: n.fingerprint, data: selected(n.name, "data"), cache: selected(n.name, "cache"),
          layout: $$("select[data-vs-layout]", form).find((s) => s.dataset.vsLayout === n.name).value}))};
      receipt = await post("/virtual-storage/review", body);
      const r = receipt.review;
      $("#vs-review-details").innerHTML = `<p><b>${esc(r.name)}</b> · ${r.size_gib.toLocaleString()} GiB usable · VIP ${esc(r.virtual_ip)} · Arbiter ${esc(nodeName(r.arbiter))}</p>
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
  $("#vs-prepare").addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      await post("/virtual-storage/software");
      toast("Preparing storage software. No disks are being erased.");
      await render();
    } catch (err) { fail(err); event.target.disabled = false; }
  });
  async function render() {
    if (rendering) return;
    rendering = true;
    clearTimeout(timer);
    const box = $("#virtual-storage-status");
    try {
      const data = await api("/virtual-storage");
      latest = data;
      const running = busy(data.job), prepared = data.nodes.length === 3 && data.nodes.every((n) => n.healthy && n.software_ready);
      const topology = data.nodes.filter((n) => n.role === "compute").length === 2 && data.nodes.filter((n) => n.role === "witness" && n.healthy).length === 1;
      $("#vs-prepare").hidden = !!data.created || prepared;
      $("#vs-prepare").disabled = running || !topology;
      $("#vs-create").hidden = !!data.created;
      $("#vs-create").disabled = running || !prepared || !topology;
      const label = {ready: "Healthy", syncing: "Synchronizing replicas", degraded: "Needs attention", preparing: "Preparing software", creating: "Creating storage"}[data.phase]
        || (prepared ? "Ready to create" : topology ? "Preparation required" : "Waiting for two nodes and Arbiter");
      const status = data.resource?.status;
      box.innerHTML = `<p><span class="badge ${data.phase === "ready" ? "online" : "warning"}">${esc(label)}</span></p>
        ${data.phase === "syncing" ? '<p class="hint">Storage is serving data while the second copy synchronizes. Full redundancy is not ready yet.</p>' : ""}
        ${data.created ? `<div class="vs-overview"><div><small>Virtual Storage</small><b>${esc(data.created.spec.name)}</b></div><div><small>Storage VIP</small><b>${esc(data.created.spec.virtual_ip)}</b></div><div><small>Active node</small><b>${esc(status?.primary ? nodeName(status.primary) : "Unavailable")}</b></div><div><small>Usable capacity</small><b>${data.created.spec.size_gib.toLocaleString()} GiB</b></div></div>`
          : `<p class="hint">${prepared ? "Choose the disks on both nodes, review what will be erased, then create your shared storage." : "Prepare the storage components first, then select the data disks and optional SSD cache yourself."} Arbiter votes in quorum and stores no VM data.</p>`}
        ${running ? `<div class="vs-job"><b>${data.job.kind === "software" ? "Preparing storage software" : "Creating Virtual Storage"}</b><p>${esc(data.job.step)}</p><div class="vs-working" aria-label="Operation in progress"></div><small>This operation continues if you close the page.</small></div>` : ""}
        ${data.job?.state === "failed" || data.job?.state === "interrupted" ? `<div class="vs-erase"><b>Operation ${esc(data.job.state)}</b><p>${esc(data.job.error)}</p><small>Inspect the recorded operation before retrying. Disk erasure is never replayed automatically.</small></div>` : ""}
        ${data.error ? `<p class="vs-erase">${esc(data.error)}</p>` : ""}
        <div class="table-wrap"><table><thead><tr><th>Node</th><th>Role</th><th>Status</th></tr></thead><tbody>${data.nodes.map((n) => {
          const resource = n.resources?.find((r) => r.name === data.created?.spec.name);
          const devices = resource?.devices || [];
          const replicated = devices.length && devices.every((d) => d["disk-state"] === "UpToDate" && d.quorum);
          const text = !n.healthy ? "Offline" : resource ? n.role === "witness" ? "Voting · no VM data" : replicated ? "Up to date" : "Synchronizing / check replication" : n.software_ready ? "Ready to create" : "Preparation required";
          return `<tr><td>${esc(n.display_name || n.name)}</td><td>${n.role === "witness" ? "Arbiter" : "Data node"}</td><td>${esc(text)}${!n.software_ready && n.healthy ? `<details><summary>Details</summary><span class="hint">${esc((n.missing_components || [n.error || "Storage components are unavailable"]).join("; "))}</span></details>` : ""}</td></tr>`;
        }).join("")}</tbody></table></div>`;
      if (running && currentPage() === "storage") timer = setTimeout(() => render().catch(fail), 2500);
    } catch (err) { box.textContent = `Could not check Virtual Storage: ${err.message}`; }
    finally { rendering = false; }
  }
  return {render};
})();
