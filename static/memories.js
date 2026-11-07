// Memory and reminder panels plus the undo bar. The server owns the data; this only shows and forwards.
async function changeItem({kind, action, id}) {
  const restore = action === 'restore', base = kind === 'reminder' ? 'reminders' : 'memories';
  const response = await fetch(`/api/${base}/${id}${restore ? '/restore' : ''}`, {method: restore ? 'POST' : 'DELETE'});
  if (!response.ok) throw Error('操作失败');
}
function itemRow(label, buttonText, onclick) {
  const row = document.createElement('div'), text = document.createElement('span'), button = document.createElement('button');
  row.style.cssText = 'display:flex;gap:10px;justify-content:space-between;align-items:center;margin-top:8px';
  text.textContent = label;
  button.textContent = buttonText; button.className = 'secondary'; button.type = 'button'; button.onclick = onclick;
  row.append(text, button);
  return row;
}
async function refreshMemories() {
  const {memories} = await (await fetch('/api/memories')).json();
  $('memoryEmpty').hidden = memories.length > 0;
  $('memoryList').replaceChildren(...memories.map(memory => itemRow(memory.content, '删除', () =>
    changeItem({action:'delete', id:memory.id}).then(() => {
      offerUndo('已删除：' + memory.content, {action:'restore', id:memory.id});
      return refreshPanels();
    }).catch(error => status(error.message)))));
}
async function refreshReminders() {
  const {reminders = []} = await (await fetch('/api/reminders')).json();
  $('reminderEmpty').hidden = reminders.length > 0;
  $('reminderList').replaceChildren(...reminders.map(reminder => itemRow(`${reminder.due_at.replace('T', ' ')} ${reminder.content}`, '取消', () =>
    changeItem({kind:'reminder', action:'delete', id:reminder.id}).then(() => {
      offerUndo('已取消：' + reminder.content, {kind:'reminder', action:'restore', id:reminder.id});
      return refreshPanels();
    }).catch(error => status(error.message)))));
}
async function refreshKnowledge() {
  const {files = []} = await (await fetch('/api/knowledge')).json();
  $('knowledgeEmpty').hidden = files.length > 0;
  $('knowledgeList').replaceChildren(...files.map(file => itemRow(
    file.chunks ? `${file.name}（${file.chunks} 段）` : `${file.name}（未提取到文字）`, '删除', () => deleteDocument(file.name))));
}
async function deleteDocument(name) {
  // A deleted file cannot be undone, unlike memories and reminders.
  if (!window.confirm(`从知识库永久删除“${name}”？`)) return;
  const path = name.split('/').map(encodeURIComponent).join('/');
  const response = await fetch(`/api/knowledge/${path}`, {method:'DELETE'});
  status(response.ok ? '已删除：' + name : '删除失败');
  await refreshKnowledge();
}
async function uploadDocument(file) {
  const form = new FormData(); form.append('file', file);
  status('正在加入知识库：' + file.name);
  const response = await fetch('/api/knowledge', {method:'POST', body:form});
  const result = await response.json();
  if (!response.ok) throw Error(result.detail || '上传失败');
  status(result.chunks ? `已加入知识库：${result.name}，共 ${result.chunks} 段` : `${result.name} 没有提取到文字，可能是扫描件或已加密`);
}
$('knowledgeFile').onchange = async event => {
  const input = event.target;
  try { for (const file of input.files) await uploadDocument(file); }
  catch (error) { status(error.message); }
  input.value = '';
  await refreshKnowledge().catch(() => {});
};
function refreshPanels() { return Promise.all([refreshMemories(), refreshReminders(), refreshKnowledge()]); }
function offerUndo(text, undo) {
  $('undoText').textContent = text; $('undoBox').hidden = !undo;
  $('undoButton').onclick = () => changeItem(undo).then(() => {
    $('undoBox').hidden = true; status('已撤销');
    return refreshPanels();
  }).catch(error => status(error.message));
}
window.memoryChanged = ({text, undo}) => {
  offerUndo(text, undo);
  refreshPanels().catch(() => {});
};
refreshPanels().catch(() => {});
