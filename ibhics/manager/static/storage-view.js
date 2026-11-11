/* Presentation only: preserve the per-node API mounts used by VM operations. */
(function (root) {
  function pools(rows) {
    const groups = new Map();
    for (const pool of rows) {
      const shared = pool.type === "nfs" && !!pool.source;
      const key = shared ? `nfs:${pool.source}` : JSON.stringify([pool.node, pool.name]);
      let group = groups.get(key);
      if (!group) {
        group = { ...pool, shared, members: [] };
        groups.set(key, group);
      } else if (!group.active && pool.active) {
        Object.assign(group, pool);
      }
      group.members.push(pool);
    }
    return [...groups.values()];
  }
  root.StorageView = { pools };
  if (typeof module !== "undefined") module.exports = root.StorageView;
})(globalThis);
