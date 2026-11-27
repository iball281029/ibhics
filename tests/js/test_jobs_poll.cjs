const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ibhics/manager/static/app.js', 'utf8');

function setup() {
  const timers = new Map(), listeners = {}, calls = [], renders = [];
  const tabs = new Set(['task-jobs']);
  const document = {hidden: false, body: {dataset: {section: 'tasks'}},
    addEventListener: (name, fn) => { listeners[name] = fn; }};
  const scroll = {scrollTop: 120, scrollLeft: 35};
  const context = vm.createContext({
    document, Map, Promise,
    $: selector => ({classList: {contains: () => tabs.has(selector.slice(5))}}),
    $$: selector => selector.endsWith('.table-wrap') ? [scroll] : [],
    setTimeout: (fn, delay) => { const id = {}; timers.set(id, {fn, delay}); return id; },
    setInterval: () => ({}),
    clearTimeout: id => timers.delete(id),
    allTasks: () => context.tasks,
    isActive: t => ['running', 'queued'].includes(t.state),
    renderTasks: () => renders.push('badge'),
    renderTaskPage: async () => { renders.push('tasks'); scroll.scrollTop = 0; scroll.scrollLeft = 0; },
    renderJobs: () => renders.push('backup'),
    renderImports: () => renders.push('migrate'),
    tasks: [],
    loaders: Object.fromEntries(['imports', 'jobs', 'moves', 'deploys', 'settingsTasks', 'replications', 'activity']
      .map(name => [name, async () => { calls.push(name); }])),
  });
  vm.runInContext(source.slice(source.indexOf('const loading = new Map();'), source.indexOf('// ---------- tasks ----------')), context);
  vm.runInContext(source.slice(source.indexOf('let jobsPollTimer ='), source.indexOf('async function refresh(')), context);
  return {context, timers, listeners, calls, renders, tabs, document, scroll};
}

test('polls idle Jobs at 20s and running/queued/retrying Jobs at 3s; preserves scroll', async () => {
  const {context: c, timers, calls, renders, scroll} = setup();
  c.syncJobsPoll();
  assert.equal([...timers.values()][0].delay, 20000);
  for (const state of ['running', 'queued', 'retry_wait']) {
    c.tasks = [{state}]; c.syncJobsPoll();
    assert.equal(timers.size, 1);
    assert.equal([...timers.values()][0].delay, 3000);
  }
  await c.pollJobs();
  assert(calls.includes('replications'));
  assert(calls.includes('activity'));
  assert.deepEqual(renders, ['badge', 'tasks']);
  assert.deepEqual(scroll, {scrollTop: 120, scrollLeft: 35});
  c.tasks = [{state: 'done'}]; c.syncJobsPoll();
  assert.equal([...timers.values()][0].delay, 20000);
});

test('stops on hidden document, page or subtab exit and resumes on visibility', async () => {
  const {context: c, timers, listeners, calls, tabs, document} = setup();
  c.syncJobsPoll(); document.hidden = true; listeners.visibilitychange();
  assert.equal(timers.size, 0);
  await c.pollJobs(); assert.equal(calls.length, 0);
  document.hidden = false; listeners.visibilitychange();
  assert.equal([...timers.values()][0].delay, 0);
  tabs.clear(); c.syncJobsPoll(); assert.equal(timers.size, 0);
  tabs.add('task-jobs'); document.body.dataset.section = 'home';
  c.syncJobsPoll(); assert.equal(timers.size, 0);
});

test('renders only the active backup or migration Jobs table', async () => {
  for (const [page, tab] of [['backup', 'jobs'], ['migrate', 'imports']]) {
    const {context: c, tabs, document, renders} = setup();
    tabs.clear(); tabs.add(tab); document.body.dataset.section = page;
    await c.pollJobs();
    assert.deepEqual(renders, ['badge', page]);
  }
});

test('shares pending reads with manual refresh and waits for all reads after failure', async () => {
  const {context: c, calls, renders, timers} = setup();
  let release;
  c.loaders.jobs = () => { calls.push('jobs'); return new Promise(resolve => { release = resolve; }); };
  c.loaders.moves = async () => { throw new Error('offline'); };
  const first = c.pollJobs();
  await Promise.resolve();
  const manual = vm.runInContext('load("jobs")', c);
  await c.pollJobs(); c.syncJobsPoll();
  assert.equal(calls.filter(name => name === 'jobs').length, 1);
  assert.equal(timers.size, 0, 'failure cannot restart polling while another read is pending');
  release(); await Promise.all([first, manual]);
  assert.equal(renders.length, 0);
  assert.equal(timers.size, 1, 'retry is scheduled after every read has settled');
});

test('does not render stale results or restart polling after leaving during a read', async () => {
  const {context: c, document, timers, renders} = setup();
  let release;
  c.loaders.jobs = () => new Promise(resolve => { release = resolve; });
  const pending = c.pollJobs(); await Promise.resolve();
  document.body.dataset.section = 'home'; c.syncJobsPoll();
  release(); await pending;
  assert.equal(renders.length, 0);
  assert.equal(timers.size, 0);
});
