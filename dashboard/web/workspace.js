/* Team profiles (view + edit), task progress, and the kitchen-update composer in the team chat.
   Loaded after app.js and experience.js; it extends their render hooks instead of replacing them.
   The browser never writes run state: updates go through the orchestrator, and profile edits are
   validated against every scenario variant by the local server before they are saved. */
let profileKitchen = null, profileKitchenChosen = false, profileData = null, profileSelected = null;
let profileDirty = false, profileMessage = null, profileKey = '', profileRequest = 0;
let taskFilter = 'all', sendingUpdate = false, updateHintLocked = false;
const orderOpen = {};  // order id -> true/false once the viewer has toggled it

const workspaceTabs = {tasks: 'Task progress', profiles: 'Team profiles'};
const kitchenOf = state => state?.config?.menu?.recipes?.burrito ? 'chipotle' : 'classic';
const kitchenNames = {chipotle: 'burrito line', classic: 'burger kitchen'};
const lines = value => String(value || '').split('\n').map(s => s.trim()).filter(Boolean);
const pad2 = n => String(n).padStart(2, '0');

/* ------------------------------------------------------------------ hooks */

const baseSetTab = setTab;
setTab = function (tab) {
  baseSetTab(tab);
  try { if (typeof history !== 'undefined' && location.hash !== `#tab=${tab}`) history.replaceState(null, '', tab === 'overview' ? location.pathname : `#tab=${tab}`); } catch (e) { /* sandboxed */ }
  if (workspaceTabs[tab]) $('breadcrumb').textContent = workspaceTabs[tab];
  renderWorkspace();
};

const baseRenderExperience = renderExperience;
renderExperience = function () {
  baseRenderExperience();
  renderWorkspace();
};

/* Simple dashboard: one kitchen and a normal shift. Problems are added from the team chat. */
const SIMPLE_KITCHEN = 'chipotle', SIMPLE_SCENARIO = 'baseline';

/* Plain-words status next to Start / Pause. No run ids, modes, or engine details. */
function renderLiveNotice() {
  const notice = $('live-notice');
  if (isStatic) { notice.className = 'live-notice hidden'; return; }
  const control = detail?.control || {}, state = detail?.state;
  let tone = 'idle', html;
  if (!state) {
    html = '<strong>Ready when you are</strong><span>Press Start new run to open the kitchen.</span>';
  } else if (control.active && control.pausing) {
    tone = 'pausing'; html = `<span class="live-dot"></span><strong>Pausing…</strong><span>Finishing minute ${state.time} first.</span>`;
  } else if (control.active) {
    tone = 'running';
    html = `<span class="live-dot"></span><strong>Kitchen running</strong><span>Minute ${state.time} of ${state.deadline}</span>`
      + (control.run_id === selected ? '' : '<button type="button" class="text-button" data-live="view">Show it →</button>');
  } else {
    const orders = Object.values(state.orders), done = orders.filter(o => o.correct === true).length;
    const onTime = orders.filter(o => o.on_time === true).length;
    if (state.status === 'completed') {
      tone = 'done'; html = `<span class="live-dot"></span><strong>Shift complete</strong><span>${done} of ${orders.length} orders done${onTime === done ? ', all on time' : `, ${onTime} on time`} · minute ${state.time}</span>`;
    } else if (state.status === 'timed_out') {
      tone = 'late'; html = `<span class="live-dot"></span><strong>Time's up</strong><span>${done} of ${orders.length} orders done in ${state.deadline} minutes</span>`;
    } else if (state.status === 'paused') {
      tone = 'paused'; html = `<span class="live-dot"></span><strong>Paused</strong><span>Minute ${state.time} of ${state.deadline} · press Continue to keep going</span>`;
    } else {
      html = `<strong>Minute ${state.time}</strong>`;
    }
  }
  notice.className = `live-notice ${tone}`;
  setContent('live-notice', html);
  for (const id of ['start', 'pause']) $(id).title = control.active ? (id === 'pause' ? 'Stop after the current minute' : 'The kitchen is already running') : (id === 'start' ? 'Run the shift' : 'Nothing is running');
}

$('live-notice').onclick = async event => {
  if (event.target.closest?.('[data-live]')?.dataset.live === 'view' && detail?.control?.run_id) {
    stopPlayer(); replayTick = null; selected = detail.control.run_id;
    await loadRun(selected, null, true);
  }
};

function syncRunMode() {
  // New runs: the burrito line on a normal shift; AI when the server has a key, otherwise rule-based.
  $('kitchen').value = SIMPLE_KITCHEN;
  $('scenario').value = SIMPLE_SCENARIO;
  $('mode').value = !isStatic && meta.ai_available ? 'ai' : 'det';
}

/* Simple dashboard has no recording picker, so always show the newest shift: the live one,
   or the most recent if nothing is running. (The hosted viewer keeps its picker.) */
let followingNewest = false;
function followNewestRun() {
  if (isStatic || followingNewest || replayTick !== null || !runs.length) return;
  const live = detail?.control?.active && detail.control.run_id;
  const newest = live || runs[0].run_id;
  if (!newest || newest === selected) return;
  followingNewest = true;
  stopPlayer();
  loadRun(newest, null, true).catch(e => error(e.message)).finally(() => { followingNewest = false; });
}

