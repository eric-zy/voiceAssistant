// Voices: pick the reply voice, preview it, upload a sample for a cloned voice.
// The flows live in createVoiceActions(ui) so tests can drive them with a stand-in ui; they use the engine's
// own playback (ws, nextTurn, player, playing) because a preview must stop like any other reply.
UI.logic.voiceChoices = data => [
  ['edge:zh-CN-XiaoxiaoNeural', '晓晓女声 · 在线'],
  ['edge:zh-CN-YunxiNeural', '云希男声 · 在线'],
  ['windows:Microsoft Huihui Desktop', '慧慧 · 本地 Windows'],
  ...data.profiles.map(profile => ['cosyvoice:' + profile.id, profile.name + ' · 自定义声音']),
];
UI.logic.voiceServiceText = data => {
  const device = data.clone_device === 'cuda' ? 'GPU' : data.clone_device === 'cpu' ? 'CPU' : '未知设备';
  return data.clone_service_ready ? `克隆服务：${device} · ${data.clone_service_busy ? '上一条合成仍在运行，请稍后重试' : '空闲，可以试听'}。`
    : '本机克隆服务未就绪。晓晓、云希仍可使用；克隆声音需要先运行 start_cosyvoice.ps1。';
};
UI.logic.createVoiceActions = ui => {
  async function load(preferred) {
    const data = await api('/api/voices');
    ui.setChoices(UI.logic.voiceChoices(data), preferred || (data.provider === 'cosyvoice' ? 'cosyvoice:' + data.profile_id : data.provider + ':' + data.voice));
    ui.status(UI.logic.voiceServiceText(data));
    return data;
  }
  async function apply() {
    try {
      if (ws.readyState !== WebSocket.OPEN) throw Error('请先等待聊天连接就绪');
      closeRecording(); nextTurn();
      const value = ui.selected(), separator = value.indexOf(':'), provider = value.slice(0, separator), name = value.slice(separator + 1);
      if (!provider) throw Error('请选择音色');
      await api('/api/voice/select', {json: {provider, voice: provider === 'cosyvoice' ? 'custom' : name, profile_id: provider === 'cosyvoice' ? name : ''}});
      await refreshHealth();
      const info = await load();
      if (provider !== 'cosyvoice') ui.status('已应用；可以点击试听。');
      else if (!info.clone_service_ready) ui.status('音色已保存，但克隆服务尚未就绪；请先运行 start_cosyvoice.ps1。');
      else if (info.clone_service_busy) ui.status('音色已应用；上一条合成尚未结束，请稍后再试听。');
      else ui.status(`音色已应用，使用${info.clone_device === 'cuda' ? 'GPU' : 'CPU'}合成。可以点击试听。`);
    } catch (error) { ui.status(error.message); }
  }
  async function upload({file, name, transcript}) {
    ui.setUploading(true);
    try {
      if (!file) throw Error('请选择参考录音');
      if (file.size > 10 * 1024 * 1024) throw Error('参考录音不能超过 10MB');
      const form = new FormData(); form.append('name', name); form.append('prompt_text', transcript); form.append('audio', file);
      ui.status('正在转换并保存参考录音…');
      const data = await api('/api/voices', {method: 'POST', body: form});
      await load('cosyvoice:' + data.id);
      ui.status('参考音色已保存在本机。请点击“应用音色”，克隆服务启动后可以试听。');
      return true;
    } catch (error) { ui.status(error.message); return false; }
    finally { ui.setUploading(false); }
  }
  async function preview() {
    ui.setPreviewing(true); ui.setCancellable(true);
    ui.status('正在准备试听…');
    let timer, deadline, rid, timedOut = false;
    try {
      if (ws.readyState !== WebSocket.OPEN) throw Error('请先等待聊天连接就绪');
      closeRecording(); rid = nextTurn();
      window.voicePreviewController = new AbortController();
      // Playback is unlocked inside the click, before any network wait, or the browser refuses it.
      let audioTimer;
      try {
        await Promise.race([unlockAudio(), new Promise((_, reject) => { audioTimer = setTimeout(() => reject(Error('浏览器未允许播放，请再点击试听或检查站点声音权限')), 5000); })]);
      } finally { clearTimeout(audioTimer); }
      if (rid !== requestId) return;
      const info = await (await fetch('/api/voices', {signal: window.voicePreviewController.signal})).json();
      if (info.provider === 'cosyvoice' && !info.clone_service_ready) throw Error('克隆服务未就绪，请运行 start_cosyvoice.ps1');
      if (info.provider === 'cosyvoice' && info.clone_service_busy) throw Error('上一条合成仍在运行，请稍后再试听；取消等待不一定立即终止模型计算。');
      if (rid !== requestId) return;
      const started = Date.now();
      const progress = () => ui.status(`正在合成试听，已等待 ${Math.floor((Date.now() - started) / 1000)} 秒${info.provider === 'cosyvoice' ? (info.clone_device === 'cuda' ? ' · GPU 推理' : ' · CPU 较慢，可能需要一分钟以上') : ''}。可以点击“停止试听”。`);
      progress(); timer = setInterval(progress, 1000);
      deadline = setTimeout(() => { timedOut = true; window.voicePreviewController.abort(); }, 120000);
      const response = await fetch('/api/voice/preview', {method: 'POST', signal: window.voicePreviewController.signal});
      if (!response.ok) { const error = await response.json(); throw Error(error.detail || '试听失败'); }
      const data = await response.arrayBuffer();
      if (rid !== requestId) return;
      const buffer = await player.decodeAudioData(data);
      if (rid !== requestId) return;
      const source = player.createBufferSource(); source.buffer = buffer; source.connect(player.destination);
      clearInterval(timer); clearTimeout(deadline);
      playing.add(source);
      source.onended = () => { playing.delete(source); if (rid === requestId) { ui.status('试听播放完毕'); ui.setCancellable(false); } };
      source.start();
      ui.status('正在播放当前音色。可以点击“停止试听”。');
    } catch (error) {
      if (rid === undefined || rid === requestId) ui.status(timedOut ? '试听超过 120 秒，已停止等待。请检查服务状态或暂用晓晓、云希。' : error.name === 'AbortError' ? '试听已取消' : error.message);
    } finally {
      clearInterval(timer); clearTimeout(deadline); ui.setPreviewing(false); ui.setCancellable(playing.size > 0);
    }
  }
  function cancel() {
    if (ws.readyState === WebSocket.OPEN) nextTurn(); else stopPlayback();
    ui.status('试听已停止；已开始的克隆计算可能仍需结束。');
    ui.setCancellable(false);
  }
  return {load, apply, upload, preview, cancel};
};

