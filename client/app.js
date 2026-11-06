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
  BLOCKED: '后续执行已结束',
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
  RESULT_UNKNOWN: '执行结果未知'
};
let credential = '', actorId = '',
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
const errorDetailsOpen = new Set();
const announcedErrors = new Set(readSessionList('deepleap.error-notices'));
const attemptedStops = new Set(readSessionList('deepleap.stop-attempts'));
const actionAttempts = new Map(readSessionList('deepleap.action-attempts').filter(item => Array.isArray(item) && item.length === 2));
let errorNoticeTimer = null;

function readSessionList(key) {
  try { const value = JSON.parse(sessionStorage.getItem(key) || '[]'); return Array.isArray(value) ? value : []; }
  catch { return []; }
}
function saveSessionList(key, values) {
  try { sessionStorage.setItem(key, JSON.stringify([...values].slice(-1000))); } catch { /* Private mode may disable storage. */ }
}

function actionKey(task, action) {
  const itemId = action === 'answer' ? task?.clarification_id : task?.plan_id;
  return actorId && task?.task_id && itemId ? JSON.stringify([actorId, task.task_id, itemId, action]) : null;
}
function actionStatus(task, action) {
  const attempt = actionAttempts.get(actionKey(task, action));
  if (!attempt) return '';
  const label = {answer:'澄清回答', approve:'批准计划', reject:'拒绝计划'}[action];
  const summary = label + (attempt.status === 'confirmed' ? '请求已受理，不会重复发送。' : attempt.status === 'rejected' ?
    '请求未受理；不会再次发送，请刷新任务状态。' : '结果未确认；只查询任务状态，不会再次发送。');
  return attempt.reason ? summary + ' 原因：' + attempt.reason : summary;
}
function saveActionAttempt(key, status, reason = '') {
  actionAttempts.set(key, reason ? {status, reason} : {status});
  // Retain operation identity, delivery knowledge, and a public Chinese reason only.
  // Never save answer text, credentials, or raw exception details.
  try { sessionStorage.setItem('deepleap.action-attempts', JSON.stringify([...actionAttempts])); } catch { /* Keep the in-memory guard. */ }
}
function requestAction(path, payload) {
  const decision = path.match(/^\/tasks\/([^/]+)\/decisions$/);
  const answer = path.match(/^\/tasks\/([^/]+)\/clarifications\/([^/]+)\/answer$/);
  if (!decision && !answer) return null;
  const action = answer ? 'answer' : payload.decision;
  const task = {task_id: decodeURIComponent((answer || decision)[1]), plan_id: payload.plan_id,
    clarification_id: answer ? decodeURIComponent(answer[2]) : null};
  return {task, action, key: actionKey(task, action)};
}

function requestErrorReason(error) {
  if (typeof error.publicMessage === 'string' && /[\u3400-\u9fff]/.test(error.publicMessage)) return error.publicMessage;
  const reasons = {
    APPROVAL_POLICY_UNAVAILABLE:'批准权限检查暂不可用，无法受理这次批准。',
    APPROVAL_DENIED:'当前用户无权批准这个任务。',
    APPROVAL_VALIDITY_UNAVAILABLE:'无法确认批准的有效期限。',
    CANCEL_POLICY_UNAVAILABLE:'停止权限检查暂不可用，无法受理这次停止。',
    AUTHORIZATION_UNAVAILABLE:'暂时无法确认任务执行权限。',
    AUTHORIZATION_DENY:'当前权限不允许执行这个任务。',
    PERSISTENCE_UNCONFIRMED:'任务记录未能确认保存，操作结果尚未确认。',
    REQUEST_KEY_REQUIRED:'请求缺少操作编号。',
    ANSWER_REQUIRED:'澄清回答不能为空。',
    NOT_FOUND:'未找到可访问的任务。',
    UNAUTHENTICATED:'访问凭据无效，请重新连接服务。',
    'task version changed':'任务状态已变化，本次操作未受理。',
    'plan version changed':'计划已变化，本次批准未受理。',
    'clarification version changed':'需要回答的问题已变化，本次回答未受理。',
    'task does not await approval':'这个任务当前不在等待批准。',
    'task already ended':'这个任务已经结束。',
    'request key content conflict':'请求编号与原请求内容不一致。',
    'invalid approval decision':'批准决定不符合要求。',
    'robot binding unavailable':'当前无法使用这个机器人。'
  };
  const reason = reasons[error.code || error.message];
  if (reason) return reason;
  if (error.status === 403) return '当前身份无权完成这项操作。';
  if (error.status === 409) return '本次操作与当前任务记录不一致。';
  if (error.status === 422) return '提交内容未通过检查。';
  if (error.status >= 500) return '服务处理请求时发生错误，操作结果尚未确认。';
  return '未收到可确认的服务响应，操作结果尚未确认。';
}

