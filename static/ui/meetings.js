// Meeting notes: upload a recording; the result shows notes, to-dos to tick into reminders, and the transcript.
UI.logic.reminderItems = rows => rows.filter(row => row.ticked).map(row => ({task: row.task, due_at: row.when ? row.when + ':00' : ''}));

function MeetingResult({task}) {
  const result = task.result;
  const [rows, setRows] = useState(() => result.proposals.map(item => ({...item, ticked: Boolean(item.due_at), when: item.due_at.slice(0, 16)})));
  const [note, setNote] = useState('');
  const [audioGone, setAudioGone] = useState(false);
  const update = (index, patch) => setRows(rows.map((row, i) => i === index ? {...row, ...patch} : row));
  const makeReminders = async () => {
    const items = UI.logic.reminderItems(rows);
    if (!items.length) { setNote('没有勾选待办'); return; }
    try {
      const {created, problems} = await api('/api/meetings/reminders', {json: {items}});
      setNote(`已建 ${created.length} 条提醒` + (problems.length ? `；未建：${problems.join('；')}` : ''));
      setRows(rows.map(row => row.ticked && !problems.some(problem => problem.startsWith(row.task.slice(0, 20))) ? {...row, ticked: false} : row));
      emit('data-changed');
    } catch (error) { setNote(error.message); }
  };
  const archive = () => api('/api/meetings/archive', {json: {task_id: task.id}}).then(saved => { setNote('已存进知识库：' + saved.name); emit('data-changed'); },
    error => setNote(error.message));
  const removeAudio = async () => {
    if (!confirm('删除这段录音？纪要和逐字稿会保留，录音删除后无法恢复。')) return;
    const response = await fetch(`/api/meetings/${result.file}`, {method: 'DELETE'});
    setNote(response.ok ? '录音已删除' : '录音已不在了'); setAudioGone(true);
  };
  return h('div', {className: 'result'},
    h('section', null, h('h3', null, '纪要'), h('div', {className: 'prose'}, result.text)),
    rows.length > 0 && h('section', null, h('h3', null, `待办（${rows.length}）`),
      rows.map((row, index) => h('div', {key: index, className: 'todo'},
        h('input', {type: 'checkbox', checked: row.ticked, onChange: event => update(index, {ticked: event.target.checked})}),
        h('span', {className: 'grow'}, row.task, (row.owner || row.due_text) && h('small', null, [row.owner, row.due_text && '原话：' + row.due_text].filter(Boolean).join(' · '))),
        h('input', {type: 'datetime-local', value: row.when, onChange: event => update(index, {when: event.target.value})}))),
      h(Button, {kind: 'primary', size: 'sm', onClick: makeReminders}, '把勾选的建为提醒')),
    result.transcript.length > 0 && h('details', null, h('summary', null, `逐字稿（${result.transcript.length} 段）`),
      h('div', {className: 'transcript'}, result.transcript.map((line, index) => h('p', {key: index}, h('span', {className: 'time'}, format.clock(line.start)), line.text)))),
    h('div', {className: 'actions'},
      h(Button, {size: 'sm', icon: 'book', onClick: archive}, '存进知识库'),
      h(Button, {size: 'sm', kind: 'ghost', icon: 'trash', disabled: audioGone, onClick: removeAudio}, '删除录音')),
    note && h('p', {className: 'status-line'}, note));
}
UI.taskDetails.meeting_notes = MeetingResult;