/* An open tab keeps running the copy of the page it loaded. When the dashboard files change,
   reload so nobody is left on an old version, but never while typing or with unsaved edits. */
let loadedUiVersion = null;
function reloadIfUpdated() {
  if (isStatic || !meta.ui_version || typeof location === 'undefined') return;
  if (loadedUiVersion === null) { loadedUiVersion = meta.ui_version; return; }
  if (meta.ui_version === loadedUiVersion || profileDirty || sendingUpdate || $('kitchen-update').value.trim()) return;
  location.reload();
}

function renderWorkspace() {
  reloadIfUpdated();
  followNewestRun();
  syncRunMode();
  renderLiveNotice();
  updateBoxState();
  if (currentTab === 'tasks') renderTasks();
  if (currentTab === 'profiles') {
    // Follow the selected run's kitchen until the viewer picks one themselves.
    const wanted = profileKitchenChosen ? profileKitchen : (detail ? kitchenOf(detail.state) : profileKitchen || 'chipotle');
    if (!isStatic && (wanted !== profileKitchen || !profileData) && !profileDirty) loadProfiles(wanted);
    else renderProfiles();
  }
}

/* ------------------------------------------------- chat: manager messages */

const baseConversationMessages = conversationMessages;
conversationMessages = function (state, events) {
  const base = baseConversationMessages(state, events);
  const typed = new Set(events.filter(e => e.type === 'operator_command' && e.payload?.command?.type === 'announce')
    .map(e => e.payload.command.text));
  const said = new Set(events.filter(e => e.type === 'operator_command').map(e => e.payload?.text));
  // Announcements the scenario agent added to an update already appear under that update's bubble.
  const agentSaid = command => (command.payload?.changes || []).filter(c => c.type === 'announce' && c.text).map(c => c.text);
  for (const e of events) if (e.type === 'operator_command') agentSaid(e).forEach(text => typed.add(text));
  const station = id => state.stations[id]?.name || title(id);
  const ingredient = id => state.ingredients[id]?.name || title(id);
  const person = id => state.workers[id]?.name || id;
  const extra = events.flatMap(event => {
    const p = event.payload || {}, b = {seq: event.seq, tick: event.tick, worker: null, task: p.task_id || null, order: p.order_id || null};
    const system = text => [{...b, kind: 'system', text, label: 'Kitchen update', eventType: event.type}];
    switch (event.type) {
      case 'operator_command': {
        const message = p.free_text ?? (p.command?.type === 'announce');
        const said = p.interpreted_by ? p.text : (message ? p.command?.text || p.text : p.text);
        const refused = (p.rejected || []).length ? ` · couldn't apply: ${p.rejected.join('; ')}` : '';
        const told = message ? [] : agentSaid(event).filter(text => text !== said);
        const toldText = told.length ? ` · told the team: “${told.join('” “')}”` : '';
        return [{...b, kind: 'manager', text: said, label: message ? 'Message to the team' : 'Kitchen update',
          detail: (message ? 'Shared with every worker' : p.description) + toldText + refused}];
      }
      case 'announcement':
        return typed.has(p.text) ? [] : [{...b, kind: 'manager', text: p.text, label: 'Announcement'}];
      case 'scenario_interpreted':  // the AI's note; the operator_command bubble already shows it
        return said.has(p.text) ? [] : [{...b, kind: 'manager', text: p.text, label: 'Kitchen update', detail: p.summary || ''}];
      case 'station_availability':
        return system(`${station(p.station_id)} is ${p.available ? 'working again' : 'out of service'}.`);
      case 'stock_changed':
        return system(`${ingredient(p.ingredient_id)} stock changed from ${p.before} to ${p.after}.`);
      case 'incident_started': case 'incident_cleared':
        return system(p.text || `${title(p.name)} ${event.type === 'incident_started' ? 'started' : 'cleared'}.`);
      case 'station_capacity_changed':
        return system(`${station(p.station_id)} now handles ${p.after} task${p.after === 1 ? '' : 's'} at once (was ${p.before}).`);
      case 'station_speed_changed':
        return system(`${station(p.station_id)} tasks now take ${p.after}× their normal time.`);
      case 'worker_skill_changed':
        return system(p.after == null ? `${person(p.worker_id)} can no longer do ${p.skill} work.`
          : `${person(p.worker_id)}'s ${p.skill} work now takes ${p.after}× the standard time.`);
      case 'order_added':
        return system(`New order ${p.order_id} was added, due by minute ${p.deadline_minutes}.`);
      case 'order_deadline_changed':
        return system(`${p.order_id} is now due by minute ${p.after} (was ${p.before}).`);
      default:
        return [];
    }
  });
  return [...base, ...extra].sort((a, b) => a.seq - b.seq).map(message => {
    if (message.kind === 'manager') return message;
    const text = plainWords(state, message.text);
    return text === message.text ? message : {...message, text, original: message.text};
  });
};

/* Swap task and worker ids inside recorded explanations for plain words, for display only.
   "free to take O6.bu2.build" -> "free to take “Build burrito” for O6 (Burrito #2)"; "w_line" -> "Bri". */
