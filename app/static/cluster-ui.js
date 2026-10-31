import { state, esc, size, api, field, modal, copyValue, confirmAction, can, selectedHost } from './core.js';

export function hostPicker() {
  const hosts = state.clusterHosts;
  if (!hosts) return '';
  return `<label class="host-picker">Resources on <select id="cluster-host" aria-label="Manage host"><option value="">${esc(hosts.local.name)} · local</option>${hosts.hosts.map(h => `<option value="${esc(h.id)}" ${selectedHost() === h.id ? 'selected' : ''}>${esc(h.name)}</option>`).join('')}</select></label>`;
}

export function switchHost(id, fragment = 'overview') {
  // Reload deliberately closes consoles/streams and drops every host-local cache.
  location.href = '/' + (id ? '?host=' + encodeURIComponent(id) : '') + '#' + fragment;
}

export function renderCluster() {
  const hosts = state.clusterInventory?.hosts || [];
  const buttons = can('admin') ? '<button class="button primary" data-action="cluster-add">+ Link host</button><button class="button secondary" data-action="cluster-key">Create link key</button>' : '';
  return `<div class="page-title"><div><h1>Cluster</h1><p>Manage virtual machines across your linked hosts.</p></div><div class="actions">${buttons}</div></div>
    <div class="hint">Choose a VM to open its workspace, or choose a host to manage its networks, media, storage, backups and tasks. Access control and your account belong to the manager you signed in to.</div>
    ${hosts.map(h => `<section class="panel"><div class="panel-header"><h3>${esc(h.name)} ${h.id ? '' : '· local'} <span class="badge ${h.status === 'online' ? 'running' : 'stopped'}">${h.status === 'online' ? 'Connected' : 'Unavailable'}</span></h3><div class="actions">${h.status === 'online' ? `<button class="button small secondary" data-action="cluster-open-host" data-id="${esc(h.id)}">Manage host</button>` : ''}${h.id && can('admin') ? `<button class="button small danger" data-action="cluster-unlink" data-id="${esc(h.id)}">Unlink</button>` : ''}</div></div>
    ${h.host ? `<p class="cluster-host-summary">${h.vms.length} VMs · ${h.host.cpu_count} CPUs · ${size(h.host.memory_used)} / ${size(h.host.memory_total)} memory · ${size(h.host.disk_free)} disk free</p>` : ''}
    ${h.warnings?.length ? `<p class="cluster-host-summary error">${h.warnings.map(esc).join('<br>')}</p>` : ''}
    ${h.status === 'online' ? `<div class="table-wrap"><table><thead><tr><th>Virtual machine</th><th>Provider</th><th>Status</th><th>Host</th><th></th></tr></thead><tbody>${h.vms.map(v => `<tr><td>${esc(v.name)}</td><td>${esc(v.provider)}</td><td><span class="badge ${esc(v.status)}">${esc(v.status)}</span></td><td>${esc(h.name)}</td><td><button class="button small secondary" data-action="cluster-open-vm" data-id="${esc(h.id + '|' + v.id)}">Manage VM</button></td></tr>`).join('') || '<tr><td colspan="5">No virtual machines on this host.</td></tr>'}</tbody></table></div>` : '<p class="cluster-host-summary">Other hosts remain available. Restore this host’s connection or link key to manage its VMs.</p>'}</section>`).join('') || '<p>Loading cluster inventory…</p>'}
    ${can('admin') ? `<section class="panel"><div class="panel-header"><h3>Incoming link keys</h3></div><p class="cluster-host-summary">Create a key on a host, then use it when linking that host from this interface. Keys grant the linking manager control of this host’s resources using its users’ roles. Only link managers whose administrators you trust. To manage the cluster from another manager as well, add the links there too.</p><div class="table-wrap"><table><thead><tr><th>Link name</th><th></th></tr></thead><tbody>${(state.clusterKeys || []).map(k => `<tr><td>${esc(k.name)}</td><td><button class="button small danger" data-action="cluster-revoke" data-id="${esc(k.id)}">Revoke</button></td></tr>`).join('') || '<tr><td colspan="2">No incoming keys.</td></tr>'}</tbody></table></div></section>` : ''}`;
}

export async function clusterAction(name, id, refresh) {
  if (name === 'cluster-open-host') switchHost(id);
  if (name === 'cluster-open-vm') {
    const [host, vm] = id.split('|');
    switchHost(host, 'vm/' + encodeURIComponent(vm) + '/summary');
  }
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