// Recording a meeting in the page. The recorder lives outside the page component, so switching to another page
// keeps it going; closing or reloading the tab would lose it, so the browser asks first.
const MAX_RECORDING_SECONDS = 3 * 60 * 60;
// Named meetingRecording: app.js already has a global "recording", and all these scripts share one global scope.
const meetingRecording = createStore({state: 'idle', seconds: 0, level: 0});
let media = null;
UI.logic.recordingName = (now = new Date()) => {
  const pad = n => String(n).padStart(2, '0');
  return `会议录音-${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}.webm`;
};
function warnBeforeLeaving(event) { event.preventDefault(); event.returnValue = ''; }
async function uploadMeeting(file, date) {
  const form = new FormData(); form.append('file', file); form.append('date', date);
  await api('/api/meetings', {method: 'POST', body: form});
  refreshTasks();
}
async function startMeetingRecording() {
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') { toast('这个浏览器不支持页面录音，请上传录好的文件', {tone: 'danger'}); return; }
  try {
    const device = typeof savedMic === 'function' ? savedMic() : '';
    const stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, ...(device ? {deviceId: {exact: device}} : {})}});
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : '';
    const recorder = new MediaRecorder(stream, {...(mimeType ? {mimeType} : {}), audioBitsPerSecond: 32000});
    const context = new AudioContext(), analyser = context.createAnalyser(), samples = new Float32Array(1024);
    context.createMediaStreamSource(stream).connect(analyser);
    media = {stream, recorder, context, chunks: [], startedAt: new Date()};
    recorder.ondataavailable = event => { if (event.data.size) media?.chunks.push(event.data); };
    recorder.start(10000);
    media.timer = setInterval(() => {
      const state = meetingRecording.get();
      analyser.getFloatTimeDomainData(samples);
      const level = Math.min(1, Math.sqrt(samples.reduce((sum, x) => sum + x * x, 0) / samples.length) * 8);
      meetingRecording.set({level, seconds: state.state === 'recording' ? state.seconds + 1 : state.seconds});
      if (state.seconds + 1 >= MAX_RECORDING_SECONDS) stopMeetingRecording(true);
    }, 1000);
    window.addEventListener('beforeunload', warnBeforeLeaving);
    meetingRecording.set({state: 'recording', seconds: 0, level: 0});
  } catch (error) { toast('无法打开麦克风：' + error.message, {tone: 'danger'}); }
}
function pauseMeetingRecording() {
  if (!media) return;
  if (meetingRecording.get().state === 'recording') { media.recorder.pause(); meetingRecording.set({state: 'paused'}); }
  else { media.recorder.resume(); meetingRecording.set({state: 'recording'}); }
}
async function stopMeetingRecording(keep) {
  if (!media) return;
  const current = media; media = null;
  clearInterval(current.timer);
  window.removeEventListener('beforeunload', warnBeforeLeaving);
  const finished = new Promise(resolve => { current.recorder.onstop = resolve; });
  current.recorder.state !== 'inactive' && current.recorder.stop();
  await finished;
  current.stream.getTracks().forEach(track => track.stop());
  current.context.close();
  if (!keep) { meetingRecording.set({state: 'idle', seconds: 0, level: 0}); toast('录音已丢弃'); return; }
  meetingRecording.set({state: 'uploading'});
  const file = new File(current.chunks, UI.logic.recordingName(current.startedAt), {type: 'audio/webm'});
  const pad = n => String(n).padStart(2, '0'), day = current.startedAt;
  try {
    await uploadMeeting(file, `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`);
    toast('录音已上传，正在生成纪要', {tone: 'success'});
  } catch (error) { toast(error.message + '。录音已下载到本机，可以稍后上传。', {tone: 'danger'}); saveLocally(file); }
  meetingRecording.set({state: 'idle', seconds: 0, level: 0});
}
function saveLocally(file) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(file); link.download = file.name; link.click();
}
function MeetingRecorder() {
  const state = useStore(meetingRecording);
  const active = state.state === 'recording' || state.state === 'paused';
  return h(Card, {title: '在页面录音', extra: active && h(Badge, {tone: state.state === 'recording' ? 'danger' : 'neutral'}, state.state === 'recording' ? '● 录音中' : '已暂停')},
    active ? h(Fragment, null,
      h('div', {className: 'recorder'}, h('span', {className: 'recorder-time'}, format.clock(state.seconds)),
        h('div', {className: 'recorder-level'}, h('span', {style: {width: Math.round(state.level * 100) + '%'}}))),
      h('div', {className: 'actions'},
        h(Button, {kind: 'primary', onClick: () => stopMeetingRecording(true)}, '结束并生成纪要'),
        h(Button, {onClick: pauseMeetingRecording}, state.state === 'recording' ? '暂停' : '继续'),
        h(Button, {kind: 'ghost', onClick: () => confirm('丢弃这段录音？丢弃后无法恢复。') && stopMeetingRecording(false)}, '丢弃')),
      h('p', {className: 'muted small'}, '录音期间可以切换到别的页面，但不要关闭或刷新这个标签页。最长 3 小时，到时自动结束并上传。'))
    : h(Fragment, null,
      h(Button, {kind: 'primary', icon: 'voice', disabled: state.state === 'uploading', onClick: startMeetingRecording}, state.state === 'uploading' ? '正在上传…' : '开始录音'),
      h('p', {className: 'muted small'}, '用当前选的麦克风录音（可在对话区的“麦克风设置”里换），结束后自动上传并生成纪要。请确认参会者知情。')));
}

function MeetingsPage() {
  const [file, setFile] = useState(null);
  const [date, setDate] = useState('');
  const [busy, setBusy] = useState(false);
  const input = useRef(null);
  const upload = async () => {
    if (!file) { toast('请先选择录音文件'); return; }
    const form = new FormData(); form.append('file', file); form.append('date', date);
    setBusy(true);
    try {
      await api('/api/meetings', {method: 'POST', body: form});
      toast('已开始处理，进度见下方', {tone: 'success'});
      setFile(null); if (input.current) input.current.value = '';
      refreshTasks();
    } catch (error) { toast(error.message, {tone: 'danger'}); }
    finally { setBusy(false); }
  };
  return h(Page, {title: '会议记录', subtitle: '录音在本机转写，纪要和待办两个 agent 同时处理；只有文字发给 DeepSeek'},
    h(MeetingRecorder),
    h(Card, {title: '上传会议录音'},
      h('div', {className: 'inline-form'},
        h('label', {className: 'file-pick'}, h(Icon, {name: 'upload'}), file ? file.name : '选择录音（m4a、mp3、wav…，200 MB、3 小时内）',
          h('input', {ref: input, type: 'file', accept: 'audio/*,video/mp4,.m4a,.mp3,.wav,.ogg,.opus,.webm,.aac,.flac', onChange: event => setFile(event.target.files[0] || null)})),
        h(Field, {label: '会议日期'}, h('input', {type: 'date', value: date, onChange: event => setDate(event.target.value)})),
        h(Button, {kind: 'primary', disabled: busy, onClick: upload}, busy ? '上传中…' : '上传并生成纪要')),
      h('p', {className: 'muted small'}, '请确认参会者知情。待办要你勾选确认后才会建成提醒。')),
    h(Card, {title: '会议'}, h(TaskList, {kinds: ['meeting_notes'], empty: '还没有会议记录'})));
}
UI.pages.push({id: 'meetings', group: '智能体', title: '会议记录', icon: 'meeting', component: MeetingsPage, order: 1});