const idPatternCache = new WeakMap();
function plainWords(state, text) {
  if (!text || !state) return text;
  if (!idPatternCache.has(state)) {
    const ids = [...Object.keys(state.tasks || {}), ...Object.keys(state.workers || {}), ...Object.keys(state.orders || {})].sort((a, b) => b.length - a.length);
    const escaped = ids.map(id => id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    idPatternCache.set(state, escaped.length ? new RegExp(`(?<![\\w.])(${escaped.join('|')})(?![\\w])`, 'g') : null);
  }
  const pattern = idPatternCache.get(state);
  if (!pattern) return text;
  return text.replace(pattern, id => {
    if (state.workers?.[id]) return state.workers[id].name || id;
    if (state.orders?.[id] && !state.tasks?.[id]) return orderLabel(state, id);
    const task = state.tasks[id], order = state.orders?.[task.order_id];
    const recipe = task.item_id ? (order?.expected_items || []).find(i => i.item_id === task.item_id)?.recipe_id : 'order';
    const step = cachedStepNames(state)[`${recipe}.${task.step_id}`] || title(task.step_id);
    const same = task.item_id ? (order?.expected_items || []).filter(i => i.recipe_id === recipe).length : 0;
    return `“${step}” for ${orderLabel(state, task.order_id)}${same > 1 ? ` (${itemLabel(state, order, task.item_id, recipe)})` : ''}`;
  });
}

/* --------------------------------------------- chat: kitchen-update composer */

function setUpdateHint(text, tone) {
  const hint = $('kitchen-update-hint');
  hint.textContent = text;
  hint.className = `kitchen-update-hint ${tone || ''}`;
}

function updateSuggestions(state) {
  const stations = Object.entries(state.stations), workers = Object.values(state.workers);
  const broken = stations.find(([, s]) => s.available === false);
  const working = stations.find(([id, s]) => s.available !== false && ['fryer', 'grill', 'tortilla'].includes(id)) || stations.find(([, s]) => s.available !== false);
  const away = workers.find(w => !w.available), here = workers.find(w => w.available);
  const ingredient = Object.keys(state.ingredients).find(id => ['avocado', 'patty', 'cheese', 'chicken'].includes(id)) || Object.keys(state.ingredients)[0];
  const open = Object.values(state.orders).filter(o => o.correct == null && o.status !== 'scheduled').sort((a, b) => a.deadline_minutes - b.deadline_minutes)[0];
  const helper = workers.find(w => w.available && w !== here) || here;
  return [
    away ? `${away.name} is back from break` : here && `${here.name} went out for a break`,
    helper && `${helper.name} burnt their hand and can only work slowly`,
    broken ? `the ${broken[1].name.toLowerCase()} is working again` : working && `the ${working[1].name.toLowerCase()} stopped working`,
    ingredient && `we just got 10 more ${ingredient}`,
    open && `${orderLabel(state, open.id)} is in a hurry`,
  ].filter(Boolean);
}

function updateBoxState() {
  const s = detail?.state;
  let disabled = false, hint;
  if (isStatic) { disabled = true; hint = 'Replay-only viewer. Run python3 dashboard.py locally to send kitchen updates.'; }
  else if (!s) { disabled = true; hint = 'Start or select a run to send kitchen updates.'; }
  else if (['completed', 'timed_out'].includes(s.status)) { disabled = true; hint = 'This shift has ended. Start a new run to send kitchen updates.'; }
  else hint = `Applied at the start of minute ${s.next_tick ?? s.time + 1}. Equipment, staff and stock changes are checked by the engine; anything else is passed to the workers as a message.`;
  $('kitchen-update').disabled = disabled;
  // The reason goes in the box itself; the line underneath only ever shows a rejection.
  $('kitchen-update').placeholder = disabled ? hint : 'Tell the kitchen anything… e.g. Deshawn burnt his hand and can\'t work';
  $('kitchen-update-send').disabled = disabled || sendingUpdate;
  $('kitchen-update-send').innerHTML = sendingUpdate ? 'Sending…' : 'Send <span aria-hidden="true">↑</span>';
  $('kitchen-update-form').classList.toggle('disabled', disabled);
  if (!updateHintLocked && !sendingUpdate) setUpdateHint('', '');
  const chips = s && !disabled ? updateSuggestions(s) : [];
  setContent('kitchen-update-suggestions', chips.map(text => `<button type="button" class="update-chip" data-update="${esc(text)}">${esc(text)}</button>`).join(''));
}

async function sendKitchenUpdate(event) {
  event?.preventDefault?.();
  const input = $('kitchen-update'), text = input.value.trim();
  if (!text || sendingUpdate || !selected || isStatic) return;
  sendingUpdate = true; updateHintLocked = true;
  setUpdateHint('', '');
  updateBoxState();
  try {
    const result = await json('/api/command', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({run_id: selected, text})});
    input.value = '';
    updateHintLocked = false;  // the manager bubble in the chat is the confirmation
    stopPlayer(); replayTick = null; followConversation = true; conversationKind = 'all';
    await loadRun(selected);
  } catch (e) {
    setUpdateHint(e.message, 'error');
  } finally {
    sendingUpdate = false;
    updateBoxState();
  }
}

$('kitchen-update-form').onsubmit = sendKitchenUpdate;
$('kitchen-update').oninput = () => { updateHintLocked = false; };
$('kitchen-update-suggestions').onclick = event => {
  const chip = event.target.closest?.('[data-update]');
  if (!chip) return;
  $('kitchen-update').value = chip.dataset.update;
  $('kitchen-update').focus?.();
};

/* ---------------------------------------------------------- task progress */

