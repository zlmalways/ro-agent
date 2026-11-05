'use strict';
const $ = id => document.getElementById(id);
const base = '/api/robot-agent';
const pages = {
  home: '首页',
  tasks: '任务',
  robots: '舰队',
  settings: '设置'
};
const states = {
  RECEIVED: '正在处理',
  RECEIVING: '正在提交',
  PLANNING: '正在生成计划',
  NEEDS_CLARIFICATION: '需要澄清',
  WAITING_APPROVAL: '等待批准',
  READY: '等待执行',
  QUEUED: '正在排队',
  RUNNING: '执行中',
  BLOCKED: '已暂停，等待核对',
  CANCEL_REQUESTED: '正在停止，等待机器人确认',
  SUCCEEDED: '已完成',
  FAILED: '任务未完成',
  CANCELLED: '已停止'
};
const outcomes = {
  NONE: '尚无执行结果',
  RUNNING: '机器人正在执行',
  SUCCEEDED: '机器人报告成功',
  FAILED: '机器人报告失败',
  RESULT_UNKNOWN: '执行结果未知，请等待核对'
};
let credential = '',
  currentTask = null,
  page = 'chat',
  rows = [],
  nextBefore = null,
  stateCounts = null;
let identityEpoch = 0,
  selection = 0,
  historyRequest = 0,
  fleetRequest = 0;
let pendingWrite = null,
  writing = false,
  polling = false,
  recording = null,
  micRequest = 0;
let submission = null;
let fleetSelection = null, fleetLoading = false, fleetExpiry = null;
let fleetView = null; // Last complete list/detail read; pending reads never partly replace it.
let returnPage = 'home';
const stopRequests = new Map();
const endedStates = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);

function node(tag, text, className) {
  const el = document.createElement(tag);
  el.textContent = text;
  if (className) el.className = className;
  return el;
}
const board = createHomeBoard({
  request: () => api('/home-tasks'),
  openTask,
  node,
  approve(task) {
    // Authorize precisely the plan/version shown on the card.
    return write('/tasks/' + encodeURIComponent(task.task_id) + '/decisions',
      {decision: 'approve', plan_id: task.plan_id, expected_version: task.version}, undefined, true);
  }
});

async function api(path, options = {}, token = credential) {
  if (!token) throw new Error('请先到设置页连接服务。');
  const epoch = identityEpoch;
  const response = await fetch(base + path, {
    ...options,
    cache: 'no-store',
    headers: {
      Authorization: 'Bearer ' + token,
      ...options.headers
    }
  });
  if (response.status === 401) {
    if (epoch === identityEpoch && token === credential) resetIdentity();
    throw new Error('访问凭据无效，请重新连接服务。');
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入。');
    error.status = response.status;
    throw error;
  }
  return data;
}

function conversation(open, updateRoute = true) {
  if (updateRoute) {
    if (open && page !== 'chat' && page !== 'settings') returnPage = page;
    const target = open ? 'chat' : returnPage;
    if (location.hash !== '#' + target) history.pushState(null, '', '#' + target);
    navigate();
    return;
  }
  document.body.classList.toggle('chat-open', open);
  $('conversation').hidden = !open;
  $('dock').hidden = open;
  $('dock').setAttribute('aria-expanded', String(open));
  if (open)(currentTask ? $('new-task') : submission ? $('cancel') : $('goal')).focus();
}

function stopRecording() {
  micRequest++;
  if (recording) recording.stop();
  recording = null;
  $('record').disabled = false;
  $('record').textContent = '开始录音';
  $('record-status').textContent = '';
}

function blankTask() {
  selection++;
  stopRecording();
  currentTask = null;
  $('task-detail').hidden = true;
  $('chat-empty').hidden = false;
  $('active-composer').hidden = true;
  $('retry-submit').hidden = true;
  $('submit-form').hidden = $('voice-input').hidden = false;
  $('goal').value = $('answer').value = $('audio').value = '';
  $('error').textContent = '';
  updateControls();
}

function canStop(task) {
  return !endedStates.has(task.state);
}

function taskRobots(view) {
  return '当前分配：' + (view.allocation_robot_binding_id || '当前未占用机器人') +
    ' · 计划机器人：' + (view.plan_robot_binding_id || '尚未生成计划');
}

