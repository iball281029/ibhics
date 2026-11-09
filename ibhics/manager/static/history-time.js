/* The selection specifies aggregation and time-grid spacing, not just lookback. */
(function (root) {
  const profiles = {
    now: { label: "Now", windowLabel: "Last 10 minutes", windowMs: 600000, tickMs: 300000 },
    "5m": { label: "Every 5 min", windowLabel: "Last 30 minutes", windowMs: 1800000, tickMs: 300000 },
    "30m": { label: "Every 30 min", windowLabel: "Last 3 hours", windowMs: 10800000, tickMs: 1800000 },
    "1d": { label: "Daily", windowLabel: "Last 7 days", windowMs: 604800000, tickMs: 86400000 },
    "7d": { label: "Every 7 days", windowLabel: "Last 30 days", windowMs: 2592000000, tickMs: 604800000 },
  };
  function ticks(start, end, interval, utcOffsetMinutes = 0) {
    const shift = utcOffsetMinutes * 60000;
    const first = Math.ceil((start + shift) / interval) * interval - shift;
    const result = [];
    for (let at = first; at <= end; at += interval) result.push(at);
    return result;
  }
  const api = { profiles, ticks };
  root.HistoryTime = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
