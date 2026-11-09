// Pages that only show and change data: chat, memories, reminders, knowledge, tasks, spending, settings.

// Account and service state, used by the sidebar, the settings page and the voice page.
const account = createStore({email: '', is_admin: false, city: '', status: null});
async function refreshHealth() {
  const data = await api('/api/status').catch(() => null);
  if (data) account.set({email: data.email || '', city: data.default_city ?? '', status: data});
  return data;
}
window.refreshHealth = refreshHealth;  // the voice page calls it after switching voices
UI.logic.healthLines = data => [
  ['语音识别', `${data.asr_provider || 'whisper'} · ${data.asr_ready ? '已准备' : '尚未下载，请运行 prepare_asr.py'}`],
  ['DeepSeek', `${data.llm_model} · ${data.api_key_configured ? 'Key 已载入（不代表调用一定成功）' : '请设置 DEEPSEEK_API_KEY 后重启'}`],
  ...(data.api_host ? [['接口', `${data.api_host} · Key 指纹 ${data.key_fingerprint}`]] : []),
  ['语音播报', `${data.tts_provider || 'windows'} · ${data.tts_voice}${data.tts_provider === 'edge' ? ' · 语速 ' + data.tts_rate : ''}`],
  ['可用工具', (data.tools || []).join('、') || '无'],
];

function ChatPage() {
  return h(Page, {title: '对话', subtitle: '本地识别你说的话，DeepSeek 回答，再用选定的音色读出来'},
    h(ChatSlot),
    h('p', {className: 'muted small footnote'}, '录音在本机识别；识别出的文字、最近六轮对话、长期记忆、搜索词和查到的资料片段会发给 DeepSeek。edge-tts 把回复文字发给微软在线语音服务；自定义声音用本机 CosyVoice。'));
}

// Memories and reminders: delete or cancel with an undo, and refresh when a voice command changes them.
async function changeItem({kind, action, id}) {
  const base = kind === 'reminder' ? 'reminders' : 'memories';
  await api(`/api/${base}/${id}${action === 'restore' ? '/restore' : ''}`, {method: action === 'restore' ? 'POST' : 'DELETE'});
}
function offerUndo(text, undo) {
  toast(text, undo ? {action: {label: '撤销', run: () => changeItem(undo).then(() => { toast('已撤销'); emit('data-changed'); }, error => toast(error.message, {tone: 'danger'}))}} : {});
}
window.memoryChanged = ({text, undo}) => { offerUndo(text, undo); emit('data-changed'); };

function MemoryPage() {
  const [state, reload] = useAsync(() => api('/api/memories'));
  useBus('data-changed', reload);
  const items = state.data?.memories || [];
  const remove = memory => changeItem({action: 'delete', id: memory.id}).then(() => { offerUndo('已删除：' + memory.content, {action: 'restore', id: memory.id}); reload(); },
    error => toast(error.message, {tone: 'danger'}));
  return h(Page, {title: '长期记忆', subtitle: '你明确说“记住……”时保存的事，回答时会参考'},
    h(Card, {title: `${items.length} 条记忆`},
      items.length ? h('ul', {className: 'rows'}, items.map(memory => h('li', {key: memory.id, className: 'row'},
        h(Icon, {name: 'memory'}), h('span', {className: 'grow'}, memory.content),
        h(Button, {size: 'sm', kind: 'ghost', icon: 'trash', onClick: () => remove(memory)}, '删除'))))
        : h(Empty, {icon: 'memory'}, state.loading ? '加载中…' : '还没有记忆。对助手说“记住我不吃香菜”试试。')));
}

function ReminderPage() {
  const [state, reload] = useAsync(() => api('/api/reminders'));
  useBus('data-changed', reload);
  const items = state.data?.reminders || [];
  const cancel = reminder => changeItem({kind: 'reminder', action: 'delete', id: reminder.id}).then(() => {
    offerUndo('已取消：' + reminder.content, {kind: 'reminder', action: 'restore', id: reminder.id}); reload();
  }, error => toast(error.message, {tone: 'danger'}));
  return h(Page, {title: '待办提醒', subtitle: '到时间会主动播报；页面关着时错过的，下次打开会补报'},
    h(Card, {title: `${items.length} 条待办`},
      items.length ? h('ul', {className: 'rows'}, items.map(reminder => h('li', {key: reminder.id, className: 'row'},
        h('span', {className: 'when'}, format.when(reminder.due_at)), h('span', {className: 'grow'}, reminder.content),
        h(Button, {size: 'sm', kind: 'ghost', onClick: () => cancel(reminder)}, '取消'))))
        : h(Empty, {icon: 'reminder'}, state.loading ? '加载中…' : '没有待办提醒。说“明天下午三点提醒我开会”试试。')));
}

