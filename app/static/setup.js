import { state, api, esc, field, check, can } from './core.js';

let phase = 'choice';
let draft = null;
let done = null;
let timer = null;
let checked = false;

export const setupDestination = (plan, origin = globalThis.location?.origin) => {
  if (plan.public_url) return plan.public_url;
  if (['0.0.0.0', '::'].includes(plan.bind_address)) return origin;
  const host = plan.bind_address.includes(':') ? `[${plan.bind_address}]` : plan.bind_address;
  return `https://${host}:8443`;
};

export function setupView(status, current, step, admin = true) {
  const progress = status.status === 'running' || step === 'progress';
  const heading = progress ? (status.status === 'completed' ? 'Your host is ready' : status.status === 'failed' ? 'Setup needs attention' : 'Setting up your host') : 'Welcome to VM Manager';
  let body;
  if (!admin) body = '<p>An administrator needs to complete host setup before this installation can manage virtual machines.</p>';
  else if (progress) {
    body = `<ol class="setup-progress" aria-live="polite">${(status.steps || []).map(s => `<li>${esc(s.message)}</li>`).join('')}</ol>`;
    if (status.status === 'running') body += '<p role="status">Setup is running on the host. You can close this browser and return to see its progress.</p>';
    if (status.status === 'failed') body += `<p class="error-banner" role="alert">${esc(status.error)}</p><p>Completed steps and existing resources are retained. Review the settings before retrying.</p><button class="button primary" type="button" id="setup-retry">Review and retry</button>`;
    if (status.status === 'completed') body += `<p>VM Manager is restarting to apply service access and the confirmed settings. Allow a few seconds, then continue.</p><a class="button primary" id="setup-continue" href="${esc(setupDestination(status.plan))}/#${status.plan.mode === 'cluster' ? 'cluster' : 'overview'}">${status.plan.mode === 'cluster' ? 'Open Cluster to link a host' : 'Open VM Manager'}</a><p>You can manage cluster links from the Cluster page at any time.</p>`;
  } else if (step === 'choice') {
    body = `<p>Choose how this machine will be used. Review the required components and networking before applying changes.</p>
      <label class="setup-choice"><input type="radio" name="mode" value="local" ${current.mode === 'local' ? 'checked' : ''}><span><strong>Host virtual machines</strong><small>Install Incus and QEMU/KVM to create and manage VMs here. This host can also link to other VM Manager hosts.</small></span></label>
      <label class="setup-choice"><input type="radio" name="mode" value="cluster" ${current.mode === 'cluster' ? 'checked' : ''}><span><strong>Manage a cluster only</strong><small>Link to existing VM Manager hosts without installing a local hypervisor or creating a VM network.</small></span></label>
      <p class="subtle">${esc(status.os || '')}${status.kvm === false ? ' · Hardware virtualization is unavailable on this host.' : ''}</p><button class="button primary" type="submit">Continue</button>`;
  } else if (step === 'config') {
    const nicAddresses = [...new Set((status.interfaces || []).map(i => i.address))];
    body = `<h2>Confirm host settings</h2><p>These settings are saved in <code>/etc/vm-manager/vm-manager.conf</code>. The listen address is required.</p>
      ${field('Listen address · VM_MANAGER_HOST', 'bind_address', current.bind_address, {hint: `Use 0.0.0.0 for all IPv4 interfaces, :: for IPv6, or an address on this host. Detected: ${nicAddresses.join(', ') || 'none'}.`})}
      ${field('Public HTTPS URL · optional', 'public_url', current.public_url, {required: false, hint: 'VM_MANAGER_PUBLIC_URL: set this when users connect through NAT or a reverse proxy. Otherwise leave it blank.'})}
      ${check('Allow HTTPS port 8443 in the active host firewall · VM_MANAGER_MANAGE_FIREWALL', 'manage_firewall', current.manage_firewall)}
      ${current.mode === 'local' ? `<h2>Virtual machine storage and network</h2><p>Incus and QEMU/KVM are required for local VM creation.</p>${check('Also install libvirt to discover and manage libvirt VMs', 'libvirt', current.libvirt)}
      ${check('Create a directory storage pool on the host filesystem', 'create_pool', current.create_pool)}
      ${field('Storage pool name', 'pool', current.pool, {hint: `Uncheck Create to use an existing pool. Available: ${(status.pools || []).join(', ') || 'none detected'}. Directory storage supports basic VMs; advanced live backups require other storage drivers.`})}
      ${check('Create a private NAT bridge for VMs', 'create_network', current.create_network)}
      ${field('VM network name', 'network', current.network, {hint: `Uncheck Create to use an existing managed bridge. Available: ${(status.networks || []).map(n => n.name + ' (' + n.address + ')').join(', ') || 'none detected'}.`})}
      ${field('VM gateway address and subnet prefix', 'network_address', current.network_address, {required: false, placeholder: '10.210.0.1/24', hint: 'Required when creating a network. Choose an unused private subnet that does not overlap your LAN, VPN or another VM network. Example: 10.210.0.1/24.'})}` : '<p>Cluster management requires no local VM storage or VM network.</p>'}
      <div class="setup-actions"><button class="button secondary" type="button" id="setup-back">Back</button><button class="button primary" type="submit">Review setup</button></div>`;
  } else {
    const rocky = /Rocky|AlmaLinux/.test(status.os || '');
    body = `<h2>Review and apply</h2><dl class="setup-summary"><dt>Role</dt><dd>${current.mode === 'local' ? 'Local VM host with cluster management' : 'Cluster management only'}</dd><dt>Listen address</dt><dd>${esc(current.bind_address)}:8443</dd><dt>Public URL</dt><dd>${esc(current.public_url || 'Direct host access')}</dd><dt>HTTPS firewall access</dt><dd>${current.manage_firewall ? 'Allow in active firewall' : 'Managed manually'}</dd>
      ${current.mode === 'local' ? `<dt>Install and start</dt><dd>Incus, QEMU/KVM${current.libvirt ? ' and libvirt' : ''}</dd><dt>Storage</dt><dd>${current.create_pool ? 'Create directory pool' : 'Use existing pool'}: ${esc(current.pool)}</dd><dt>VM network</dt><dd>${current.create_network ? 'Create private NAT bridge' : 'Use existing bridge'}: ${esc(current.network)}${current.create_network ? ' · ' + esc(current.network_address) : ''}</dd>` : ''}</dl>
      ${current.mode === 'local' && rocky ? '<p>Rocky/AlmaLinux setup enables EPEL and CRB, the signed current Incus repository (cofob), and ligenix/enterprise-qemu-spice for graphical console support. The Incus signing key is pinned in VM Manager. DNF verifies package signatures. Internet access is required.</p>' : current.mode === 'local' ? '<p>Setup installs packages from the configured Debian repositories. Internet access is required.</p>' : ''}
      <p>Existing VM data, storage pools, networks and other configuration settings are preserved. VM Manager restarts after setup.</p>
      <label class="check"><input type="checkbox" name="confirmed" required> I confirm these host settings${current.mode === 'local' && current.create_network ? ' and the VM network gateway/subnet' : ''}, and authorize the listed changes.</label>
      <div class="setup-actions"><button class="button secondary" type="button" id="setup-back">Back</button><button class="button primary" type="submit">Apply setup</button></div>`;
  }
  return `<main class="setup-screen"><form class="setup-card" id="setup-form"><div class="login-brand">▣ CARRTECH</div><span class="product">VM MANAGER · FIRST-TIME SETUP</span><h1>${heading}</h1><p class="subtle">${progress ? 'Apply and verify' : step === 'choice' ? '1. Host role' : step === 'config' ? '2. Required configuration and VM network' : '3. Confirm changes'}</p>${body}<p class="form-error" id="setup-error" role="alert" hidden></p><button class="button secondary" type="button" id="setup-logout">Sign out</button></form></main>`;
}

