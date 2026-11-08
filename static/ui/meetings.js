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
