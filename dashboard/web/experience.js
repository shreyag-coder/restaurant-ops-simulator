/* Conversation-led presentation of real recorded events. No dialogue is generated here. */
let conversationWorker = 'all', conversationKind = 'all', conversationQuery = '';
let followConversation = true, showEarlier = false, lastConversationKey = '';
let lastConversationRun = null, lastConversationSequence = 0;
const sceneIcons = {grill:'flame',prep:'knife',assembly:'assemble',fryer:'fries',packing:'package',pickup:'check-circle',line:'assemble',expo:'package',tortilla:'assemble'};

// Plain words for a task id ("Assemble burger · O5 · Cheeseburger"); workspace.js defines taskText.
const taskName=(state,id)=>id&&typeof taskText==='function'?taskText(state,id):(id||'');
const orderName=(state,id)=>id&&typeof orderLabel==='function'?orderLabel(state,id):(id||'');

function conversationMessages(state, events) {
  const shared = new Set(events.filter(e=>e.type==='worker_message').map(e=>{
    const p=e.payload; return `${e.tick}|${p.worker_id}|${p.task_id}|${p.text}`;
  }));
  return events.flatMap(event=>{
    const p=event.payload||{}, base={seq:event.seq,tick:event.tick,worker:p.worker_id||null,task:p.task_id||null,order:p.order_id||null};
    if(event.type==='worker_message') return [{...base,kind:'team',text:p.text,label:p.kind==='request_help'?'Help request · to the team':'Blocker report · to the team',accepted:true,mode:p.mode||state.mode}];
    if(event.type==='decision'&&p.proposal){
      const proposal=p.proposal;
      if(shared.has(`${event.tick}|${p.worker_id}|${proposal.task_id}|${proposal.why}`))return [];
      return [{...base,task:proposal.task_id,kind:'decision',text:proposal.why,
        label:state.mode==='ai'?'AI decision explanation':'Rule-based decision explanation',
        action:proposal.action,accepted:p.result?.accepted,reason:p.result?.reason,mode:p.mode||state.mode}];
    }
    const worker=state.workers[p.worker_id]?.name||p.worker_id||'A worker';
    let text='';
    if(event.type==='worker_availability')text=`${worker} is ${p.available?'available again':'unavailable for the rest of this shift'}.`;
    if(event.type==='task_interrupted')text=`${worker} had to stop “${taskName(state,p.task_id)}”. It went back on the board and its ingredients were released.`;
    if(event.type==='blocker')text=`${taskName(state,p.task_id)}: ${p.reason}`;
    if(event.type==='order_admitted')text=`${p.order_id} arrived. Recipe tasks are now on the board.`;
    if(event.type==='order_verified')text=`${p.order_id} ${p.correct?'passed':'failed'} its final check${p.correct?(p.on_time?' and met its deadline.':', but missed its deadline.'):'.'}`;
    if(event.type==='run_finished')text=`Shift ${p.status==='completed'?'completed':'reached its time limit'}: ${p.correct_orders} correctly finished orders.`;
    if(event.type==='decision_error')text=`${worker}'s decision request failed. The run paused without switching to scripted decisions.`;
    return text?[{...base,kind:'system',text,label:'Kitchen update',eventType:event.type}]:[];
  });
}

function meaningfulMoment(state,events){
  const current=events.filter(e=>e.tick===state.time);
  const priority=['decision_error','task_interrupted','blocker','order_verified','order_admitted','task_started'];
  const event=priority.map(kind=>current.find(e=>e.type===kind)).find(Boolean);
  if(!event)return {title:`Minute ${state.time} · work continues`,body:'Tasks keep their configured duration. Use the timeline to follow the next handoff.',tone:'normal'};
  const p=event.payload,worker=state.workers[p.worker_id]?.name||p.worker_id;
  if(event.type==='task_interrupted')return {title:`${worker} stepped away`,body:`“${taskName(state,p.task_id)}” is waiting for a qualified teammate to restart it.`,tone:'alert'};
  if(event.type==='blocker')return {title:'The team has hit a skill gap',body:p.reason,tone:'alert'};
  if(event.type==='decision_error')return {title:'Waiting for a valid AI decision',body:'The simulation is paused. No hidden fallback has been used.',tone:'alert'};
  if(event.type==='order_verified')return {title:`${orderName(state,p.order_id)} ${p.correct?'is ready for pickup':'needs attention'}`,body:p.on_time?'Its final verification passed within the order deadline.':'Correctness and deadline results are recorded separately.',tone:p.correct?'success':'alert'};
  if(event.type==='order_admitted')return {title:`${orderName(state,p.order_id)} just joined the queue`,body:'The kitchen creates its recipe steps, dependencies, packing, and final check.',tone:'normal'};
  return {title:`${worker} picked up the next task`,body:`${taskName(state,p.task_id)} · ready at minute ${p.finishes_at}`,tone:'normal'};
}

