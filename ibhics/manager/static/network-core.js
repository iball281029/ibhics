/* Pure network policy and label placement helpers; shared by UI and Node checks. */
(function (root) {
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
      const members = vms.filter(v => v.nics?.[0]?.network === net.name && (stopped || v.state === 'running'));
      const cols = Math.max(2, Math.ceil(Math.sqrt(members.length))), rows = Math.max(1, Math.ceil(members.length / cols));
      return {y: 0, w: Math.max(14, cols * 5.4 + 3), d: Math.max(12, rows * 5.4 + 5),
        slots: members.map((v, j) => ({uuid: v.uuid, x: (j % cols - (cols - 1) / 2) * 5.4, z: -rows * 5.4 / 2 + 2.7 + Math.floor(j / cols) * 5.4 + 1}))};
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
  const api = { zoneAt, stageNicMove, draftRuleFromDrag, dioramaLayout, clampSlot, ipNumber, inCIDR, ports, endpoint, matches, simulate, overlaps, placeLabels };
  if (typeof module !== 'undefined') module.exports = api;
  else root.NetworkCore = api;
})(globalThis);
