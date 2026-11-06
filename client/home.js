'use strict';

// Render only this user's persisted work. The existing task dialog owns answers and decisions.
function createHomeBoard({request, openTask, approve, node, taskError, errorCard, taskState, actionStatus, cancellationMessage}) {
  const boxes = {
    questions: document.getElementById('home-questions'),
    approvals: document.getElementById('home-approvals'),
    running: document.getElementById('home-running'),
    queue: document.getElementById('home-queue'),
    errors: document.getElementById('home-errors')
  };
  const sync = document.getElementById('home-sync');
  const feedback = document.getElementById('home-feedback');
  const limits = {};
  let items = [], connected = false, loaded = false, pending = false, requestId = 0;
  const empties = {questions:'没有待回答的问题。', approvals:'没有待批准的计划。',
    running:'当前没有执行中的任务。', queue:'当前没有排队任务。', errors:'暂无发生错误的任务。'};

  function category(task) {
    if (taskError(task)) return 'errors';
    switch (task.state) {
      case 'NEEDS_CLARIFICATION': return 'questions';
      case 'WAITING_APPROVAL': return 'approvals';
      case 'RUNNING':
      case 'CANCEL_REQUESTED': return 'running';
      case 'READY':
      case 'QUEUED': return 'queue';
      default: return null;
    }
  }

  function button(label, onClick, primary = false) {
    const el = node('button', label, primary ? '' : 'quiet');
    el.type = 'button';
    el.onclick = onClick;
    return el;
  }

  function stepStatus(task, step) {
    const command = (task.result_summary || []).findLast(c => c.step_id === step.step_id);
    if (command?.evidence_conflict) return '执行回执冲突';
    if (command?.outcome === 'SUCCEEDED' || task.completed?.includes(step.step_id)) return '已完成';
    if (command?.outcome === 'RUNNING') return '机器人执行中';
    if (command?.outcome === 'RESULT_UNKNOWN') return '结果未知';
    if (command?.outcome === 'FAILED') return '机器人报告失败';
    if (command?.dispatch_state === 'SENT') return '已下发，等待反馈';
    if (command?.dispatch_state === 'DISPATCHING') return '正在下发';
    return '待执行';
  }

  function card(task, group, index, sectionRows) {
    const article = document.createElement('article');
    if (task.created_at && (group === 'questions' || group === 'approvals')) {
      const time = new Date(task.created_at);
      article.append(node('small', '提交于 ' + time.toLocaleString(undefined, {month:'numeric', day:'numeric',
        hour:'2-digit', minute:'2-digit'}), 'board-meta'));
    }
    const title = node('h3', task.goal_text || '任务 ' + task.task_id);
    article.append(title);
    const actions = node('div', '', 'board-actions');
    const details = () => openTask(task.task_id);
    if (group === 'errors') {
      article.append(node('p', taskState(task), 'board-meta'), errorCard(task));
      if (task.cancel_result) article.append(node('p', '停止请求：' + cancellationMessage(task), 'board-meta'));
      actions.append(button('查看任务', details));
    } else if (group === 'questions') {
      article.append(node('p', task.clarification || '等待补充任务信息。', 'board-question'));
      const status = actionStatus(task, 'answer');
      if (status) article.append(node('p', status, 'board-meta'));
      actions.append(button(status ? '查看任务' : '回答问题', () => openTask(task.task_id, !status), !status));
    } else if (group === 'queue') {
      const state = task.state === 'QUEUED' ? '已排队，等待执行机会' : '计划已就绪，等待调度';
      const position = task.queue_seq == null ? '顺序待同步' : '我的队列 · 第 ' + (index + 1) + ' 项';
      const heading = node('div', '', 'board-queue-head');
      heading.append(title, button('查看任务', details));
      article.append(heading, node('p', position + ' · ' + state, 'board-meta'));
      const previous = sectionRows[index - 1];
      const executing = !previous && items.find(row => row.state === 'RUNNING');
      if (previous) article.append(node('small', '我的上一项：' + previous.goal_text, 'board-meta'));
      else if (executing) article.append(node('small', '当前执行：' + executing.goal_text, 'board-meta'));
      return article;
    } else {
      const running = group === 'running';
      if (running) {
        const robot = task.result_summary?.at(-1)?.robot_binding_id || task.binding_id;
        const state = task.state === 'CANCEL_REQUESTED' ? cancellationMessage(task) : '执行进度';
        article.append(node('p', robot ? robot + ' · ' + state : state, 'board-meta'));
      }
      const steps = task.plan_steps || [];
      if (steps.length) {
        const list = document.createElement('ol');
        for (const step of steps) {
          const li = node('li', step.description);
          if (running) {
            const status = stepStatus(task, step);
            li.append(node('span', status, status === '机器人执行中' ? 'board-state current' : 'board-state'));
            if (status === '执行回执冲突') li.append(node('small', '收到相互矛盾的回执，无法确认执行结果', 'board-meta'));
          }
          list.append(li);
        }
        article.append(list);
      } else {
        article.append(node('p', '暂无已保存的计划步骤。', 'board-meta'));
      }
      if (!running && task.plan_id && steps.length) {
        const status = actionStatus(task, 'approve');
        const approval = button('批准计划', () => approve(task), true);
        approval.disabled = Boolean(status);
        if (status) article.append(node('p', status, 'board-meta'));
        actions.append(approval);
      }
      actions.append(button(running ? '查看执行进度' : '查看详情', details));
    }
    article.append(actions);
    return article;
  }

  function render() {
    for (const [group, container] of Object.entries(boxes)) {
      const matches = items.filter(task => category(task) === group);
      const visible = limits[group] || 5;
      container.replaceChildren(...matches.slice(0, visible).map((task, index) => card(task, group, index, matches)));
      if (!matches.length) {
        const message = !connected ? '请到设置页连接服务。' : loaded ? empties[group] : '尚未取得此类任务。';
        container.append(node('p', message, 'empty'));
      } else if (matches.length > visible) {
        container.append(button('展开更多（还有 ' + (matches.length - visible) + ' 项）', () => {
          limits[group] = visible + 10;
          render();
        }));
      }
    }
  }

  function reset() {
    requestId++; pending = false; loaded = false; connected = false; items = [];
    for (const group of Object.keys(boxes)) delete limits[group];
    sync.textContent = feedback.textContent = '';
    render();
  }

  async function load(background = false) {
    if (background && pending) return;
    const ticket = ++requestId;
    pending = true;
    if (!connected) { connected = true; render(); }
    try {
      const data = await request();
      if (ticket !== requestId) return;
      const changed = !loaded || JSON.stringify(items) !== JSON.stringify(data);
      loaded = true; items = data;
      if (changed) render();
      sync.textContent = '';
    } catch (error) {
      if (ticket === requestId) sync.textContent = '首页任务状态未更新。' + error.message;
    } finally {
      if (ticket === requestId) pending = false;
    }
  }

  function observe(view, owned) {
    const index = items.findIndex(row => row.task_id === view.task_id);
    if (index < 0 && !owned) return;
    if (index >= 0 && items[index].version > view.version) return;
    requestId++; pending = false;
    if (!category(view)) {
      if (index >= 0) items.splice(index, 1);
    } else if (index < 0) items.push(view);
    else items[index] = {...items[index], ...view};
    render();
  }

  return {load, observe, reset, refreshControls: render, announce: message => { feedback.textContent = message; }};
}
