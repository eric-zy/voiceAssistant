// Spoken content: ask for a script, read and edit it, then voice it; play, download or delete the result.
UI.logic.scriptLength = text => `${text.length} 字，约 ${Math.max(1, Math.round(text.length / 250))} 分钟`;
UI.logic.audioInfo = result => `${Math.floor(result.seconds / 60)} 分 ${result.seconds % 60} 秒 · ${(result.bytes / 1024 / 1024).toFixed(1)} MB`;

function ScriptEditor({task}) {
  const [text, setText] = useState(task.result.text);
  const [busy, setBusy] = useState(false);
  const render = async () => {
    setBusy(true);
    try { await api('/api/audio/render', {json: {text, title: task.title}}); toast('已开始合成', {tone: 'success'}); refreshTasks(); }
    catch (error) { toast(error.message, {tone: 'danger'}); setBusy(false); }
  };
  return h('div', {className: 'result'},
    h('textarea', {className: 'script', rows: 12, value: text, onChange: event => setText(event.target.value)}),
    h('div', {className: 'actions'}, h('span', {className: 'muted small grow'}, UI.logic.scriptLength(text) + '。可以直接修改，满意后再合成。'),
      h(Button, {kind: 'primary', icon: 'play', disabled: busy, onClick: render}, '用当前音色合成')));
}
function AudioPlayer({task}) {
  const url = `/api/audio/${task.result.audio}`;
  const [gone, setGone] = useState(false);
  const remove = async () => {
    if (!confirm('删除这段音频？删除后无法恢复，稿子会保留。')) return;
    const response = await fetch(url, {method: 'DELETE'});
    toast(response.ok ? '音频已删除' : '音频已不在了'); setGone(true);
  };
  return h('div', {className: 'result'},
    !gone && h('audio', {controls: true, preload: 'none', src: url, className: 'player'}),
    h('div', {className: 'actions'}, h('span', {className: 'muted small grow'}, UI.logic.audioInfo(task.result) + ' · 开头有“本内容由 AI 合成”提示'),
      !gone && h('a', {className: 'btn btn-secondary btn-sm', href: url, download: task.title + '.ogg'}, '下载'),
      h(Button, {size: 'sm', kind: 'ghost', icon: 'trash', disabled: gone, onClick: remove}, '删除音频')),
    h('details', null, h('summary', null, '稿子'), h('div', {className: 'prose'}, task.result.text)));
}
function AudioResult({task}) { return task.result.stage === 'audio' ? h(AudioPlayer, {task}) : h(ScriptEditor, {task}); }
UI.taskDetails.audio_script = AudioResult;
UI.taskDetails.audio_render = AudioResult;

function AudioPage() {
  const [form, setForm] = useState({topic: '', minutes: 3, style: '', search: false, review: true});
  const [busy, setBusy] = useState(false);
  const set = patch => setForm({...form, ...patch});
  const start = async () => {
    if (form.topic.trim().length < 2) { toast('请写一下主题'); return; }
    setBusy(true);
    try {
      await api('/api/audio/scripts', {json: {topic: form.topic.trim(), minutes: form.minutes, style: form.style, search: form.search, render: !form.review}});
      toast('已开始生成', {tone: 'success'}); refreshTasks();
    } catch (error) { toast(error.message, {tone: 'danger'}); }
    finally { setBusy(false); }
  };
  return h(Page, {title: '有声内容', subtitle: '写稿 agent 写初稿，编辑 agent 改成朗读稿，再用当前音色合成；开头会加“本内容由 AI 合成”'},
    h(Card, {title: '新的内容'},
      h('textarea', {rows: 3, maxLength: 500, value: form.topic, placeholder: '主题或大纲，例如：给孩子讲一个关于勇气的睡前故事', onChange: event => set({topic: event.target.value})}),
      h('div', {className: 'inline-form'},
        h(Field, {label: '时长'}, h('select', {value: form.minutes, onChange: event => set({minutes: Number(event.target.value)})},
          [1, 3, 5, 10].map(minutes => h('option', {key: minutes, value: minutes}, minutes + ' 分钟')))),
        h(Field, {label: '风格'}, h('input', {value: form.style, maxLength: 30, placeholder: '轻松自然', onChange: event => set({style: event.target.value})})),
        h('label', {className: 'check'}, h('input', {type: 'checkbox', checked: form.search, onChange: event => set({search: event.target.checked})}), '需要查资料'),
        h('label', {className: 'check'}, h('input', {type: 'checkbox', checked: form.review, onChange: event => set({review: event.target.checked})}), '先看稿子再合成'),
        h(Button, {kind: 'primary', disabled: busy, onClick: start}, '开始生成')),
      h('p', {className: 'muted small'}, '克隆音色在本机合成，比实时慢；edge-tts 会把稿子发给微软在线服务。')),
    h(Card, {title: '作品'}, h(TaskList, {kinds: ['audio_script', 'audio_render'], empty: '还没有作品'})));
}
UI.pages.push({id: 'audio', group: '智能体', title: '有声内容', icon: 'audio', component: AudioPage, order: 2});
