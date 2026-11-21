/* Pure network policy and label placement helpers; shared by UI and Node checks. */
(function (root) {
  // Guest-reported OS takes precedence over the configured VM profile. Never
  // infer an OS from a VM name (a server called "windows-backup" may run Linux).
  function detectOS(vm = {}, guest = null) {
    const values = [guest?.os_name, vm.guest_info?.os_name, vm.guest?.os_name,
      vm.guest_os?.os_name, vm.guest_os?.name, vm.guest_os, vm.os_type];
    for (const value of values) {
      if (typeof value !== 'string') continue;
      if (/\bwindows\b|^win(?:dows)?(?:server|xp|vista|[0-9])/i.test(value)) return 'windows';
      if (/\blinux\b|ubuntu|debian|centos|fedora|rocky|rhel|red\s*hat|alma(?:linux)?|suse|alpine|amazon linux|oracle linux|arch linux/i.test(value)) return 'linux';
      // A concrete guest report of another OS wins over a stale configured OS.
      if (value.trim() && !/^(unknown|other|n\/a|not reported)$/i.test(value.trim())) return 'other';
    }
    return 'other';
  }
  // Original hologram line art. Only fixed, trusted paths enter the SVG markup.
  const osMarks = {
    windows: '<g fill="ACCENT" stroke="none"><path d="M18 17h12v12H18zM34 17h12v12H34zM18 33h12v12H18zM34 33h12v12H34z"/></g>',
    linux: '<path fill="DARK" d="M21 30c-4 6-7 11-5 15l7-3h18l7 3c2-4-1-9-5-15 2-17-24-17-22 0Z"/><ellipse cx="32" cy="36" rx="10" ry="13" fill="PALE"/><path fill="ACCENT" d="m26 26 6-3 6 3-6 5Zm-3 19-9 5 14 1 2-5m11-1 9 5-14 1-2-5"/><g fill="PALE" stroke="none"><circle cx="27" cy="21" r="3"/><circle cx="37" cy="21" r="3"/></g><g fill="DARK" stroke="none"><circle cx="28" cy="21" r="1.3"/><circle cx="36" cy="21" r="1.3"/></g>',
    other: '<path d="m32 15 15 9v17l-15 9-15-9V24Zm-15 9 15 9 15-9M32 33v17"/>'
  };
  function deviceIcon(kind = 'other', options = {}) {
    const muted = options.stopped, color = muted ? '#8193a5' : ({windows:'#65cfff',linux:'#ffd76e',firewall:'#b49bff'}[kind] || '#3ef0ff');
    const mark = k => osMarks[k].replaceAll('ACCENT', muted ? '#8193a5' : k === 'windows' ? '#65cfff' : '#ffd76e').replaceAll('PALE', muted ? '#9ba7b2' : '#eafaff').replaceAll('DARK','#092335');
    const paths = {
      node: '<rect x="17" y="6" width="30" height="50" rx="3"/><path d="M13 58h38M22 14h14m-14 9h14m-14 9h14m-14 9h14m-14 9h14"/><g fill="'+(muted ? '#8193a5' : '#6dffb3')+'"><circle cx="41" cy="14" r="1.5"/><circle cx="41" cy="23" r="1.5"/><circle cx="41" cy="32" r="1.5"/><circle cx="41" cy="41" r="1.5"/><circle cx="41" cy="50" r="1.5"/></g>',
      switch: '<path d="m6 23 8-9h36l8 9v24H6Zm0 0h52M11 40h4v-9h-4Zm9 0h4v-9h-4Zm9 0h4v-9h-4Zm9 0h4v-9h-4Zm9 0h4v-9h-4"/>',
      internet: '<circle cx="32" cy="30" r="23"/><ellipse cx="32" cy="30" rx="10" ry="23"/><path d="M9 30h46M13 17q19 10 38 0M13 43q19-10 38 0M32 7v46M24 59h16M32 53v6"/>',
      firewall: '<path d="m32 6 22 8-3 22Q46 51 32 58 18 51 13 36l-3-22Z"/><rect x="22" y="29" width="20" height="16" rx="3"/><path d="M26 29v-6a6 6 0 0 1 12 0v6m-6 6v4"/>',
      windows: '<rect x="6" y="7" width="52" height="43" rx="4"/><path d="M25 50v7m14-7v7M18 58h28"/>'+mark('windows'),
      linux: '<rect x="9" y="5" width="46" height="54" rx="4"/><path d="M15 54h8m24 0h2"/>'+mark('linux'),
      other: '<rect x="8" y="7" width="48" height="50" rx="5"/>'+mark('other')
    };
    const content = options.symbolOnly ? mark(osMarks[kind] ? kind : 'other') : (paths[kind] || paths.other);
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="nw-device-icon" aria-hidden="true">${content}</svg>`;
  }
  function deviceLegend() {
    return '<div class="nw-device-legend" aria-label="Device legend">'+[['node','ibHiCS node'],['switch','Trunk switch'],['internet','Internet / router'],['firewall','Firewall'],['windows','Windows VM'],['linux','Linux VM'],['other','Other VM']].map(([k,label])=>`<span>${deviceIcon(k)}${label}</span>`).join('')+'<span>● Green = running · Gray = stopped</span></div>';
  }
  const ipNumber = (s) => {
    const p = String(s).split('.');
    return p.length === 4 && p.every(x => /^\d{1,3}$/.test(x) && +x <= 255 && String(+x) === x)
      ? p.reduce((a, x) => a * 256 + +x, 0) : null;
  };
  function inCIDR(ip, cidr) {
    const [address, prefix = '32', ...extra] = cidr.split('/'), n = ipNumber(ip), base = ipNumber(address), bits = Number(prefix);
    if (extra.length || n === null || base === null || !/^\d+$/.test(prefix) || bits < 0 || bits > 32) return false;
    const size = 2 ** (32 - bits);
    return Math.floor(n / size) === Math.floor(base / size);
  }
  function ports(text) {
    if (!text) return [];
    if (!/^\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*$/.test(text)) throw Error('Use ports such as 80,443,8000-8100');
    return text.split(',').map(part => {
      const [a, b = a] = part.split('-').map(Number);
      if (!(a > 0 && a <= b && b < 65536)) throw Error('Ports must be 1–65535 with ascending ranges');
      return [a, b];
    });
  }
  function endpoint(value, inventory) {
    value = value.trim();
    if (ipNumber(value) !== null) return { label: value, ips: [value], networks: [], vm: null };
    const vm = inventory.find(v => v.name === value.replace(/^vm:/, ''));
    if (!vm) throw Error(`No VM named ${value}`);
    return { label: vm.name, ips: vm.ips || [], networks: (vm.nics || []).map(n => n.network), vm };
  }
  function matches(end, point, members = {}) {
    if (end === 'any') return true;
    if (end.startsWith('vm:')) return point.vm?.name === end.slice(3);
    if (end.startsWith('group:')) return !!point.vm && members[point.vm.uuid] === end.slice(6);
    return point.networks.includes(end) || point.ips.some(ip => inCIDR(ip, end));
  }
  function simulate(rules, flow, inventory, members) {
    const a = endpoint(flow.src, inventory), b = endpoint(flow.dst, inventory);
    for (const [i, r] of rules.entries()) {
      if (r.enabled === false || (r.proto !== 'any' && r.proto !== flow.proto)) continue;
      const ranges = [...ports(r.ports || ''), ...(r.port ? [[r.port, r.port]] : [])];
      if (ranges.length && !(flow.port && ranges.some(([lo, hi]) => +flow.port >= lo && +flow.port <= hi))) continue;
      if (matches(r.src, a, members) && matches(r.dst, b, members)) return { allowed: r.action === 'allow', rule: r.name, index: i + 1, src: a.label, dst: b.label };
    }
    return { allowed: true, rule: '', index: null, src: a.label, dst: b.label };
  }
  const overlaps = (a, b, gap = 5) => a.x < b.x + b.w + gap && a.x + a.w + gap > b.x && a.y < b.y + b.h + gap && a.y + a.h + gap > b.y;
  function placeLabels(points, width, height, obstacles = []) {
    const occupied = [...obstacles], out = [];
    for (const p of points) {
      let box = null;
      // Search near the front anchor first, then expand in small steps. Sparse
      // six-direction placement left avoidable holes in a 20-VM isometric scene.
      for (let radius = 12; radius <= 240 && !box; radius += 12) {
        for (const dy of [1, -1]) {
          for (const dx of [0, .5, -.5, 1, -1]) {
            const candidate = { x: p.x - p.w / 2 + dx * radius, y: p.y + dy * radius - (dy < 0 ? p.h : 0), w: p.w, h: p.h };
            if (candidate.x < 4 || candidate.y < 4 || candidate.x + p.w > width - 4 || candidate.y + p.h > height - 4 || occupied.some(b => overlaps(candidate, b))) continue;
            box = candidate; break;
          }
          if (box) break;
        }
      }
      if (!box) {
        let nearest = Infinity;
        for (let y = 4; y + p.h <= height - 4; y += 12) {
          for (let x = 4; x + p.w <= width - 4; x += 12) {
            const distance = (x + p.w / 2 - p.x) ** 2 + (y - p.y) ** 2;
            if (distance >= nearest) continue;
            const candidate = {x, y, w: p.w, h: p.h};
            if (occupied.some(b => overlaps(candidate, b))) continue;
            box = candidate; nearest = distance;
          }
        }
      }
      if (box) occupied.push(box);
      out.push({ ...p, box }); // Secondary labels are hidden when no free space remains.
    }
    return out;
  }
  // One floor, variable sized VLAN zones, with a central equipment aisle.
  function dioramaLayout(networks, vms, stopped = true) {
    const columns = Math.max(1, Math.ceil(Math.sqrt(networks.length)));
    const zones = networks.map(net => {
      const members = vms.filter(v => v.nics?.some(n => n.network === net.name) && (stopped || v.state === 'running'));
      const cols = Math.max(2, Math.ceil(Math.sqrt(members.length))), rows = Math.max(1, Math.ceil(members.length / cols));
      return {y: 0, w: Math.max(14, cols * 5.4 + 3), d: Math.max(12, rows * 5.4 + 5),
        slots: members.map((v, j) => ({uuid: v.uuid, ghost: v.nics[0].network !== net.name, x: (j % cols - (cols - 1) / 2) * 5.4, z: -rows * 5.4 / 2 + 2.7 + Math.floor(j / cols) * 5.4 + 1}))};
    });
    const widths = Array.from({length:columns}, (_,c) => Math.max(0,...zones.filter((_,i)=>i%columns===c).map(z=>z.w)));
    const depths = Array.from({length:Math.ceil(zones.length/columns)}, (_,r) => Math.max(...zones.slice(r*columns,(r+1)*columns).map(z=>z.d)));
    const aisle = Math.max(9, new Set([...networks.flatMap(n=>n.nodes||[n.node].filter(Boolean)), ...vms.map(v=>v.node).filter(Boolean)]).size * 2 + 3);
    const total = widths.reduce((a,b)=>a+b,0) + Math.max(0,columns-1)*3 + aisle;
    const depth = depths.reduce((a,b)=>a+b,0) + Math.max(0,depths.length-1)*3;
    return zones.map((zone,i)=>{
      const c=i%columns,r=Math.floor(i/columns), split=Math.floor(columns/2);
      return {...zone,x:-total/2+widths.slice(0,c).reduce((a,b)=>a+b+3,0)+widths[c]/2+(c>=split?aisle:0),
        z:-depth/2+depths.slice(0,r).reduce((a,b)=>a+b+3,0)+depths[r]/2};
    });
  }
  // Ordered membership only; CSS grid owns all dimensions and containment.
  function topViewLayout(networks, vms, stopped = true) {
    return [...networks].sort((a,b) => (a.name === 'default' ? -1 : b.name === 'default' ? 1 : (a.vlan || 1) - (b.vlan || 1)))
      .map(network => ({ network, cards: vms.filter(vm => (stopped || vm.state === 'running') && vm.nics?.some(n => n.network === network.name))
        .map(vm => ({ vm, ghost: vm.nics[0].network !== network.name })) }));
  }
  function clampSlot(p, zone) {
    return { x: Math.max(-zone.w / 2 + 1.8, Math.min(zone.w / 2 - 1.8, p.x)), z: Math.max(-zone.d / 2 + 2.5, Math.min(zone.d / 2 - 1.8, p.z)) };
  }
  function zoneAt(zones, point) {
    return zones.findIndex(z=>Math.abs(point.x-z.x)<=z.w/2 && Math.abs(point.z-z.z)<=z.d/2);
  }
  function stageNicMove(vms, uuid, index, network, networks) {
    const vm=vms.find(v=>v.uuid===uuid);
    if(!vm || !Number.isInteger(index) || !vm.nics[index]) throw Error('Select an existing NIC');
    if(!networks.some(n=>n.name===network)) throw Error('Target network no longer exists');
    return vms.map(v=>v.uuid===uuid?{...v,nics:v.nics.map((n,i)=>i===index?{...n,network}:{...n})}:v);
  }
  function draftRuleFromDrag(vm, target) {
    if(!vm || !target || target === `vm:${vm.name}`) return null;
    return {src:`vm:${vm.name}`,dst:target};
  }
  const api = { topViewLayout, detectOS, deviceIcon, deviceLegend, zoneAt, stageNicMove, draftRuleFromDrag, dioramaLayout, clampSlot, ipNumber, inCIDR, ports, endpoint, matches, simulate, overlaps, placeLabels };
  if (typeof module !== 'undefined') module.exports = api;
  else root.NetworkCore = api;
})(globalThis);
