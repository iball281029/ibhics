'use strict';
const $ = (id) => document.getElementById(id);
async function api(path, body) {
  const response = await fetch('/api/' + path, {method: body ? 'POST' : 'GET', credentials: 'same-origin', headers: body ? {'Content-Type':'application/json'} : {}, body: body ? JSON.stringify(body) : undefined, cache:'no-store'});
  const data = await response.json();
  if (!response.ok) { const error = new Error(data.detail || 'Request failed'); error.status = response.status; throw error; }
  return data;
}
function showLogin() {
  $('status-panel').hidden = true; $('logout').hidden = true; $('login-panel').hidden = false;
  $('pairing-key').value = ''; $('login-form').elements.password.value = '';
}
async function loadStatus() {
  try {
    const s = await api('status');
    $('login-panel').hidden = true; $('status-panel').hidden = false; $('logout').hidden = false;
    $('node-name').textContent = s.name; $('node-id').value = s.name;
    $('address').textContent = s.address; $('agent-url').value = s.agent_url; $('pairing-key').value = s.pairing_key;
    $('agent-state').textContent = s.agent_running ? 'Online' : 'Offline'; $('agent-state').className = 'state ' + (s.agent_running ? 'online' : 'offline');
    const r = s.replication;
    $('replication-state').textContent = r.error ? 'Setup required' : r.resources.length ? 'Configured' : 'Not configured';
    $('replication-detail').textContent = r.error || ('DRBD ' + r.kernel_version + ' · ' + r.resources.length + ' resources. Cluster joining and storage replication are separate setup steps.');
    $('status-error').textContent = '';
  } catch (e) { if (e.status === 401) showLogin(); else $('status-error').textContent = e.message; }
}
$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button=event.target.querySelector('button'); button.disabled=true; $('login-error').textContent='';
  try { await api('login', {username:'root', password:event.target.elements.password.value}); event.target.elements.password.value=''; await loadStatus(); }
  catch(e) { $('login-error').textContent=e.message; } finally { button.disabled=false; }
});
$('logout').addEventListener('click', async () => {try {await api('logout', {});} finally {showLogin();}});
$('refresh').addEventListener('click', loadStatus);
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
  const field=$(button.dataset.copy);
  try {await navigator.clipboard.writeText(field.value); $('copy-note').textContent=button.dataset.copy==='pairing-key' ? 'Pairing key copied' : 'Agent URL copied';}
  catch {field.focus();field.select();$('copy-note').textContent='Selected — press Ctrl+C or Command+C to copy';}
}));
loadStatus();