const toneLabels = {done: 'Completed', active: 'In progress', delayed: 'Delayed', waiting: 'Waiting'};
const sections = [
  ['delayed', 'Needs attention', 'Blocked, past the order deadline, or due to finish after it'],
  ['active', 'In progress', 'Work is under way'],
  ['waiting', 'Up next', 'Waiting on earlier steps, a free worker, or the order to arrive'],
  ['done', 'Completed', 'Verified and handed off'],
];

function stepNames(state) {
  const menu = state.config?.menu || {}, names = {};
  for (const [recipeId, recipe] of Object.entries(menu.recipes || {}))
    for (const step of recipe.steps || []) names[`${recipeId}.${step.id}`] = step.name;
  for (const step of menu.order_steps || []) names[`order.${step.id}`] = step.name;
  return names;
}

const stepNameCache = new WeakMap();
function cachedStepNames(state) {
  if (!state || typeof state !== 'object') return {};
  if (!stepNameCache.has(state)) stepNameCache.set(state, stepNames(state));
  return stepNameCache.get(state);
}

/* What people call an order: "Maya #101" (customer + ticket), else "Order 1". Never the raw "O1". */
function orderLabel(state, orderId) {
  const order = state?.orders?.[orderId] || {};
  if (order.customer && order.ticket) return `${order.customer} #${order.ticket}`;
  if (order.ticket) return `#${order.ticket}`;
  const digits = String(orderId || '').replace(/\D/g, '');
  return digits ? `Order ${digits}` : String(orderId || '');
}

function itemLabel(state, order, itemId, recipe) {
  const name = state?.config?.menu?.recipes?.[recipe]?.name || title(recipe || 'item');
  const same = (order?.expected_items || []).filter(i => i.recipe_id === recipe).length;
  return same > 1 ? `${name} ${String(itemId).split('.').pop().replace(/^\D+/, '')}` : name;
}

/* Plain words for a task id: "O5.b1.assemble" -> "Assemble burger · O5 · Cheeseburger". */
function taskText(state, taskId) {
  if (!taskId) return '';
  const parts = String(taskId).split('.'), task = state?.tasks?.[taskId];
  const orderId = task?.order_id || parts[0], order = state?.orders?.[orderId];
  const stepId = task?.step_id || parts[parts.length - 1];
  const itemId = task ? task.item_id : (parts.length > 2 ? parts.slice(0, -1).join('.') : null);
  const recipe = itemId ? (order?.expected_items || []).find(i => i.item_id === itemId)?.recipe_id : 'order';
  const step = cachedStepNames(state)[`${recipe}.${stepId}`] || title(stepId);
  return itemId ? `${step} · ${orderLabel(state, orderId)} · ${itemLabel(state, order, itemId, recipe)}` : `${step} · ${orderLabel(state, orderId)}`;
}

function itemsPhrase(state, order) {
  const recipes = state.config?.menu?.recipes || {};
  return (order.items || []).map(item => {
    const name = recipes[item.recipe]?.name || title(item.recipe);
    const mods = (item.modifiers || []).map(m => recipes[item.recipe]?.modifiers?.[m]?.label || title(m));
    return `${item.quantity}× ${name}${mods.length ? ` (${mods.join(', ')})` : ''}`;
  }).join(' · ');
}

function taskView(state, task, order, names, recipeOf) {
  const time = state.time, deadline = order?.deadline_minutes, status = task.status;
  let pct = 0;
  if (status === 'done') pct = 100;
  else if (status === 'active') {
    const length = Math.max(1, task.finishes_at - task.started_at);
    pct = Math.min(99, Math.max(0, Math.round((time - task.started_at) / length * 100)));
  }
  const blocked = state.blockers?.[task.id];
  const late = deadline != null && ((status !== 'done' && time > deadline) || (status === 'active' && task.finishes_at > deadline));
  const finishedLate = status === 'done' && deadline != null && task.completed_at > deadline;
  const tone = status === 'done' ? 'done' : (blocked || late) ? 'delayed' : status === 'active' ? 'active' : 'waiting';
  const ready = status === 'pending' && (task.depends_on || []).every(id => state.tasks[id]?.status === 'done');
  const recipe = task.item_id ? recipeOf[task.item_id] : 'order';
  const name = names[`${recipe}.${task.step_id}`] || title(task.step_id);
  const item = task.item_id ? itemLabel(state, order, task.item_id, recipe) : 'Whole order';
  let note;
  if (status === 'done') note = `Done at minute ${task.completed_at}${finishedLate ? ' · after the deadline' : ''}`;
  else if (blocked) note = blocked;
  else if (status === 'active') note = `Ready at minute ${task.finishes_at}${late ? ' · after the deadline' : ''}`;
  else if (late) note = `Not started · due at minute ${deadline}`;
  else if (ready) note = 'Ready to start';
  else {
    const waiting = (task.depends_on || []).filter(id => state.tasks[id]?.status !== 'done').map(id => cachedStepNames(state)[`${recipeOf[state.tasks[id]?.item_id] || 'order'}.${state.tasks[id]?.step_id}`] || title(state.tasks[id]?.step_id || id));
    note = waiting.length ? `Waiting for: ${[...new Set(waiting)].join(', ')}` : 'Waiting on earlier steps';
  }
  return {task, tone, pct, name, item, note, worker: state.workers[task.worker_id],
    station: state.stations[task.station_id]?.name || task.station_id};
}

