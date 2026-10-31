import { test } from 'node:test';
import assert from 'node:assert/strict';
import { apiURL, hostURL, state } from '../app/static/core.js';
import { renderEmbeddedConsole } from '../app/static/console-view.js';
import { renderCluster } from '../app/static/cluster-ui.js';

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

test('duplicate VM names remain distinct by host and an unavailable host is explicit', () => {
  state.user = {role: 'viewer'};
  state.clusterInventory = { hosts: [
    {id: '', name: 'local', status: 'online', vms: [{id: 'incus:same', name: 'same', provider: 'incus', status: 'running'}]},
    {id: 'remote', name: '<remote>', status: 'online', vms: [{id: 'incus:same', name: 'same', provider: 'incus', status: 'stopped'}]},
    {id: 'offline', name: 'unavailable', status: 'offline', vms: [], warnings: ['Host unavailable']},
  ]};
  const html = renderCluster();
  assert.match(html, /data-id="\|incus:same"/);
  assert.match(html, /data-id="remote\|incus:same"/);
  assert.match(html, /&lt;remote&gt;/);
  assert.match(html, /Unavailable/);
  assert.doesNotMatch(html, /data-action="cluster-(add|key|revoke|unlink)"/);
});
