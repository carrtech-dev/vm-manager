import {test} from 'node:test';
import assert from 'node:assert/strict';
import {setupView, setupDestination} from '../app/static/setup.js';
import {apiURL} from '../app/static/core.js';

const draft = {mode:'local',bind_address:'0.0.0.0',public_url:'',manage_firewall:true,libvirt:true,create_pool:true,pool:'vm-manager',create_network:true,network:'vmmbr0',network_address:'10.210.0.1/24'};
const status = {os:'Rocky Linux 10.2',pools:[],networks:[],interfaces:[]};

test('setup requires a chosen gateway and confirms configuration before applying', () => {
  const config = setupView(status,draft,'config');
  assert.match(config, /VM_MANAGER_HOST/);
  assert.match(config, /VM_MANAGER_PUBLIC_URL/);
  assert.match(config, /name="network_address"/);
  const review = setupView(status,draft,'review');
  assert.match(review, /10\.210\.0\.1\/24/);
  assert.match(review, /name="confirmed" required/);
  assert.match(review, /cofob/);
  assert.match(review, /Incus, QEMU\/KVM and libvirt/);
});

test('cluster-only setup requires no network and leads to cluster linking', () => {
  const config = setupView(status,{...draft,mode:'cluster'},'config');
  assert.doesNotMatch(config, /name="network_address"/);
  const done = setupView({status:'completed',plan:{...draft,mode:'cluster'},steps:[]},draft,'progress');
  assert.match(done, /#cluster/);
  assert.match(done, /Open Cluster to link a host/);
});

test('failed setup escapes host output and offers a retry without losing progress', () => {
  const html = setupView({status:'failed',error:'<bad>',steps:[{message:'<script>'}]},draft,'progress');
  assert.match(html, /&lt;bad&gt;/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /Review and retry/);
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(setupView(status,draft,'choice',false), /type="submit"/);
});

test('setup always targets the local manager and reconnects using confirmed address', () => {
  globalThis.location = {search:'?host=remote'};
  try { assert.equal(apiURL('/setup'),'/api/setup'); } finally { delete globalThis.location; }
  assert.equal(setupDestination(draft,'https://current:8443'),'https://current:8443');
  assert.equal(setupDestination({...draft,bind_address:'192.168.8.236'}),'https://192.168.8.236:8443');
  assert.equal(setupDestination({...draft,bind_address:'::1'}),'https://[::1]:8443');
});