function orderView(state, order, names) {
  const recipeOf = Object.fromEntries((order.expected_items || []).map(i => [i.item_id, i.recipe_id]));
  const views = (order.task_ids || []).map(id => state.tasks[id]).filter(Boolean).map(t => taskView(state, t, order, names, recipeOf));
  const pct = views.length ? Math.round(views.reduce((sum, v) => sum + v.pct, 0) / views.length) : 0;
  const unfinished = order.correct == null || order.correct === false;
  const tone = order.correct === true ? 'done'
    : (views.some(v => v.tone === 'delayed') || (unfinished && state.time > order.deadline_minutes) || order.correct === false) ? 'delayed'
    : views.some(v => v.tone === 'active' || v.tone === 'done') ? 'active' : 'waiting';
  let status;
  if (order.correct === true) status = order.on_time ? `Verified on time · minute ${order.completed_at}` : `Verified late · minute ${order.completed_at}`;
  else if (order.correct === false) status = 'Not finished';
  else if (!views.length) status = `Arrives minute ${order.arrival_minutes} · due ${order.deadline_minutes}`;
  else {
    const left = order.deadline_minutes - state.time;
    status = left >= 0 ? `Due minute ${order.deadline_minutes} · ${left} min left` : `Due minute ${order.deadline_minutes} · ${-left} min over`;
  }
  return {order, views, pct, tone, status};
}

function taskRow(v) {
  const who = v.worker
    ? `${avatar(v.worker)}<span><b>${esc(v.worker.name)}</b><small>${esc(v.station)}</small></span>`
    : `<span class="task-unassigned"><b>Unassigned</b><small>${esc(v.station)}</small></span>`;
  return `<div class="task-row ${v.tone}"><span class="task-dot" title="${toneLabels[v.tone]}"></span>`
    + `<div class="task-name" title="${esc(v.task.id)}"><b>${esc(v.name)}</b><small>${esc(v.item)}</small></div>`
    + `<div class="task-who">${who}</div>`
    + `<div class="task-progress"><div class="progress-track"><i style="width:${v.pct}%"></i></div><span class="task-pct">${v.pct}%</span></div>`
    + `<div class="task-state"><span class="task-badge ${v.tone}">${toneLabels[v.tone]}</span><small>${esc(v.note)}</small></div></div>`;
}

function renderTasks() {
  if (!detail) { setContent('task-board', '<div class="empty">Select a run to see its tasks.</div>'); return; }
  const state = detail.state, names = stepNames(state);
  taskFilter = $('task-filter').value || 'all';
  const orders = Object.values(state.orders).sort((a, b) => natural(a.id, b.id)).map(o => orderView(state, o, names));
  const tasks = orders.flatMap(o => o.views);
  const count = tone => tasks.filter(v => v.tone === tone).length;
  const overall = tasks.length ? Math.round(tasks.reduce((sum, v) => sum + v.pct, 0) / tasks.length) : 0;
  const notArrived = Object.values(state.orders).filter(o => !(o.task_ids || []).length).length;
  setContent('task-summary', `<div class="task-overall"><div class="task-overall-top"><strong>${overall}%</strong><span>of task work done at minute ${pad2(state.time)} · ${tasks.length} tasks on the board${notArrived ? ` · ${notArrived} order${notArrived === 1 ? '' : 's'} not arrived yet` : ''}</span></div><div class="progress-track big"><i style="width:${overall}%"></i></div></div>`
    + `<div class="task-chips">${['done', 'active', 'delayed', 'waiting'].map(tone => `<button type="button" class="task-chip ${tone} ${taskFilter !== 'all' && taskFilter !== tone ? 'dim' : ''} ${taskFilter === tone ? 'selected' : ''}" data-task-tone="${tone}" aria-pressed="${taskFilter === tone}"><i></i><b>${count(tone)}</b> ${toneLabels[tone].toLowerCase()}</button>`).join('')}</div>`);

  const html = sections.map(([tone, heading, help]) => {
    const group = orders.filter(o => o.tone === tone).map(o => {
      const shown = taskFilter === 'all' ? o.views : o.views.filter(v => v.tone === taskFilter);
      if (taskFilter !== 'all' && !shown.length && !(taskFilter === 'waiting' && !o.views.length)) return '';
      const open = taskFilter !== 'all' ? true : orderOpen[o.order.id] ?? tone !== 'done';
      const counts = ['done', 'active', 'delayed'].map(t => o.views.filter(v => v.tone === t).length);
      const body = o.views.length ? shown.map(taskRow).join('') : '<div class="task-empty">No tasks yet: the order has not arrived.</div>';
      return `<details class="task-order ${o.tone}" data-order="${esc(o.order.id)}" ${open ? 'open' : ''}><summary>`
        + `<span class="task-order-id">${esc(orderLabel(state, o.order.id))}</span>`
        + `<span class="task-order-items">${esc(itemsPhrase(state, o.order))}</span>`
        + `<span class="task-order-status">${esc(o.status)}</span>`
        + `<span class="task-order-counts">${o.views.length ? `${counts[0]}/${o.views.length} done${counts[1] ? ` · ${counts[1]} active` : ''}${counts[2] ? ` · ${counts[2]} delayed` : ''}` : ''}</span>`
        + `<span class="task-order-pct">${o.pct}%</span>`
        + `<button type="button" class="task-order-open" data-order-open="${esc(o.order.id)}" title="Open order details">↗</button>`
        + `<span class="task-order-bar progress-track"><i style="width:${o.pct}%"></i></span></summary>`
        + `<div class="task-rows">${body}</div></details>`;
    }).filter(Boolean);
    if (!group.length) return '';
    return `<section class="task-section ${tone}"><header><span class="task-section-dot"></span><h3>${heading}</h3><span class="task-section-count">${group.length} order${group.length === 1 ? '' : 's'}</span><small>${help}</small></header>${group.join('')}</section>`;
  }).join('');
  setContent('task-board', html || `<div class="empty">No ${toneLabels[taskFilter]?.toLowerCase() || ''} tasks at minute ${state.time}.</div>`);
}

