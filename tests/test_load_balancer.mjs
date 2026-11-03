import {test} from 'node:test';
import assert from 'node:assert/strict';
import {state} from '../app/static/core.js';
import {edgeScreen, edgeContent, edgeDiagram} from '../app/static/load-balancer.js';
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

test('load balancer sections expose one active panel and accessible tab relationships', () => {
  const screen = edgeScreen();
  const data = {settings:{enabled:false,routes:[],cloudflare:{}},status:{state:'standby',message:'Waiting',services:[]}};
  const tabs = ['overview', 'routes', 'kubernetes', 'cloudflare'];
  assert.equal((screen.match(/role="tab"/g) || []).length, 4);
  for (const active of tabs) {
    const html = edgeContent(data, active);
    for (const id of tabs) {
      assert.ok(screen.includes(`aria-controls="edge-panel-${id}"`));
      assert.ok(html.includes(`id="edge-panel-${id}" role="tabpanel" aria-labelledby="edge-tab-${id}" tabindex="0"${id === active ? '' : ' hidden'}>`));
    }
    assert.equal((html.match(/role="tabpanel"[^>]* hidden/g) || []).length, 3);
  }
  assert.match(edgeContent(data), /Launch the load balancer in Overview/);
  assert.match(edgeDiagram(data), /No traffic paths yet/);
});

test('network diagram maps direct HTTPS, shared tunnel and cluster destinations without claiming health', () => {
  state.devops = {clusters:[{id:'cluster',name:'Apps <cluster>',status:'ready'}]};
  const data = {settings:{enabled:true,address:'192.0.2.10',cloudflare:{token:'PRIVATE'},routes:[
    {id:'manager-lan',hostname:'192.0.2.10',mode:'manager',upstreams:['192.0.2.10:8443']},
    {id:'web',hostname:'web.example.com',mode:'letsencrypt',upstreams:['10.0.0.1:80','10.0.0.2:80']},
    {id:'tunnel',hostname:'<script>',mode:'cloudflare',cluster_id:'cluster',namespace:'apps',service:'web',service_port:80,private_key:'PRIVATE'},
    {id:'manager',hostname:'manager.example.com',mode:'manual',backend:'vm-manager'}
  ]},status:{state:'failed',services:[]}};
  const html = edgeDiagram(data);
  assert.match(html,/Direct HTTPS/);
  assert.match(html,/Public HTTPS → shared tunnel/);
  assert.match(html,/127\.0\.0\.1:18080 · local HTTP/);
  assert.match(html,/HTTPS to VM Manager/);
  assert.match(html,/HTTP to application/);
  assert.match(html,/Caddy · matches hostname/);
  for (const destination of ['192.0.2.10:8443', '10.0.0.1:80', '10.0.0.2:80', 'apps/web:80', 'VM Manager (verified HTTPS origin)']) {
    assert.ok(html.includes(destination));
  }
  assert.match(html,/Apps &lt;cluster&gt;/);
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>|PRIVATE/);
  assert.match(html,/not a live connectivity test/);
  assert.match(html,/Overall state: failed/);
});

test('TCP flow bypasses Cloudflare and pending listeners show their actual reason', () => {
  const service = {cluster_id:'a',namespace:'apps',name:'database',ports:[5432],state:'configured',address:'192.0.2.10'};
  const data = {settings:{enabled:true,address:'192.0.2.10',routes:[]},status:{state:'ready',services:[service]}};
  let html = edgeDiagram(data);
  assert.match(html,/TCP listener configured/);
  assert.match(html,/Direct TCP · no Cloudflare Tunnel/);
  assert.match(html,/Ports 5432/);
  assert.match(html,/HAProxy · matches port/);
  service.state = 'pending';
  service.address = '';
  service.message = 'Port <reserved>';
  html = edgeDiagram(data);
  assert.match(html,/TCP listener pending/);
  assert.match(html,/Port &lt;reserved&gt;/);
  assert.doesNotMatch(html,/TCP listener configured/);
});
