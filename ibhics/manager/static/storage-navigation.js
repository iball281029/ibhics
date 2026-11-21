/* Independent Storage workspaces. Hashes are compatible with the main page router. */
(() => {
  const tabs = [...document.querySelectorAll('[data-storage-tab]')];
  function sync() {
    const parts = location.hash.slice(1).split('/');
    const name = ['local', 'iso'].includes(parts[1]) ? parts[1] : 'virtual';
    tabs.forEach((tab) => {
      const selected = tab.dataset.storageTab === name;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    document.querySelectorAll('[data-storage-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.storagePanel !== name;
    });
    if (parts[0] === 'storage' && name === 'virtual') {
      window.VirtualStorageUI.selectTab(parts[2] || 'overview');
    }
  }
  const nav = document.getElementById('storage-tabs');
  function go(tab) {
    location.hash = `storage/${tab.dataset.storageTab}`;
    sync();
  }
  nav.addEventListener('click', (event) => {
    const tab = event.target.closest('[data-storage-tab]');
    if (tab) go(tab);
  });
  nav.addEventListener('keydown', (event) => {
    const index = tabs.indexOf(event.target);
    if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    go(tabs[next]); tabs[next].focus();
  });
  document.getElementById('page-storage').addEventListener('click', (event) => {
    const button = event.target.closest('[data-storage-view]');
    if (!button) return;
    const panel = button.closest('[data-storage-panel]');
    panel.classList.toggle('storage-panels', button.dataset.storageView === 'panels');
    panel.querySelectorAll('[data-storage-view]').forEach((item) => {
      item.setAttribute('aria-pressed', String(item === button));
    });
  });
  window.addEventListener('hashchange', sync);
  const syncVisibility = () => document.body.classList.toggle('storage-document-hidden', document.hidden);
  document.addEventListener('visibilitychange', syncVisibility);
  syncVisibility();
  sync();
})();
