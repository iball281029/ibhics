const test = require('node:test');
const assert = require('node:assert/strict');
const {profiles, ticks} = require('../../ibhics/manager/static/history-time.js');
const end = Date.parse('2026-11-09T10:38:00Z'); // 17:38 Asia/Bangkok
const offset = 420;
function hhmm(at) { return new Date(at + offset * 60000).toISOString().slice(11, 16); }
for (const key of ['now', '5m', '30m']) {
  test(`${key} labels match the selected interval and land on clock boundaries`, () => {
    const p = profiles[key];
    const values = ticks(end - p.windowMs, end, p.tickMs, offset);
    assert(values.length >= 2);
    for (let i = 1; i < values.length; i++) assert.equal(values[i] - values[i-1], p.tickMs);
    for (const at of values) assert.equal(new Date(at).getUTCMinutes() % (key === '30m' ? 30 : 5), 0);
    if (key === 'now') assert.deepEqual(values.map(hhmm), ['17:30','17:35']);
    if (key === '30m') assert.deepEqual(values.map(hhmm), ['15:00','15:30','16:00','16:30','17:00','17:30']);
  });
}
test('5-minute view has a denser labelled grid than Now', () => {
  assert(ticks(end-profiles['5m'].windowMs,end,300000,offset).length > ticks(end-profiles.now.windowMs,end,300000,offset).length);
});
test('daily ticks use local midnight', () => {
  for(const at of ticks(end-profiles['1d'].windowMs,end,86400000,offset)) assert.equal(hhmm(at),'00:00');
});
