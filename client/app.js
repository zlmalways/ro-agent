'use strict';
let currentTask = null;
let busy = false;
const $ = (id) => document.getElementById(id);
const states = {RECEIVED:'已受理', PLANNING:'正在生成计划', NEEDS_CLARIFICATION:'需要澄清', WAITING_APPROVAL:'等待批准', READY:'等待执行', QUEUED:'正在排队', RUNNING:'执行中', BLOCKED:'已暂停，等待核对', CANCEL_REQUESTED:'已请求取消，等待反馈', SUCCEEDED:'已完成', FAILED:'任务未完成', CANCELLED:'任务已结束'};
const outcomes = {NONE:'尚无执行结果', RUNNING:'机器人正在执行', SUCCEEDED:'机器人报告成功', FAILED:'机器人报告失败', RESULT_UNKNOWN:'执行结果未知，请等待核对'};
async function api(path, options={}) {
  const token = $('credential').value.trim();
  if (!token) throw new Error('请先输入访问凭据。');
  const headers = {Authorization:'Bearer ' + token, ...options.headers};
  const result = await fetch('/api/robot-agent' + path, {...options, headers});
  const body = await result.json();
  if (!result.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '请求失败，请检查权限和输入。');
  return body;
}
function render(view) {
  currentTask = view;
  $('status').textContent = states[view.state] || view.state;
  $('task-id').textContent = '任务编号：' + view.task_id;
  $('reason').textContent = view.result_disputed ? '执行证据存在冲突，需要人工核对。' : view.reason_code;
  $('question').textContent = view.clarification;
  $('answer-form').hidden = view.state !== 'NEEDS_CLARIFICATION';
  $('decisions').hidden = view.state !== 'WAITING_APPROVAL' && !view.reason_code.startsWith('AUTHORIZATION_');
  $('cancel').hidden = ['SUCCEEDED','FAILED','CANCELLED','CANCEL_REQUESTED'].includes(view.state);
  $('plan').replaceChildren();
  for (const step of view.plan_steps || []) {
    const item = document.createElement('li');
    item.textContent = step.description;
    $('plan').append(item);
  }
  $('results').replaceChildren();
  if (!view.result_summary.length) $('results').textContent = '尚无机器人反馈。';
  for (const command of view.result_summary) {
    const row = document.createElement('div');
    row.className = 'result' + (command.outcome === 'RESULT_UNKNOWN' || command.outcome === 'FAILED' ? ' warn' : '');
    const state = document.createElement('strong');
    state.textContent = outcomes[command.outcome] || command.outcome;
    const action = document.createElement('p'); action.textContent = command.description;
    const details = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = '查看执行依据';
    const evidence = document.createElement('small'); evidence.textContent = '指令：' + command.command_id + ' / 反馈依据：' + (command.evidence_ids.join('、') || '暂无');
    details.append(summary, evidence); row.append(state, action, details); $('results').append(row);
  }
}
async function operate(action) {
  if (busy) return;
  busy = true; $('error').textContent = '';
  try { render(await action()); } catch (error) { $('error').textContent = error.message; }
  finally { busy = false; }
}
function post(path, body) {
  return api(path, {method:'POST', headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()}, body:JSON.stringify(body)});
}
$('submit-form').addEventListener('submit', (event)=>{event.preventDefault(); operate(()=>post('/tasks',{text:$('goal').value}));});
$('answer-form').addEventListener('submit', (event)=>{event.preventDefault(); operate(()=>post('/tasks/' + currentTask.task_id + '/clarifications/' + currentTask.clarification_id + '/answer',{answer:$('answer').value,expected_version:currentTask.version}));});
for (const decision of ['approve','reject']) $(decision).addEventListener('click',()=>operate(()=>post('/tasks/' + currentTask.task_id + '/decisions',{plan_id:currentTask.plan_id,decision,expected_version:currentTask.version})));
$('cancel').addEventListener('click',()=>operate(()=>post('/tasks/' + currentTask.task_id + '/cancel',{expected_version:currentTask.version})));
$('lookup-form').addEventListener('submit',(event)=>{event.preventDefault(); operate(()=>api('/tasks/' + encodeURIComponent($('lookup').value.trim())));});
$('audio-submit').addEventListener('click',()=>operate(async()=>{const file=$('audio').files[0];if(!file) throw new Error('请先选择语音文件。');const form=new FormData();form.append('audio',file);return api('/tasks/audio',{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:form});}));
setInterval(async()=>{if(!currentTask || busy || ! $('credential').value) return;try{render(await api('/tasks/' + currentTask.task_id));}catch(error){$('error').textContent=error.message;}},700);
