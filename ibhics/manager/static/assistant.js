"use strict";
(() => {
  const panel = document.getElementById("ai-panel"), launcher = document.getElementById("ai-launcher");
  const form = document.getElementById("ai-chat-form"), list = document.getElementById("ai-chat");
  const input = form.elements.message, send = document.getElementById("ai-send"), include = form.elements.include_context;
  const statusText = document.getElementById("ai-status"), enable = document.getElementById("ai-enabled");
  const copy = AI_TEXT;
  const errorText = err => copy.errors[err.message] || copy.unavailable;
  let history = [], busy = false, available = false, generation = 0, allowance = null;
  function layout() {
    const viewport=window.visualViewport, margin=14, gap=12;
    const width=Math.min(document.documentElement.clientWidth, viewport?.width || innerWidth);
    const height=viewport?.height || document.documentElement.clientHeight;
    const offsetX=viewport?.offsetLeft || 0, offsetY=viewport?.offsetTop || 0;
    const dollWidth=width<600 ? 100 : 128, dollHeight=dollWidth*1.12;
    const dollLeft=offsetX+Math.max(margin,width-margin-dollWidth);
    const dollTop=offsetY+Math.max(margin,height-margin-dollHeight);
    Object.assign(launcher.style,{left:`${dollLeft}px`,top:`${dollTop}px`,right:"auto",bottom:"auto",width:`${dollWidth}px`,height:`${dollHeight}px`});
    const panelWidth=Math.min(410,Math.max(0,width-margin*2));
    const panelHeight=Math.min(650,Math.max(220,height-dollHeight-margin*3),height-margin*2);
    const left=offsetX+Math.max(margin,width-margin-panelWidth);
    const top=offsetY+Math.max(margin,dollTop-offsetY-gap-panelHeight);
    Object.assign(panel.style,{left:`${left}px`,top:`${top}px`,right:"auto",bottom:"auto",width:`${panelWidth}px`,height:`${panelHeight}px`});
  }
  window.addEventListener("resize",layout);
  window.visualViewport?.addEventListener("resize",layout);
  window.visualViewport?.addEventListener("scroll",layout);
  layout();
  function message(text, kind = "assistant") {
    const el = document.createElement("div"); el.className = `ai-message ${kind}`;
    el.textContent = text; list.append(el); list.scrollTop = list.scrollHeight; return el;
  }
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
    try {
      const s = await api("/assistant/status"); available = s.available;
      statusText.textContent = (copy.status[s.reason_code] || copy.errors[s.reason] || copy.status.ready) + (s.available && allowance ? copy.usage(allowance.used,allowance.limit) : ""); enable.checked = s.enabled;
      if(s.preview_notice)statusText.textContent += " " + s.preview_notice;
      enable.closest("label").hidden = !isAdmin(); enable.disabled = busy || (!s.enabled && (!s.licensed || !s.configured));
      document.getElementById("ai-privacy").textContent = copy.privacy;
      input.disabled = !available || busy; send.disabled = !available || busy;
      return s;
    } catch (err) {available = false; statusText.textContent = errorText(err); input.disabled = send.disabled = true;}
  }
  launcher.addEventListener("click", async () => {
    panel.hidden = !panel.hidden; layout(); launcher.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) {await refreshStatus(); if (available) input.focus();}
  });
  document.getElementById("ai-open-settings").addEventListener("click", () => {if(panel.hidden)launcher.click();});
  document.getElementById("ai-close").addEventListener("click", () => {panel.hidden = true;launcher.setAttribute("aria-expanded", "false");launcher.focus();});
  panel.addEventListener("keydown", e => {if(e.key === "Escape"){document.getElementById("ai-close").click();e.stopPropagation();}});
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
    e.preventDefault(); if(busy || !available || !input.value.trim()) return;
    const text=input.value.trim(), requestGeneration=generation;
    busy=true;send.disabled=input.disabled=enable.disabled=true;
    message(text,"user");input.value="";const pending=message(copy.thinking);pending.setAttribute("role","status");
    try {
      const r=await post("/assistant/chat",{message:text,history:history.slice(-8),include_context:include.checked});
      pending.remove();
      if(requestGeneration===generation){message(r.reply);history.push({role:"user",content:text},{role:"assistant",content:r.reply.slice(0,3000)});history=history.slice(-8);}
      if(r.allowance?.limit)allowance=r.allowance;
      if(r.allowance?.limit)statusText.textContent=copy.usage(r.allowance.used,r.allowance.limit);
    } catch(err){pending.remove();if(requestGeneration===generation)message(errorText(err),"error");}
    finally{busy=false;await refreshStatus();if(!panel.hidden && available)input.focus();}
  });
  welcome();
})();
