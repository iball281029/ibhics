const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ibhics/manager/static/app.js', 'utf8');
const html = fs.readFileSync('ibhics/manager/static/index.html', 'utf8');

function setup() {
  const stop = {}, hint = {}, calls = [], messages = [];
  const form = {elements: {mode: {value: 'offline'}, allow_stop: {checked: true}, node: {}}};
  const c = vm.createContext({
    $: s => ({'#ct-migrate-form': form, '#ct-migrate-stop': stop,
      '#ct-migrate-mode-hint': hint, '#ct-migrate-summary': {}}[s]),
    state: {}, handlers: {}, esc: s => s, enc: encodeURIComponent,
    api: async () => ({members: [{name: 'node2', role: 'compute', healthy: true}]}),
    openDialog: () => {},
    post: async (url, body) => {calls.push({url, body}); return {result: c.result};},
    toast: m => messages.push(m), refresh: async () => {}, load: async () => {},
    renderTasks: () => {},
  });
  vm.runInContext(source.slice(source.indexOf('function updateCtMigrateMode('),
    source.indexOf('async function openCtSchedule(')), c);
  return {c, form, stop, hint, calls, messages};
}

test('Live is the default; only Offline requires stop consent for running guests', async () => {
  const {c, form, stop, hint} = setup();
  assert.match(html, /name="mode" value="live" checked/);
  await c.openCtMigrate({name: 'app', node: 'node1', state: 'running'});
  assert.equal(form.elements.mode.value, 'live');
  assert.equal(stop.hidden, true);
  assert.equal(form.elements.allow_stop.disabled, true);
  assert.equal(form.elements.allow_stop.required, false);
  assert.equal(form.elements.allow_stop.checked, false);
  assert.match(hint.textContent, /CRIU/);
  form.elements.mode.value = 'offline'; form.onchange();
  assert.equal(stop.hidden, false);
  assert.equal(form.elements.allow_stop.required, true);
  assert.equal(form.elements.allow_stop.disabled, false);
  assert.match(hint.textContent, /not transferred/);
  await c.openCtMigrate({name: 'app', node: 'node1', state: 'stopped'});
  form.elements.mode.value = 'offline'; form.onchange();
  assert.equal(form.elements.allow_stop.required, false);
});

test('sends migration mode and displays the actual result, including fallback reason', async () => {
  const {c, calls, messages} = setup();
  for (const result of ['Live (memory kept)', 'Restarted on destination: cgroup error']) {
    c.result = result;
    const values = new Map([['node', 'node2'], ['mode', 'live']]);
    await c.handlers['ct-migrate-dialog'](values, {name: 'app'});
    assert.equal(calls.at(-1).body.mode, 'live');
    assert.equal(calls.at(-1).body.allow_stop, false);
    assert.equal(messages.at(-1), result);
  }
});