function setContent(id,html){const node=$(id);if(node.innerHTML!==html)node.innerHTML=html}

function renderFloor(state,events){
  const stationEntries=Object.entries(state.stations),workers=Object.values(state.workers);
  const active=Object.values(state.tasks).filter(t=>t.status==='active');
  const moment=meaningfulMoment(state,events);
  $('workspace-name').textContent=state.config.scenario||'Restaurant simulation';
  $('staff-total').textContent=`/ ${workers.length} workers`;
  $('station-count').textContent=`${stationEntries.length} stations`;
  $('floor-status').textContent=`${active.length} tasks in progress`;
  $('shift-source').textContent=state.mode==='ai'?'Real AI explanations · recorded outcomes':'Rule-based explanations · recorded outcomes';
  setContent('moment-card',`<span class="moment-symbol ${moment.tone}">${icon(moment.tone==='alert'?'alert':moment.tone==='success'?'check-circle':'activity')}</span><div><strong>${esc(moment.title)}</strong><p>${esc(moment.body)}</p></div><span class="moment-time">MIN ${String(state.time).padStart(2,'0')}</span>`);
  setContent('crew-strip',workers.map(w=>`<button class="crew-person ${conversationWorker===w.id?'selected':''} ${!w.available?'away':''}" data-crew="${esc(w.id)}" title="Follow ${esc(w.name)} · ${esc(w.role)}" aria-label="Follow ${esc(w.name)} in the conversation">${avatar(w)}<span>${esc(w.name)}<small>${!w.available?'Away':w.current_task?'Working':'Available'}</small></span><i></i></button>`).join(''));
  setContent('stations',stationEntries.map(([id,station])=>{
    const current=station.active_tasks||station.active||[], assigned=current.map(key=>state.tasks[key]).filter(Boolean);
    const workerIds=new Set(assigned.map(t=>t.worker_id));
    const waiting=Object.values(state.tasks).filter(t=>t.station_id===id&&t.status==='pending'&&t.depends_on.every(key=>state.tasks[key]?.status==='done'));
    const blocked=waiting.filter(t=>state.blockers?.[t.id]);
    const done=Object.values(state.tasks).filter(t=>t.station_id===id&&t.status==='done').length;
    return `<article class="station ${assigned.length?'working':''} ${blocked.length?'scene-blocked':''}" data-station="${esc(id)}"><div class="station-top"><span class="station-symbol">${icon(sceneIcons[id]||sceneIcons[station.type])}</span><span class="station-status"><i></i>${assigned.length?'Working':blocked.length?'Blocked':'Ready'}</span></div><h3>${esc(station.name||title(id))}</h3><div class="station-crew">${assigned.length?assigned.map(task=>{const w=state.workers[task.worker_id];return `<button data-crew="${esc(w.id)}" class="station-worker" title="Follow ${esc(w.name)}">${avatar(w)}<span>${esc(w.name)}</span></button>`}).join(''):'<span class="station-ready">'+(blocked.length?'Waiting for a qualified teammate':'Room for the next handoff')+'</span>'}</div>${assigned.map(task=>{
      const elapsed=Math.max(0,state.time-task.started_at),duration=Math.max(1,task.finishes_at-task.started_at),percent=Math.min(100,elapsed/duration*100);
      return `<button class="scene-task" data-task-order="${esc(task.order_id)}"><span title="${esc(task.id)}">${esc(taskName(state,task.id))}</span><small>${Math.max(0,task.finishes_at-state.time)} min left</small><i style="--progress:${percent}%"></i></button>`;
    }).join('')}<div class="station-bottom"><span>${current.length}/${station.capacity} slots</span><span>${waiting.length} ready · ${done} done</span></div>${assigned.length?'<div class="station-motion" aria-hidden="true"><i></i><i></i><i></i></div>':''}</article>`;
  }).join(''));
}