function normalComposerMessage(task) {
  if (task?.state === 'RUNNING') {
    const command = (task.result_summary || []).findLast(item => !['SUCCEEDED', 'FAILED'].includes(item.outcome));
    if (command?.dispatch_state === 'PREPARED') return '正在执行前检查，尚未下发当前动作';
    if (command?.dispatch_state === 'DISPATCHING') return '正在下发动作，是否送达尚待确认';
    if (command?.dispatch_state === 'SENT' && command.outcome !== 'RUNNING') return '动作已下发，等待机器人执行回执';
    return '机器人正在执行，等待动作回执';
  }
  return {
    RECEIVED:'任务已收到，等待生成计划',
    PLANNING:'正在生成计划，尚未下发动作',
    NEEDS_CLARIFICATION:'等待补充信息，请回答上方问题',
    WAITING_APPROVAL:'计划等待批准，批准前不会执行',
    QUEUED:'任务正在排队，等待执行机会',
    READY:'计划已就绪，等待执行前检查'
  }[task?.state] || '任务状态待确认，请刷新任务状态';
}

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
  taskError,
  errorCard,
  taskState,
  actionStatus,
  cancellationMessage,
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
    const error = new Error();
    error.status = response.status;
    error.code = data.error?.code || (typeof data.detail === 'string' ? data.detail : '');
    error.publicMessage = data.error?.message;
    error.message = requestErrorReason(error);
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
  if (open)(canContinueTask(currentTask) ? $('goal') : currentTask ? $('new-task') :
    submission?.ticket === selection ? $('cancel') : $('goal')).focus();
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
  $('conversation-history').replaceChildren();
  $('task-detail').hidden = true;
  $('chat-empty').hidden = false;
  $('active-composer').hidden = true;
  $('task-error-card').hidden = $('cancel-result').hidden = true;
  $('submit-form').hidden = $('voice-input').hidden = false;
  $('goal').value = $('answer').value = $('audio').value = '';
  $('error').textContent = '';
  updateControls();
}

function canStop(task) {
  if (!task || endedStates.has(task.state) || task.state === 'CANCEL_REQUESTED' || task.cancel_result ||
      stopRequests.has(task.task_id) || attemptedStops.has(task.task_id)) return false;
  const error = taskError(task);
  return !error || error.execution_status === 'UNKNOWN';
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
  const description = row.cancel_result ? cancellationMessage(row) : stopError || (taskError(row) ? '' : taskReason(row));
  if (description) open.append(node('small', description, 'task-description'));
  el.append(open);
  if (taskError(row)) { el.classList.add('task-row-error'); el.append(errorCard(row)); }
  if (canStop(row)) {
    const busy = row.pending ? submission?.stopRequested : stopRequests.get(row.task_id)?.busy;
    const stop = node('button', taskError(row) ? '请求停止机器人' : busy ? '正在停止…' : '■ 停止', 'quiet task-stop');
    stop.setAttribute('aria-label', '停止任务：' + row.goal_text);
    stop.disabled = Boolean(busy);
    stop.onclick = () => row.pending ? stopSubmission() : stopTask(row);
    el.append(stop);
  }
  return el;
}

function canContinueTask(task) {
  if (!task || task.state === 'CANCEL_REQUESTED') return false;
  return endedStates.has(task.state) || task.advancement_stopped === true;
}

