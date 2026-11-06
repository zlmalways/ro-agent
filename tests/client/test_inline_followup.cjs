// UI state contracts only. Real HTTP/WS acceptance is recorded separately.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../client/app.js'), 'utf8');
function controls(task, overrides = {}) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      hidden: ['submit-form', 'voice-input'].includes(id), value: id === 'goal' ? '下一条独立任务草稿' : '',
      textContent: '', disabled: false, querySelector() { return get(id + '-button'); }
    });
    return elements.get(id);
  };
  const context = {
    $: get, credential: 'test-only', writing: false, recording: null, currentTask: task,
    submission: null, selection: 1, stopRequests: new Map(), attemptedStops: new Set(),
    endedStates: new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']),
    taskError: value => value?.error || null, actionStatus: () => '',
    canStop: value => value?.error?.execution_status === 'UNKNOWN',
    errorFooter: () => '执行结果尚未确认', normalComposerMessage: () => '任务仍在进行',
    cancellationMessage: () => '等待停止确认', ...overrides
  };
  const helperStart = source.indexOf('function canContinueTask(');
  const controlStart = source.indexOf('function updateControls()');
  vm.runInNewContext((helperStart < 0 ? '' : source.slice(helperStart, controlStart)) +
    source.slice(controlStart, source.indexOf('function renderPending()', controlStart)) + '\nupdateControls();', context);
  return {get, context};
}

for (const state of ['SUCCEEDED', 'FAILED', 'CANCELLED']) {
  test(`${state}: ended task accepts another independent input without clearing draft`, () => {
    const {get} = controls({task_id:'first', state});
    assert.equal(get('submit-form').hidden, false);
    assert.equal(get('voice-input').hidden, true, 'only text follow-up is in scope');
    assert.equal(get('send').disabled, false);
    assert.equal(get('goal').value, '下一条独立任务草稿');
    assert.equal(get('followup-new').hidden, true, 'no extra new-task click is required');
  });
}

for (const state of ['RECEIVED', 'PLANNING', 'NEEDS_CLARIFICATION', 'WAITING_APPROVAL', 'READY', 'QUEUED', 'RUNNING', 'CANCEL_REQUESTED', 'BLOCKED']) {
  test(`${state}: unfinished task does not expose follow-up input`, () => {
    const {get} = controls({task_id:'first', state});
    assert.equal(get('submit-form').hidden, true);
    assert.equal(get('send').disabled, true);
  });
}

test('stopped advancement can accept a new task while preserving unknown-result controls', () => {
  const {get} = controls({task_id:'first', state:'BLOCKED', advancement_stopped:true,
    error:{execution_status:'UNKNOWN'}});
  assert.equal(get('submit-form').hidden, false);
  assert.equal(get('active-composer').hidden, false);
  assert.equal(get('cancel').hidden, false);
  assert.match(get('stop-hint').textContent, /尚未确认/);
});

test('unfinished stop confirmation overrides an advancement-stopped marker', () => {
  const {get} = controls({task_id:'first', state:'CANCEL_REQUESTED', advancement_stopped:true,
    error:{execution_status:'UNKNOWN'}});
  assert.equal(get('submit-form').hidden, true);
});

test('lost initial submission response does not expose another input', () => {
  const {get} = controls(null, {submission:{ticket:1,unconfirmed:true}});
  assert.equal(get('submit-form').hidden, true);
  assert.equal(get('send').disabled, true);
});

test('no credential prevents follow-up submission', () => {
  assert.equal(controls({task_id:'first',state:'SUCCEEDED'}, {credential:''}).get('send').disabled, true);
});

test('explicit new-task view is independent of an earlier unconfirmed request', () => {
  const {get} = controls(null, {selection:2,submission:{ticket:1,unconfirmed:true}});
  assert.equal(get('submit-form').hidden, false);
  assert.equal(get('send').disabled, false);
});