function renderConversation(){
  if(!detail)return;
  const {state,events}=detail;
  const workers=Object.values(state.workers);
  const workerOptions='<option value="all">Everyone</option>'+workers.map(w=>`<option value="${esc(w.id)}">${esc(w.name)} · ${esc(w.role)}</option>`).join('');
  setContent('conversation-worker',workerOptions);
  if(conversationWorker!=='all'&&!state.workers[conversationWorker])conversationWorker='all';
  $('conversation-worker').value=conversationWorker;
  const all=conversationMessages(state,events),query=conversationQuery.toLowerCase();
  const filtered=all.filter(message=>(conversationWorker==='all'||message.worker===conversationWorker)
    &&(conversationKind==='all'||message.kind===conversationKind)
    &&(!query||`${message.text} ${message.task||''} ${taskName(state,message.task)} ${state.workers[message.worker]?.name||''}`.toLowerCase().includes(query)));
  $('conversation-count').textContent=filtered.length;
  const hasShared=all.some(m=>m.kind==='team');
  $('conversation-source').textContent=state.mode==='ai'?'AI voices':'Rule-based voices';
  $('conversation-context').textContent=hasShared?'Shared team requests and recorded decision explanations. Kitchen updates are labeled separately.':(state.mode==='ai'?'The workers’ actual AI-generated explanations, shown as a conversation feed. This recording contains no separate peer-to-peer chat.':'This run uses scripted rules. These bubbles show recorded decision explanations, not AI-generated dialogue.');
  const visible=showEarlier?filtered:filtered.slice(-40);
  $('earlier-messages').classList.toggle('hidden',showEarlier||filtered.length<=40);
  $('earlier-messages').textContent=`Show ${Math.max(0,filtered.length-40)} earlier messages`;
  let previousMinute=null;
  const html=visible.map(message=>{
    let heading='';
    if(message.tick!==previousMinute){heading=`<div class="conversation-minute"><span>Minute ${String(message.tick).padStart(2,'0')}</span><button data-seek="${message.tick}" title="View this recorded minute">View moment ↗</button></div>`;previousMinute=message.tick}
    if(message.kind==='manager')return heading+`<article class="chat-manager ${message.pending?'pending':''}" data-message-seq="${message.seq}"><span class="avatar owner">MGR</span><div class="chat-body"><div class="chat-author"><strong>Kitchen manager</strong><span>${esc(message.label)}</span><time>${String(message.tick).padStart(2,'0')}:00</time></div><div class="chat-bubble"><p>${esc(message.text)}</p></div>${message.detail?`<div class="chat-outcome">${esc(message.detail)}</div>`:''}</div></article>`;
    if(message.kind==='system')return heading+`<article class="chat-system ${['blocker','task_interrupted','decision_error'].includes(message.eventType)?'important':''}" data-message-seq="${message.seq}"><span>${icon(message.eventType==='order_verified'?'check-circle':message.eventType==='blocker'?'alert':'activity')}</span><p>${esc(message.text)}</p><small>ENGINE</small></article>`;
    const worker=state.workers[message.worker]||{id:message.worker,name:message.worker};
    const rejected=message.accepted===false;
    const fresh=lastConversationRun===selected&&message.seq>lastConversationSequence;
    return heading+`<article class="chat-message ${fresh?'fresh':''} ${message.kind==='team'?'team-message':''} ${rejected?'rejected':''}" data-message-seq="${message.seq}">${avatar(worker)}<div class="chat-body"><div class="chat-author"><strong>${esc(worker.name)}</strong><span>${esc(worker.role||'Kitchen worker')}</span><time>${String(message.tick).padStart(2,'0')}:00</time></div><div class="chat-bubble"><div class="chat-type">${message.kind==='team'?icon('users'):icon('activity')}<span>${esc(message.label)}</span></div><p${message.original?` title="Recorded wording: ${esc(message.original)}"`:''}>${esc(message.text)}</p>${message.task?`<button class="chat-task" title="${esc(message.task)} · open the order" data-task-order="${esc(state.tasks[message.task]?.order_id||message.task.split('.')[0])}">${icon('receipt')}${esc(taskName(state,message.task))} ↗</button>`:''}</div><div class="chat-outcome ${rejected?'rejected':''}">${rejected?'↳ Rejected: '+esc(message.reason||'Invalid proposal'):message.kind==='team'?'↗ Shared with the team':`✓ ${esc(title(message.action||'Accepted'))}`}</div></div></article>`;
  }).join('')||`<div class="conversation-empty">${icon('users')}<strong>${all.length?'No messages match this view':'The team is ready'}</strong><p>${all.length?'Try another worker or message filter.':'Press Watch shift to follow decisions as they happen in the recording.'}</p></div>`;
  const box=$('conversation-messages');
  const key=`${selected}:${state.time}:${conversationWorker}:${conversationKind}:${conversationQuery}:${showEarlier}:${events.length}`;
  if(key!==lastConversationKey){box.innerHTML=html;lastConversationKey=key;lastConversationRun=selected;lastConversationSequence=events.length?events[events.length-1].seq:0;if(followConversation)box.scrollTop=box.scrollHeight;}
  $('conversation-position').textContent=`${visible.length} visible · through minute ${state.time}`;
  $('follow-conversation').setAttribute('aria-pressed',String(followConversation));
  $('follow-conversation').textContent=followConversation?'↓ Following':'↓ Follow';
}

