import {test} from 'node:test';
import assert from 'node:assert/strict';
import {state, apiURL} from '../app/static/core.js';
import {renderDevops, nodeFields} from '../app/static/devops.js';
import {resourceTables} from '../app/static/kubernetes.js';

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

test('graphical resources filter namespaces, escape metadata and restrict pod controls', () => {
  const data = {nodes:[{name:'<master>',ready:'True',roles:['control-plane'],addresses:[],version:'v1'}],
    pods:[{name:'<pod>',namespace:'default',status:'Running',ready:1,restarts:2,containers:[{name:'web',running:true}],node:'master',ip:'1.2.3.4',owner:'ReplicaSet/app',restartable:true,deletable:true},
      {name:'other',namespace:'system',status:'Pending',ready:0,restarts:0,containers:[],node:'',owner:'',restartable:false,deletable:true}]};
  state.user={role:'viewer'};
  let html=resourceTables(data,'default');
  assert.match(html,/&lt;master&gt;/);
  assert.match(html,/&lt;pod&gt;/);
  assert.match(html,/data-kube-action="pod-describe"/);
  assert.doesNotMatch(html,/data-kube-action="(?:terminal|restart|delete)"/);
  assert.doesNotMatch(html,/>other</);
  state.user={role:'operator'};
  html=resourceTables(data,'system');
  assert.match(html,/data-kube-action="restart"[^>]*disabled/);
  assert.match(html,/data-kube-action="terminal"[^>]*disabled/);
  assert.match(html,/data-kube-action="delete"/);
  assert.match(resourceTables(data,'','nothing'),/No matching pods/);
});

test('disabled DevOps renders no tools and ready clusters offer graphical management to viewers', () => {
  state.user={role:'viewer',features:{devops:{enabled:false}}};
  assert.doesNotMatch(renderDevops(),/data-action=/);
  state.user={role:'viewer'};
  assert.match(renderDevops(),/data-action="devops-manage"/);
});