function KnowledgePage() {
  const [state, reload] = useAsync(() => api('/api/knowledge'));
  useBus('data-changed', reload);
  const [busy, setBusy] = useState('');
  const files = state.data?.files || [];
  const upload = async list => {
    for (const file of list) {
      setBusy('正在加入：' + file.name);
      const form = new FormData(); form.append('file', file);
      try {
        const result = await api('/api/knowledge', {method: 'POST', body: form});
        toast(result.chunks ? `已加入：${result.name}，共 ${result.chunks} 段` : `${result.name} 没有提取到文字，可能是扫描件或已加密`, {tone: result.chunks ? 'success' : 'info'});
      } catch (error) { toast(error.message, {tone: 'danger'}); }
    }
    setBusy(''); reload();
  };
  const remove = async name => {
    if (!confirm(`从知识库永久删除“${name}”？删除后不能恢复。`)) return;
    try { await api(`/api/knowledge/${name.split('/').map(encodeURIComponent).join('/')}`, {method: 'DELETE'}); toast('已删除：' + name); }
    catch (error) { toast(error.message, {tone: 'danger'}); }
    reload();
  };
  return h(Page, {title: '知识库', subtitle: '你的文档和笔记，提问时只把查到的片段发给 DeepSeek'},
    h(Card, {title: '上传文件'},
      h('label', {className: 'dropzone'}, h(Icon, {name: 'upload', size: 26}),
        h('span', null, busy || '选择文件：md、txt（2 MB 内），pdf、docx（20 MB 内），可多选'),
        h('input', {type: 'file', multiple: true, accept: '.md,.txt,.pdf,.docx', onChange: event => { upload([...event.target.files]); event.target.value = ''; }})),
      h('p', {className: 'muted small'}, '提问时说“查一下我的资料”最容易用到知识库。')),
    h(Card, {title: `${files.length} 个文件`},
      files.length ? h('ul', {className: 'rows'}, files.map(file => h('li', {key: file.name, className: 'row'},
        h(Icon, {name: 'book'}), h('span', {className: 'grow'}, file.name),
        h(Badge, {tone: file.chunks ? 'neutral' : 'warn'}, file.chunks ? `${file.chunks} 段` : '未提取到文字'),
        h(Button, {size: 'sm', kind: 'ghost', icon: 'trash', onClick: () => remove(file.name)}, '删除'))))
        : h(Empty, {icon: 'book'}, state.loading ? '加载中…' : '还没有文件')));
}

function TasksPage() {
  return h(Page, {title: '后台任务', subtitle: '后台 agent 只读不写：要改动的内容会列出来，由你确认后才执行。每人同时一个任务，每天有花费上限。'},
    h(Card, null, h(TaskList, {empty: '还没有任务。会议记录、有声内容、英语对话总结都会在这里出现。'})));
}

// Spending: what the model calls and web searches cost, today, this month, per day, per agent, per account.
UI.logic.usageNotes = totals => [
  totals.incomplete && `${totals.incomplete} 次被打断的对话没有拿到用量，实际花费会略高`,
  totals.unpriced && `${totals.unpriced} 次调用的模型没有价格，未计入金额`,
].filter(Boolean);
UI.logic.usageRows = (items, key) => items.map(item => [item[key], format.yuan(item.yuan), String(item.calls - item.searches),
  String(item.searches), format.tokens(item.hit_tokens + item.miss_tokens + item.output_tokens)]);
function UsagePage() {
  const [state, reload] = useAsync(() => api('/api/usage'));
  const data = state.data;
  const headers = ['金额', '对话', '搜索', 'token'];
  return h(Page, {title: '花费', subtitle: '按 DeepSeek 官方价格估算，记录在本机。工作日 9–12、14–18 点高峰价，其余半价。以 DeepSeek 账单为准。',
    actions: h(Button, {onClick: reload}, '刷新')},
    !data ? h(Card, null, h(Empty, {icon: 'money'}, state.error || '加载中…')) : h(Fragment, null,
      h('div', {className: 'stats'},
        h(Stat, {label: '今天', value: format.yuan(data.today.yuan), hint: `${data.today.calls - data.today.searches} 次对话 · ${data.today.searches} 次搜索`}),
        h(Stat, {label: '本月', value: format.yuan(data.month.yuan), hint: `${data.month.calls - data.month.searches} 次对话 · ${data.month.searches} 次搜索`}),
        h(Stat, {label: '缓存命中', value: data.month.cache_rate === null ? '—' : Math.round(data.month.cache_rate * 100) + '%', hint: '命中的输入价格是未命中的 1/50'}),
        h(Stat, {label: '本月 token', value: format.tokens(data.month.hit_tokens + data.month.miss_tokens + data.month.output_tokens),
          hint: `输出 ${format.tokens(data.month.output_tokens)}`})),
      UI.logic.usageNotes(data.month).map(note => h('p', {key: note, className: 'notice'}, note)),
      h('div', {className: 'grid-2'},
        h(Card, {title: '最近每天'}, h(Table, {columns: ['日期', ...headers], rows: UI.logic.usageRows([...data.days].reverse(), 'day')})),
        h(Card, {title: '本月按 agent'}, h(Table, {columns: ['agent', ...headers], rows: UI.logic.usageRows(data.agents, 'agent')}))),
      data.all && h(Card, {title: `全站本月 ${format.yuan(data.all.month.yuan)}`},
        h(Table, {columns: ['账号', ...headers], rows: UI.logic.usageRows(data.all.users, 'email')}))));
}

