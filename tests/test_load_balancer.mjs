import {test} from 'node:test';
import assert from 'node:assert/strict';
import {state} from '../app/static/core.js';
import {edgeScreen, edgeContent} from '../app/static/load-balancer.js';
import {workloadTables, workloadForm, clusterScreen} from '../app/static/kubernetes.js';
import {renderDevops} from '../app/static/devops.js';

test('load balancer has a dedicated route and secrets never enter its rendered tables', () => {
  state.user = {role:'administrator'};
  state.inventory = {features:{devops:{enabled:true}}};
  state.devopsCluster = 'load-balancer';
  assert.equal(renderDevops(), edgeScreen());
  state.devopsCluster = '';
  const data = {settings:{enabled:true,address:'192.0.2.10',clusters:[],cloudflare:{},routes:[{id:'route',hostname:'<host>',mode:'manual',upstreams:['10.0.0.1:8080'],private_key:'SECRET'}]},status:{state:'ready',message:'<ready>',services:[]},load_balancer_class:'vmm.carrtech.dev/edge'};
  let html = edgeContent(data);
  assert.match(html,/&lt;host&gt;/);
  assert.match(html,/&lt;ready&gt;/);
  assert.doesNotMatch(html,/SECRET/);
  assert.match(html,/data-edge-action="route"/);
  state.user = {role:'operator'};
  html = edgeContent(data);
  assert.doesNotMatch(html,/data-edge-action="(?:route|launch|cloudflare|bind|reconcile|remove)"/);
});

test('workload forms and tables cover deployments, Services and Ingress with permission checks', () => {
  const data = {deployments:[{name:'<web>',namespace:'apps',images:['image:v1'],ready:1,replicas:2,managed:true}],services:[{name:'web',namespace:'apps',type:'LoadBalancer',ports:[{port:8080,targetPort:80}],external:[],managed:true}],ingresses:[]};
  state.user={role:'operator'};
  const html = workloadTables(data,'apps');
  assert.match(html,/&lt;web&gt;/);
  assert.match(html,/deployment-update/);
  assert.match(html,/service-remove/);
  assert.match(html,/External address pending/);
  assert.doesNotMatch(workloadTables(data,'other'),/image:v1/);
  for (const kind of ['deployment','service','ingress']) assert.match(workloadForm(kind),/kube-author-form/);
  assert.match(clusterScreen({id:'a',name:'test',version:'v1'}),/deployment-create/);
  state.user={role:'viewer'};
  assert.doesNotMatch(workloadTables(data),/data-kube-action="(?:deployment-update|service-remove)"/);
  assert.doesNotMatch(clusterScreen({id:'a',name:'test',version:'v1'}),/deployment-create/);
});


test('automatic integration is the default and only optional pause controls are shown', () => {
  state.user = {role:'administrator'};
  state.devops = {clusters:[{id:'cluster',name:'Ready cluster',status:'ready'}]};
  const data = {settings:{enabled:false,clusters:[],excluded_clusters:[],routes:[],cloudflare:{}},status:{state:'standby',message:'Automatic',services:[]}};
  let html = edgeContent(data);
  assert.match(html,/Clusters connect automatically/);
  assert.match(html,/Automatic integration enabled/);
  assert.match(html,/Pause automatic integration/);
  assert.doesNotMatch(html,/>Connect cluster</);
  assert.match(workloadForm('service'),/No connection step is required/);
  data.settings.excluded_clusters = ['cluster'];
  html = edgeContent(data);
  assert.match(html,/Resume automatic integration/);
});