function updateControls() {
  const visibleSubmission = submission?.ticket === selection;
  const mayCompose = currentTask ? canContinueTask(currentTask) : !visibleSubmission;
  $('submit-form').hidden = !mayCompose;
  // Keep follow-up changes limited to text; existing audio entry remains for a blank chat.
  $('voice-input').hidden = Boolean(currentTask || visibleSubmission);
  $('send').disabled = !credential || writing || !mayCompose || !$('goal').value.trim();
  $('goal').placeholder = currentTask ? '继续输入下一条任务…' : '描述你想让机器人做的事…';
  $('continuation-note').hidden = !currentTask || !mayCompose;
  $('audio-submit').disabled = !credential || writing;
  if (!recording) $('record').disabled = !credential || writing;
  $('chat-access-note').hidden = Boolean(credential);
  $('answer-form').querySelector('button').disabled = writing || Boolean(actionStatus(currentTask, 'answer'));
  $('answer').disabled = Boolean(actionStatus(currentTask, 'answer'));
  $('approve').disabled = writing || Boolean(actionStatus(currentTask, 'approve'));
  $('reject').disabled = writing || Boolean(actionStatus(currentTask, 'reject'));
  const pending = submission && submission.ticket === selection && !currentTask;
  const task = currentTask;
  const ending = task && endedStates.has(task.state);
  const failed = task && Boolean(taskError(task));
  let actionNotice = $('action-result');
  if (!actionNotice) {
    actionNotice = node('p', '', 'hint');
    actionNotice.id = 'action-result';
    $('decisions').after(actionNotice);
  }
  const actions = !failed && task?.state === 'NEEDS_CLARIFICATION' ? ['answer'] :
    !failed && task?.state === 'WAITING_APPROVAL' ? ['approve', 'reject'] : [];
  actionNotice.textContent = actions.map(action => actionStatus(task, action)).filter(Boolean).join(' ');
  actionNotice.hidden = !actionNotice.textContent;
  const unconfirmed = pending && submission.unconfirmed;
  const stopError = stopRequests.get(task?.task_id)?.error;
  const stopRejected = stopRequests.get(task?.task_id)?.rejected;
  const waiting = task?.state === 'CANCEL_REQUESTED';
  const busy = pending ? submission.stopRequested : stopRequests.get(task?.task_id)?.busy;
  const stopAttempted = task && (task.cancel_result || attemptedStops.has(task.task_id));
  $('cancel').hidden = task ? !canStop(task) : !pending || Boolean(unconfirmed);
  $('refresh-task').hidden = !task && !unconfirmed;
  $('refresh-task').disabled = Boolean(submission?.checking);
  $('refresh-task').textContent = unconfirmed ? '查询提交结果' : '刷新任务状态';
  $('followup-new').hidden = !unconfirmed;
  $('active-composer').hidden = !pending && (!task || (mayCompose && !failed && !canStop(task)));
  $('cancel').disabled = Boolean(waiting || busy);
  $('cancel').textContent = failed ? '请求停止机器人' : busy ? '正在停止…' : '■ 停止';
  $('composer-message').textContent = unconfirmed ? '提交结果未确认' : failed ? '后续执行已结束，此任务不会继续' : ending ? '这个任务已结束' : waiting ? cancellationMessage(task) : stopError ? (stopRejected ? '停止请求未受理' : '停止请求结果未确认') : stopAttempted ? '停止请求已尝试，结果以任务记录为准' : pending ? '正在提交任务' : normalComposerMessage(task);
  $('stop-hint').textContent = unconfirmed ? '只查询原请求，不会重新提交' : failed ? errorFooter(taskError(task)) : stopError ? '不会再次发送停止请求，只可刷新任务状态' : ending ? '原始任务和已有记录会保留' : pending && submission.stopRequested ? '等待接收确认后请求停止任务' : waiting || stopAttempted ? '不会再次发送停止请求；收到可靠回执才确认动作结束' : '停止后不再继续这个任务';
}

