// Background tasks panel: what the agents are working on, what they finished, and a way to stop them.
const TASK_STATUS = {queued:'排队中', running:'进行中', done:'已完成', failed:'失败', cancelled:'已取消'};
const TASK_POLL_MS = 3000;
let taskTimer = null;
// Features add a renderer for their finished results here, keyed by task kind.
const taskDetails = {};
function taskPreview(task) {
  if (task.status === 'failed') return task.error;
  if (task.status === 'running' || task.status === 'queued') return task.progress;
  const result = task.result || {};
  return String(result.text || result.summary || '').slice(0, 200);
}
function taskRow(task) {
  const row = document.createElement('div'), head = document.createElement('div'), body = document.createElement('div');
  row.style.cssText = 'margin-top:10px;padding:10px 12px;border:1px solid #dde5ef;border-radius:10px';
  head.style.cssText = 'display:flex;gap:10px;justify-content:space-between;align-items:center';
  const label = document.createElement('strong'); label.textContent = task.title;
  const state = document.createElement('span'); state.className = 'hint'; state.textContent = TASK_STATUS[task.status] || task.status;
  head.append(label, state);
  if (task.status === 'queued' || task.status === 'running') {
    const cancel = document.createElement('button');
    cancel.textContent = '取消'; cancel.className = 'secondary'; cancel.type = 'button';
    cancel.onclick = () => fetch(`/api/tasks/${task.id}/cancel`, {method:'POST'}).then(refreshTasks).catch(() => {});
    head.append(cancel);
  }
  body.className = 'hint'; body.style.whiteSpace = 'pre-wrap'; body.textContent = taskPreview(task) || '';
  const detail = task.status === 'done' && task.result && taskDetails[task.kind];
  row.append(head, detail ? detail(task) : body);
  return row;
}
async function refreshTasks() {
  const response = await fetch('/api/tasks');
  if (!response.ok) return;
  const {tasks} = await response.json();
  $('taskEmpty').hidden = tasks.length > 0;
  $('taskList').replaceChildren(...tasks.map(taskRow));
  // Say once when something finished while the page was open, then remember it was seen.
  for (const task of tasks) {
    if (!task.seen && ['done', 'failed'].includes(task.status)) {
      status(`后台任务${task.status === 'done' ? '完成' : '失败'}：${task.title}`);
      fetch(`/api/tasks/${task.id}/seen`, {method:'POST'}).catch(() => {});
    }
  }
  clearTimeout(taskTimer);
  if (tasks.some(task => task.status === 'queued' || task.status === 'running')) taskTimer = setTimeout(() => refreshTasks().catch(() => {}), TASK_POLL_MS);
}
// Features call this after submitting, so the panel starts following the new task.
async function submitTask(kind, title, input) {
  const response = await fetch('/api/tasks', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({kind, title, input})});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.detail || '提交失败');
  $('taskPanel').open = true;
  await refreshTasks();
  return data.id;
}
refreshTasks().catch(() => {});
