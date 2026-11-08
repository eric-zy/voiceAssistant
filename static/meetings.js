// Meeting notes: upload a recording, then read the notes, tick the to-dos to become reminders, archive or delete.
const meetingDetails = new Map();  // kept per task, so polling for another task does not reset ticked boxes
function el(tag, text, style) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (style) node.style.cssText = style;
  return node;
}
function meetingButton(text, onclick) {
  const button = el('button', text); button.className = 'secondary'; button.type = 'button'; button.onclick = onclick;
  return button;
}
function meetingClock(seconds) {
  const s = Math.floor(seconds), pad = n => String(n).padStart(2, '0');
  return s >= 3600 ? `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s % 3600 / 60))}:${pad(s % 60)}` : `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}
async function meetingPost(url, body) {
  const response = await fetch(url, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.detail || '操作失败');
  return data;
}
function meetingDetail(task) {
  if (meetingDetails.has(task.id)) return meetingDetails.get(task.id);
  const result = task.result, box = el('div', undefined, 'margin-top:8px'), note = el('p', '', 'margin:6px 0');
  note.className = 'hint';
  const notes = el('details'); notes.open = true;
  notes.append(el('summary', '纪要'), el('div', result.text, 'white-space:pre-wrap;margin-top:6px'));
  box.append(notes);
  const rows = result.proposals.map(item => {
    const row = el('label', undefined, 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px');
    const tick = el('input'); tick.type = 'checkbox'; tick.checked = Boolean(item.due_at); tick.style.cssText = 'flex:0 0 auto';
    const when = el('input'); when.type = 'datetime-local'; when.value = item.due_at.slice(0, 16); when.style.cssText = 'flex:0 0 auto;padding:4px';
    const said = [item.owner, item.due_text && `原话：${item.due_text}`].filter(Boolean).join(' · ');
    row.append(tick, el('span', item.task, 'flex:1 1 10em'), when, el('span', said, 'font-size:13px;color:#77859a;flex-basis:100%;padding-left:24px'));
    return {item, tick, when, row};
  });
  if (rows.length) {
    const todos = el('details'); todos.open = true;
    todos.append(el('summary', `待办（${rows.length}）`), ...rows.map(r => r.row));
    todos.append(meetingButton('把勾选的建为提醒', async () => {
      const items = rows.filter(r => r.tick.checked).map(r => ({task: r.item.task, due_at: r.when.value ? r.when.value + ':00' : ''}));
      if (!items.length) { note.textContent = '没有勾选待办'; return; }
      try {
        const {created, problems} = await meetingPost('/api/meetings/reminders', {items});
        note.textContent = `已建 ${created.length} 条提醒` + (problems.length ? `；未建：${problems.join('；')}` : '');
        rows.forEach(r => { if (r.tick.checked && !problems.some(p => p.startsWith(r.item.task.slice(0, 20)))) r.tick.checked = false; });
        if (typeof refreshPanels === 'function') refreshPanels();
      } catch (error) { note.textContent = error.message; }
    }));
    box.append(todos);
  }
  if (result.transcript.length) {
    const transcript = el('details');
    transcript.append(el('summary', `逐字稿（${result.transcript.length} 段）`),
      ...result.transcript.map(line => el('div', `[${meetingClock(line.start)}] ${line.text}`, 'margin-top:4px')));
    box.append(transcript);
  }
  const actions = el('div', undefined, 'display:flex;gap:8px;flex-wrap:wrap;margin-top:8px');
  actions.append(meetingButton('存进知识库', () => meetingPost('/api/meetings/archive', {task_id: task.id})
    .then(saved => { note.textContent = '已存进知识库：' + saved.name; if (typeof refreshPanels === 'function') refreshPanels(); })
    .catch(error => { note.textContent = error.message; })));
  const remove = meetingButton('删除录音', async () => {
    if (!confirm('删除这段录音？纪要和逐字稿会保留，录音删除后无法恢复。')) return;
    const response = await fetch(`/api/meetings/${result.file}`, {method:'DELETE'});
    note.textContent = response.ok ? '录音已删除' : '录音已不在了';
    remove.disabled = true;
  });
  actions.append(remove);
  box.append(actions, note);
  meetingDetails.set(task.id, box);
  return box;
}
taskDetails.meeting_notes = meetingDetail;
$('meetingUpload').onclick = async () => {
  const file = $('meetingFile').files[0];
  if (!file) { $('meetingStatus').textContent = '请先选择录音文件'; return; }
  const form = new FormData(); form.append('file', file); form.append('date', $('meetingDate').value);
  $('meetingUpload').disabled = true; $('meetingStatus').textContent = '正在上传…';
  try {
    const response = await fetch('/api/meetings', {method:'POST', body:form});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.detail || '上传失败');
    $('meetingStatus').textContent = '已开始处理，进度见“后台任务”';
    $('meetingFile').value = '';
    $('taskPanel').open = true;
    await refreshTasks();
  } catch (error) { $('meetingStatus').textContent = error.message; }
  finally { $('meetingUpload').disabled = false; }
};
refreshTasks().catch(() => {});