function taskRow(row) {
  const el = node('div', '', 'task-row');
  el.dataset.taskId = row.task_id || '';
  const open = node('button', '', 'task-open');
  const when = row.created_at ? new Date(row.created_at).toLocaleString() + ' · ' : '';
  open.append(node('span', row.goal_text), node('small', when + taskState(row)));
  if (!row.pending) open.append(node('small', taskRobots(row), 'task-description'));
  open.onclick = () => row.pending ? openSubmission() : openTask(row.task_id);
  const stopError = stopRequests.get(row.task_id)?.error;
  const description = canStop(row) && row.state !== 'CANCEL_REQUESTED' && stopError ? stopError : taskReason(row);
  if (description) open.append(node('small', description, 'task-description'));
  el.append(open);
  if (canStop(row)) {
    const busy = row.pending ? submission?.stopRequested : stopRequests.get(row.task_id)?.busy;
    const waiting = row.state === 'CANCEL_REQUESTED';
    const stop = node('button', waiting ? '等待确认' : busy ? '正在停止…' : stopError ? '重试停止' : '■ 停止', 'quiet task-stop');
    stop.setAttribute('aria-label', '停止任务：' + row.goal_text);
    stop.disabled = Boolean(waiting || busy);
    stop.onclick = () => row.pending ? stopSubmission() : stopTask(row);
    el.append(stop);
  }
  return el;
}

function updateControls() {
  $('send').disabled = !credential || writing || !$('goal').value.trim();
  $('audio-submit').disabled = !credential || writing;
  if (!recording) $('record').disabled = !credential || writing;
  $('chat-access-note').hidden = Boolean(credential);
  $('answer-form').querySelector('button').disabled = writing;
  $('approve').disabled = $('reject').disabled = writing;
  const pending = submission && submission.ticket === selection && !currentTask;
  const task = currentTask;
  const ending = task && endedStates.has(task.state);
  const rejected = pending && submission.rejected;
  const stopError = stopRequests.get(task?.task_id)?.error;
  const waiting = task?.state === 'CANCEL_REQUESTED';
  const busy = pending ? submission.stopRequested : stopRequests.get(task?.task_id)?.busy;
  $('cancel').hidden = Boolean(ending || rejected || (!pending && !task));
  $('followup-new').hidden = !ending && !rejected;
  $('cancel').disabled = Boolean(waiting || busy);
  $('cancel').textContent = waiting ? '等待确认' : busy ? '正在停止…' : stopError ? '重试停止' : '■ 停止';
  $('composer-message').textContent = rejected ? '这次提交未受理' : ending ? '这个任务已结束' : waiting ? '停止请求已发出，等待机器人反馈' : '任务进行中，可以随时停止';
  $('stop-hint').textContent = rejected ? '请新建任务，核对输入后再发送' : !ending && !waiting && stopError ? '停止尚未确认，请重试或核对任务状态' : ending ? '原始任务和已有记录会保留' : pending && submission.stopRequested ? '等待接收确认后停止任务' : waiting ? '收到可靠回执后才确认结束' : '停止后不再继续这个任务';
}

function renderPending() {
  $('task-robots').textContent = '';
  if (!submission || submission.ticket !== selection) return;
  currentTask = null;
  $('chat-empty').hidden = true;
  $('task-detail').hidden = $('active-composer').hidden = false;
  $('submit-form').hidden = $('voice-input').hidden = true;
  $('task-goal').textContent = submission.goal;
  $('sent-status').textContent = '正在发送';
  $('status').textContent = submission.stopRequested ? '正在停止' : '正在处理';
  $('reason').textContent = submission.stopRequested ? '等待平台确认收到任务，随后立即请求停止。' : '正在提交任务…';
  $('question').textContent = $('task-id').textContent = '';
  $('answer-form').hidden = $('decisions').hidden = $('execution-detail').hidden = $('task-reference').hidden = true;
  updateControls();
}

function openSubmission() {
  if (!submission) return;
  blankTask();
  submission.ticket = selection;
  renderPending(); conversation(true);
}

function stopSubmission() {
  if (!submission) return;
  submission.stopRequested = true;
  renderPending(); renderHistory();
}

function totalTasks(counts) {
  return Object.values(counts).reduce((total, value) => total + value, 0);
}

