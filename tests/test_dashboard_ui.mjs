// Offline rendering checks against real exported recordings. This is a small
// DOM test double, not a replacement for visual browser verification.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const web=path.join(root,'dashboard/web');
const dist=path.join(root,'dashboard/replay-site/dist');
const html=fs.readFileSync(path.join(web,'index.html'),'utf8');
const nodes=new Map();
for(const [,id] of html.matchAll(/\bid="([^"]+)"/g)){
  const classes=new Set();
  nodes.set(id,{value:id.includes('filter')?'all':'',innerHTML:'',textContent:'',style:{},disabled:false,
    classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),toggle:(c,on)=>on?classes.add(c):classes.delete(c)},
    setAttribute(){},querySelector(){return {disabled:false}},showModal(){this.open=true},close(){this.open=false}});
}
const storage=new Map();
const sandbox={console,Blob,URL,JSON,Map,Set,window:{DASHBOARD_CONFIG:{static:true}},
  document:{getElementById(id){assert(nodes.has(id),`HTML is missing #${id}`);return nodes.get(id)},
    querySelectorAll(){return []},createElement(){return {click(){}}}},
  localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
  navigator:{clipboard:{writeText:async()=>{}}},setInterval:()=>1,clearInterval(){},setTimeout:()=>1,
  fetch:async url=>({ok:true,json:async()=>JSON.parse(fs.readFileSync(path.join(dist,url),'utf8'))})};
const context=vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(web,'app.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(web,'experience.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(web,'workspace.js'),'utf8'),context);
await new Promise(resolve=>setImmediate(resolve));
await new Promise(resolve=>setImmediate(resolve));
assert.equal(nodes.get('error-banner').textContent,'');
const manifest=JSON.parse(fs.readFileSync(path.join(dist,'data/manifest.json'),'utf8'));
for(const run of manifest.runs){
  const bundle=JSON.parse(fs.readFileSync(path.join(dist,'data/runs',run.run_id+'.json'),'utf8'));
  await vm.runInContext(`loadRun(${JSON.stringify(run.run_id)},null,true)`,context);
  assert.equal(nodes.get('metric-orders').textContent,String(run.correct).padStart(2,'0'));
  assert.equal(nodes.get('metric-ontime').textContent,String(run.on_time).padStart(2,'0'));
  assert.equal(nodes.get('metric-time').textContent,String(run.time).padStart(2,'0'));
  assert.equal((nodes.get('stations').innerHTML.match(/<article class="station /g)||[]).length,Object.keys(bundle.state.stations).length);
  assert.equal((nodes.get('workers').innerHTML.match(/<article class="worker /g)||[]).length,Object.keys(bundle.state.workers).length);
  assert(nodes.get('start').disabled,'Hosted viewer must not enable live controls');
  await vm.runInContext(`loadRun(${JSON.stringify(run.run_id)},5)`,context);
  assert.equal(nodes.get('metric-time').textContent,'05');
  const historical=bundle.snapshots['5'];
  assert.equal(nodes.get('metric-staff').textContent,Object.values(historical.workers).filter(w=>w.available).length);
  assert(nodes.get('conversation-messages').innerHTML.includes('chat-message'));
  const chips=[...nodes.get('conversation-messages').innerHTML.matchAll(/class="chat-task"[^>]*>[\s\S]*?<\/svg>([^<]*)↗/g)].map(m=>m[1].trim());
  assert(chips.length>0&&chips.every(text=>!/^O\d+\.[a-z]+\d*\.|^O\d+\.(pack|verify)$/.test(text)),`Chat chips must use plain words: ${chips.slice(0,3)}`);
  const hasShared=bundle.events.some(e=>e.type==='worker_message'&&e.tick<=5);
  assert(nodes.get('conversation-context').textContent.includes(hasShared?'Shared team requests':run.mode==='ai'?'AI-generated':'scripted rules'));
  const worker=Object.values(historical.workers)[0];
  vm.runInContext(`followWorker(${JSON.stringify(worker.id)})`,context);
  assert.equal(nodes.get('conversation-worker').value,worker.id);
  vm.runInContext(`followWorker(${JSON.stringify(worker.id)})`,context);
  assert.equal(nodes.get('conversation-worker').value,'all');
  vm.runInContext("setTab('activity')",context);
  assert(nodes.get('event-log').innerHTML.includes('event-row'));
  vm.runInContext("showOrder('O1')",context);
  assert(nodes.get('order-detail').innerHTML.includes('task-row'),'Order dialog lists its tasks');
  assert(!/>O1\.[a-z]+\d*\.[a-z_]+</.test(nodes.get('order-detail').innerHTML),'Order dialog shows task names, not raw ids');
  assert.equal(nodes.get('order-dialog').open,true);
  assert(bundle.snapshots['5'],'Minute-five replay snapshot missing');
  // Task progress: one row per created task, coloured by status, percentages 0-100.
  await vm.runInContext(`loadRun(${JSON.stringify(run.run_id)},null)`,context);
  vm.runInContext("setTab('tasks')",context);
  const board=nodes.get('task-board').innerHTML,final=bundle.state;
  assert.equal((board.match(/<div class="task-row /g)||[]).length,Object.keys(final.tasks).length);
  assert.equal((board.match(/<div class="task-row done"/g)||[]).length,Object.values(final.tasks).filter(t=>t.status==='done').length);
  const pcts=[...board.matchAll(/class="task-pct">(\d+)%</g)].map(m=>Number(m[1]));
  assert.equal(pcts.length,Object.keys(final.tasks).length,'Every task shows a percentage');
  assert(pcts.every(p=>p>=0&&p<=100));
  if(final.status==='timed_out')assert(board.includes('task-row delayed'),'Unfinished tasks of a timed-out run must show as delayed');
  assert(nodes.get('task-summary').innerHTML.includes('task-chip done'));
  assert(!/>O\d+</.test(board)&&!/>O\d+</.test(nodes.get('orders-preview').innerHTML),'Orders are shown by name/number, never as O1');
  // Team profiles: read-only in the hosted viewer, one card per worker.
  vm.runInContext("setTab('profiles')",context);
  assert.equal((nodes.get('profiles-list').innerHTML.match(/class="profile-card /g)||[]).length,Object.keys(final.workers).length);
  assert(nodes.get('profile-editor').innerHTML.includes('profile-view'));
  assert(nodes.get('kitchen-update').disabled,'Hosted viewer must not accept kitchen updates');
  vm.runInContext("setTab('overview')",context);
}
assert(html.includes('href="styles.css"'),'Stylesheet must be linked');
const chatState={mode:'ai',workers:{A:{name:'Alex'}}};
context.chatState=chatState;
context.chatEvents=[{seq:1,tick:2,type:'worker_message',payload:{worker_id:'A',task_id:'O1.grill',kind:'request_help',text:'Can someone take the grill?',mode:'ai'}},
  {seq:2,tick:2,type:'decision',payload:{worker_id:'A',proposal:{action:'request_help',task_id:'O1.grill',why:'Can someone take the grill?'},result:{accepted:true}}}];
const messages=vm.runInContext('conversationMessages(chatState,chatEvents)',context);
assert.equal(messages.length,1,'A shared request must not appear twice');
assert.equal(messages[0].kind,'team');
assert.equal(messages[0].text,'Can someone take the grill?');
console.log(`Dashboard rendering checks passed for ${manifest.runs.length} real recordings, historical snapshots, orders, and events.`);