function syncPlaybackUI(){
  $('watch-label').textContent=player?'Pause replay':'Watch shift';
  $('watch-shift').classList.toggle('playing',!!player);
  $('shift-heading').textContent=player?'Following the shift':'The shift, moment by moment';
}

function renderExperience(){
  if(!detail)return;
  renderFloor(detail.state,detail.events);
  renderConversation();
  syncPlaybackUI();
}

async function seekMoment(tick){
  if(!detail)return;
  stopPlayer();replayTick=Number(tick);
  try{await loadRun(selected,replayTick)}catch(e){error(e.message)}
}
async function replayStep(direction){
  if(!detail)return;
  const time=detail.state.time,ticks=detail.ticks;
  const next=direction>0?ticks.find(t=>t>time):ticks.slice().reverse().find(t=>t<time);
  if(next!==undefined)await seekMoment(next);
}
function followWorker(id){
  conversationWorker=conversationWorker===id?'all':id;
  showEarlier=false;renderExperience();
}
function experienceClick(event){
  const crew=event.target.closest?.('[data-crew]');
  const seek=event.target.closest?.('[data-seek]');
  const task=event.target.closest?.('[data-task-order]');
  if(crew)followWorker(crew.dataset.crew);
  else if(seek)seekMoment(seek.dataset.seek);
  else if(task&&detail?.state.orders[task.dataset.taskOrder])showOrder(task.dataset.taskOrder);
}
$('crew-strip').onclick=experienceClick;$('stations').onclick=experienceClick;$('conversation-messages').onclick=experienceClick;
$('conversation-worker').onchange=event=>{conversationWorker=event.target.value;showEarlier=false;renderExperience()};
$('conversation-kind').onchange=event=>{conversationKind=event.target.value;showEarlier=false;renderConversation()};
$('conversation-search').oninput=event=>{conversationQuery=event.target.value;showEarlier=false;renderConversation()};
$('follow-conversation').onclick=()=>{followConversation=!followConversation;renderConversation();if(followConversation){const box=$('conversation-messages');box.scrollTop=box.scrollHeight}};
$('earlier-messages').onclick=()=>{showEarlier=true;followConversation=false;renderConversation()};
$('conversation-latest').onclick=async()=>{stopPlayer();replayTick=null;await loadRun(selected);followConversation=true;renderConversation();$('conversation-messages').scrollTop=$('conversation-messages').scrollHeight};
$('watch-shift').onclick=togglePlayer;$('replay-back').onclick=()=>replayStep(-1);$('replay-forward').onclick=()=>replayStep(1);
$('replay-speed').onchange=()=>{if(player){stopPlayer();togglePlayer()}};
if(detail)renderExperience();