function renderHistory() {
  const shown = rows;
  for (const [id, data] of [
      ['history', shown],
      ['recent', shown.slice(0, 5)]
    ]) {
    $(id).replaceChildren(...data.map(taskRow));
    if (!data.length) {
      let message = '请先到设置中连接工作台。';
      if (credential) message = stateCounts === null ? '尚未取得任务列表。' : '当前没有已加载的任务。';
      $(id).append(node('p', message, 'empty'));
    }
  }
  $('thread-history').replaceChildren(...shown.slice(0, 6).map(row => {
    const button = node('button', '', 'thread-row');
    button.append(node('span', row.goal_text), node('small', taskState(row)));
    if (currentTask?.task_id === row.task_id && row.task_id) button.setAttribute('aria-current', 'page');
    button.onclick = () => row.pending ? openSubmission() : openTask(row.task_id);
    return button;
  }));
  $('more').hidden = nextBefore == null;
  const unknown = credential ? '待同步' : '—';
  $('recent-count').textContent = stateCounts ? totalTasks(stateCounts) : unknown;
  $('waiting-count').textContent = stateCounts ? ['NEEDS_CLARIFICATION', 'WAITING_APPROVAL', 'BLOCKED']
    .reduce((n, state) => n + (stateCounts[state] || 0), 0) : unknown;
  $('active-count').textContent = stateCounts ? stateCounts.RUNNING || 0 : unknown;
}

function rememberTask(view, submittedHere) {
  // A detail can belong to a different user whose plan this user may approve.
  const previous = rows.find(row => row.task_id === view.task_id);
  if (previous && previous.version > view.version) return;
  if (previous && previous.version === view.version) {
    previous.allocation_robot_binding_id = view.allocation_robot_binding_id;
    previous.plan_robot_binding_id = view.plan_robot_binding_id;
    renderHistory();
    return;
  }
  if (!previous && !submittedHere) {
    stateCounts = null;
  } else {
    // Update exact totals locally only when the last snapshot covered all rows.
    // With partial history a replay can return a task outside the loaded pages.
    if (stateCounts && nextBefore === null && rows.length === totalTasks(stateCounts) &&
        (!previous || stateCounts[previous.state] > 0)) {
      if (previous) stateCounts[previous.state]--;
      stateCounts[view.state] = (stateCounts[view.state] || 0) + 1;
    } else {
      stateCounts = null;
    }
    if (previous) Object.assign(previous, {state: view.state, version: view.version, goal_text: view.goal_text, reason_code: view.reason_code, allocation_robot_binding_id: view.allocation_robot_binding_id, plan_robot_binding_id: view.plan_robot_binding_id});
    else rows.unshift({task_id: view.task_id, state: view.state, version: view.version, goal_text: view.goal_text, reason_code: view.reason_code, allocation_robot_binding_id: view.allocation_robot_binding_id, plan_robot_binding_id: view.plan_robot_binding_id});
  }
  renderHistory();
}

async function loadHistory(more = false) {
  if (!credential) return;
  const request = ++historyRequest;
  try {
    const data = await api('/tasks' + (more && nextBefore != null ? '?before=' + nextBefore : ''));
    if (request !== historyRequest) return;
    rows = more ? rows.concat(data.tasks) : data.tasks;
    nextBefore = data.next_before;
    stateCounts = data.state_counts;
    $('notice').textContent = '';
    renderHistory();
  } catch (error) {
    if (request === historyRequest) $('notice').textContent = '任务状态未更新。' + error.message;
  }
}

