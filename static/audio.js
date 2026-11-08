// Spoken content: ask for a script, read and edit it, then voice it; play, download or delete the result.
const audioDetails = new Map();
const AUDIO_CHARS_PER_MINUTE = 250;
function audioEl(tag, text, style) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (style) node.style.cssText = style;
  return node;
}
function audioButton(text, onclick, secondary = true) {
  const button = audioEl('button', text); if (secondary) button.className = 'secondary'; button.type = 'button'; button.onclick = onclick;
  return button;
}
async function audioPost(url, body) {
  const response = await fetch(url, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.detail || '提交失败');
  return data;
}
function scriptEditor(task, note) {
  const box = audioEl('div', undefined, 'display:grid;gap:8px;margin-top:8px');
  const area = audioEl('textarea'); area.value = task.result.text; area.rows = 12; area.style.cssText = 'width:100%;padding:10px;font:inherit';
  const count = audioEl('span', ''); count.className = 'hint';
  const recount = () => { count.textContent = `${area.value.length} 字，约 ${Math.max(1, Math.round(area.value.length / AUDIO_CHARS_PER_MINUTE))} 分钟。可以直接修改，满意后再合成。`; };
  area.oninput = recount; recount();
  const render = audioButton('用当前音色合成', async () => {
    render.disabled = true;
    try {
      await audioPost('/api/audio/render', {text: area.value, title: task.title});
      note.textContent = '已开始合成';
      await refreshTasks();
    } catch (error) { note.textContent = error.message; render.disabled = false; }
  }, false);
  render.style.justifySelf = 'start';
  box.append(area, count, render);
  return box;
}
function audioPlayer(task, note) {
  const box = audioEl('div', undefined, 'display:grid;gap:8px;margin-top:8px'), url = `/api/audio/${task.result.audio}`;
  const player = audioEl('audio'); player.controls = true; player.preload = 'none'; player.src = url; player.style.width = '100%';
  const minutes = Math.floor(task.result.seconds / 60), seconds = task.result.seconds % 60;
  const info = audioEl('span', `${minutes} 分 ${seconds} 秒 · ${(task.result.bytes / 1024 / 1024).toFixed(1)} MB · 开头有“本内容由 AI 合成”提示`); info.className = 'hint';
  const actions = audioEl('div', undefined, 'display:flex;gap:8px;flex-wrap:wrap');
  const download = audioEl('a', '下载'); download.href = url; download.download = `${task.title}.ogg`; download.className = 'hint';
  const remove = audioButton('删除音频', async () => {
    if (!confirm('删除这段音频？删除后无法恢复，稿子会保留。')) return;
    const response = await fetch(url, {method:'DELETE'});
    note.textContent = response.ok ? '音频已删除' : '音频已不在了';
    remove.disabled = true; player.removeAttribute('src');
  });
  actions.append(download, remove);
  const script = audioEl('details'); script.append(audioEl('summary', '稿子'), audioEl('div', task.result.text, 'white-space:pre-wrap;margin-top:6px'));
  box.append(player, info, actions, script);
  return box;
}
function audioDetail(task) {
  const key = `${task.id}:${task.result.stage}`;
  if (audioDetails.has(key)) return audioDetails.get(key);
  const note = audioEl('p', ''); note.className = 'hint';
  const box = audioEl('div');
  box.append(task.result.stage === 'audio' ? audioPlayer(task, note) : scriptEditor(task, note), note);
  audioDetails.set(key, box);
  return box;
}
taskDetails.audio_script = audioDetail;
taskDetails.audio_render = audioDetail;
$('audioStart').onclick = async () => {
  const topic = $('audioTopic').value.trim();
  if (topic.length < 2) { $('audioStatus').textContent = '请写一下主题'; return; }
  $('audioStart').disabled = true;
  try {
    await audioPost('/api/audio/scripts', {topic, minutes: Number($('audioMinutes').value), style: $('audioStyle').value,
      search: $('audioSearch').checked, render: !$('audioReview').checked});
    $('audioStatus').textContent = '已开始，进度见“后台任务”';
    $('taskPanel').open = true;
    await refreshTasks();
  } catch (error) { $('audioStatus').textContent = error.message; }
  finally { $('audioStart').disabled = false; }
};
