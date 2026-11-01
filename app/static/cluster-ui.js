import { state, esc, api, field, modal, copyValue, confirmAction, can, selectedHost } from './core.js';

export function hostPicker() {
  const hosts = state.clusterHosts;
  if (!hosts) return '';
  return `<label class="host-picker">Resources on <select id="cluster-host" aria-label="Manage host"><option value="">${esc(hosts.local.name)} · local</option>${hosts.hosts.map(h => `<option value="${esc(h.id)}" ${selectedHost() === h.id ? 'selected' : ''}>${esc(h.name)}</option>`).join('')}</select></label>`;
}

export function switchHost(id, fragment = 'overview') {
  // Reload deliberately closes consoles/streams and drops every host-local cache.
  location.href = resourceURL(id, fragment);
}

const collapseKey = 'vmm-cluster-collapsed-hosts';
const collapsedHosts = new Set();
function isCollapsed(id) {
  try {
    if (!globalThis.localStorage) return collapsedHosts.has(id);
    const saved = JSON.parse(globalThis.localStorage.getItem(collapseKey) || '[]');
    if (Array.isArray(saved)) return saved.includes(id);
  } catch { /* Browsers can disable storage; retain in-memory state. */ }
  return collapsedHosts.has(id);
}

export function toggleClusterHost(id, root = document) {
  const collapsed = !isCollapsed(id);
  if (collapsed) collapsedHosts.add(id); else collapsedHosts.delete(id);
  try {
    const saved = JSON.parse(globalThis.localStorage?.getItem(collapseKey) || '[]');
    const values = new Set(Array.isArray(saved) ? saved : []);
    if (collapsed) values.add(id); else values.delete(id);
    globalThis.localStorage?.setItem(collapseKey, JSON.stringify([...values]));
  } catch { /* Collapse remains usable without persistent browser storage. */ }
  const group = root.getElementById('host-tree-' + (id || 'local'));
  if (!group) return;
  const button = group.querySelector('.tree-host-toggle');
  button.setAttribute('aria-expanded', String(!collapsed));
  button.setAttribute('aria-label', `${collapsed ? 'Expand' : 'Collapse'} ${group.dataset.hostname} resources`);
  button.firstElementChild.textContent = collapsed ? '▸' : '▾';
  group.querySelector('.tree-host-resources').hidden = collapsed;
}

export function resourceURL(host, fragment = 'overview') {
  return '/' + (host ? '?host=' + encodeURIComponent(host) : '') + '#' + fragment;
}

export function renderClusterTree() {
  const currentHost = selectedHost();
  const localPage = ['cluster', 'users', 'account', 'devops'].includes(state.page);
  const inventoryHost = localPage ? '' : currentHost;
  const fallback = state.inventory ? [{id: inventoryHost, name: state.inventory.host.name,
    host: state.inventory.host, vms: state.inventory.vms, status: 'online'}] : [];
  const hosts = state.clusterInventory?.hosts || fallback;
  const search = state.search.toLowerCase();
  return hosts.map(host => {
    const hostname = host.host?.name || host.name;
    const collapsed = isCollapsed(host.id);
    const key = host.id || 'local';
    // Selected-host polling is fresher than the cluster snapshot after VM operations.
    const vms = host.status === 'online' && state.inventory && host.id === inventoryHost
      ? state.inventory.vms : host.vms;
    const visible = (vms || []).filter(v => !search || v.name.toLowerCase().includes(search))
      .slice().sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
    return `<section class="tree-host-group" id="host-tree-${esc(key)}" data-hostname="${esc(hostname)}">
      <div class="tree-host ${host.id === currentHost && !localPage ? 'selected' : ''}">
        <button type="button" class="tree-host-toggle" data-action="toggle-host-tree" data-id="${esc(host.id)}" aria-expanded="${!collapsed}" aria-controls="host-tree-resources-${esc(key)}" aria-label="${collapsed ? 'Expand' : 'Collapse'} ${esc(hostname)} resources"><span aria-hidden="true">${collapsed ? '▸' : '▾'}</span></button>
        ${copyValue(hostname, "hostname", host.status === 'online' ? `<a class="tree-host-name" href="${esc(resourceURL(host.id))}" title="${esc(host.name)}">${esc(hostname)}</a>` : `<span class="tree-host-name">${esc(hostname)}</span>`)}
        <span class="subtle tree-host-count">${host.status === 'online' ? visible.length : 'Unavailable'}</span>
      </div>
      <div class="tree-host-resources" id="host-tree-resources-${esc(key)}"${collapsed ? ' hidden' : ''}>
        ${host.status !== 'online' ? '<div class="tree-vm subtle">Host unavailable</div>' : visible.map(vm =>
          `<div class="tree-vm ${!localPage && state.page === 'vm' && currentHost === host.id && state.vm === vm.id ? 'active' : ''}"><span class="dot ${esc(vm.status)}" aria-hidden="true"></span>${copyValue(vm.name, "hostname", `<a href="${esc(resourceURL(host.id, 'vm/' + encodeURIComponent(vm.id) + '/summary'))}" title="${esc(vm.name + ' · ' + vm.provider + ' · ' + vm.status)}">${esc(vm.name)}</a>`)}<span class="tree-vm-provider">${esc(vm.provider)}</span></div>`
        ).join('') || '<div class="tree-vm subtle">No matching VMs</div>'}
      </div></section>`;
  }).join('') || '<p class="muted">Loading cluster hosts…</p>';
}