function VoicesPage() {
  const [status, setStatus] = useState('正在检查克隆服务…');
  const [choices, setChoices] = useState([]);
  const [selected, setSelected] = useState('');
  const [previewing, setPreviewing] = useState(false);
  const [cancellable, setCancellable] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [form, setForm] = useState({name: '', transcript: '', file: null});
  const selectedRef = useRef(''); selectedRef.current = selected;
  const actions = useMemo(() => UI.logic.createVoiceActions({
    status: setStatus, selected: () => selectedRef.current, setPreviewing, setCancellable, setUploading,
    setChoices: (list, value) => { setChoices(list); setSelected(value); },
  }), []);
  useEffect(() => { actions.load().catch(error => setStatus(error.message)); }, []);
  const custom = choices.filter(([value]) => value.startsWith('cosyvoice:'));
  return h(Page, {title: '音色', subtitle: '选择回复用的声音；自己的声音可以上传一段录音来克隆'},
    h(Card, {title: '当前音色'},
      h('div', {className: 'voice-grid'}, choices.map(([value, label]) => h('button', {key: value, type: 'button',
        className: 'voice-option' + (value === selected ? ' selected' : ''), onClick: () => setSelected(value)},
        h(Icon, {name: value.startsWith('cosyvoice:') ? 'voice' : 'audio'}), h('span', null, label)))),
      h('div', {className: 'actions'},
        h(Button, {kind: 'primary', onClick: actions.apply}, '应用音色'),
        h(Button, {icon: 'play', disabled: previewing, onClick: actions.preview}, '试听'),
        h(Button, {disabled: !cancellable, onClick: actions.cancel}, '停止试听'),
        h(Button, {kind: 'ghost', onClick: () => actions.load().catch(error => setStatus(error.message))}, '检查服务')),
      h('p', {className: 'status-line', role: 'status'}, status)),
    h(Card, {title: '上传自定义声音', extra: custom.length ? h(Badge, null, `已有 ${custom.length} 个`) : null},
      h('p', {className: 'muted small'}, '使用自己或已获授权的录音。建议 10 秒左右的单人清晰声音，支持 3～30 秒、最大 10MB。录音和文字保存在本机，只发给本机克隆服务。'),
      h('div', {className: 'form-grid'},
        h(Field, {label: '音色名称'}, h('input', {value: form.name, maxLength: 60, placeholder: '例如：我的声音', onChange: event => setForm({...form, name: event.target.value})})),
        h(Field, {label: '参考录音'}, h('input', {type: 'file', accept: 'audio/*', onChange: event => setForm({...form, file: event.target.files[0] || null})})),
        h(Field, {label: '录音里说了什么', grow: true}, h('textarea', {rows: 3, maxLength: 2000, value: form.transcript, placeholder: '准确填写录音说的话，不能填写要合成的新内容',
          onChange: event => setForm({...form, transcript: event.target.value})}))),
      h(Button, {kind: 'primary', icon: 'upload', disabled: uploading, onClick: () => actions.upload(form).then(saved => saved && setForm({name: '', transcript: '', file: null}))}, '保存参考音色')));
}
UI.pages.push({id: 'voices', group: '助手', title: '音色', icon: 'voice', component: VoicesPage});