function SettingsPage() {
  const state = useStore(account);
  const [city, setCity] = useState(null);
  useEffect(() => { refreshHealth(); }, []);
  const save = () => api('/api/settings', {json: {default_city: city ?? state.city}}).then(() => { toast('默认城市已保存', {tone: 'success'}); refreshHealth(); },
    error => toast(error.message, {tone: 'danger'}));
  return h(Page, {title: '设置', subtitle: '账号、默认城市和本机服务状态'},
    h('div', {className: 'grid-2'},
      h(Card, {title: '账号'},
        h('p', null, state.email || '…'),
        h(Button, {onClick: logout}, '退出登录')),
      h(Card, {title: '默认城市'},
        h('div', {className: 'inline-form'}, h('input', {value: city ?? state.city, maxLength: 30, placeholder: '例如：北京', onChange: event => setCity(event.target.value)}),
          h(Button, {kind: 'primary', onClick: save}, '保存')),
        h('p', {className: 'muted small'}, '查天气时没说地点就用它。'))),
    h(Card, {title: '服务状态', extra: h(Button, {size: 'sm', onClick: refreshHealth}, '重新检查')},
      state.status ? h('dl', {className: 'kv'}, UI.logic.healthLines(state.status).map(([key, value]) => h(Fragment, {key}, h('dt', null, key), h('dd', null, value))))
        : h('p', {className: 'muted'}, '正在检查…')),
    h(MetricsCard));
}
// For administrators: how the service is doing right now (recognition queue, request counts, latencies).
const METRIC_NAMES = {asr: '识别', asr_wait: '识别排队', http_api: '接口请求', reply_asr_done: '回复：识别完成', reply_first_text: '回复：第一个字',
  reply_first_audio: '回复：第一段语音', reply_llm_done: '回复：文字完成', reply_all_audio_sent: '回复：语音发完'};
UI.logic.metricRows = data => Object.entries(data.latency).map(([name, item]) => [METRIC_NAMES[name] || name, String(item.n),
  item.p50.toFixed(2) + ' 秒', item.p95.toFixed(2) + ' 秒', item.max.toFixed(2) + ' 秒']);
function MetricsCard() {
  // Only administrators get server details in /api/status; everyone else is not asked about metrics at all.
  const admin = useStore(account).status?.server_pid !== undefined;
  const [state, reload] = useAsync(() => admin ? api('/api/admin/metrics').catch(() => null) : Promise.resolve(null), [admin]);
  const data = state.data;
  if (!data) return null;
  const limited = Object.entries(data.counts).filter(([name]) => name.startsWith('limited_')).map(([name, n]) => `${name.slice(8)} ${n} 次`).join('、');
  return h(Card, {title: '运行情况（管理员）', extra: h(Button, {size: 'sm', onClick: reload}, '刷新')},
    h('div', {className: 'stats'},
      h(Stat, {label: '已运行', value: `${Math.floor(data.uptime_seconds / 3600)} 小时 ${Math.floor(data.uptime_seconds % 3600 / 60)} 分`}),
      h(Stat, {label: '识别', value: `${data.recognition.running} / ${data.recognition.slots}`, hint: `正在识别 / 同时上限；排队 ${data.recognition.waiting}`}),
      h(Stat, {label: '请求', value: String(data.counts.http_2xx || 0), hint: `出错 ${(data.counts.http_5xx || 0)} · 被拒 ${(data.counts.http_4xx || 0)}`})),
    limited && h('p', {className: 'notice'}, '触发限流：' + limited),
    h(Table, {columns: ['环节（最近 500 次）', '次数', '中位', '最慢 5%', '最慢'], rows: UI.logic.metricRows(data), empty: '还没有数据'}));
}

async function logout() { await fetch('/api/logout', {method: 'POST'}).catch(() => {}); location.replace('/'); }

UI.pages.push(
  {id: 'chat', group: '助手', title: '对话', icon: 'chat', component: ChatPage},
  {id: 'memory', group: '我的资料', title: '长期记忆', icon: 'memory', component: MemoryPage},
  {id: 'reminders', group: '我的资料', title: '待办提醒', icon: 'reminder', component: ReminderPage},
  {id: 'knowledge', group: '我的资料', title: '知识库', icon: 'book', component: KnowledgePage},
  {id: 'tasks', group: '智能体', title: '后台任务', icon: 'tasks', component: TasksPage, order: 9},
  {id: 'usage', group: '账户', title: '花费', icon: 'money', component: UsagePage},
  {id: 'settings', group: '账户', title: '设置', icon: 'settings', component: SettingsPage},
);
