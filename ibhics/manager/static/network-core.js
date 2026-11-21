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
      for (const radius of [16, 40, 68, 100, 140, 180]) {
        for (const [dx, dy] of [[1,-1],[-1,-1],[1,1],[-1,1],[0,-1],[0,1]]) {
          const candidate = { x: p.x + (dx < 0 ? -p.w - radius : dx ? radius : -p.w / 2), y: p.y + (dy < 0 ? -p.h - radius : radius), w: p.w, h: p.h };
          if (candidate.x < 4 || candidate.y < 4 || candidate.x + p.w > width - 4 || candidate.y + p.h > height - 4 || occupied.some(b => overlaps(candidate, b))) continue;
          box = candidate; break;
        }
        if (box) break;
      }
      if (box) occupied.push(box);
      out.push({ ...p, box }); // Secondary labels are hidden when no free space remains.
    }
    return out;
  }
  const api = { ipNumber, inCIDR, ports, endpoint, matches, simulate, overlaps, placeLabels };
  if (typeof module !== 'undefined') module.exports = api;
  else root.NetworkCore = api;
})(globalThis);