function resetIdentity() {
  identityEpoch++;
  historyRequest++;
  fleetRequest++;
  fleetSelection = null;
  fleetView = null;
  fleetLoading = false;
  clearTimeout(fleetExpiry);
  $('robot-list').replaceChildren();
  credential = '';
  submission = null;
  stopRequests.clear();
  pendingWrite = null;
  writing = false;
  blankTask();
  rows = [];
  nextBefore = null;
  stateCounts = null;
  $('connection-status').textContent = '尚未连接';
  board.reset();
  renderHistory();
  $('robot-fields').replaceChildren();
  $('robot-status').textContent = '请先设置访问凭据。';
  $('lookup').value = $('notice').textContent = '';
  $('task-goal').textContent = $('task-id').textContent = $('question').textContent = '';
  $('plan').replaceChildren();
  $('results').replaceChildren();
  conversation(page === 'chat', false);
}
$('credential').oninput = resetIdentity;
$('settings-form').onsubmit = async event => {
  event.preventDefault();
  resetIdentity();
  const epoch = identityEpoch;
  const candidate = $('credential').value.trim();
  if (!candidate) {
    $('notice').textContent = '请输入访问凭据。';
    return;
  }
  $('connection-status').textContent = '正在连接…';
  try {
    const identity = await api('/session', {}, candidate);
    if (epoch !== identityEpoch) return;
    credential = candidate;
    $('connection-status').textContent = '已连接 · ' + identity.actor_id;
    renderHistory();
    if (page === 'settings') location.hash = 'chat';
    else navigate();
  } catch (error) {
    if (epoch !== identityEpoch) return;
    $('connection-status').textContent = '尚未连接';
    $('notice').textContent = '连接失败。' + error.message;
  }
};
const robotIssues = {
  OFFLINE: '离线', DISABLED: '已停用', ISOLATED: '已隔离，等待核对',
  REPORT_INVALID: '上报资料有误', CREDENTIAL_MISSING: '平台未配置机器人连接凭据'
};
function robotState(robot) {
  return robot.issues.length ? robot.issues.map(value => robotIssues[value] || value).join(' · ') : '在线';
}
function renderFleet(current, status) {
  const robots = fleetView?.robots || [];
  $('robot-list').replaceChildren(...robots.map(robot => {
    const label = robot.wire_robot_id + ' · ' + robot.binding_id + ' · ' + robotState(robot);
    const button = node('button', label + (current ? '' : '（历史）'), 'quiet');
    button.dataset.bindingId = robot.binding_id;
    button.setAttribute('aria-pressed', String(robot.binding_id === fleetView.selected));
    button.onclick = () => loadRobot(robot.binding_id);
    return button;
  }));
  $('robot-fields').replaceChildren();
  const detail = fleetView?.detail;
  if (detail) {
    const s = detail.snapshot;
    const readiness = current ? s?.readiness || {} : {};
    const ready = value => value === true ? '已就绪' : value === false ? '未就绪' : '尚未确认';
    const brain = readiness.brain_server_ready;
    const brainReady = brain === false ? false : brain === true && readiness.brain_engine != null ?
      readiness.brain_engine === 'ready' : undefined;
    const history = detail.last_report || {};
    const lastNames = history.waypoint_names?.value || [];
    const fields = [
      ['机器人编号', detail.robot.binding_id],
      [current ? '连接与登记' : '上次连接与登记（历史）', robotState(detail.robot)],
      ['导航服务', ready(readiness.nav_server_ready)],
      ['技能服务', ready(brainReady)],
      [current ? '可用航点' : '上次核实航点（历史）', Object.keys(s?.waypoints || {}).join('、') || '暂无已核实航点'],
      [current ? '可用技能' : '上次核实技能（历史）', Object.keys(s?.brain_skills || {}).join('、') || '暂无已核实技能'],
      ['历史上报航点（不代表现在可用）', lastNames.join('、') || '暂无有效历史记录'],
      [current ? '平台登记地点' : '上次平台登记地点（历史）', Object.values(detail.environment?.entries || {}).join('、') || '暂无登记'],
      [current ? '平台登记技能' : '上次平台登记技能（历史）', Object.values(detail.capability?.entries || {}).join('、') || '暂无登记']
    ];
    for (const [label, value] of fields) $('robot-fields').append(node('dt', label), node('dd', value));
  }
  $('robot-status').textContent = status;
}

function sameRobotRead(listed, detail) {
  if (!listed) return false;
  const fields = ['binding_id', 'wire_robot_id', 'enabled', 'connected', 'isolation_reason', 'generation'];
  return fields.every(key => listed[key] === detail[key]) &&
    JSON.stringify([...listed.issues].sort()) === JSON.stringify([...detail.issues].sort());
}

function loadRobotDetails(bindingId) {
  // Read only. The caller decides when a complete view can be published.
  return api('/robots/' + encodeURIComponent(bindingId));
}

