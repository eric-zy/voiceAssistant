// Free English conversation. The talk happens in the chat dock (the same input box, microphone and playback as
// the 对话 page), shown on this page. Each of your sentences is recorded when spoken and reviewed beside the
// reply: language (a correction), content (does it answer and make sense), fluency (rate and pauses, measured
// here) and pronunciation (when a scoring service is set up). Every turn is saved with its audio, so a
// conversation can be read and replayed afterwards, and any of your sentences can be said again.
const english = createStore({overview: {levels: [], sessions: [], counts: {}, level: 'intermediate'}, practicing: false,
  status: '', viewer: null, session: null, review: {due: 0, next_due_at: null, quizzes: []}, quiz: null});

// What a review says, as short labelled chips; shared by the live conversation and the record viewer.
UI.logic.reviewChips = review => {
  const chips = [];
  const pron = review?.pron;
  if (pron?.error) chips.push({label: '发音', text: '评测失败：' + pron.error, tone: 'neutral'});
  else if (pron) chips.push({label: '发音', text: `${pron.overall} 分（准确 ${pron.accuracy} · 流利 ${pron.fluency} · 完整 ${pron.completeness}）`,
    tone: pron.overall >= 80 ? 'success' : pron.overall >= 60 ? 'warn' : 'danger'});
  const weak = (pron?.words || []).filter(word => word.score < 60);
  if (weak.length) chips.push({label: '要练的词', text: weak.map(word => `${word.word}（${word.score}）`).join('、'), tone: 'danger'});
  const fluency = review?.fluency;
  if (fluency) chips.push({label: '流利度', text: `语速 ${fluency.wpm} 词/分 · 停顿 ${fluency.pauses} 次${fluency.pauses ? `（最长 ${fluency.longest_pause} 秒）` : ''}`,
    tone: fluency.wpm >= 90 && fluency.pauses <= 1 ? 'success' : 'neutral'});
  if (review?.used?.length) chips.push({label: '复习词', text: '用上了 ' + review.used.join('、') + '，算一次复习', tone: 'success'});
  const logic = review?.logic;
  if (logic) chips.push({label: '内容', text: `${logic.score}/5${logic.comment ? ' · ' + logic.comment : ''}`, tone: logic.score >= 4 ? 'success' : logic.score >= 3 ? 'warn' : 'danger'});
  return chips;
};
UI.logic.turnAudio = (sessionId, turn) => `/api/practice/sessions/${sessionId}/audio/${turn}`;

// One player for every replay button, so starting one stops the other.
const replay = (() => {
  let audio = null, queue = [], onChange = () => {};
  function stop() { if (audio) { audio.onended = null; audio.pause(); } audio = null; queue = []; onChange(null); }
  function next() {
    const item = queue.shift();
    if (!item) { stop(); return; }
    audio = new Audio(item.url); onChange(item.key);
    audio.onended = next; audio.onerror = next;
    audio.play().catch(next);
  }
  return {
    play(items, changed = () => {}) { stop(); onChange = changed; queue = [...items]; next(); },
    stop,
  };
})();

// Hear the right way to say it: the standard English voice, or the learner's own cloned voice.
UI.logic.sayText = note => note.better || note.corrected || '';
async function sayAloud(text, voice) {
  const response = await fetch('/api/practice/say', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({text, voice})});
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw Error(data.detail || '合成失败'); }
  const url = URL.createObjectURL(await response.blob());
  replay.play([{key: 'say', url}]);
}
function sayButtons(text) {
  const make = (label, voice) => {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'btn btn-ghost btn-sm say-' + voice; button.textContent = label;
    button.onclick = async () => {
      button.disabled = true; button.textContent = voice === 'mine' ? '合成中，可能要十几秒…' : '合成中…';
      try { await sayAloud(text, voice); } catch (error) { toast(error.message, {tone: 'danger'}); }
      finally { button.disabled = false; button.textContent = label; }
    };
    return button;
  };
  const row = document.createElement('div'); row.className = 'say-row';
  row.append(make('▶ 标准读法', 'standard'), make('▶ 我的声音', 'mine'));
  return row;
}

