/* Data-backed spatial presentation. Existing app.js owns all management actions. */
window.SpatialUI = (() => {
  // The VM page shows only the cards and list; summary figures sit above them.
  function renderVMs() {
    const all = state.vms, running = all.filter(v => v.state === 'running');
    const measuredCPU = running.map(v => state.stats[v.uuid]?.cpu_pct).filter(Number.isFinite);
    const measuredMemory = running.map(v => state.stats[v.uuid]?.mem_used_mb).filter(Number.isFinite);
    const cpu = measuredCPU.length ? measuredCPU.reduce((a,b) => a+b,0) / measuredCPU.length : null;
    const memory = measuredMemory.reduce((a,b) => a+b,0);
    const rx = running.reduce((s,v) => s + (state.stats[v.uuid]?.net_rx_bps || 0), 0);
    const tx = running.reduce((s,v) => s + (state.stats[v.uuid]?.net_tx_bps || 0), 0);
    const kpi = (label,value,note,cls) => `<div class="spatial-kpi ${cls}"><span>${esc(label)}</span><strong>${value}</strong><p>${esc(note)}</p></div>`;
    document.getElementById('spatial-vm-kpis').innerHTML = [
      kpi('ONLINE WORKLOADS', `${running.length}<small> / ${all.length}</small>`, `${all.length-running.length} not running`, 'k-green'),
      kpi('VM CPU AVERAGE', cpu == null ? '—' : `${cpu.toFixed(1)}<small> %</small>`, `${measuredCPU.length} of ${running.length} running VMs reporting`, 'k-cyan'),
      kpi('VM MEMORY IN USE', measuredMemory.length ? gbText(memory) : '—', `${measuredMemory.length} of ${running.length} running VMs reporting`, 'k-pink'),
      kpi('VM NETWORK', `${rate(rx)}`, `received · ${tx ? `${size(tx)}/s` : '0'} sent`, 'k-violet'),
    ].join('');
  }
  const observer = new MutationObserver(renderVMs);
  ['vms','vm-panel'].forEach(id => observer.observe(document.getElementById(id), {childList:true}));
  function storage(data) {
    const box = document.getElementById('virtual-storage-status');
    box.querySelector('.spatial-fabric')?.remove();
    const computes = data.nodes.filter(n => n.role === 'compute');
    const witness = data.nodes.find(n => n.role === 'witness');
    const entries = computes.map((node,index) => ({node,x:computes.length===1?50:20+60*index/Math.max(1,computes.length-1),y:68}));
    if (witness) entries.push({node:witness,x:50,y:27});
    const lines = entries.filter(e => e.node.role !== 'witness').map(e => `<path d="M50 48L${e.x} ${e.y}"/>`).join('') + (witness ? '<path d="M50 27V48"/>' : '');
    const graph = document.createElement('div'); graph.className = 'spatial-fabric';
    graph.innerHTML = `<div class="spatial-fabric-grid" aria-hidden="true"></div><div class="spatial-fabric-caption">REPLICATION / STORAGE TOPOLOGY</div><svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><g stroke="#9ebbea" stroke-width=".35" fill="none">${lines}</g><circle cx="50" cy="48" r="1.8" fill="#b9a1f5"/></svg>` + entries.map(({node,x,y}) => {
      const resource = node.resources?.find(r => r.name === data.created?.spec.name);
      const disks = resource?.devices || [];
      const upToDate = disks.length && disks.every(d => d['disk-state'] === 'UpToDate' && d.quorum);
      const status = !node.healthy ? 'Offline' : node.role==='witness' ? 'Quorum witness' : resource ? upToDate ? 'Up to date' : 'Check synchronization' : node.software_ready ? 'Software ready' : 'Preparation required';
      return `<div class="spatial-fabric-node ${node.role==='witness'?'witness':''}" style="left:${x}%;top:${y}%"><strong>${esc(node.display_name || node.name)}</strong><small>${node.role==='witness'?'Arbiter · no VM data':'Data node'}</small><span class="badge ${node.healthy?'plain':'failed'}">${esc(status)}</span></div>`;
    }).join('') + `<div class="spatial-fabric-foot">${witness ? 'Arbiter votes in quorum and stores no VM data.' : 'No Arbiter connected.'} ${data.created ? 'Configured capacity: '+esc(String(data.created.spec.size_gib))+' GiB.' : 'Virtual Storage has not been created.'}</div>`;
    box.prepend(graph);
  }
  return {renderVMs,storage};
})();

/* Home scene: links use the existing hash router; all geometry is decorative. */
(() => {
  const hero = document.querySelector('.nexus-hero');
  const visual = hero?.querySelector('.nexus-visual');
  if (!visual) return;
  const button = visual.querySelector('.nexus-motion');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let paused = reduced.matches;
  const sync = () => {
    visual.classList.toggle('is-paused', paused);
    button.disabled = reduced.matches;
    button.setAttribute('aria-pressed', String(paused));
    button.setAttribute('aria-label', paused ? 'Resume infrastructure animation' : 'Pause infrastructure animation');
    button.textContent = reduced.matches ? 'Reduced motion' : paused ? 'Resume motion' : 'Pause motion';
  };
  button.addEventListener('click', () => { paused = !paused; sync(); });
  reduced.addEventListener('change', () => { paused = reduced.matches; sync(); });
  visual.addEventListener('pointermove', event => {
    if (paused || reduced.matches || event.pointerType !== 'mouse') return;
    const rect = visual.getBoundingClientRect();
    visual.style.setProperty('--tilt-x', `${(0.5 - (event.clientY - rect.top) / rect.height) * 9}deg`);
    visual.style.setProperty('--tilt-y', `${((event.clientX - rect.left) / rect.width - 0.5) * 12}deg`);
  }, {passive:true});
  visual.addEventListener('pointerleave', () => {
    visual.style.setProperty('--tilt-x', '0deg');
    visual.style.setProperty('--tilt-y', '0deg');
  });
  const visibility = new IntersectionObserver(entries => {
    hero.classList.toggle('is-offscreen', !entries[0].isIntersecting || document.hidden);
  });
  visibility.observe(hero);
  document.addEventListener('visibilitychange', () => {
    hero.classList.toggle('is-offscreen', document.hidden || hero.getBoundingClientRect().bottom < 0);
  });
  sync();
})();