async function loadRobot(bindingId) {
  if (!credential) return;
  const detailOnly = typeof bindingId === 'string';
  if (detailOnly) fleetSelection = bindingId;
  const request = ++fleetRequest;
  fleetLoading = true;
  clearTimeout(fleetExpiry);
  const historical = '显示上次资料（历史），当前就绪状态尚未确认。';
  renderFleet(false, '正在读取机器人资料。' + historical);
  try {
    let robots = detailOnly ? fleetView?.robots || [] : await api('/robots');
    if (request !== fleetRequest) return;
    const selected = robots.some(robot => robot.binding_id === fleetSelection) ? fleetSelection : robots[0]?.binding_id || null;
    const detail = selected ? await loadRobotDetails(selected) : null;
    if (request !== fleetRequest) return;
    if (detail && detail.robot.binding_id !== selected) throw new Error('返回的机器人编号不一致，请重新读取。');
    if (detail && !detailOnly && !sameRobotRead(robots.find(robot => robot.binding_id === selected), detail.robot)) {
      throw new Error('列表与详情状态不一致，请重新读取。');
    }
    if (detailOnly && detail) robots = robots.map(robot => robot.binding_id === selected ? detail.robot : robot);
    // Publish only after every required read succeeds and belongs to this request/customer.
    fleetSelection = selected;
    fleetView = {robots, detail, selected};
    const deadline = detail?.snapshot?.valid_until;
    const current = !deadline || Date.parse(deadline) > Date.now();
    const expired = '上次资料已过期。' + historical + '请刷新资料。';
    renderFleet(current, !selected ? '当前客户尚未登记机器人。' : current ?
      '本次读取：' + new Date().toLocaleTimeString() + ' · ' + robotState(detail.robot) : expired);
    if (deadline && current) {
      fleetExpiry = setTimeout(() => {
        if (request === fleetRequest) renderFleet(false, expired);
      }, Math.max(0, Date.parse(deadline) - Date.now()));
    }
  } catch (error) {
    if (request === fleetRequest) renderFleet(false, '资料未更新。' + historical + error.message);
  } finally {
    if (request === fleetRequest) fleetLoading = false;
  }
}

