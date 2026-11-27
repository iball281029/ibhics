"use strict";
(() => {
  const panel = document.getElementById("ai-panel"), launcher = document.getElementById("ai-launcher");
  const form = document.getElementById("ai-chat-form"), list = document.getElementById("ai-chat");
  const input = form.elements.message, send = document.getElementById("ai-send"), include = form.elements.include_context;
  const statusText = document.getElementById("ai-status"), enable = document.getElementById("ai-enabled");
  let language = "en";
  try {if (localStorage.getItem("ibhics-ai-language") === "th") language = "th";} catch {}
  let copy = AI_LOCALES[language];
  const languageSelect = document.getElementById("ai-language");
  const errorText = err => copy.errors[err.message] || copy.unavailable;
  let history = [], busy = false, available = false, generation = 0, allowance = null;
  let licensed = null, statusRequest = null;
  const licenseNotice = document.createElement("section");
  licenseNotice.className = "ai-license-notice";
  licenseNotice.id = "ai-license-notice";
  licenseNotice.lang = "en";
  licenseNotice.setAttribute("aria-label", "Support license required");
  licenseNotice.hidden = true;
  const noticeClose = document.createElement("button");
  noticeClose.type = "button";
  noticeClose.className = "ai-license-close";
  noticeClose.setAttribute("aria-label", "Dismiss license reminder");
  noticeClose.textContent = "×";
  const noticeTitle = document.createElement("strong");
  noticeTitle.textContent = "Nong ib is sleeping";
  const noticeText = document.createElement("p");
  noticeText.setAttribute("role", "status");
  noticeText.textContent = "Please activate or renew your Support License to wake Nong ib and use AI chat.";
  const licenseLink = document.createElement("a");
  licenseLink.href = "#system";
  licenseLink.dataset.tabLink = "about";
  licenseLink.textContent = "Manage Support License →";
  licenseNotice.append(noticeClose, noticeTitle, noticeText, licenseLink);
  launcher.after(licenseNotice);
  const sleepMarks = document.createElement("span");
  sleepMarks.className = "ai-sleep-marks";
  sleepMarks.setAttribute("aria-hidden", "true");
  ["z", "Z", "Z"].forEach(text => {
    const mark = document.createElement("i"); mark.textContent = text; sleepMarks.append(mark);
  });
  launcher.append(sleepMarks);
  function collapse() {
    panel.hidden = licenseNotice.hidden = true;
    launcher.setAttribute("aria-expanded", "false");
  }
  function applyLicense(value) {
    const wasLicensed = licensed;
    licensed = value;
    launcher.classList.toggle("is-sleeping", !value);
    const image = launcher.querySelector("img");
    image.src = value ? "ai-elephant.png" : "ai-elephant-sleeping.png";
    image.alt = value ? copy.mascotAlt : "Nong ib sleeping — Support License required";
    launcher.setAttribute("aria-label", value ? copy.mascotAlt : "Nong ib is sleeping. Manage Support License");
    launcher.setAttribute("aria-controls", value ? "ai-panel" : licenseNotice.id);
    if (!value) {
      panel.hidden = true;
      launcher.setAttribute("aria-expanded", String(!licenseNotice.hidden));
      if (wasLicensed === true) {generation++; history = []; input.value = ""; welcome();}
    } else if (wasLicensed !== true) {collapse();}
  }
  function layout() {
    const viewport=window.visualViewport, margin=14, gap=12;
    const width=Math.min(document.documentElement.clientWidth, viewport?.width || innerWidth);
    const height=viewport?.height || document.documentElement.clientHeight;
    const offsetX=viewport?.offsetLeft || 0, offsetY=viewport?.offsetTop || 0;
    const dollWidth=width<600 ? 50 : 64, dollHeight=dollWidth*1.12+10;
    const dollLeft=offsetX+Math.max(margin,width-margin-dollWidth);
    const dollTop=offsetY+Math.max(margin,height-margin-dollHeight);
    Object.assign(launcher.style,{left:`${dollLeft}px`,top:`${dollTop}px`,right:"auto",bottom:"auto",width:`${dollWidth}px`,height:`${dollHeight}px`});
    const panelWidth=Math.min(410,Math.max(0,width-margin*2));
    const panelHeight=Math.min(650,Math.max(220,height-dollHeight-margin*3),height-margin*2);
    const left=offsetX+Math.max(margin,width-margin-panelWidth);
    const top=offsetY+Math.max(margin,dollTop-offsetY-gap-panelHeight);
    Object.assign(panel.style,{left:`${left}px`,top:`${top}px`,right:"auto",bottom:"auto",width:`${panelWidth}px`,height:`${panelHeight}px`});
    Object.assign(licenseNotice.style, {
      left:`${left}px`, bottom:`${Math.max(margin, innerHeight-dollTop+gap)}px`,
      width:`${Math.min(320, Math.max(0, width-margin*2))}px`,
      maxHeight:`${Math.max(80,dollTop-offsetY-margin-gap)}px`
    });
    // Align the narrower reminder with the right edge of the chat panel.
    licenseNotice.style.left = `${offsetX+Math.max(margin,width-margin-Math.min(320,width-margin*2))}px`;
  }
  window.addEventListener("resize",layout);
  window.visualViewport?.addEventListener("resize",layout);
  window.visualViewport?.addEventListener("scroll",layout);
  layout();
  function message(text, kind = "assistant") {
    const el = document.createElement("div"); el.className = `ai-message ${kind}`;
    el.textContent = text; list.append(el); list.scrollTop = list.scrollHeight; return el;
  }
  const operations = createAssistantOperations({list,message,api,post,getLanguage:()=>language,
    onBusy: value => {input.disabled=send.disabled=value; if(!value)refreshStatus();}
  });
  document.getElementById("ai-actions").addEventListener("click", async () => {
    if (busy) return;
    try {await operations.open();} catch (err) {message(err.message || copy.unavailable,"error");}
  });
  function welcome() {
    list.replaceChildren();
    const mascot=document.createElement("img");mascot.src="ai-elephant.png";mascot.alt=copy.mascotAlt;mascot.className="ai-welcome";list.append(mascot);
    message(copy.welcome);
    const suggestions = document.createElement("div"); suggestions.className = "ai-suggestions";
    copy.questions.forEach(text => {
      const b = document.createElement("button"); b.type = "button"; b.textContent = text;
      b.addEventListener("click", () => {input.value = text; input.focus();}); suggestions.append(b);
    }); list.append(suggestions);
  }
  async function refreshStatus() {
    if (statusRequest) return statusRequest;
    statusRequest = loadStatus();
    try {return await statusRequest;} finally {statusRequest = null;}
  }
  async function loadStatus() {
    try {
      const s = await api("/assistant/status"); available = s.available;
      applyLicense(s.licensed === true);
      statusText.textContent = (copy.status[s.reason_code] || copy.errors[s.reason] || copy.status.ready) + (s.available && allowance ? copy.usage(allowance.used,allowance.limit) : ""); enable.checked = s.enabled;
      if(s.preview_notice)statusText.textContent += " " + s.preview_notice;
      enable.closest("label").hidden = !isAdmin(); enable.disabled = busy || (!s.enabled && (!s.licensed || !s.configured));
      document.getElementById("ai-privacy").textContent = copy.privacy;
      input.disabled = !available || busy; send.disabled = !available || busy;
      return s;
    } catch (err) {available = false; statusText.textContent = errorText(err); input.disabled = send.disabled = true;}
  }
  launcher.addEventListener("click", async () => {
    if (!panel.hidden || !licenseNotice.hidden) {collapse(); return;}
    const status = await refreshStatus();
    if (!status) {toast(copy.unavailable, true); return;}
    if (!licensed) {
      licenseNotice.hidden = false;
      layout(); launcher.setAttribute("aria-expanded", "true");
      licenseLink.focus();
      return;
    }
    panel.hidden = false; layout(); launcher.setAttribute("aria-expanded", "true");
    if (available) input.focus();
  });
  document.getElementById("ai-open-settings").addEventListener("click", () => {
    if (panel.hidden && licenseNotice.hidden) launcher.click();
  });
  document.getElementById("ai-close").addEventListener("click", () => {collapse();launcher.focus();});
  noticeClose.addEventListener("click", () => {collapse();launcher.focus();});
  licenseLink.addEventListener("click", () => {
    collapse();
    // Also select the tab when Settings is already the current page.
    selectTab("system-tabs", "about");
    setTimeout(() => document.getElementById("subscription-cards")?.scrollIntoView({block:"center"}), 0);
  });
  [panel, licenseNotice].forEach(surface => surface.addEventListener("keydown", e => {
    if(e.key === "Escape"){collapse();launcher.focus();e.stopPropagation();}
  }));
  window.addEventListener("focus", () => {refreshStatus();});
  document.addEventListener("visibilitychange", () => {if (!document.hidden) refreshStatus();});
  setInterval(() => {if (!document.hidden) refreshStatus();}, 60000);
  document.getElementById("ai-clear").addEventListener("click", () => {generation++;history=[];input.value="";welcome();});
  enable.addEventListener("change", async () => {
    enable.disabled = true;
    try {await api("/assistant/config", {method:"PUT",body:JSON.stringify({enabled:enable.checked})});}
    catch(err){statusText.textContent=errorText(err);}
    await refreshStatus();
  });
  document.getElementById("ai-preview-context").addEventListener("click", async () => {
    const el = document.getElementById("ai-context");el.hidden=!el.hidden;
    if (!el.hidden) {
      try {el.textContent=JSON.stringify(await api("/assistant/context"),null,2);}
      catch(err){el.textContent=errorText(err);}
    }
  });
  form.addEventListener("submit", async e => {
    e.preventDefault(); if(busy || operations.isBusy() || !available || !input.value.trim()) return;
    const text=input.value.trim(), requestGeneration=generation;
    busy=true;languageSelect.disabled=true;send.disabled=input.disabled=enable.disabled=true;
    message(text,"user");input.value="";const pending=message(copy.thinking);pending.setAttribute("role","status");
    try {
      const r=await post("/assistant/chat",{message:text,history:history.slice(-8),include_context:include.checked,language});
      pending.remove();
      if(requestGeneration===generation){message(r.reply);if(r.proposal)operations.proposal(r.proposal);history.push({role:"user",content:text},{role:"assistant",content:r.reply.slice(0,3000)});history=history.slice(-8);}
      if(r.allowance?.limit)allowance=r.allowance;
      if(r.allowance?.limit)statusText.textContent=copy.usage(r.allowance.used,r.allowance.limit);
    } catch(err){pending.remove();if(requestGeneration===generation)message(errorText(err),"error");}
    finally{busy=false;languageSelect.disabled=false;await refreshStatus();if(!panel.hidden && available)input.focus();}
  });
  function applyLanguage() {
    copy = AI_LOCALES[language];
    panel.lang = language;
    panel.setAttribute("aria-label", language === "th" ? "น้อง ib ผู้ช่วย AI ของ ibHiCS" : "Nong ib AI Assistant");
    launcher.querySelector(".ai-name").textContent = copy.name;
    panel.querySelector("header strong").textContent = copy.name;
    panel.querySelector("header small").textContent = language === "th" ? "ผู้ช่วย AI ของ ibHiCS · พร้อมดูแลคุณค่ะ" : "ibHiCS AI Assistant · Your support companion";
    enable.closest("label").lastChild.textContent = language === "th" ? " เปิดใช้ผู้ช่วย AI สำหรับระบบนี้" : " AI enabled for this installation";
    include.closest("label").lastChild.textContent = language === "th" ? " แนบสรุปสถานะระบบ" : " Include system summary";
    input.placeholder = language === "th" ? "พิมพ์คำถามให้น้อง ib ช่วยได้เลยค่ะ…" : "Ask Nong ib a question…";
    input.setAttribute("aria-label", language === "th" ? "คำถามของคุณ" : "Your question");
    document.getElementById("ai-preview-context").textContent = language === "th" ? "ดูข้อมูลที่จะแนบ" : "Preview context";
    document.getElementById("ai-clear").textContent = language === "th" ? "เริ่มแชตใหม่" : "New chat";
    document.getElementById("ai-actions").textContent = language === "th" ? "จัดการระบบ" : "Actions";
    send.textContent = language === "th" ? "ส่งข้อความ ↗" : "Send message ↗";
    document.getElementById("ai-close").setAttribute("aria-label", language === "th" ? "ย่อหน้าต่างแชต" : "Close chat");
    document.getElementById("ai-privacy").textContent = copy.privacy;
    languageSelect.value = language;
    if (!history.length && !busy) welcome();
    refreshStatus();
  }
  languageSelect.addEventListener("change", () => {
    language = languageSelect.value === "th" ? "th" : "en";
    try {localStorage.setItem("ibhics-ai-language", language);} catch {}
    applyLanguage();
  });
  window.addEventListener("ibhics-license-changed", () => {refreshStatus();});
  applyLanguage();
})();
