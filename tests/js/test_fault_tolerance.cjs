// How many servers can fail: min of quorum, compute nodes, data copies and spare RAM.
const assert = require("assert");
const fs = require("fs");
const src = fs.readFileSync(`${__dirname}/../../ibhics/manager/static/app.js`, "utf8");
const gbText = (mb) => `${mb / 1024} GB`; // eslint-disable-line no-unused-vars
const ft = eval(`(${src.slice(src.indexOf("function clusterFaultTolerance"), src.indexOf("function renderCluster()"))})`);
const cluster = (compute, witness) => ({ members: [
  ...Array.from({ length: compute }, (_, i) => ({ name: `n${i}`, role: "compute" })),
  ...Array.from({ length: witness }, (_, i) => ({ name: `w${i}`, role: "witness" })),
] });
const hosts = Object.fromEntries(Array.from({ length: 5 }, (_, i) => [`n${i}`, { memory_mb: 65536 }]));
const vstore = [{ shared: true, name: "virtualstorage" }], san = [{ shared: true, name: "san" }];
const vms = [{ state: "running", memory_mb: 8192 }];
const n = (c, w, pools, v = vms) => ft(cluster(c, w), pools, v, hosts).n;
assert.strictEqual(n(2, 0, vstore), 0, "2 nodes without a witness");
assert.strictEqual(n(2, 1, vstore), 1, "2 nodes + witness");
assert.strictEqual(n(3, 0, vstore), 1);
assert.strictEqual(n(4, 0, san), 1);
assert.strictEqual(n(5, 0, san), 2, "5 nodes on shared storage");
assert.strictEqual(n(5, 0, vstore), 1, "two copies allow one failure");
assert.strictEqual(n(3, 0, []), 0, "local disks only");
assert.strictEqual(n(5, 0, san, [{ state: "running", memory_mb: 65536 * 4 + 1024 }]), 0, "RAM full");
assert.deepStrictEqual(ft(cluster(5, 0), san, vms, hosts).limiting.map((l) => l.key), ["quorum"]);
console.log("Cluster fault tolerance checks passed");