$('task-filter').onchange = renderTasks;
$('task-summary').onclick = event => {
  const chip = event.target.closest?.('[data-task-tone]');
  if (!chip) return;
  $('task-filter').value = $('task-filter').value === chip.dataset.taskTone ? 'all' : chip.dataset.taskTone;
  renderTasks();
};
$('task-board').onclick = event => {
  const open = event.target.closest?.('[data-order-open]');
  if (open) {
    event.preventDefault();
    if (detail?.state.orders[open.dataset.orderOpen]) showOrder(open.dataset.orderOpen);
    return;
  }
  const summary = event.target.closest?.('summary');
  const card = summary?.parentElement;
  if (card?.dataset?.order) orderOpen[card.dataset.order] = !card.open;  // click fires before the toggle
};

/* -------------------------------------------- order dialog (from chat chips) */

showOrder = function (id) {
  const state = detail.state, order = state.orders[id];
  if (!order) return;
  const view = orderView(state, order, cachedStepNames(state));
  const counts = ['delayed', 'active', 'waiting', 'done'].map(t => [t, view.views.filter(v => v.tone === t).length]).filter(([, n]) => n);
  $('order-detail').innerHTML = `<div class="eyebrow">ORDER DETAIL</div>`
    + `<h2 class="order-dialog-title">${esc(orderLabel(state, id))} <span class="task-badge ${view.tone}">${esc(view.status)}</span></h2>`
    + `<p class="dialog-subtitle">${esc(itemsPhrase(state, order))}<br>Arrives at minute ${order.arrival_minutes}. Due at minute ${order.deadline_minutes}.</p>`
    + (view.views.length
      ? `<div class="order-dialog-progress"><div class="progress-track big ${view.tone}"><i style="width:${view.pct}%"></i></div><strong>${view.pct}%</strong></div>`
        + `<div class="order-dialog-counts">${counts.map(([t, n]) => `<span class="task-chip ${t}"><i></i><b>${n}</b> ${toneLabels[t].toLowerCase()}</span>`).join('')}</div>`
        + `<div class="order-dialog-tasks">${view.views.map(taskRow).join('')}</div>`
      : '<div class="empty">Tasks will be created when this order arrives.</div>');
  $('order-dialog').showModal();
};

/* ----------------------------------------------------------- team profiles */

const speedOptions = [[0.5, '2× faster'], [0.75, 'Faster'], [1, 'Normal speed'], [1.25, 'A bit slower'], [1.5, '1.5× time'], [2, 'Half speed'], [3, 'Third speed']];

async function loadProfiles(kitchen) {
  profileKitchen = kitchen || 'chipotle';
  $('profiles-kitchen').value = profileKitchen;
  const request = ++profileRequest;
  if (isStatic) { profileData = null; profileKey = ''; renderProfiles(); return; }
  renderProfiles();
  try {
    const data = await json(`/api/profiles?kitchen=${encodeURIComponent(profileKitchen)}`);
    if (request !== profileRequest) return;
    profileData = data; profileDirty = false; profileKey = '';
    if (!data.workers.some(w => w.id === profileSelected)) profileSelected = data.workers[0]?.id || null;
    renderProfiles();
  } catch (e) {
    profileMessage = {tone: 'error', text: e.message}; renderProfiles();
  }
}

function skillChips(skills) {
  return Object.entries(skills || {}).map(([skill, m]) => `<span class="skill">${esc(title(skill))}${m === 1 ? '' : ` ×${esc(m)}`}</span>`).join('');
}

function profileSummary(w, stations) {
  const p = w.profile;
  if (!p) return 'No profile details yet';
  const years = p.experience_years === 0 ? 'New this month' : `${p.experience_years} yr${p.experience_years === 1 ? '' : 's'}`;
  return `${years} · ${stations[p.home_station] || title(p.home_station)}`;
}

