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
  RECEIVED: '已受理',
  PLANNING: '正在生成计划',
  NEEDS_CLARIFICATION: '需要澄清',
  WAITING_APPROVAL: '等待批准',
  READY: '等待执行',
  QUEUED: '正在排队',
  RUNNING: '执行中',
  BLOCKED: '已暂停，等待核对',
  CANCEL_REQUESTED: '已请求取消，等待反馈',
  SUCCEEDED: '已完成',
  FAILED: '任务未完成',
  CANCELLED: '任务已结束'
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
  page = 'home',
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

function node(tag, text, className) {
  const el = document.createElement(tag);
  el.textContent = text;
  if (className) el.className = className;
  return el;
}
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
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入。');
  return data;
}

function conversation(open) {
  $('conversation').hidden = !open;
  $('dock').hidden = open;
  $('dock').setAttribute('aria-expanded', String(open));
  if (open)(currentTask ? $('new-task') : $('goal')).focus();
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
  $('submit-form').hidden = $('voice-input').hidden = false;
  $('goal').value = $('answer').value = $('audio').value = '';
  $('error').textContent = '';
}

function taskRow(row) {
  const el = node('button', '', 'task-row');
  const when = row.created_at ? new Date(row.created_at).toLocaleString() + ' · ' : '';
  el.append(node('span', row.goal_text), node('small', when + (states[row.state] || row.state)));
  el.onclick = () => openTask(row.task_id);
  return el;
}

function totalTasks(counts) {
  return Object.values(counts).reduce((total, value) => total + value, 0);
}

function renderHistory() {
  for (const [id, data] of [
      ['history', rows],
      ['recent', rows.slice(0, 5)]
    ]) {
    $(id).replaceChildren(...data.map(taskRow));
    if (!data.length) {
      let message = '请先到设置中连接工作台。';
      if (credential) message = stateCounts === null ? '尚未取得任务列表。' : '当前没有已加载的任务。';
      $(id).append(node('p', message, 'empty'));
    }
  }
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
  if (previous && previous.version >= view.version) return;
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
    if (previous) Object.assign(previous, {state: view.state, version: view.version, goal_text: view.goal_text});
    else rows.unshift({task_id: view.task_id, state: view.state, version: view.version, goal_text: view.goal_text});
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
  credential = '';
  pendingWrite = null;
  writing = false;
  blankTask();
  rows = [];
  nextBefore = null;
  stateCounts = null;
  $('connection-status').textContent = '尚未连接';
  renderHistory();
  $('robot-fields').replaceChildren();
  $('robot-status').textContent = '请先设置访问凭据。';
  $('lookup').value = $('notice').textContent = '';
  $('task-goal').textContent = $('task-id').textContent = $('question').textContent = '';
  $('plan').replaceChildren();
  $('results').replaceChildren();
  conversation(false);
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
    if (page === 'settings') location.hash = 'home';
    else navigate();
  } catch (error) {
    if (epoch !== identityEpoch) return;
    $('connection-status').textContent = '尚未连接';
    $('notice').textContent = '连接失败。' + error.message;
  }
};
async function loadRobot() {
  if (!credential) return;
  const request = ++fleetRequest;
  $('robot-status').textContent = '正在读取机器人资料…';
  $('robot-fields').replaceChildren();
  try {
    const s = await api('/resources/snapshot');
    if (request !== fleetRequest) return;
    const readiness = s.readiness || {};

    function ready(value) {
      return value === undefined ? '尚未确认' : value ? '已就绪' : '未就绪';
    }
    const brain = readiness.brain_server_ready;
    const brainReady = brain === false ? false : brain === true && readiness.brain_engine != null ?
      readiness.brain_engine === 'ready' : undefined;
    const fields = [
      ['机器人编号', s.robot_binding_id],
      ['导航服务', ready(readiness.nav_server_ready)],
      ['技能服务', ready(brainReady)],
      ['可用航点', Object.keys(s.waypoints || {}).join('、') || '暂无已核实航点'],
      ['可用技能', Object.keys(s.brain_skills || {}).join('、') || '暂无已核实技能']
    ];
    for (const [label, value] of fields) $('robot-fields').append(node('dt', label), node('dd', value));
    $('robot-status').textContent = '本次读取的机器人资料';
  } catch (error) {
    if (request === fleetRequest) $('robot-status').textContent = '资料未更新。' + error.message;
  }
}

