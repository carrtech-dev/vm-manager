import { test } from 'node:test';
import assert from 'node:assert/strict';
import { apiURL, hostURL, state } from '../app/static/core.js';
import { renderEmbeddedConsole } from '../app/static/console-view.js';
import { renderCluster, renderClusterTree, toggleClusterHost } from '../app/static/cluster-ui.js';

const onHost = fn => {
  globalThis.location = { search: '?host=second-node' };
  try { fn(); } finally { delete globalThis.location; }
};

test('all operational requests use the selected host while account and trust controls stay local', () => onHost(() => {
  for (const path of ['/inventory', '/vms/incus:same/action', '/tasks/one/output?after=4', '/isos', '/backups', '/monitoring/host', '/operations/patching/check', '/console/incus:same'])
    assert.equal(apiURL(path), '/api/cluster/hosts/second-node/proxy' + path);
  for (const path of ['/me', '/login', '/logout', '/password', '/users/3', '/cluster/inventory'])
    assert.equal(apiURL(path), '/api' + path);
  assert.equal(apiURL('/inventory', ''), '/api/inventory');
}));

test('standalone, embedded and switched consoles retain owning host identity', () => onHost(() => {
  assert.equal(hostURL('/console?vm=incus%3Atest'), '/console?vm=incus%3Atest&host=second-node');
  const html = renderEmbeddedConsole({id: 'incus:host-test', name: 'test', provider: 'incus'});
  assert.match(html, /terminal\?vm=incus%3Ahost-test&amp;host=second-node&amp;embedded=1/);
  assert.match(html, /console\?vm=incus%3Ahost-test&amp;host=second-node/);
}));

const clusterFixture = () => {
  Object.assign(state, {user: {role: 'viewer'}, inventory: null, page: 'cluster', search: '', project: 'unassigned', tag: 'untagged',
    clusterInventory: {hosts: [
      {id: '', name: 'local', host:{name:'ctlnxdbn001'}, status: 'online', vms: [{id: 'incus:same', name: 'same', provider: 'incus', status: 'running'}]},
      {id: 'remote', name: 'Nested test host', host:{name:'vmm-cluster-test-host'}, status: 'online', vms: [{id: 'incus:same', name: 'same', provider: 'incus', status: 'stopped'}, {id:'libvirt:other',name:'<other>',provider:'libvirt',status:'running'}]},
      {id: 'offline', name: '<unavailable>', status: 'offline', vms: [], warnings: ['Host unavailable']},
    ]}});
};

test('sidebar shows every host and its VMs with distinct owning-host links and escaped names', () => {
  clusterFixture();
  const html = renderClusterTree();
  assert.match(html, /ctlnxdbn001/);
  assert.match(html, /vmm-cluster-test-host/);
  assert.match(html, /href="\/#vm\/incus%3Asame\/summary"/);
  assert.match(html, /href="\/\?host=remote#vm\/incus%3Asame\/summary"/);
  assert.match(html, /&lt;other&gt;/);
  assert.match(html, /&lt;unavailable&gt;/);
  assert.match(html, /Host unavailable/);
  assert.match(html, /dot running/);
  assert.match(html, /dot stopped/);
  // Host-local project/tag filters cannot hide another host's VMs.
  assert.equal((html.match(/class="tree-vm /g) || []).length, 4);
});

test('only the VM on the selected host is highlighted when names collide', () => onHost(() => {
  clusterFixture();
  state.clusterInventory.hosts[1].id = 'second-node';
  state.page = 'vm'; state.vm = 'incus:same';
  const html = renderClusterTree();
  assert.equal((html.match(/tree-vm active/g) || []).length, 1);
  assert.match(html, /tree-vm active[\s\S]*?href="\/\?host=second-node#vm\/incus%3Asame\/summary"/);
}));

test('search finds VMs across hosts and retains host headings for empty results', () => {
  clusterFixture(); state.search = '<OTHER>';
  const html = renderClusterTree();
  assert.match(html, /ctlnxdbn001/);
  assert.match(html, /vmm-cluster-test-host/);
  assert.match(html, /&lt;other&gt;/);
  assert.doesNotMatch(html, /incus%3Asame/);
  assert.match(html, /No matching VMs/);
});

test('each host collapses independently and remains collapsed after navigation and reload', () => {
  clusterFixture();
  const storage = new Map();
  globalThis.localStorage = {getItem:key => storage.get(key),setItem:(key,value) => storage.set(key,value)};
  const groups = new Map();
  const group = id => {
    const button = {setAttribute(key,value) { this[key] = value; },firstElementChild:{}};
    const body = {hidden:false};
    const result = {dataset:{hostname:id},button,body,querySelector:selector => selector === '.tree-host-toggle' ? button : body};
    groups.set('host-tree-'+id,result);
    return result;
  };
  const remote = group('remote'), local = group('local');
  const root = {getElementById:id => groups.get(id)};
  try {
    toggleClusterHost('remote',root);
    assert.equal(remote.body.hidden,true);
    assert.equal(remote.button['aria-expanded'],'false');
    assert.equal(local.body.hidden,false);
    state.page = 'account';
    assert.match(renderClusterTree(), /id="host-tree-resources-remote" hidden/);
    const restored = JSON.parse(storage.get('vmm-cluster-collapsed-hosts'));
    assert.deepEqual(restored,['remote']);
    toggleClusterHost('',root);
    assert.equal(local.body.hidden,true);
    toggleClusterHost('remote',root);
    assert.equal(remote.body.hidden,false);
    toggleClusterHost('',root);
  } finally { delete globalThis.localStorage; }
});

test('cluster page contains membership and key controls while VM navigation lives in the sidebar', () => {
  clusterFixture();
  let html = renderCluster();
  assert.match(html, /Cluster hosts/);
  assert.match(html, /vmm-cluster-test-host/);
  assert.match(html, /Unavailable/);
  assert.doesNotMatch(html, /same|Manage VM|Manage host|cluster-open-vm|cluster-open-host/);
  assert.doesNotMatch(html, /data-action="cluster-(add|key|revoke|unlink)"/);
  state.user.role = 'administrator';
  html = renderCluster();
  for (const action of ['cluster-add','cluster-key','cluster-unlink']) assert.match(html,new RegExp('data-action="'+action+'"'));
  assert.match(html, /Incoming link keys/);
});