function renderProfiles() {
  const editable = !isStatic && !!profileData && profileData.kitchen === profileKitchen;
  const workers = editable ? profileData.workers : isStatic ? Object.values(detail?.state.workers || {}) : [];
  const stations = editable ? profileData.stations : Object.fromEntries(Object.entries(detail?.state.stations || {}).map(([id, s]) => [id, s.name]));
  if (workers.length && !workers.some(w => w.id === profileSelected)) profileSelected = workers[0].id;
  $('profiles-kitchen').value = profileKitchen || 'chipotle';
  $('profiles-caption').textContent = editable
    ? `Edits save to ${profileData.paths.workers} and ${profileData.paths.personas}/ and apply to new runs. Recorded runs keep the team they started with.`
    : isStatic ? 'Read-only: this hosted viewer shows the team as recorded in the selected run.' : 'Loading profiles…';
  setContent('profiles-note', profileMessage ? `<div class="profile-message ${profileMessage.tone}">${esc(profileMessage.text)}</div>` : '');
  setContent('profiles-list', workers.map(w => `<button type="button" class="profile-card ${w.id === profileSelected ? 'selected' : ''}" data-profile="${esc(w.id)}">${avatar(w)}<span class="profile-card-text"><span class="profile-card-name"><strong>${esc(w.name)}</strong><small>${esc(w.role)}</small></span><small class="profile-card-meta">${esc(profileSummary(w, stations))}</small><span class="worker-skills">${skillChips(w.skills)}</span></span></button>`).join('') || '<div class="empty">Loading the team…</div>');
  const worker = workers.find(w => w.id === profileSelected);
  const key = `${editable}:${profileKitchen}:${profileSelected}:${editable ? JSON.stringify(worker) + (profileData.personas[profileSelected] || '') : detail?.state.run_id}`;
  if (key === profileKey) return;
  profileKey = key;
  $('profile-editor').innerHTML = !worker ? '' : editable ? profileForm(worker) : profileReadOnly(worker, stations);
}

function profileReadOnly(w, stations) {
  const p = w.profile || {};
  const list = (label, items) => Array.isArray(items) && items.length ? `<dt>${label}</dt><dd><ul>${items.map(x => `<li>${esc(x)}</li>`).join('')}</ul></dd>` : '';
  return `<div class="profile-view"><header class="pf-head">${avatar(w)}<div><h3>${esc(w.name)} <span>${esc(w.role)}</span></h3></div></header><p class="profile-description">${esc(w.description)}</p><div class="worker-skills">${skillChips(w.skills)}</div><dl>${p.background ? `<dt>Background</dt><dd>${esc(p.background)}</dd>` : ''}${p.home_station ? `<dt>Home station</dt><dd>${esc(stations[p.home_station] || p.home_station)}</dd>` : ''}${p.shift ? `<dt>Shift</dt><dd>${esc(p.shift)}</dd>` : ''}${list('Certifications', p.certifications)}${list('Strengths', p.strengths)}${list('Watch-outs', p.watch_outs)}</dl></div>`;
}

function profileForm(w) {
  const p = w.profile || {}, persona = profileData.personas[w.id] || '';
  const field = (id, label, value, attrs = '') => `<label class="pf-field"><span>${label}</span><input id="pf-${id}" value="${esc(value ?? '')}" ${attrs}></label>`;
  const area = (id, label, value, rows = 3, help = '') => `<label class="pf-field wide">${label ? `<span>${label}${help ? ` <small>${help}</small>` : ''}</span>` : ''}<textarea id="pf-${id}" rows="${rows}">${esc(value ?? '')}</textarea></label>`;
  const skills = profileData.skills.map(skill => {
    const has = Object.prototype.hasOwnProperty.call(w.skills || {}, skill), value = has ? Number(w.skills[skill]) : 1;
    const options = speedOptions.some(([v]) => v === value) ? speedOptions : [...speedOptions, [value, `${value}× time`]].sort((a, b) => a[0] - b[0]);
    return `<div class="pf-skill ${has ? 'on' : ''}"><label class="pf-skill-toggle"><input type="checkbox" data-skill-on="${esc(skill)}" ${has ? 'checked' : ''}><span class="pf-switch" aria-hidden="true"></span><span class="pf-skill-name">${esc(title(skill))}</span></label><select data-skill-x="${esc(skill)}" aria-label="${esc(title(skill))} speed" ${has ? '' : 'disabled'}>${options.map(([v, label]) => `<option value="${v}" ${v === value ? 'selected' : ''}>${label}</option>`).join('')}</select></div>`;
  }).join('');
  const stationOptions = '<option value="">Choose a station</option>' + Object.entries(profileData.stations).map(([id, name]) => `<option value="${esc(id)}" ${p.home_station === id ? 'selected' : ''}>${esc(name)}</option>`).join('');
  return `<form id="profile-form" class="profile-form" autocomplete="off">
    <header class="pf-head">${avatar(w)}<div><h3>${esc(w.name)} <span>${esc(w.role)}</span></h3><p><code>${esc(w.id)}</code> · ${esc(kitchenNames[profileKitchen] || profileKitchen)}</p></div></header>
    <section class="pf-card"><h4>Basics</h4><div class="pf-grid two">${field('name', 'Name', w.name, 'maxlength="40" required')}${field('role', 'Role', w.role, 'maxlength="60" required')}${area('description', 'Description', w.description, 2, 'shown to teammates and in the story')}</div></section>
    <section class="pf-card"><h4>Skills and speed <small>Only switched-on skills can be done. Speed sets how long each task takes for this person.</small></h4><div class="pf-skills">${skills}</div></section>
    <section class="pf-card"><h4>Profile <small>Optional. If you fill any of it, years, home station and background are required.</small></h4><div class="pf-grid">${field('experience_years', 'Years of experience', p.experience_years, 'type="number" min="0" max="60" step="1"')}
    <label class="pf-field"><span>Home station</span><select id="pf-home_station">${stationOptions}</select></label>
    ${field('shift', 'Shift', p.shift, 'maxlength="80" placeholder="e.g. Lunch (10:30 a.m. to 3:00 p.m.)"')}
    ${area('background', 'Background', p.background, 3)}</div>
    <div class="pf-grid lists">${area('certifications', 'Certifications', (p.certifications || []).join('\n'), 4, 'one per line')}${area('strengths', 'Strengths', (p.strengths || []).join('\n'), 4, 'one per line')}${area('watch_outs', 'Watch-outs', (p.watch_outs || []).join('\n'), 4, 'one per line')}</div></section>
    <section class="pf-card"><h4>Persona <small>What this worker's AI reads in AI mode: background, priorities, tone. It never grants skills.</small></h4>${area('persona', '', persona, 10)}</section>
    <div class="pf-actions"><span id="pf-dirty" class="pf-dirty ${profileDirty ? 'on' : ''}">${profileDirty ? '● Unsaved changes' : 'No unsaved changes'}</span><button type="button" id="pf-revert" class="button button-outline">Discard</button><button type="submit" id="pf-save" class="button button-dark">Save profile</button></div>
  </form>`;
}