function renderPending() {
  $('task-robots').textContent = '';
  if (!submission || submission.ticket !== selection) return;
  currentTask = null;
  $('chat-empty').hidden = true;
  $('task-detail').hidden = $('active-composer').hidden = false;
  $('submit-form').hidden = $('voice-input').hidden = true;
  $('task-goal').textContent = submission.goal;
  $('sent-status').textContent = submission.unconfirmed ? '提交结果未确认' : '正在发送';
  $('status').classList.remove('error-badge');
  $('status').textContent = submission.unconfirmed ? '提交结果未确认' : submission.stopRequested ? '等待提交确认后停止' : '正在处理';
  $('reason').textContent = submission.unconfirmed ? '尚未查到这次提交的任务记录，不能确认是否已受理。可用原请求编号只读查询。' : submission.stopRequested ? '等待平台确认收到任务，随后请求停止。' : '正在提交任务…';
  $('reason').hidden = false;
  $('task-error-card').hidden = $('cancel-result').hidden = true;
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
    Object.assign(previous, view);
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
    if (previous) Object.assign(previous, view);
    else rows.unshift({...view});
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
  actorId = '';
  submission = null;
  stopRequests.clear();
  clearTimeout(errorNoticeTimer);
  $('task-alert').textContent = '';
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
    actorId = identity.actor_id;
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

const legacyReasons = {
  PLANNER_UNAVAILABLE: '生成计划时模型服务不可用。',
  PLANNER_INVALID_RESPONSE: '模型返回的计划格式有误。',
  PLAN_INVALID: '生成的计划未通过检查。',
  ACTION_FAILED: '机器人报告动作执行失败。',
  RESULT_UNKNOWN: '现有记录无法确认动作执行结果。',
  ROBOT_DISCONNECTED: '机器人连接已断开，动作执行结果需要核实。',
  ROBOT_STATE_UNKNOWN: '机器人是否空闲尚未确认。',
  NO_ONLINE_ROBOT: '没有在线机器人，无法安排此任务。',
  MULTI_ROBOT_SCHEDULING_NOT_IMPLEMENTED: '有多台机器人可供选择，当前尚不支持自动选择。',
  ROBOT_DISABLED: '机器人已停用，平台已结束此任务。已发动作是否停止以机器人回执为准。',
  RESTART_TASK_LOST: '服务重启后，平台已结束旧任务。已发动作结果以机器人回执为准。'
};
function taskError(view) {
  if (view.error) return view.error;
  if (view.state !== 'FAILED' && view.state !== 'BLOCKED' &&
      !['ROBOT_DISABLED', 'RESTART_TASK_LOST'].includes(view.reason_code)) return null;
  const lastResult = view.result_summary?.at(-1)?.outcome;
  // Older records have no structured error. Preserve missing facts explicitly.
  return {error_id: null, code: view.reason_code || null, source: null, phase: null,
    message: legacyReasons[view.reason_code] || '历史记录未提供具体错误原因。', occurred_at: null,
    execution_status: view.result_disputed ? 'CONFLICT' : {RESULT_UNKNOWN:'UNKNOWN', FAILED:'FAILED', SUCCEEDED:'SUCCEEDED'}[lastResult] || null};
}
function errorExecution(error) {
  return {
    NOT_SENT: '尚未向机器人发送动作指令', FAILED: '机器人已报告动作执行失败',
    UNKNOWN: '动作是否执行或结束尚未确认', CONFLICT: '执行回执存在冲突，无法确认结果',
    PARTIAL: '已有部分步骤完成，后续执行已结束', SUCCEEDED: '机器人已报告已发动作成功，后续步骤不再执行'
  }[error.execution_status] || '记录未提供执行情况，不能确认机器人是否执行或停止';
}
function errorFooter(error) {
  return {
    NOT_SENT: '本任务没有下发动作；原任务不会自动重试',
    FAILED: '机器人已报告失败；原任务不会自动重试',
    UNKNOWN: '动作执行结果尚未确认；后续步骤不再执行',
    CONFLICT: '执行回执相互矛盾，无法确认结果；后续步骤不再执行',
    PARTIAL: '已完成步骤记录保留；剩余步骤不再执行',
    SUCCEEDED: '已发动作已报告完成；原任务不再继续'
  }[error.execution_status] || '记录未提供执行情况；原任务不会自动重试';
}
function errorCard(view) {
  const error = taskError(view);
  const card = node('section', '', 'task-error-card');
  card.setAttribute('aria-label', '任务错误');
  const sources = {scheduling:'任务调度', planning:'计划生成', resources:'机器人资料检查',
    authorization:'权限检查', transport:'机器人通信', execution:'机器人执行', runtime:'平台运行',
    input:'任务输入', storage:'任务记录'};
  const phases = {scheduling:'安排机器人', planning:'生成计划', admission:'计划受理检查', approval:'批准检查', dispatch:'下发前检查',
    write:'发送指令', feedback:'接收执行回执', timeout:'等待结果', cancel:'请求停止', recovery:'恢复检查', runtime:'平台运行', persistence:'保存任务记录'};
  card.append(node('h3', '任务发生错误'), node('p', '当时的错误：' + (error.message || '记录未提供具体错误原因。'), 'error-message'));
  const facts = node('dl', '', 'error-facts');
  facts.append(node('dt', '出错来源'), node('dd', sources[String(error.source).toLowerCase()] || '记录未提供可识别来源'),
    node('dt', '当前执行情况'), node('dd', errorExecution(error)));
  if (error.phase) facts.append(node('dt', '发生环节'), node('dd', phases[String(error.phase).toLowerCase()] || '记录未提供可识别环节'));
  const details = document.createElement('details');
  const identity = view.task_id + ':' + (error.error_id || error.code || 'legacy');
  details.className = 'error-details';
  details.open = errorDetailsOpen.has(identity);
  details.ontoggle = () => details.open ? errorDetailsOpen.add(identity) : errorDetailsOpen.delete(identity);
  details.append(node('summary', '查看错误详情'));
  const records = node('dl', '', 'error-records');
  const fields = [['错误编号', error.error_id], ['错误代码', error.code], ['发生时间', error.occurred_at ? new Date(error.occurred_at).toLocaleString() : null],
    ['任务编号', view.task_id], ['发生步骤', error.step_id], ['动作指令', error.command_id]];
  for (const [label, value] of fields) records.append(node('dt', label), node('dd', value || '未提供'));
  details.append(records);
  card.append(facts, details, node('p', '后续执行已结束 · 错误记录已保留', 'error-footnote'));
  if (!announcedErrors.has(identity)) {
    announcedErrors.add(identity);
    saveSessionList('deepleap.error-notices', announcedErrors);
    $('task-alert').textContent = '任务发生错误：' + (error.message || '请查看任务记录。') + ' 后续执行已结束。';
    clearTimeout(errorNoticeTimer);
    errorNoticeTimer = setTimeout(() => { $('task-alert').textContent = ''; }, 6000);
  }
  return card;
}
function taskState(view) {
  const error = taskError(view);
  if (error) return {NOT_SENT:'未执行', FAILED:'执行失败', UNKNOWN:'执行结果未知', CONFLICT:'执行证据冲突', PARTIAL:'部分完成', SUCCEEDED:'后续执行已结束'}[error.execution_status] || '任务发生错误';
  if (view.state === 'CANCEL_REQUESTED') return view.cancel_result?.status === 'SENT' ? '等待机器人确认停止' : view.cancel_result?.status === 'NOT_SENT' ? '停止请求未发送' : '停止请求结果未确认';
  return ["RESTART_TASK_LOST", "ROBOT_DISABLED"].includes(view.reason_code) ? "任务已结束" : (states[view.state] || view.state);
}

function cancellationMessage(view) {
  const original = view.cancel_result?.message || '停止请求结果尚未确认，机器人是否结束当前动作要以回执为准。';
  if (!view.cancel_result) return original;
  if (!Array.isArray(view.result_summary) || typeof view.result_disputed !== 'boolean') {
    return '停止请求记录已保存，当前执行情况请查看任务详情';
  }
  if (view.result_disputed || ['CONFLICT', 'UNKNOWN'].includes(view.error?.execution_status) ||
      view.result_summary.some(command => command.evidence_conflict)) return original;
  const sent = view.result_summary.filter(command => !['PREPARED', 'NOT_SENT'].includes(command.dispatch_state));
  if (!sent.length) return '任务已取消，未下发动作';
  if (sent.every(command => ['SUCCEEDED', 'FAILED'].includes(command.outcome))) {
    return '后续步骤已结束，机器人已返回当前动作的最终结果';
  }
  return original;
}

function taskReason(view) {
  if (taskError(view)) return taskError(view).message;
  if (view.cancel_result || view.state === 'CANCEL_REQUESTED') return cancellationMessage(view);
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
  return reasons[view.reason_code] || ({
    RECEIVED: '任务已收到，等待生成计划。',
    PLANNING: '正在生成计划，还没有向机器人下发指令。',
    QUEUED: '正在等待前面的任务结束。',
    READY: '计划已生成，等待执行前检查与下发。',
    RUNNING: '机器人正在执行，具体步骤和回执可在对话中查看。',
    NEEDS_CLARIFICATION: '需要补充信息，请在对话中回答。',
    WAITING_APPROVAL: '计划等待批准，请在对话中核对。',
    CANCELLED: '这个任务已结束，后续步骤不再执行。动作结果以机器人回执为准。',
    SUCCEEDED: '机器人已报告任务完成。',
    FAILED: '任务没有完成，请在对话中查看原因。'
  }[view.state] || '');
}

function renderConversation(view) {
  if (currentTask?.task_id === view.task_id && view.version < currentTask.version) return;
  if (endedStates.has(view.state) || view.state === 'CANCEL_REQUESTED') {
    const stopRequest = stopRequests.get(view.task_id);
    if (stopRequest) { stopRequest.resolved = true; delete stopRequest.error; }
    if (/^停止(?:尚未确认|请求未受理|请求结果未确认)：/.test($('error').textContent)) $('error').textContent = '';
  }
  $('task-robots').textContent = taskRobots(view);
  if (currentTask?.task_id === view.task_id && JSON.stringify(view) === JSON.stringify(currentTask)) {
    updateControls();
    return;
  }
  currentTask = view;
  $('chat-empty').hidden = true;
  $('active-composer').hidden = false;
  $('task-reference').hidden = false;
  $('sent-status').textContent = '已提交';
  $('task-detail').hidden = false;
  $('submit-form').hidden = $('voice-input').hidden = true;
  $('task-goal').textContent = view.goal_text || '';
  $('status').textContent = taskState(view);
  $('status').classList.toggle('error-badge', Boolean(taskError(view)));
  $('task-id').textContent = view.task_id;
  $('reason').textContent = taskReason(view);
  const error = taskError(view);
  $('reason').hidden = Boolean(error);
  $('task-error-card').hidden = !error;
  $('task-error-card').replaceChildren(...(error ? [errorCard(view)] : []));
  const cancelMessage = view.cancel_result ? cancellationMessage(view) : stopRequests.get(view.task_id)?.error;
  $('cancel-result').hidden = !cancelMessage;
  $('cancel-result').textContent = cancelMessage ? '停止请求：' + cancelMessage : '';
  $('question').textContent = error ? '' : view.clarification || '';
  $('answer-form').hidden = Boolean(error) || view.state !== 'NEEDS_CLARIFICATION';
  $('decisions').hidden = Boolean(error) || view.state !== 'WAITING_APPROVAL';
  const results = view.result_summary || [];
  $('execution-detail').hidden = !(view.plan_steps?.length || results.length);
  updateControls();
  $('plan').replaceChildren(...(view.plan_steps || []).map(step => node('li', step.description)));
  const expanded = new Set(Array.from($('results').querySelectorAll('details[open]'), item => item.dataset.commandId));
  $('results').replaceChildren();
  if (!results.length) $('results').textContent = '尚无机器人反馈。';
  for (const cmd of results) {
    const item = node('div', '', 'result');
    const details = document.createElement('details');
    details.dataset.commandId = cmd.command_id;
    details.open = expanded.has(cmd.command_id);
    details.append(node('summary', '查看执行依据'), node('small', '指令 ' + cmd.command_id + ' · ' + ((cmd.evidence_ids || [])
      .join('、') || '暂无反馈依据')));
    const dispatch = {PREPARED: '已准备，尚未下发', DISPATCHING: '正在下发，是否送达待核实', SENT: '已发送，执行结果看回执'};
    const execution = {SUCCEEDED:'已完成，机器人已报告成功', FAILED:'已结束，机器人已报告失败', RESULT_UNKNOWN:'执行结果未知'};
    const resultLabel = cmd.evidence_conflict ? '执行回执冲突' : outcomes[cmd.outcome] || cmd.outcome;
    const resultDetail = cmd.evidence_conflict ? '收到相互矛盾的回执，无法确认执行结果' :
      execution[cmd.outcome] || dispatch[cmd.dispatch_state] || cmd.dispatch_state;
    item.append(node('strong', resultLabel), node('p', cmd.description),
      node('p', '指令目标：' + cmd.robot_binding_id + ' · ' + resultDetail), details);
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
async function acceptSubmission(view, attempt) {
  const showReply = submission === attempt && attempt.ticket === selection;
  if (submission === attempt) submission = null;
  pendingWrite = null;
  board.observe(view, true);
  rememberTask(view, true);
  if (showReply) { $('error').textContent = ''; renderConversation(view); }
  if (attempt.stopRequested) await stopTask(view);
  if (page === 'home') board.load();
  loadHistory();
}

async function lookupSubmission(attempt = submission) {
  if (!attempt || attempt.checking || !credential) return;
  const epoch = identityEpoch;
  attempt.checking = true;
  updateControls();
  try {
    const result = await api('/task-requests/' + encodeURIComponent(attempt.request.key));
    if (epoch !== identityEpoch || submission !== attempt) return;
    if (result.found && result.task) {
      await acceptSubmission(result.task, attempt);
    } else if (attempt.ticket === selection) {
      renderPending();
      $('error').textContent = '提交结果未确认 · 请求编号 ' + attempt.request.key + '。尚未查到任务记录，不会重新提交。';
    }
  } catch (error) {
    if (epoch === identityEpoch && submission === attempt && attempt.ticket === selection) {
      $('error').textContent = '提交结果未确认，状态查询未完成。' + error.message + ' · 请求编号 ' + attempt.request.key;
    }
  } finally {
    attempt.checking = false;
    if (epoch === identityEpoch) updateControls();
  }
}

async function refreshCurrentTask() {
  if (!currentTask) { await lookupSubmission(); return; }
  const ticket = selection;
  const id = currentTask.task_id;
  try {
    const view = await api('/tasks/' + encodeURIComponent(id));
    if (ticket !== selection) return;
    $('error').textContent = '';
    renderConversation(view);
    board.observe(view, rows.some(row => row.task_id === id));
    rememberTask(view, false);
  } catch (error) {
    if (ticket === selection) $('error').textContent = '任务状态未更新。' + error.message;
  }
}

async function queryActionTask(operation, epoch, fromBoard) {
  try {
    const view = await api('/tasks/' + encodeURIComponent(operation.task.task_id));
    if (epoch !== identityEpoch) return;
    board.observe(view, rows.some(row => row.task_id === view.task_id));
    rememberTask(view, false);
    if (currentTask?.task_id === view.task_id) renderConversation(view);
  } catch (error) {
    if (epoch !== identityEpoch) return;
    const message = actionStatus(operation.task, operation.action) + ' 任务状态未更新。' + requestErrorReason(error);
    if (currentTask?.task_id === operation.task.task_id) $('error').textContent = message;
    if (fromBoard) board.announce(message);
  }
}

async function write(path, payload, audioFile, fromBoard = false) {
  if (writing) return;
  if (!credential) {
    $('error').textContent = '请先在设置中应用访问凭据。';
    return;
  }
  const isSubmission = path === '/tasks' || path === '/tasks/audio';
  // A follow-up is a new task, never a retry or amendment of the ended task.
  if (isSubmission && currentTask && !canContinueTask(currentTask)) return;
  if (isSubmission && !currentTask && submission?.ticket === selection) return;
  if (isSubmission && currentTask) {
    archiveCurrentTask();
    pendingWrite = null;
  }
  const operation = requestAction(path, payload);
  if (operation && (!operation.key || actionAttempts.has(operation.key))) {
    updateControls();
    board.refreshControls();
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
  const request = pendingWrite,
    epoch = identityEpoch,
    ticket = ++selection;
  if (operation) {
    saveActionAttempt(operation.key, 'unknown');
    board.refreshControls();
  }
  writing = true;
  const attempt = isSubmission ? {
    goal: audioFile ? '语音任务：' + audioFile.name : payload.text,
    request, ticket, stopRequested: false
  } : null;
  if (attempt) {
    $('goal').value = '';
    $('answer').value = '';
    submission = attempt; renderPending(); renderHistory();
  }
  updateControls();
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
    if (operation) saveActionAttempt(operation.key, 'confirmed');
    if (epoch !== identityEpoch) return;
    if (attempt) { await acceptSubmission(view, attempt); return; }
    pendingWrite = null;
    board.observe(view, rows.some(row => row.task_id === view.task_id));
    rememberTask(view, false);
    if (ticket === selection && !fromBoard) renderConversation(view);
    if (fromBoard) board.announce('已提交「' + view.goal_text + '」的批准决定，当前状态：' + (states[view.state] || view.state) + '。');
    if (page === 'home') board.load();
    loadHistory();
  } catch (error) {
    if (operation) saveActionAttempt(operation.key, error.status >= 400 && error.status < 500 ? 'rejected' : 'unknown', requestErrorReason(error));
    if (epoch === identityEpoch && (ticket === selection || (attempt && attempt.ticket === selection))) {
      if (attempt) {
        attempt.unconfirmed = true;
        renderPending();
      }
      const message = operation ? actionStatus(operation.task, operation.action) :
        error.message + ' · 请求编号 ' + request.key + '。请先查看任务历史。';
      if (fromBoard) {
        board.announce(message);
        board.load();
      } else $('error').textContent = message;
    }
    if (epoch === identityEpoch && attempt && submission === attempt) {
      attempt.unconfirmed = true;
      await lookupSubmission(attempt);
    }
    if (epoch === identityEpoch && operation) await queryActionTask(operation, epoch, fromBoard);
  } finally {
    if (epoch === identityEpoch) { writing = false; updateControls(); board.refreshControls(); }
  }
}

// Cancellation has its own per-task request. A slow submit/answer/approval never
// swallows it, and responses only update the matching task and current identity.
async function stopTask(task) {
  const id = task.task_id;
  if (!id || !canStop(task)) return;
  const request = {key: crypto.randomUUID(), version: task.version ?? null, busy: true};
  stopRequests.set(id, request);
  attemptedStops.add(id);
  saveSessionList('deepleap.stop-attempts', attemptedStops);
  const epoch = identityEpoch;
  updateControls(); renderHistory();
  const observe = view => {
    if (epoch !== identityEpoch) return;
    board.observe(view, rows.some(row => row.task_id === id));
    rememberTask(view, false);
    if (currentTask?.task_id === id) renderConversation(view);
  };
  try {
    if (request.version === null) {
      const fresh = await api('/tasks/' + encodeURIComponent(id));
      if (epoch !== identityEpoch) return;
      observe(fresh);
      if (endedStates.has(fresh.state) || fresh.state === 'CANCEL_REQUESTED' || fresh.cancel_result) return;
      if (taskError(fresh) && taskError(fresh).execution_status !== 'UNKNOWN') return;
      request.version = fresh.version;
    }
    const view = await api('/tasks/' + encodeURIComponent(id) + '/cancel', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': request.key},
      body: JSON.stringify({expected_version: request.version})
    });
    if (epoch !== identityEpoch) return;
    request.resolved = true;
    observe(view);
    if (currentTask?.task_id === id) $('error').textContent = '';
    $('notice').textContent = '';
    if (page === 'home') board.load();
    loadHistory();
  } catch (error) {
    if (epoch === identityEpoch) {
      // A newer status query can confirm cancellation before a lost POST reply
      // finally fails. Do not overwrite that authoritative state with an old error.
      const known = currentTask?.task_id === id ? currentTask : rows.find(row => row.task_id === id);
      if (known && (endedStates.has(known.state) || known.state === 'CANCEL_REQUESTED')) {
        request.resolved = true;
        return;
      }
      const rejected = error.status >= 400 && error.status < 500;
      const detail = {'task version changed':'任务状态已变化', 'task already ended':'任务已经结束',
        'request key content conflict':'请求编号对应的内容不一致', CANCEL_POLICY_UNAVAILABLE:'无法取得停止权限', NOT_FOUND:'未找到可访问的任务'}[error.message] || error.message;
      const message = (rejected ? '停止请求未受理：' : '停止请求结果未确认：') + detail + '。不会再次发送停止请求，请刷新任务状态。';
      request.rejected = rejected;
      request.error = message;
      $('notice').textContent = message;
      if (currentTask?.task_id === id) {
        $('error').textContent = message;
        $('cancel-result').hidden = false;
        $('cancel-result').textContent = message;
      }
      // Only a read may follow an ambiguous write; never send cancel again.
      try { const fresh = await api('/tasks/' + encodeURIComponent(id)); observe(fresh); } catch { /* Keep the previous facts. */ }
    }
  } finally {
    if (epoch === identityEpoch) { request.busy = false; updateControls(); renderHistory(); }
  }
}

function archiveCurrentTask() {
  if (!canContinueTask(currentTask)) return;
  const previous = $('task-detail').cloneNode(true);
  previous.removeAttribute('id');
  previous.classList.add('conversation-turn');
  previous.dataset.taskId = currentTask.task_id;
  previous.hidden = false;
  // Old turns are readable snapshots, not a second set of working controls.
  previous.querySelectorAll('form, #decisions, #action-result').forEach(element => element.remove());
  previous.querySelectorAll('[id]').forEach(element => element.removeAttribute('id'));
  previous.prepend(node('p', '此前任务 · 以下保留提交下一条时的记录', 'previous-task-note'));
  $('conversation-history').append(previous);
}

function newTask() {
  blankTask();
  pendingWrite = null;
  conversation(true);
}
$('new-task').onclick = $('sidebar-new').onclick = $('followup-new').onclick = newTask;
$('goal').oninput = updateControls;
$('refresh-task').onclick = refreshCurrentTask;
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
    // Submit this recording once. A lost reply is resolved by request-key lookup.
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
    if (submission?.unconfirmed) lookupSubmission();
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