// Chinese translation of a sentence: fetched the first time it is opened, then just shown and hidden.
const translations = new Map();
async function translate(text) {
  if (!translations.has(text)) translations.set(text, api('/api/english/translate', {json: {text}}).then(data => data.zh).catch(error => { translations.delete(text); throw error; }));
  return translations.get(text);
}
function translateButton(box, text) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'btn btn-ghost btn-sm practice-zh'; button.textContent = '译';
  button.onclick = async () => {
    let shown = box.querySelector('.zh');
    if (shown) { shown.hidden = !shown.hidden; button.textContent = shown.hidden ? '译' : '收起译文'; return; }
    button.disabled = true; button.textContent = '翻译中…';
    try {
      shown = document.createElement('div'); shown.className = 'zh'; shown.textContent = await translate(text);
      box.children[1] ? box.insertBefore(shown, box.children[1].nextSibling) : box.append(shown);
      button.textContent = '收起译文';
    } catch (error) { toast(error.message, {tone: 'danger'}); button.textContent = '译'; }
    finally { button.disabled = false; }
  };
  return button;
}

// Select a word, phrase or sentence in the conversation or the record: its meaning there, how to say it,
// and it goes into the vocabulary book.
UI.lookup = (() => {
  let popup = null, last = '';
  function close() { popup?.remove(); popup = null; last = ''; }
  function host(node) {
    const element = node?.nodeType === 3 ? node.parentElement : node;
    return element?.closest?.('#history .bubble, .viewer-lines .line') || null;
  }
  function contextOf(box) { return (box.querySelector('.line-text') || box.children[1] || box).textContent.trim(); }
  function el(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
  async function show(text, context, rect) {
    close();
    last = text;
    popup = el('div', undefined, 'lookup');
    popup.setAttribute('role', 'dialog');
    const head = el('div', undefined, 'lookup-head');
    head.append(el('strong', text), el('span', '', 'lookup-phonetic'));
    const shut = el('button', '×', 'lookup-close'); shut.type = 'button'; shut.onclick = close; head.append(shut);
    const body = el('div', '查询中…', 'lookup-body');
    popup.append(head, body);
    document.body.append(popup);
    const left = Math.max(12, Math.min(rect.left, window.innerWidth - 340));
    popup.style.left = left + 'px';
    popup.style.top = (rect.bottom + 8 + 220 > window.innerHeight ? Math.max(12, rect.top - 230) : rect.bottom + 8) + 'px';
    try {
      const data = await api('/api/english/explain', {json: {text, context}});
      if (last !== text || !popup) return;
      head.querySelector('.lookup-phonetic').textContent = data.phonetic || '';
      body.replaceChildren(el('div', data.meaning, 'lookup-meaning'));
      if (data.note) body.append(el('div', data.note, 'lookup-note'));
      if (data.example) body.append(el('div', data.example, 'lookup-example'));
      body.append(sayButtons(text), el('div', '已记进生词本', 'lookup-saved'));
      emit('english-vocab');
    } catch (error) { if (popup && last === text) body.textContent = error.message; }
  }
  function onSelect(event) {
    if (event.target && popup?.contains(event.target)) return;
    setTimeout(() => {
      const selection = window.getSelection?.();
      const text = (selection?.toString() || '').replace(/\s+/g, ' ').trim();
      if (!text) { close(); return; }
      if (!location.hash.startsWith('#/english') || text === last || text.length > 200 || !/[A-Za-z]/.test(text)) return;
      const box = host(selection.anchorNode);
      if (!box || box !== host(selection.focusNode)) return;
      show(text, contextOf(box), selection.getRangeAt(0).getBoundingClientRect());
    }, 0);
  }
  let settle = null;
  function onSelectionChange() {
    clearTimeout(settle);
    settle = setTimeout(() => onSelect({target: null}), 700);
  }
  if (typeof document.addEventListener === 'function') {
    document.addEventListener('mouseup', onSelect);
    document.addEventListener('touchend', onSelect);
    if (window.matchMedia?.('(pointer: coarse)').matches) document.addEventListener('selectionchange', onSelectionChange);
    document.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
  }
  return {show, close, contextOf, onSelect};
})();

// The live conversation in the dock is plain DOM owned by the engine, so this part is not React.
UI.english = (() => {
  const bubbles = new Map();  // turn number -> bubble in the dock
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function noteCard(note) {
    const card = el('div', undefined, 'practice-note');
    card.append(el('div', `${note.category}${note.original ? '：' + note.original : ''}`, 'note-kind'), el('div', `→ ${note.corrected || note.better}`, 'note-fix'));
    if (note.better && note.better !== note.corrected) card.append(el('div', `更地道：${note.better}`));
    if (note.explain) card.append(el('div', note.explain, 'note-why'));
    if (UI.logic.sayText(note)) card.append(sayButtons(UI.logic.sayText(note)));
    return card;
  }
  function reviewCard(review) {
    const card = el('div', undefined, 'practice-review');
    for (const chip of UI.logic.reviewChips(review)) {
      const row = el('div', undefined, 'chip chip-' + chip.tone);
      row.append(el('b', chip.label), el('span', chip.text));
      card.append(row);
    }
    return card;
  }
  function tools(box) {
    let bar = box.querySelector('.turn-tools');
    if (!bar) { bar = el('div', undefined, 'turn-tools'); box.append(bar); }
    return bar;
  }
  function playButton(box, turn) {
    const session = english.get().session;
    if (!session) return;
    const button = el('button', '▶ 回放', 'btn btn-ghost btn-sm practice-play');
    button.type = 'button';
    button.onclick = () => replay.play([{key: turn, url: UI.logic.turnAudio(session, turn)}]);
    tools(box).append(button);
  }
  function redoButton(turn) {
    const button = el('button', '↺ 重说这句', 'btn btn-ghost btn-sm practice-redo');
    button.type = 'button';
    button.onclick = () => {
      if (!english.get().practicing || ws?.readyState !== 1) return;
      ws.send(JSON.stringify({type: 'practice_rewind', request_id: nextTurn(), turn}));
    };
    return button;
  }
  function markLearner(box, turn, hasAudio) {
    if (!box) return;
    bubbles.set(turn, box);
    if (hasAudio) playButton(box, turn);
    addTranslate(box);
    tools(box).append(redoButton(turn));
  }
  function addTranslate(box) {
    const text = (box.children[1]?.textContent || '').trim();
    if (text && !box.querySelector('.practice-zh')) tools(box).append(translateButton(box, text));
  }
  function offerRedo(text) { $('text').value = text; $('text').focus?.(); }
  function showEarlier(turns, notes) {
    turns.forEach((turn, index) => {
      const box = bubble(turn.who === 'tutor' ? 'assistant' : 'user', turn.text).parentElement;
      bubbles.set(index, box);
      if (turn.who === 'tutor') { if (turn.audio) playButton(box, index); addTranslate(box); return; }
      for (const note of notes.filter(item => item.turn === index)) box.append(noteCard(note));
      if (turn.review) box.append(reviewCard(turn.review));
      markLearner(box, index, Boolean(turn.audio));
    });
  }
  function before(box, node) { box.insertBefore(node, box.querySelector('.turn-tools')); }
  function handle(message) {
    if (message.type === 'practice_started') {
      bubbles.clear();
      english.set({session: message.session_id});
      const about = `${message.level}${message.topic ? '，' + message.topic : ''}`;
      if (message.turns) { showEarlier(message.turns, message.notes || []); offerRedo(message.redo); }
      english.set({practicing: true, status: message.turns ? `接着之前的对话（${about}）。要重说的那句已经放进输入框，改好后发送，或者直接说。`
        : `对话中（${about}）。用英语说或打字，卡住了可以说中文；说的话会录下来并逐句评判，点“重说这句”可以从那句重来。`
          + (message.review_words?.length ? `这次会用到复习词：${message.review_words.join('、')}，试着自己也用一用。` : '')});
      refreshEnglish();
    } else if (message.type === 'practice_turn') {
      if (message.who === 'learner') markLearner(userBubbleFor(message.request_id), message.turn, message.audio);
      else if (typeof replyBubble !== 'undefined' && replyBubble) {
        const box = replyBubble.parentElement;
        bubbles.set(message.turn, box);
        if (message.audio) playButton(box, message.turn);
        addTranslate(box);
      }
      if (message.who === 'tutor') refreshEnglish();  // the record shows a conversation from its first saved turn
    } else if (message.type === 'practice_note') {
      const target = bubbles.get(message.turn) || userBubbleFor(message.request_id);
      if (target) before(target, noteCard(message));
      else english.set({status: `改法：${message.heard} → ${message.corrected || message.better}`});
      emit('english-mistake');
    } else if (message.type === 'practice_review') {
      const target = bubbles.get(message.turn) || userBubbleFor(message.request_id);
      if (target && UI.logic.reviewChips(message).length) before(target, reviewCard(message));
    } else if (message.type === 'practice_rewound') {
      const box = bubbles.get(message.turn);
      while (box?.nextElementSibling) box.nextElementSibling.remove();
      box?.remove();
      for (const turn of [...bubbles.keys()]) if (turn >= message.turn) bubbles.delete(turn);
      offerRedo(message.text);
      english.set({status: '已回到这句之前。句子放进了输入框，改好后发送，或者直接重新说。'});
    } else if (message.type === 'practice_stopped') {
      for (const box of bubbles.values()) box.querySelectorAll('.practice-redo').forEach(button => button.remove());
      english.set({practicing: false, status: message.task_id ? `对话结束，${message.notes} 条批注已记进错题本，总结正在后台生成。可以在右侧“对话记录”里回放。` : `对话结束。${message.error}`});
      if (message.task_id) refreshTasks();
      refreshEnglish();
    }
  }
  return {handle, noteCard, reviewCard, bubbles};
})();
window.practiceMessage = UI.english.handle;

async function refreshEnglish() {
  const [overview, review] = await Promise.all([api('/api/practice').catch(() => null), api('/api/english/review').catch(() => null)]);
  if (overview) english.set({overview});
  if (review) english.set({review});
}
function startEnglish(level, topic) {
  if (!ws || ws.readyState !== 1) { toast('还没连上服务器，稍等再点'); return; }
  const rid = nextTurn(); replyActive = true; unlockAudio().catch(() => {});
  ws.send(JSON.stringify({type: 'practice_start', request_id: rid, level, topic: topic.trim()}));
}
function resumeEnglish(sessionId, turn) {
  if (!ws || ws.readyState !== 1) { toast('还没连上服务器，稍等再点'); return; }
  const rid = nextTurn(); replyActive = false; unlockAudio().catch(() => {});
  ws.send(JSON.stringify({type: 'practice_resume', request_id: rid, session_id: sessionId, turn}));
  english.set({viewer: null});
}

UI.logic.scheduleText = item => item.mastered ? '已掌握' : item.tested ? `复习第 ${item.box} 级 · 答对 ${item.correct}/${item.tested} · 下次 ${format.when(item.due_at)}` : '还没复习过';
UI.logic.resultLabel = result => result.near ? '≈ 基本对' : result.ok ? '✓ 对了' : '✗ 不对';
const QUESTION_KIND = {correct: '改错：写出正确的说法', dictation: '听写：听一遍，写下来', translate: '翻译成中文',
  cloze: '填空：填上缺的词', spell: '看中文写英文', choice: '选出正确的意思'};
UI.logic.nextReviewText = review => !review ? '' : review.mastered ? '已掌握，不再出题'
  : (() => { const days = Math.round((new Date(review.due_at) - new Date()) / 86400000); return days <= 0 ? '今天还会再考' : days === 1 ? '明天再考' : `${days} 天后再考`; })();
async function startQuiz() {
  try { english.set({quiz: {...await api('/api/english/quizzes', {method: 'POST'}), answers: {}, result: null}}); }
  catch (error) { toast(error.message); }
}
function QuestionInput({question, value, onChange, disabled}) {
  if (question.type === 'choice') return h('div', {className: 'choices'}, question.options.map((option, index) => h('label', {key: index, className: 'choice' + (value === index ? ' picked' : '')},
    h('input', {type: 'radio', name: 'q' + question.id, checked: value === index, disabled, onChange: () => onChange(index)}), option)));
  const field = question.type === 'correct' || question.type === 'translate' ? 'textarea' : 'input';
  return h(field, {value: value || '', disabled, rows: 2, placeholder: question.type === 'translate' ? '写中文' : '写英文',
    onChange: event => onChange(event.target.value), autoComplete: 'off', spellCheck: false});
}
function QuizCard() {
  const {quiz} = useStore(english);
  const [busy, setBusy] = useState(false);
  if (!quiz) return null;
  const setAnswer = (id, value) => english.set({quiz: {...quiz, answers: {...quiz.answers, [id]: value}}});
  const results = quiz.result ? Object.fromEntries(quiz.result.results.map(result => [result.id, result])) : {};
  const submit = async () => {
    setBusy(true);
    try { english.set({quiz: {...quiz, result: await api(`/api/english/quizzes/${quiz.id}/submit`, {json: {answers: quiz.answers}})}}); refreshEnglish(); emit('english-vocab'); emit('english-mistake'); }
    catch (error) { toast(error.message, {tone: 'danger'}); }
    finally { setBusy(false); }
  };
  const answered = quiz.questions.filter(question => quiz.answers[question.id] !== undefined && quiz.answers[question.id] !== '').length;
  return h(Card, {className: 'quiz', title: quiz.result ? `测验结果：${quiz.result.score} / ${quiz.result.total}` : `复习测验 · ${quiz.questions.length} 题`,
    extra: h(Button, {size: 'sm', kind: 'ghost', onClick: () => english.set({quiz: null})}, quiz.result ? '完成' : '先不做了')},
    h('ol', {className: 'questions'}, quiz.questions.map(question => {
      const result = results[question.id];
      return h('li', {key: question.id, className: 'question' + (result ? (result.ok ? ' right' : ' wrong') : '')},
        h('div', {className: 'q-kind'}, QUESTION_KIND[question.type]),
        question.prompt && h('div', {className: 'q-prompt'}, question.prompt),
        question.type === 'dictation' && h(SayButton, {text: question.say, voice: 'standard', label: '▶ 播放'}),
        question.hint && question.type !== 'dictation' && h('div', {className: 'muted small'}, (question.type === 'correct' ? '提示：' : '') + question.hint),
        h(QuestionInput, {question, value: quiz.answers[question.id], disabled: Boolean(quiz.result), onChange: value => setAnswer(question.id, value)}),
        result && h('div', {className: 'q-result'}, h('strong', null, UI.logic.resultLabel(result)),
          !result.ok && h('span', null, '  答案：' + result.expected),
          result.comment && h('div', {className: 'small'}, result.comment),
          h('div', {className: 'muted small'}, UI.logic.nextReviewText(result.review))));
    })),
    !quiz.result && h('div', {className: 'actions'}, h('span', {className: 'muted small grow'}, `已答 ${answered} / ${quiz.questions.length}，没答的算错`),
      h(Button, {kind: 'primary', disabled: busy, onClick: submit}, busy ? '批改中…' : '交卷')));
}
function QuizHistory() {
  const [state] = useAsync(() => api('/api/english/quizzes'));
  const [open, setOpen] = useState(null);
  if (open) return h(QuizReview, {id: open, onBack: () => setOpen(null)});
  const items = state.data?.quizzes || [];
  if (!items.length) return h(Empty, {icon: 'tasks'}, state.loading ? '加载中…' : '还没有做过测验。');
  return h('ul', {className: 'rows'}, items.map(item => h('li', {key: item.id, className: 'row clickable', onClick: () => setOpen(item.id)},
    h('div', {className: 'grow'}, format.when(item.created_at)),
    h(Badge, {tone: item.score === item.count ? 'success' : item.score >= item.count / 2 ? 'warn' : 'danger'}, `${item.score} / ${item.count}`),
    h('span', {className: 'chevron'}, '›'))));
}
UI.logic.answerText = (question, answer) => question.type === 'choice' ? (Number.isInteger(answer) ? question.options[answer] : '（没答）')
  : (typeof answer === 'string' && answer.trim() ? answer : '（没答）');
function QuizReview({id, onBack}) {
  const [state] = useAsync(() => api(`/api/english/quizzes/${id}`), [id]);
  const data = state.data;
  if (!data) return h('p', {className: 'muted'}, state.error || '加载中…');
  const results = Object.fromEntries(data.results.map(result => [result.id, result]));
  return h('div', null,
    h('div', {className: 'viewer-head'}, h('strong', null, `${format.when(data.created_at)} · ${data.score} / ${data.questions.length}`),
      h(Button, {size: 'sm', kind: 'ghost', onClick: onBack}, '返回')),
    h('ol', {className: 'questions'}, data.questions.map(question => {
      const result = results[question.id] || {};
      return h('li', {key: question.id, className: 'question ' + (result.ok ? 'right' : 'wrong')},
        h('div', {className: 'q-kind'}, QUESTION_KIND[question.type]),
        h('div', {className: 'q-prompt'}, question.prompt || question.say),
        h('div', {className: 'small'}, '你的答案：' + UI.logic.answerText(question, (data.answers || {})[String(question.id)])),
        h('div', {className: 'q-result'}, h('strong', null, UI.logic.resultLabel(result)), !result.ok && h('span', null, '  答案：' + result.expected),
          result.comment && h('div', {className: 'small'}, result.comment)));
    })));
}
function Translation({text}) {
  const [state, setState] = useState({open: false, zh: '', busy: false});
  const toggle = async () => {
    if (state.zh) { setState({...state, open: !state.open}); return; }
    setState({...state, busy: true});
    try { setState({open: true, zh: await translate(text), busy: false}); }
    catch (error) { toast(error.message, {tone: 'danger'}); setState({open: false, zh: '', busy: false}); }
  };
  return h('div', null, state.open && h('div', {className: 'zh'}, state.zh),
    h('button', {type: 'button', className: 'link small', disabled: state.busy, onClick: toggle}, state.busy ? '翻译中…' : state.open ? '收起译文' : '译'));
}
function VocabBook() {
  const [state, reload] = useAsync(() => api('/api/english/items'));
  useBus('english-vocab', reload);
  const items = (state.data?.items || []).filter(item => item.kind !== 'mistake');
  const act = (method, url) => fetch(url, {method}).then(() => { reload(); refreshEnglish(); });
  if (!items.length) return h(Empty, {icon: 'book'}, state.loading ? '加载中…' : '在对话里选中看不懂的单词或句子，查过的会记在这里。');
  return h('ul', {className: 'rows'}, items.map(item => h('li', {key: item.id, className: 'row vocab'},
    h('div', {className: 'grow'},
      h('div', null, h('strong', null, item.text), item.phonetic && h('span', {className: 'muted small'}, '  ' + item.phonetic)),
      item.meaning && h('div', null, item.meaning),
      item.note && h('div', {className: 'muted small'}, item.note),
      item.context && item.context !== item.text && h('div', {className: 'muted small'}, '原句：' + item.context),
      h('div', {className: 'muted small'}, UI.logic.scheduleText(item)),
      h('div', {className: 'say-row'}, h(SayButton, {text: item.text, voice: 'standard', label: '▶ 标准读法'}),
        h(SayButton, {text: item.text, voice: 'mine', label: '▶ 我的声音'}))),
    h(Button, {size: 'sm', onClick: () => act('POST', `/api/english/items/${item.id}/mastered`)}, '已掌握'),
    h(Button, {size: 'sm', kind: 'ghost', icon: 'trash', onClick: () => act('DELETE', `/api/english/items/${item.id}`)}, '删除'))));
}
function MistakeBook() {
  const [state, reload] = useAsync(() => api('/api/english/items?kind=mistake'));
  useBus('english-mistake', reload);
  const items = state.data?.items || [];
  const act = (method, url) => fetch(url, {method}).then(() => { reload(); refreshEnglish(); });
  if (!items.length) return h(Empty, {icon: 'english'}, state.loading ? '加载中…' : '对话中被指出的问题会记在这里，以后用来出测试题。');
  return h('ul', {className: 'rows'}, items.map(item => h('li', {key: item.id, className: 'row mistake'},
    h('div', {className: 'grow'}, h('div', {className: 'muted'}, item.text), h('div', {className: 'fix'}, '→ ' + item.meaning),
      item.note && h('div', {className: 'small'}, item.note),
      h('div', {className: 'muted small'}, item.created_at.slice(0, 10) + (item.seen_count > 1 ? ` · 出现 ${item.seen_count} 次` : '') + ' · ' + UI.logic.scheduleText(item))),
    h(Button, {size: 'sm', onClick: () => act('POST', `/api/english/items/${item.id}/mastered`)}, '已掌握'),
    h(Button, {size: 'sm', kind: 'ghost', icon: 'trash', onClick: () => act('DELETE', `/api/english/items/${item.id}`)}, '删除'))));
}
function SayButton({text, voice, label}) {
  const [busy, setBusy] = useState(false);
  const run = () => { setBusy(true); sayAloud(text, voice).catch(error => toast(error.message, {tone: 'danger'})).finally(() => setBusy(false)); };
  return h(Button, {size: 'sm', kind: 'ghost', disabled: busy, onClick: run}, busy ? (voice === 'mine' ? '合成中，可能要十几秒…' : '合成中…') : label);
}
function NoteView({note}) {
  const say = UI.logic.sayText(note);
  return h('div', {className: 'practice-note'}, h('div', {className: 'note-kind'}, `${note.category}${note.original ? '：' + note.original : ''}`),
    h('div', {className: 'note-fix'}, '→ ' + (note.corrected || note.better)),
    note.better && note.better !== note.corrected && h('div', null, '更地道：' + note.better),
    note.explain && h('div', {className: 'note-why'}, note.explain),
    say && h('div', {className: 'say-row'}, h(SayButton, {text: say, voice: 'standard', label: '▶ 标准读法'}), h(SayButton, {text: say, voice: 'mine', label: '▶ 我的声音'})));
}
function ReviewView({review}) {
  const chips = UI.logic.reviewChips(review);
  if (!chips.length) return null;
  return h('div', {className: 'practice-review'}, chips.map(chip => h('div', {key: chip.label, className: 'chip chip-' + chip.tone}, h('b', null, chip.label), h('span', null, chip.text))));
}
function SessionViewer({id}) {
  const [state] = useAsync(() => api(`/api/practice/sessions/${id}`), [id]);
  const [playingTurn, setPlayingTurn] = useState(null);
  useEffect(() => () => replay.stop(), []);
  const session = state.data;
  if (!session) return h('p', {className: 'muted'}, state.error || '加载中…');
  const withAudio = session.transcript.map((turn, index) => ({turn, index})).filter(item => item.turn.audio);
  const playAll = () => replay.play(withAudio.map(item => ({key: item.index, url: UI.logic.turnAudio(session.id, item.index)})), setPlayingTurn);
  const reviewed = session.transcript.filter(turn => turn.review);
  return h('div', {className: 'viewer'},
    h('div', {className: 'viewer-head'}, h('strong', null, `${format.when(session.started_at)} · ${session.level_title}${session.topic ? ' · ' + session.topic : ''}`),
      h(Button, {size: 'sm', kind: 'ghost', onClick: () => english.set({viewer: null})}, '收起')),
    h('div', {className: 'actions viewer-actions'},
      playingTurn === null ? h(Button, {size: 'sm', kind: 'primary', icon: 'play', disabled: !withAudio.length, onClick: playAll}, withAudio.length ? '连续回放' : '这次没有录音')
        : h(Button, {size: 'sm', onClick: () => replay.stop()}, '停止回放'),
      h('span', {className: 'muted small'}, `${session.transcript.filter(turn => turn.who === 'learner').length} 句 · 批注 ${session.notes.length} 条 · 评判 ${reviewed.length} 句`)),
    h('div', {className: 'viewer-lines'}, session.transcript.map((turn, index) => h('div', {key: index,
      className: 'line ' + (turn.who === 'learner' ? 'line-me' : 'line-partner') + (playingTurn === index ? ' line-playing' : '')},
      h('div', {className: 'line-who'}, turn.who === 'learner' ? '你' : '陪练', turn.seconds ? ` · ${turn.seconds} 秒` : ''), h('div', {className: 'line-text'}, turn.text),
      h(Translation, {text: turn.text}),
      turn.who === 'learner' && session.notes.filter(note => note.turn === index).map((note, i) => h(NoteView, {key: i, note})),
      turn.review && h(ReviewView, {review: turn.review}),
      h('div', {className: 'turn-tools'},
        turn.audio && h(Button, {size: 'sm', kind: 'ghost', icon: 'play', onClick: () => replay.play([{key: index, url: UI.logic.turnAudio(session.id, index)}], setPlayingTurn)}, '回放'),
        turn.who === 'learner' && h(Button, {size: 'sm', kind: 'ghost', icon: 'undo', onClick: () => resumeEnglish(session.id, index)}, '从这句重说'))))),
    session.report && h('div', {className: 'report'}, h('h3', null, '总结'), h('div', {className: 'prose'}, session.report)));
}
function SessionList() {
  const {overview, viewer} = useStore(english);
  if (viewer) return h(SessionViewer, {id: viewer});
  if (!overview.sessions.length) return h(Empty, {icon: 'chat'}, '每次对话都会逐句保存在这里（说话的句子连录音一起），可以回看、回放，也可以从任意一句重新说。');
  return h('ul', {className: 'rows'}, overview.sessions.map(item => h('li', {key: item.id, className: 'row clickable', onClick: () => english.set({viewer: item.id})},
    h('div', {className: 'grow'}, h('div', null, `${format.when(item.started_at)} · ${item.level}${item.topic ? ' · ' + item.topic : ''}`),
      h('div', {className: 'muted small'}, `说了 ${item.turns} 句 · 批注 ${item.notes} 条`)),
    h(Badge, {tone: item.ended ? (item.has_report ? 'success' : 'neutral') : 'info'}, item.ended ? (item.has_report ? '有总结' : '已结束') : '未结束'),
    h('span', {className: 'chevron'}, '›'))));
}
function EnglishPage() {
  const state = useStore(english);
  const status = useStore(account).status;
  const [level, setLevel] = useState(null);
  const [topic, setTopic] = useState('');
  const [tab, setTab] = useState('history');
  useEffect(() => { refreshEnglish(); }, []);
  const chosen = level || state.overview.level;
  const counts = state.overview.counts, vocabCount = (counts.word || 0) + (counts.phrase || 0) + (counts.sentence || 0);
  useBus('english-vocab', refreshEnglish);
  return h(Page, {title: '英语对话', subtitle: '自由聊天；你说的每句话都会录下来，并在后台评判语言、内容、流利度和发音，结束后可以回放'},
    h(Card, {className: 'english-controls'},
      h('div', {className: 'inline-form'},
        h(Field, {label: '水平'}, h('select', {value: chosen, disabled: state.practicing, onChange: event => setLevel(event.target.value)},
          state.overview.levels.map(item => h('option', {key: item.id, value: item.id}, item.title)))),
        h(Field, {label: '话题（可不填）', grow: true}, h('input', {value: topic, maxLength: 100, disabled: state.practicing, placeholder: '例如：最近看的电影', onChange: event => setTopic(event.target.value)})),
        state.practicing ? h(Button, {kind: 'danger', onClick: () => ws?.readyState === 1 && ws.send(JSON.stringify({type: 'practice_stop'}))}, '结束对话')
          : h(Button, {kind: 'primary', onClick: () => startEnglish(chosen, topic)}, '开始对话')),
      state.status && h('p', {className: 'status-line'}, state.status),
      h('div', {className: 'review-line'},
        h('span', {className: 'grow'}, state.review.due ? `今天有 ${state.review.due} 个错题和生词要复习` :
          state.review.next_due_at ? `今天的复习做完了，下一个在 ${format.when(state.review.next_due_at)}` : '对话里积累的错题和生词，会按遗忘规律安排复习'),
        state.review.quizzes[0] && h('span', {className: 'muted small'}, `上次测验 ${state.review.quizzes[0].score}/${state.review.quizzes[0].count}`),
        h(Button, {size: 'sm', kind: 'primary', disabled: !state.review.due || state.practicing || Boolean(state.quiz), onClick: startQuiz}, '开始测验')),
      status && h('p', {className: 'muted small'}, `发音评测：${status.pronunciation || '未配置'}。用“开始说话”或“持续监听”说的句子才有录音和发音、流利度评判，打字的句子只评语言和内容。每句话点“译”看中文；选中看不懂的单词或句子，会弹出意思和读法，并记进生词本。`)),
    h(QuizCard),
    h('div', {className: 'english-layout'},
      h('div', {className: 'english-talk'}, h(ChatSlot, {compact: true})),
      h(Card, {className: 'english-side'},
        h('div', {className: 'tabs'},
          h('button', {className: tab === 'history' ? 'active' : '', onClick: () => setTab('history')}, '对话记录'),
          h('button', {className: tab === 'vocab' ? 'active' : '', onClick: () => setTab('vocab')}, `生词本${vocabCount ? `（${vocabCount}）` : ''}`),
          h('button', {className: tab === 'mistakes' ? 'active' : '', onClick: () => setTab('mistakes')}, `错题本${state.overview.counts.mistake ? `（${state.overview.counts.mistake}）` : ''}`),
          h('button', {className: tab === 'quizzes' ? 'active' : '', onClick: () => setTab('quizzes')}, '测验')),
        tab === 'history' ? h(SessionList) : tab === 'vocab' ? h(VocabBook) : tab === 'quizzes' ? h(QuizHistory) : h(MistakeBook))));
}
UI.taskDetails.practice_report = ({task}) => h('div', {className: 'prose'}, task.result.text);
UI.pages.push({id: 'english', group: '智能体', title: '英语对话', icon: 'english', component: EnglishPage, order: 3});