function navigate() {
  const target = location.hash.slice(1);
  page = Object.hasOwn(pages, target) ? target : 'home';
  // Home and fleet display the same single-robot snapshot panel.
  for (const id of Object.keys(pages)) $(id).hidden = page !== id && !(id === 'robots' && page === 'home');
  document.querySelectorAll('nav a').forEach(a => {
    if (a.hash === '#' + page) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  $('page-title').textContent = pages[page];
  $('notice').textContent = '';
  if (page === 'robots' || page === 'home') loadRobot();
  if (page === 'home' || page === 'tasks') loadHistory();
}

function renderConversation(view) {
  if (currentTask?.task_id === view.task_id && view.version < currentTask.version) return;
  currentTask = view;
  $('task-detail').hidden = false;
  $('submit-form').hidden = $('voice-input').hidden = true;
  $('task-goal').textContent = view.goal_text || '';
  $('status').textContent = states[view.state] || view.state;
  $('task-id').textContent = view.task_id;
  $('reason').textContent = view.result_disputed ? '执行证据存在冲突，需要人工核对。' : view.reason_code;
  $('question').textContent = view.clarification;
  $('answer-form').hidden = view.state !== 'NEEDS_CLARIFICATION';
  $('decisions').hidden = view.state !== 'WAITING_APPROVAL' && !view.reason_code.startsWith('AUTHORIZATION_');
  $('cancel').hidden = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'CANCEL_REQUESTED'].includes(view.state);
  $('plan').replaceChildren(...(view.plan_steps || []).map(step => node('li', step.description)));
  $('results').replaceChildren();
  if (!view.result_summary.length) $('results').textContent = '尚无机器人反馈。';
  for (const cmd of view.result_summary) {
    const item = node('div', '', 'result');
    const details = document.createElement('details');
    details.append(node('summary', '查看执行依据'), node('small', '指令 ' + cmd.command_id + ' · ' + (cmd.evidence_ids
      .join('、') || '暂无反馈依据')));
    item.append(node('strong', outcomes[cmd.outcome] || cmd.outcome), node('p', cmd.description), details);
    $('results').append(item);
  }
}
async function openTask(id) {
  blankTask();
  const ticket = selection;
  conversation(true);
  try {
    const view = await api('/tasks/' + encodeURIComponent(id));
    if (ticket === selection) renderConversation(view);
  } catch (error) {
    if (ticket === selection) $('error').textContent = error.message;
  }
}
async function write(path, payload, audioFile) {
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
  const request = pendingWrite,
    epoch = identityEpoch,
    ticket = ++selection;
  writing = true;
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
    rememberTask(view, path === '/tasks' || path === '/tasks/audio');
    if (ticket === selection) renderConversation(view);
    loadHistory();
  } catch (error) {
    if (epoch === identityEpoch && ticket === selection) {
      $('error').textContent = error.message + ' · 请求编号 ' + request.key + '。请先查看任务历史。';
    }
  } finally {
    if (epoch === identityEpoch) writing = false;
  }
}

function newTask() {
  blankTask();
  pendingWrite = null;
  conversation(true);
}
$('new-task').onclick = newTask;
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
$('cancel').onclick = () => write('/tasks/' + currentTask.task_id + '/cancel', {
  expected_version: currentTask.version
});
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
  if (!credential || !currentTask || writing || polling || $('conversation').hidden) return;
  const ticket = selection,
    id = currentTask.task_id;
  polling = true;
  try {
    const view = await api('/tasks/' + encodeURIComponent(id));
    if (ticket !== selection) return;
    const changed = view.version !== currentTask.version;
    renderConversation(view);
    if (changed) {
      rememberTask(view, false);
      loadHistory();
    }
  } catch (error) {
    if (ticket === selection) $('error').textContent = '任务状态未更新。' + error.message;
  } finally {
    polling = false;
  }
}, 700);
renderHistory();
navigate();