export function renderCluster() {
  const hosts = state.clusterInventory?.hosts || [];
  const buttons = can('admin') ? '<button class="button primary" data-action="cluster-add">+ Link host</button><button class="button secondary" data-action="cluster-key">Create link key</button>' : '';
  return `<div class="page-title"><div><h1>Cluster</h1><p>Link hosts and manage trusted connections.</p></div><div class="actions">${buttons}</div></div>
    <div class="hint">Browse all hosts and their virtual machines in the left resource tree. This page manages cluster membership and incoming link keys.</div>
    <section class="panel"><div class="panel-header"><h3>Cluster hosts</h3></div><div class="table-wrap"><table><thead><tr><th>Host</th><th>Link name</th><th>Connection</th><th>Actions</th></tr></thead><tbody>
    ${hosts.map(h => `<tr><td>${esc(h.host?.name || h.name)}</td><td>${h.id ? esc(h.name) : 'Local manager'}</td><td><span class="badge ${h.status === 'online' ? 'running' : 'stopped'}">${h.status === 'online' ? 'Connected' : 'Unavailable'}</span></td><td>${h.id && can('admin') ? `<button class="button small danger" data-action="cluster-unlink" data-id="${esc(h.id)}">Unlink</button>` : '—'}</td></tr>`).join('') || '<tr><td colspan="4">Loading cluster hosts…</td></tr>'}
    </tbody></table></div></section>
    ${can('admin') ? `<section class="panel"><div class="panel-header"><h3>Incoming link keys</h3></div><p class="cluster-host-summary">Create a key on a host, then use it when linking that host from this interface. Keys grant the linking manager control of this host’s resources using its users’ roles. Only link managers whose administrators you trust. To manage the cluster from another manager as well, add the links there too.</p><div class="table-wrap"><table><thead><tr><th>Link name</th><th></th></tr></thead><tbody>${(state.clusterKeys || []).map(k => `<tr><td>${esc(k.name)}</td><td><button class="button small danger" data-action="cluster-revoke" data-id="${esc(k.id)}">Revoke</button></td></tr>`).join('') || '<tr><td colspan="2">No incoming keys.</td></tr>'}</tbody></table></div></section>` : ''}`;
}

export async function clusterAction(name, id, refresh) {
  if (name === 'cluster-add') {
    const dialog = modal('Link a VM Manager host',
      `<p>Install VM Manager on the other host, open its Cluster page and create a link key. Use an HTTPS address reachable from this server.</p>${field('Host name', 'name')}${field('HTTPS address', 'url', '', {placeholder: 'https://192.168.8.236:8443'})}${field('Link key', 'token', '', {type: 'password'})}<label class="field full"><span>Public PEM certificate or CA (optional)</span><textarea name="certificate" rows="5" placeholder="-----BEGIN CERTIFICATE-----"></textarea><small>Required for a self-signed installation. Copy the public certificate from /opt/carrtech/vm-manager/tls/server.crt on that host. Leave blank for a publicly trusted certificate. HTTPS verification is always enabled.</small></label>`,
      async f => { await api('/cluster/hosts', 'POST', Object.fromEntries(f)); await refresh(true); },
      {submit: 'Verify & link', wide: true});
    dialog.querySelector('[name=name]').maxLength = 64;
  }
  if (name === 'cluster-key') modal('Create incoming link key',
    `<p>This lets another trusted VM Manager manage resources on this host. The key is shown once.</p>${field('Link name', 'name', '', {placeholder: 'Main manager'})}`,
    async f => {
      const key = await api('/cluster/keys', 'POST', {name: f.get('name')});
      // Wait for the original modal submit handler to close before displaying the key.
      setTimeout(() => {
        const dialog = modal('Save this link key', `<p>Copy this key into the other manager’s Link host form. It will not be shown again.</p><div class="hint" style="overflow-wrap:anywhere">${copyValue(key.token, 'link key')}</div>`, async () => { await refresh(true); }, {submit: 'Done'});
        dialog.querySelector('.close-modal').focus();
      }, 0);
    }, {submit: 'Create key'});
  if (name === 'cluster-unlink') {
    const host = state.clusterHosts.hosts.find(h => h.id === id);
    if (host) confirmAction('Unlink host', host.name,
      'This removes the connection from this manager. VMs and data remain on the other host. Revoke the incoming key there if it is no longer needed.',
      async () => { await api('/cluster/hosts/' + id, 'DELETE'); await refresh(true); });
  }
  if (name === 'cluster-revoke') {
    const key = state.clusterKeys.find(k => k.id === id);
    if (key) confirmAction('Revoke link key', key.name,
      'Managers using this key will lose access to this host. Their active streams close at the next permission check.',
      async () => { await api('/cluster/keys/' + id, 'DELETE'); await refresh(true); });
  }
}