export function renderFirstRun() {
  if (!state.setup?.required && !state.setupScreen) return false;
  const root = document.querySelector('#app');
  const changedStep = root.dataset.setupStep !== phase;
  root.dataset.setupStep = phase;
  root.innerHTML = setupView(state.setup, draft, phase, can('admin'));
  if (changedStep) root.querySelector('.setup-screen').scrollIntoView({block: 'start'});
  const error = message => { const target = root.querySelector('#setup-error'); target.textContent = message; target.hidden = false; };
  root.querySelector('#setup-continue')?.addEventListener('click', event => {
    event.preventDefault();
    const target = event.currentTarget.href;
    if (new URL(target).origin === location.origin) { location.href = target; location.reload(); }
    else location.assign(target);
  });
  root.querySelector('#setup-logout').onclick = async () => { await api('/logout', 'POST'); location.reload(); };
  root.querySelector('#setup-retry')?.addEventListener('click', () => { phase = 'config'; renderFirstRun(); });
  root.querySelector('#setup-back')?.addEventListener('click', () => { capture(); phase = phase === 'review' ? 'config' : 'choice'; renderFirstRun(); });
  function capture() {
    const form = new FormData(root.querySelector('#setup-form'));
    if (phase === 'choice') draft.mode = form.get('mode');
    if (phase === 'config') {
      for (const key of ['bind_address', 'public_url', 'pool', 'network', 'network_address'])
        if (form.has(key)) draft[key] = form.get(key).trim();
      for (const key of ['manage_firewall', 'libvirt', 'create_pool', 'create_network'])
        if (root.querySelector(`[name="${key}"]`)) draft[key] = form.has(key);
    }
  }
  root.querySelector('#setup-form').onsubmit = async event => {
    event.preventDefault();
    capture();
    if (phase === 'choice') {
      if (draft.mode === 'local' && !state.setup.supported) return error('Automatic local setup is supported on Debian 13 and Rocky/AlmaLinux 10.');
      if (draft.mode === 'local' && !state.setup.kvm) return error('Enable hardware or nested virtualization before configuring local VM hosting.');
      phase = 'config';
    } else if (phase === 'config') {
      if (!draft.bind_address) return error('Confirm the VM Manager listen address.');
      if (draft.mode === 'local' && draft.create_network && !draft.network_address) return error('Enter and confirm the VM network gateway address and subnet prefix.');
      phase = 'review';
    } else if (phase === 'review') {
      const button = root.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        state.setup = await api('/setup', 'POST', {...draft, confirmed: true});
        phase = 'progress';
        schedule();
      } catch (e) { button.disabled = false; return error(e.message); }
    }
    renderFirstRun();
  };
  return true;
}

