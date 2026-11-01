import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createContext, runInContext} from 'node:vm';
import * as core from '../app/static/core.js';
import {renderDevops} from '../app/static/devops.js';
import {hostPicker, renderClusterTree} from '../app/static/cluster-ui.js';

const appSource = readFileSync(new URL('../app/static/app.js', import.meta.url), 'utf8')
  .replace(/^import[\s\S]*?from "[^"]+";\n/gm, '')
  .replace('\ninit();\n', '\n');

function screen() {
  const cluster = {id:'a'.repeat(32),name:'test-cluster',version:'v1',status:'ready',message:'Ready',spec:{masters:[{}],workers:[]},nodes:[]};
  let inventory = {vms:[],projects:[],tags:[],warnings:[],host:{name:'local',address:'127.0.0.1'},features:{devops:{enabled:true}}};
  Object.assign(core.state, {user:{username:'operator',role:'operator'},inventory,devops:{clusters:[cluster]},devopsCluster:'',
    page:'devops',tasks:[],search:'',project:'all',tag:'all',clusterInventory:null});
  let html='', draws=0, mounts=0, stops=0, root=null;
  const app = {set innerHTML(value) {html=value;draws++;root=value.includes('id="kubernetes-screen"') ? {} : null;}};
  const location = {hash:'#devops/'+cluster.id};
  const context = createContext({...core, renderDevops, hostPicker, renderClusterTree, location,
    document:{activeElement:{tagName:'IFRAME'},addEventListener(){}},window:{addEventListener(){}},setInterval(){},
    bindCopyButtons(){},refreshMonitoring(){},mountClusterScreen(){mounts++;return () => {stops++;};},
    $: selector => ({'#app':app,'#modal':{open:false},'#kubernetes-screen':root,'#resource-tree':{scrollTop:0,innerHTML:''}})[selector] || null,
    api:async path => {
      if(path==='/cluster/hosts') return {hosts:[]};
      if(path==='/inventory') return inventory;
      if(path==='/devops/clusters') return {clusters:[cluster]};
      assert.fail('Unexpected API '+path);
    },toast:message => assert.fail(message),
  });
  runInContext(appSource,context);
  const methods=runInContext('({render,navigate,refresh})',context);
  return {...methods,location,cluster,get html(){return html;},get draws(){return draws;},get mounts(){return mounts;},get stops(){return stops;},
    disable(){inventory={...inventory,features:{devops:{enabled:false}}};}};
}

test('dedicated cluster route survives app polling and forced refresh without rebuilding its terminal', async () => {
  const page=screen();page.navigate(false);page.render();
  assert.match(page.html,/id="kubernetes-screen"/);
  assert.match(page.html,/<h1>test-cluster<\/h1>/);
  assert.doesNotMatch(page.html,/<dialog|Close dialog/);
  assert.equal(page.mounts,1);
  await page.refresh();await page.refresh(true);
  assert.equal(page.draws,1);
  assert.equal(page.mounts,1);
  assert.equal(page.stops,0);
});

test('Back to DevOps stops the screen and Forward mounts it again; disabled DevOps closes it',async () => {
  const page=screen();page.navigate(false);page.render();
  page.location.hash='#devops';page.navigate(false);await page.refresh(true);
  assert.equal(page.stops,1);
  assert.doesNotMatch(page.html,/id="kubernetes-screen"/);
  assert.match(page.html,/data-action="devops-manage"/);
  page.location.hash='#devops/'+page.cluster.id;page.navigate(false);await page.refresh(true);
  assert.equal(page.mounts,2);
  page.disable();await page.refresh();
  assert.equal(page.stops,2);
  assert.match(page.html,/DevOps is not enabled/);
});

test('unknown or unavailable cluster URLs show a return link without mounting management', () => {
  const page=screen();page.location.hash='#devops/missing';page.navigate(false);page.render();
  assert.match(page.html,/could not be found/);
  assert.match(page.html,/href="#devops"/);
  assert.equal(page.mounts,0);
  page.cluster.status='deleted';page.location.hash='#devops/'+page.cluster.id;page.navigate(false);page.render();
  assert.match(page.html,/must be Ready/);
  assert.equal(page.mounts,0);
});

test('leaving a mounted screen aborts pending requests, stops polling and removes its terminal',async () => {
  const source=readFileSync(new URL('../app/static/kubernetes.js',import.meta.url),'utf8')
    .replace(/^import[^\n]+\n/,'').replaceAll('export function ','function ');
  const elements=new Map();
  for(const key of ['#kube-detail','#kube-resources','#kube-namespaces','#kube-status','#kube-error']) elements.set(key,{innerHTML:'',textContent:''});
  let requestSignal,finish,cleared=false;
  const context=createContext({...core,AbortController,document:{hidden:false},
    $:selector=>elements.get(selector),setInterval:()=>42,clearInterval:id=>{assert.equal(id,42);cleared=true;},
    api:(_path,_method,_body,{signal})=>{requestSignal=signal;return new Promise(resolve=>{finish=resolve;});}});
  runInContext(source,context);
  const mount=runInContext('mountClusterScreen',context);
  const cleanup=mount({addEventListener(){}},{id:'test'});
  elements.get('#kube-detail').innerHTML='<iframe></iframe>';
  cleanup();
  assert.equal(requestSignal.aborted,true);
  assert.equal(cleared,true);
  assert.equal(elements.get('#kube-detail').innerHTML,'');
  const before=elements.get('#kube-resources').innerHTML;
  finish({nodes:[],pods:[]});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(elements.get('#kube-resources').innerHTML,before,'late responses must not update a departed screen');
});