function readProfileForm(w) {
  const get = id => document.getElementById(`pf-${id}`);
  const skills = {};
  for (const box of document.querySelectorAll('[data-skill-on]')) {
    if (!box.checked) continue;
    const skill = box.dataset.skillOn;
    skills[skill] = Number(document.querySelector(`[data-skill-x="${skill}"]`).value);
  }
  const years = get('experience_years').value.trim(), profile = {};
  if (years !== '') profile.experience_years = Number(years);
  if (get('background').value.trim()) profile.background = get('background').value.trim();
  if (get('home_station').value) profile.home_station = get('home_station').value;
  if (get('shift').value.trim()) profile.shift = get('shift').value.trim();
  for (const key of ['certifications', 'strengths', 'watch_outs']) { const items = lines(get(key).value); if (items.length) profile[key] = items; }
  const worker = {name: get('name').value.trim(), role: get('role').value.trim(), description: get('description').value.trim(), skills};
  if (Object.keys(profile).length) {
    const missing = ['experience_years', 'background', 'home_station'].filter(k => !(k in profile));
    if (missing.length) throw new Error(`A profile needs ${missing.map(k => title(k).toLowerCase()).join(', ')} (or clear all profile fields).`);
    worker.profile = profile;
  }
  if (!Object.keys(skills).length) throw new Error('Switch on at least one skill.');
  return {kitchen: profileKitchen, worker_id: w.id, worker, persona: get('persona').value};
}

async function saveProfile(event) {
  event.preventDefault();
  const worker = profileData?.workers.find(w => w.id === profileSelected);
  if (!worker) return;
  const save = document.getElementById('pf-save');
  try {
    const payload = readProfileForm(worker);
    if (save) { save.disabled = true; save.textContent = 'Saving…'; }
    profileData = await json('/api/profiles', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    profileDirty = false; profileKey = '';
    profileMessage = {tone: 'ok', text: `Saved ${payload.worker.name}. Start a new ${kitchenNames[profileKitchen]} run to use the change.`};
  } catch (e) {
    profileMessage = {tone: 'error', text: e.message};
    if (save) { save.disabled = false; save.textContent = 'Save profile'; }
  }
  renderProfiles();
}

function markDirty() {
  profileDirty = true;
  const flag = document.getElementById('pf-dirty');
  if (flag) { flag.textContent = '● Unsaved changes'; flag.classList.add('on'); }
}

$('profiles-kitchen').onchange = event => {
  if (profileDirty) { event.target.value = profileKitchen; profileMessage = {tone: 'error', text: 'Save or discard your changes before switching kitchens.'}; renderProfiles(); return; }
  profileMessage = null; profileKitchenChosen = true; loadProfiles(event.target.value);
};
$('profiles-list').onclick = event => {
  const card = event.target.closest?.('[data-profile]');
  if (!card || card.dataset.profile === profileSelected) return;
  if (profileDirty) { profileMessage = {tone: 'error', text: 'Save or discard your changes before opening another profile.'}; renderProfiles(); return; }
  profileSelected = card.dataset.profile; profileMessage = null; renderProfiles();
};
$('profile-editor').onsubmit = saveProfile;
$('profile-editor').oninput = event => {
  if (isStatic || !profileData) return;
  markDirty();
  const toggle = event.target.closest?.('[data-skill-on]');
  if (toggle) {
    const row = toggle.closest('.pf-skill');
    row.classList.toggle('on', toggle.checked);
    row.querySelector('select').disabled = !toggle.checked;
  }
};
$('profile-editor').onchange = $('profile-editor').oninput;
$('profile-editor').onclick = event => {
  if (event.target.id !== 'pf-revert') return;
  profileDirty = false; profileKey = ''; profileMessage = null; renderProfiles();
};

$('profiles-kitchen').classList.toggle('hidden', isStatic);
if (isStatic && typeof document !== 'undefined' && document.body?.classList) document.body.classList.add('static-viewer');  // replay-only: keep the recording picker
renderWorkspace();
{ // Deep link: open a tab from the address, e.g. #tab=tasks
  const wanted = typeof location !== 'undefined' && /tab=([a-z]+)/.exec(location.hash || '');
  if (wanted && document.querySelector?.(`[data-tab="${wanted[1]}"]`)) setTab(wanted[1]);
}
