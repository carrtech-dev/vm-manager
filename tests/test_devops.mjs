import {test} from 'node:test';
import assert from 'node:assert/strict';
import {state, apiURL} from '../app/static/core.js';
import {renderDevops, nodeFields} from '../app/static/devops.js';

test('DevOps deployments stay on coordinator when the inventory host changes', () => {
  globalThis.location = {search:'?host=remote'};
  try { assert.equal(apiURL('/devops/clusters'), '/api/devops/clusters'); }
  finally {delete globalThis.location;}
});

test('node form offers host-specific pools, networks and independent VM specs', () => {
  const hosts = [{id:'',name:'Local',options:{pools:[{name:'local',driver:'zfs'}],networks:[{name:'local-net',managed:true,type:'bridge',config:{}}]}},
    {id:'remote',name:'Remote <host>',options:{pools:[{name:'remote-pool',driver:'dir'}],networks:[{name:'remote-lan',managed:true,type:'macvlan',config:{}}]}}];
  const html = nodeFields({host_id:'remote',cpu:6,memory_gib:12,disk_gib:80}, 'master-0', hosts);
  assert.match(html,/Remote &lt;host&gt;/);
  assert.match(html,/remote-pool/);
  assert.match(html,/remote-lan/);
  assert.doesNotMatch(html,/local-net/);
  assert.match(html,/name="master-0_cpu"[^>]*value="6"/);
  assert.match(html,/name="master-0_memory"[^>]*value="12"/);
  assert.match(html,/name="master-0_disk"[^>]*value="80"/);
});

test('cluster results escape metadata and viewers cannot obtain credentials or terminals', () => {
  state.devops={clusters:[{id:'id',name:'<cluster>',version:'v1',status:'ready',message:'<ready>',spec:{masters:[{}],workers:[]},nodes:[{name:'<node>',role:'master',host_name:'<host>',address:'1.2.3.4',cpu:2,memory_gib:4,disk_gib:30,status:'ready'}]}]};
  state.user={role:'viewer'};
  let html=renderDevops();
  assert.match(html,/&lt;cluster&gt;/);
  assert.match(html,/&lt;node&gt;/);
  assert.doesNotMatch(html,/data-action="devops-(?:terminal|create|delete|kubeconfig)"/);
  state.user={role:'operator'};
  html=renderDevops();
  assert.match(html,/data-action="devops-terminal"/);
  assert.match(html,/data-action="devops-kubeconfig"/);
});

test('DevOps task output uses the coordinator despite a retained host query', () => {
  globalThis.location={search:'?host=remote',hash:'#devops'};
  try {assert.equal(apiURL('/tasks/one/output'),'/api/tasks/one/output');}
  finally {delete globalThis.location;}
});