function navigate() {
  const target = location.hash.slice(1);
  page = Object.hasOwn(pages, target) ? target : 'chat';
  if (page !== 'chat' && page !== 'settings') returnPage = page;
  if (page === 'chat' && target !== 'chat') history.replaceState(null, '', '#chat');
  // The original home board and fleet remain explicit navigation destinations.
  for (const id of Object.keys(pages)) $(id).hidden = page !== id && !(id === 'robots' && page === 'home');
  document.querySelectorAll('nav a').forEach(a => {
    if (a.hash === '#' + page) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  $('page-title').textContent = pages[page] || '任务对话';
  $('notice').textContent = '';
  conversation(page === 'chat', false);
  updateControls();
  if (page === 'home' && credential) board.load();
  if (page === 'robots' || page === 'home') loadRobot();
  if (['home', 'tasks', 'chat'].includes(page)) loadHistory();
}

function taskState(view) {
  return ["RESTART_TASK_LOST", "ROBOT_DISABLED"].includes(view.reason_code) ? "任务已结束" : (states[view.state] || view.state);
}

function taskReason(view) {
  if (view.reason_code === "ROBOT_DISABLED") return "机器人已停用：平台已结束此任务，不会继续后续步骤。已发动作是否停止仍以机器人回执为准。";
  if (view.reason_code === "RESTART_TASK_LOST") return "重启任务丢失：平台已结束旧任务，不会继续后续步骤。机器人动作结果以回执为准。";
  if (view.result_disputed) return '执行证据存在冲突，需要人工核对。';
  const reasons = {
    PLANNER_UNAVAILABLE: '计划生成失败：模型服务暂时不可用。',
    EXECUTION_CAPACITY_BUSY: '机器人正在处理其他任务，排队中。',
    QUEUE_ORDER: '等待前面的任务，排队中。',
    ROBOT_BUSY: '机器人忙碌，排队中。',
    ROBOT_STATE_UNKNOWN: '机器人是否空闲尚未确认。',
    NO_ONLINE_ROBOT: '没有在线机器人，任务暂不能调度。',
    MULTI_ROBOT_SCHEDULING_NOT_IMPLEMENTED: '当前有多台在线机器人，多机器人调度尚未实现，任务暂不能调度。'
  };
  return reasons[view.reason_code] || view.reason_code || ({
    RECEIVED: '任务已收到，等待生成计划。',
    PLANNING: '正在生成计划，还没有向机器人下发指令。',
    QUEUED: '正在等待前面的任务结束。',
    READY: '计划已生成，等待执行前检查与下发。',
    RUNNING: '机器人正在执行，具体步骤和回执可在对话中查看。',
    NEEDS_CLARIFICATION: '需要补充信息，请在对话中回答。',
    WAITING_APPROVAL: '计划等待批准，请在对话中核对。',
    CANCEL_REQUESTED: '停止请求已发出。机器人是否已结束当前动作，要等回执确认。',
    CANCELLED: '这个任务已停止，后续步骤不再执行。',
    SUCCEEDED: '机器人已报告任务完成。',
    FAILED: '任务没有完成，请在对话中查看原因。'
  }[view.state] || '');
}

function renderConversation(view) {
  if (currentTask?.task_id === view.task_id && view.version < currentTask.version) return;
  if (endedStates.has(view.state) || view.state === 'CANCEL_REQUESTED') {
    stopRequests.delete(view.task_id);
    if ($('error').textContent.startsWith('停止尚未确认：')) $('error').textContent = '';
  }
  $('task-robots').textContent = taskRobots(view);
  if (currentTask?.task_id === view.task_id && view.version === currentTask.version) {
    updateControls();
    return;
  }
  currentTask = view;
  $('chat-empty').hidden = true;
  $('active-composer').hidden = false;
  $('retry-submit').hidden = true;
  $('task-reference').hidden = false;
  $('sent-status').textContent = '已发送';
  $('task-detail').hidden = false;
  $('submit-form').hidden = $('voice-input').hidden = true;
  $('task-goal').textContent = view.goal_text || '';
  $('status').textContent = taskState(view);
  $('task-id').textContent = view.task_id;
  $('reason').textContent = taskReason(view);
  $('question').textContent = view.clarification;
  $('answer-form').hidden = view.state !== 'NEEDS_CLARIFICATION';
  $('decisions').hidden = view.state !== 'WAITING_APPROVAL' && !view.reason_code.startsWith('AUTHORIZATION_');
  $('execution-detail').hidden = !(view.plan_steps?.length || view.result_summary.length);
  updateControls();
  $('plan').replaceChildren(...(view.plan_steps || []).map(step => node('li', step.description)));
  const expanded = new Set(Array.from($('results').querySelectorAll('details[open]'), item => item.dataset.commandId));
  $('results').replaceChildren();
  if (!view.result_summary.length) $('results').textContent = '尚无机器人反馈。';
  for (const cmd of view.result_summary) {
    const item = node('div', '', 'result');
    const details = document.createElement('details');
    details.dataset.commandId = cmd.command_id;
    details.open = expanded.has(cmd.command_id);
    details.append(node('summary', '查看执行依据'), node('small', '指令 ' + cmd.command_id + ' · ' + (cmd.evidence_ids
      .join('、') || '暂无反馈依据')));
    const dispatch = {PREPARED: '已准备，尚未下发', DISPATCHING: '正在下发，是否送达待核实', SENT: '已发送，执行结果看回执'};
    item.append(node('strong', outcomes[cmd.outcome] || cmd.outcome), node('p', cmd.description),
      node('p', '指令目标：' + cmd.robot_binding_id + ' · ' + (dispatch[cmd.dispatch_state] || cmd.dispatch_state)), details);
    $('results').append(item);
  }
}
async function openTask(id, focusAnswer = false) {
  blankTask();
  const ticket = selection;
  conversation(true);
  try {
    const view = await api('/tasks/' + encodeURIComponent(id));
    if (ticket === selection) {
      renderConversation(view);
      if (focusAnswer && view.state === 'NEEDS_CLARIFICATION') $('answer').focus();
    }
  } catch (error) {
    if (ticket === selection) $('error').textContent = error.message;
  }
}
async function write(path, payload, audioFile, fromBoard = false, retryRequest = null) {
  if (writing) return;
  if (!credential) {
    $('error').textContent = '请先在设置中应用访问凭据。';
    return;
  }
  const stamp = JSON.stringify([path, payload]);
  if (!pendingWrite || pendingWrite.stamp !== stamp || pendingWrite.file !== audioFile) {
    pendingWrite = {
      stamp,
      file: audioFile,
      key: crypto.randomUUID()
    };
  }
  const request = retryRequest || pendingWrite,
    epoch = identityEpoch,
    ticket = ++selection;
  writing = true;
  const isSubmission = path === '/tasks' || path === '/tasks/audio';
  const attempt = isSubmission ? {
    goal: audioFile ? '语音任务：' + audioFile.name : payload.text,
    path, payload, audioFile, request, ticket, stopRequested: retryRequest ? Boolean(submission?.stopRequested) : false
  } : null;
  if (attempt) { submission = attempt; renderPending(); renderHistory(); }
  updateControls();
  $('retry-submit').hidden = true;
  $('error').textContent = '';
  try {
    const headers = {
      'Idempotency-Key': request.key
    };
    let body;
    if (audioFile) {
      body = new FormData();
      body.append('audio', audioFile);
    } else {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(payload);
    }
    const view = await api(path, {
      method: 'POST',
      headers,
      body
    });
    if (epoch !== identityEpoch) return;
    pendingWrite = null;
    const showReply = attempt ? submission === attempt && attempt.ticket === selection : ticket === selection;
    if (submission === attempt) submission = null;
    const submittedHere = path === '/tasks' || path === '/tasks/audio';
    board.observe(view, submittedHere || rows.some(row => row.task_id === view.task_id));
    rememberTask(view, submittedHere);
    if (showReply && !fromBoard) renderConversation(view);
    if (attempt?.stopRequested) await stopTask(view);
    if (fromBoard) board.announce('已提交「' + view.goal_text + '」的批准决定，当前状态：' + (states[view.state] || view.state) + '。');
    if (page === 'home') board.load();
    loadHistory();
  } catch (error) {
    if (epoch === identityEpoch && (ticket === selection || (attempt && attempt.ticket === selection))) {
      if (attempt) {
        attempt.rejected = Boolean(error.status >= 400 && error.status < 500);
        $('sent-status').textContent = attempt.rejected ? '未发送成功' : '提交结果待确认';
        $('status').textContent = attempt.rejected ? '提交失败' : '提交尚未确认';
        $('reason').textContent = attempt.rejected ? '平台未受理这次提交。请核对输入或访问权限。' : '暂时无法确认平台是否已收到任务，重试会沿用同一个请求编号。';
        $('retry-submit').hidden = false;
      }
      const message = error.message + ' · 请求编号 ' + request.key + '。请先查看任务历史。';
      if (fromBoard) {
        const stale = ['task version changed', 'plan version changed'].includes(error.message);
        board.announce(stale ? '任务或计划已变更，请核对最新计划后再批准。' : message);
        board.load();
      } else $('error').textContent = message;
    }
  } finally {
    if (epoch === identityEpoch) { writing = false; updateControls(); }
  }
}

// Cancellation has its own per-task request. A slow submit/answer/approval never
// swallows it, and responses only update the matching task and current identity.
async function stopTask(task) {
  const id = task.task_id;
  if (!id || endedStates.has(task.state) || task.state === 'CANCEL_REQUESTED') return;
  let request = stopRequests.get(id);
  if (request?.busy) return;
  request = request || {key: crypto.randomUUID(), version: task.version ?? null};
  request.busy = true;
  delete request.error;
  stopRequests.set(id, request);
  const epoch = identityEpoch;
  updateControls(); renderHistory();
  const observe = view => {
    if (epoch !== identityEpoch) return;
    board.observe(view, rows.some(row => row.task_id === id));
    rememberTask(view, false);
    if (currentTask?.task_id === id) renderConversation(view);
  };
  try {
    for (let tries = 0; tries < 3; tries++) {
      if (request.version === null) {
        const fresh = await api('/tasks/' + encodeURIComponent(id));
        if (epoch !== identityEpoch) return;
        observe(fresh);
        if (endedStates.has(fresh.state) || fresh.state === 'CANCEL_REQUESTED') return;
        request.version = fresh.version;
      }
      try {
        const view = await api('/tasks/' + encodeURIComponent(id) + '/cancel', {
          method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': request.key},
          body: JSON.stringify({expected_version: request.version})
        });
        if (epoch !== identityEpoch) return;
        observe(view);
        stopRequests.delete(id);
        if (currentTask?.task_id === id) $('error').textContent = '';
        $('notice').textContent = '';
        break;
      } catch (error) {
        if (error.status !== 409 || error.message !== 'task version changed' || tries === 2) throw error;
        // The rejected request made no change. Re-read and cancel this same task.
        request.version = null;
        request.key = crypto.randomUUID();
      }
    }
    if (page === 'home') board.load();
    loadHistory();
  } catch (error) {
    if (epoch === identityEpoch) {
      // A newer status query can confirm cancellation before a lost POST reply
      // finally fails. Do not overwrite that authoritative state with an old error.
      const known = currentTask?.task_id === id ? currentTask : rows.find(row => row.task_id === id);
      if (known && (endedStates.has(known.state) || known.state === 'CANCEL_REQUESTED')) {
        stopRequests.delete(id);
        return;
      }
      const message = '停止尚未确认：' + error.message + '。可以重试停止或刷新任务状态。';
      request.error = message;
      $('notice').textContent = message;
      if (currentTask?.task_id === id) $('error').textContent = message;
    }
  } finally {
    if (epoch === identityEpoch) { request.busy = false; updateControls(); renderHistory(); }
  }
}

function newTask() {
  blankTask();
  pendingWrite = null;
  conversation(true);
}
$('new-task').onclick = $('sidebar-new').onclick = $('followup-new').onclick = newTask;
$('goal').oninput = updateControls;
$('retry-submit').onclick = () => {
  if (submission && !writing) write(submission.path, submission.payload, submission.audioFile, false, submission.request);
};
$('compose').onclick = $('dock').onclick = () => conversation(true);
$('collapse').onclick = () => conversation(false);
$('refresh-tasks').onclick = () => loadHistory();
$('more').onclick = () => loadHistory(true);
$('refresh-robot').onclick = loadRobot;
$('lookup-form').onsubmit = event => {
  event.preventDefault();
  openTask($('lookup').value.trim());
};
$('submit-form').onsubmit = event => {
  event.preventDefault();
  if ($('goal').value.trim()) write('/tasks', {
    text: $('goal').value.trim()
  });
};
$('goal').onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $('submit-form').requestSubmit();
  }
};
$('answer-form').onsubmit = event => {
  event.preventDefault();
  write('/tasks/' + currentTask.task_id + '/clarifications/' + currentTask.clarification_id + '/answer', {
    answer: $('answer').value,
    expected_version: currentTask.version
  });
};
for (const decision of ['approve', 'reject']) {
  $(decision).onclick = () => write('/tasks/' + currentTask.task_id + '/decisions', {
    decision,
    plan_id: currentTask.plan_id,
    expected_version: currentTask.version
  });
}
$('cancel').onclick = () => currentTask ? stopTask(currentTask) : stopSubmission();
$('audio-submit').onclick = () => {
  const file = $('audio').files[0];
  if (file) write('/tasks/audio', null, file);
  else $('error').textContent = '请先选择 WAV 文件。';
};
$('record').onclick = async () => {
  if (recording) {
    recording.stop();
    return;
  }
  const ticket = ++micRequest;
  const epoch = identityEpoch;
  try {
    $('record').disabled = true;
    const capture = await captureMicrophone();
    if (ticket !== micRequest) {
      capture.stop();
      await capture.stopped;
      return;
    }
    recording = capture;
    $('record-status').textContent = '正在录音';
    $('record').disabled = false;
    $('record').textContent = '结束并提交录音';
    const blob = await capture.stopped;
    if (ticket !== micRequest) return;
    $('record').disabled = true;
    $('record-status').textContent = '正在转换为 WAV';
    const wav = await recordingToWav(blob);
    if (ticket !== micRequest || epoch !== identityEpoch) return;
    // Keep the exact file for a manual retry if the server's reply gets lost.
    const files = new DataTransfer();
    files.items.add(wav);
    $('audio').files = files.files;
    await write('/tasks/audio', null, $('audio').files[0]);
  } catch (error) {
    if (ticket === micRequest) $('error').textContent = error.message;
  } finally {
    if (ticket === micRequest) {
      recording = null;
      $('record').textContent = '开始录音';
      $('record-status').textContent = '';
    }
    if (ticket === micRequest) $('record').disabled = false;
  }
};
document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    conversation($('conversation').hidden);
  } else if (event.key === 'Escape') conversation(false);
});
window.addEventListener('hashchange', navigate);
window.addEventListener('pagehide', stopRecording);
setInterval(async () => {
  if (!credential || !currentTask || polling || $('conversation').hidden) return;
  const ticket = selection,
    id = currentTask.task_id;
  polling = true;
  try {
    const view = await api('/tasks/' + encodeURIComponent(id));
    if (ticket !== selection) return;
    const changed = view.version !== currentTask.version;
    renderConversation(view);
    if (changed) {
      board.observe(view, rows.some(row => row.task_id === view.task_id));
      rememberTask(view, false);
      if (page === 'home') board.load();
      loadHistory();
    }
  } catch (error) {
    if (ticket === selection) $('error').textContent = '任务状态未更新。' + error.message;
  } finally {
    polling = false;
  }
}, 700);
setInterval(() => {
  if (credential && !document.hidden) {
    if (page === 'home') board.load(true);
    if (page === 'tasks') loadHistory();
    if ((page === 'home' || page === 'robots') && !fleetLoading) loadRobot();
  }
}, 4000);
// Opening/reloading the app always starts a new conversation, including old
// bookmarked #tasks URLs. Merely changing views never submits/cancels a task.
history.replaceState(null, '', '#chat');
blankTask();
board.reset();
renderHistory();
navigate();
