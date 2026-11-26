"use strict";

// Only Manager-generated plans are accepted. Model text never becomes a command.
function createAssistantOperations({list, message, api, post, getLanguage, onBusy}) {
  let busy = false;
  const text = (en, th) => getLanguage() === "th" ? th : en;
  const element = (tag, content, cls) => {
    const el = document.createElement(tag);
    if (content) el.textContent = content;
    if (cls) el.className = cls;
    return el;
  };
  const error = err => message(err.message || String(err), "error");
  function validRoute(plan) {
    const kind = plan.operation?.kind;
    if (kind === "create_vlan") return plan.path === "/networks" && plan.body?.vlan === plan.operation.vlan;
    const action = {start_vm:"start",shutdown_vm:"stop",restart_vm:"restart"}[kind];
    return Boolean(action && /^[a-f0-9-]{36}$/i.test(plan.operation.target)
      && plan.path === `/vms/${plan.operation.target}/${action}`
      && (action === "start" || plan.body?.force === false));
  }
  async function showStatus(target = "") {
    const r = await post("/assistant/plan", {kind:"status", target, language:getLanguage()});
    message(r.reply);
  }
  function proposal(plan) {
    if (!validRoute(plan)) {message("Unsupported action plan", "error"); return;}
    const card = element("div", "", "ai-operation-card");
    card.append(element("strong", plan.summary));
    const details = element("ul");
    (plan.details || []).forEach(detail => details.append(element("li", detail)));
    card.append(details);
    const row = element("div", "", "ai-operation-buttons");
    const confirm = element("button", text("Confirm action", "ยืนยันดำเนินการ"));
    const cancel = element("button", text("Cancel", "ยกเลิก"));
    confirm.type = cancel.type = "button";
    row.append(confirm, cancel); card.append(row); list.append(card);
    list.scrollTop = list.scrollHeight;
    cancel.addEventListener("click", () => {confirm.disabled = cancel.disabled = true;card.append(element("p", text("Cancelled. No change was made.", "ยกเลิกแล้ว ยังไม่ได้เปลี่ยนค่า")));});
    confirm.addEventListener("click", async () => {
      if (busy || confirm.disabled) return;
      busy = true;onBusy(true);confirm.disabled = cancel.disabled = true;
      try {
        // Recheck entitlement, permissions, expiry and resource state before the
        // ordinary mutation API enforces its own permissions and approval policy.
        const fresh = await post("/assistant/review", {token:plan.token});
        if (!validRoute(fresh)) throw new Error("Unsupported action plan");
        await api(fresh.path, {method:"POST", headers:{"Content-Type":"application/json", "X-IBHICS-Assistant-Plan":plan.token}, body:JSON.stringify(fresh.body)});
        if (fresh.verification.kind === "network") {
          const v = fresh.verification;
          const networks = await api("/networks");
          const found = networks.find(n => n.name === v.name && n.vlan === v.vlan);
          if (!found || !v.nodes.every(node => found.configurations?.[node]?.vlan === v.vlan)) {
            throw new Error(text("Request sent, but the VLAN is not verified on every node. Check Networks before retrying.", "ส่งคำขอแล้ว แต่ยังตรวจยืนยัน VLAN ได้ไม่ครบทุกโหนด กรุณาตรวจหน้า Network ก่อนลองใหม่"));
          }
          message(text(`VLAN ${v.vlan} (${v.name}) verified on ${v.nodes.join(", ")}.`, `สร้าง VLAN ${v.vlan} (${v.name}) และตรวจครบโหนด ${v.nodes.join(", ")} แล้วค่ะ`));
        } else {
          const v = fresh.verification;
          const vm = await api(`/vms/${v.uuid}`);
          message(text(`${v.name}: ${v.action} request accepted. Current hypervisor state: ${vm.state}. OS readiness has not yet been verified.`, `${v.name}: รับคำสั่ง ${v.action} แล้ว สถานะ Hypervisor ล่าสุดคือ ${vm.state} ยังไม่ได้ยืนยันความพร้อมภายใน OS ค่ะ`));
          const check = element("button", text("Check VM status", "ตรวจสถานะ VM"));
          check.type = "button";
          check.addEventListener("click", async () => {
            check.disabled = true;
            try {await showStatus(v.uuid);} catch (err) {error(err);} finally {check.disabled = false;}
          });
          card.append(check);
        }
        card.append(element("p", text("Request processed through ibHiCS permissions and approval rules.", "ดำเนินการผ่านสิทธิ์และระบบอนุมัติของ ibHiCS แล้ว")));
      } catch (err) {
        if (typeof ApprovalPending !== "undefined" && err instanceof ApprovalPending) {
          message(text("Awaiting approval. No completed action is being claimed. ", "รอผู้อนุมัติ ยังไม่ได้สรุปว่าดำเนินการเสร็จค่ะ ") + err.message);
        } else {
          error(err);
          card.append(element("p", text("Do not resend automatically. Check current status or prepare a new plan.", "กรุณาตรวจสถานะปัจจุบันหรือเตรียมแผนใหม่ ไม่ส่งคำสั่งซ้ำอัตโนมัติค่ะ")));
        }
      } finally {busy = false;onBusy(false);}
    });
  }
  async function open() {
    if (busy) return;
    const inventory = await api("/assistant/operations");
    const form = element("form", "", "ai-operation-card ai-operation-form");
    form.append(element("strong", text("Manage ibHiCS", "จัดการ ibHiCS")));
    const kind = element("select");kind.name = "kind";
    const choices = [["status",text("Check status", "ตรวจสถานะ")]];
    if (inventory.can_create_vlan) choices.push(["create_vlan",text("Add virtual switch (VLAN)", "เพิ่ม Virtual switch (VLAN)")]);
    if (inventory.can_manage_vms) choices.push(["start_vm",text("Power on / resume VM", "Power on / ทำงาน VM ต่อ")],["shutdown_vm",text("Shutdown VM", "Shutdown VM")],["restart_vm",text("Restart VM", "รีสตาร์ต VM")]);
    choices.forEach(([value,label]) => {const option = element("option", label);option.value=value;kind.append(option);});
    const target = element("select");target.name="target";
    const all = element("option",text("System status / all VMs", "สถานะระบบ / VM ทั้งหมด"));all.value="";target.append(all);
    inventory.vms.forEach(vm => {const option=element("option",`${vm.name} · ${vm.node} · ${vm.state}`);option.value=vm.uuid;target.append(option);});
    const vlan=element("input");vlan.type="number";vlan.min="1";vlan.max="4094";vlan.name="vlan";vlan.placeholder="VLAN ID (1–4094)";
    const name=element("input");name.name="name";name.maxLength=63;name.placeholder=text("Network name (optional)","ชื่อ Network (ไม่บังคับ)");
    const row=element("div","","ai-operation-buttons"), prepare=element("button",text("Review / check", "ตรวจค่า / เตรียมแผน")), cancel=element("button",text("Cancel","ยกเลิก"));
    prepare.type="submit";cancel.type="button";cancel.addEventListener("click",()=>form.remove());row.append(prepare,cancel);
    const labeled = (label,field) => {const holder=element("label",label);holder.append(field);form.append(holder);return holder;};
    labeled(text("Action","คำสั่ง"),kind);
    const targetLabel=labeled(text("VM","VM"),target), vlanLabel=labeled("VLAN ID",vlan), nameLabel=labeled(text("Network name","ชื่อ Network"),name);
    form.append(row);list.append(form);
    function fields() {
      const network=kind.value==="create_vlan";
      targetLabel.hidden=network;target.required=!network && kind.value!=="status";
      all.disabled=kind.value!=="status";
      if (all.disabled && !target.value) target.selectedIndex=inventory.vms.length ? 1 : 0;
      vlanLabel.hidden=nameLabel.hidden=!network;vlan.required=network;
    }
    kind.addEventListener("change",fields);fields();
    form.addEventListener("submit",async event=>{
      event.preventDefault();if(busy || prepare.disabled)return;prepare.disabled=true;
      try {
        const body={kind:kind.value,language:getLanguage()};
        if(kind.value==="create_vlan"){body.vlan=Number(vlan.value);body.name=name.value.trim();}
        else body.target=target.value;
        const r=await post("/assistant/plan",body);form.remove();message(r.reply);if(r.proposal)proposal(r.proposal);
      } catch(err){error(err);prepare.disabled=false;}
    });
    list.scrollTop=list.scrollHeight;
  }
  return {open,proposal,isBusy:()=>busy};
}