function schedule() {
  clearTimeout(timer);
  if (state.user && state.setup?.status === 'running') timer = setTimeout(async () => {
    try { await checkFirstRun(done, true); } catch { schedule(); }
  }, 2000);
}

export async function checkFirstRun(onDone, poll = false) {
  if (checked && !state.setupScreen) return false;
  done = onDone;
  const status = await api('/setup');
  checked = true;
  if (status.status === 'running' || (state.setupScreen && status.status === 'completed')) phase = 'progress';
  state.setup = status;
  if (!status.required && !state.setupScreen) return false;
  if (!draft) {
    draft = {mode: 'local', libvirt: true, pool: status.pools?.[0] || 'vm-manager', create_pool: !status.pools?.length,
      network: status.networks?.[0]?.name || 'vmmbr0', create_network: !status.networks?.length,
      network_address: '', ...status.config, ...status.plan};
    if (['running', 'failed', 'completed'].includes(status.status)) phase = 'progress';
  }
  state.setupScreen = true;
  // Ordinary inventory polling must not replace a form the user is editing.
  if (poll || !document.querySelector('#setup-form') || phase === 'progress') renderFirstRun();
  schedule();
  return true;
}

export function resetFirstRun() {
  clearTimeout(timer);
  checked = false; draft = null; phase = 'choice'; state.setupScreen = false; state.setup = null;
}
