// Shared pieces for the React pages. No build step: h() stands in for JSX.
// The voice engine (app.js, speech.js) stays plain JavaScript; its chat area is a fixed block of HTML, the
// "dock", which ChatSlot moves into whichever page shows the conversation.
const h = React.createElement;
const {useState, useEffect, useRef, useMemo, useCallback, Fragment} = React;
const UI = window.UI = {logic: {}, pages: [], taskDetails: {}};

// A tiny store: pages subscribe, anything can update.
function createStore(initial) {
  let state = initial;
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) { state = typeof patch === 'function' ? patch(state) : {...state, ...patch}; listeners.forEach(listener => listener(state)); },
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
  };
}
function useStore(store) {
  const [state, setState] = useState(store.get());
  useEffect(() => store.subscribe(setState), [store]);
  return state;
}

// API calls: JSON in and out, the server's message on failure, back to sign-in when the session is gone.
async function api(url, options = {}) {
  const init = {...options};
  if ('json' in options) {
    init.method = init.method || 'POST';
    init.headers = {'content-type': 'application/json'};
    init.body = JSON.stringify(options.json);
    delete init.json;
  }
  const response = await fetch(url, init);
  if (response.status === 401) { location.replace('/'); throw Error('请先登录'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '操作失败');
  return data;
}

// App-wide events, such as "memories changed" from a voice command.
const bus = new EventTarget();
function emit(name, detail) { bus.dispatchEvent(new CustomEvent(name, {detail})); }
function useBus(name, handler) {
  const saved = useRef(handler); saved.current = handler;
  useEffect(() => {
    const listener = event => saved.current(event.detail);
    bus.addEventListener(name, listener);
    return () => bus.removeEventListener(name, listener);
  }, [name]);
}

// Toasts: short messages in the corner, optionally with one action such as undo.
const toasts = createStore({items: []});
let toastId = 0;
function toast(text, {action = null, tone = 'info', seconds = action ? 8 : 4} = {}) {
  const id = ++toastId;
  toasts.set(state => ({items: [...state.items.slice(-3), {id, text, action, tone}]}));
  setTimeout(() => dismissToast(id), seconds * 1000);
  return id;
}
function dismissToast(id) { toasts.set(state => ({items: state.items.filter(item => item.id !== id)})); }
function Toasts() {
  const {items} = useStore(toasts);
  return h('div', {className: 'toasts', 'aria-live': 'polite'}, items.map(item => h('div', {key: item.id, className: 'toast toast-' + item.tone},
    h('span', null, item.text),
    item.action && h('button', {className: 'btn btn-ghost btn-sm', onClick: () => { dismissToast(item.id); item.action.run(); }}, item.action.label),
    h('button', {className: 'toast-close', 'aria-label': '关闭', onClick: () => dismissToast(item.id)}, '×'))));
}

// Hash routing: #/english shows the English page; the menu writes the hash.
const route = createStore({page: (location.hash.match(/^#\/([\w-]+)/) || [])[1] || 'chat'});
window.addEventListener('hashchange', () => route.set({page: (location.hash.match(/^#\/([\w-]+)/) || [])[1] || 'chat'}));
function navigate(page) { location.hash = '#/' + page; }

// Formatting shared by several pages.
const format = UI.logic.format = {
  yuan: value => '¥' + (value > 0 && value < 0.01 ? value.toFixed(4) : value.toFixed(2)),
  tokens: n => n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n),
  clock(seconds) {
    const s = Math.floor(seconds), pad = n => String(n).padStart(2, '0');
    return s >= 3600 ? `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s % 3600 / 60))}:${pad(s % 60)}` : `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
  },
  when: iso => (iso || '').slice(0, 16).replace('T', ' '),
};

// Building blocks with one look across pages.
function Icon({name, size = 18}) {
  const paths = ICONS[name] || ICONS.dot;
  return h('svg', {width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.8,
    strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true}, paths.map((d, i) => h('path', {key: i, d})));
}
const ICONS = {
  chat: ['M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z'],
  voice: ['M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z', 'M19 11a7 7 0 0 1-14 0', 'M12 18v3'],
  memory: ['M12 3l2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 15.3 7.2 17.9l.9-5.4L4.2 8.7l5.4-.8z'],
  reminder: ['M12 8v4l3 2', 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z'],
  book: ['M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z', 'M4 19V5'],
  meeting: ['M3 7h18v12H3z', 'M8 3v4M16 3v4', 'M7 12h10M7 16h6'],
  audio: ['M9 18V6l10-2v12', 'M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z', 'M19 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z'],
  english: ['M4 5h9M8.5 5v2c0 4-2 7-4.5 8', 'M6 10c1.5 2.5 4 4 7 4.5', 'M14 20l3.5-9 3.5 9M15.2 17h4.6'],
  tasks: ['M9 6h11M9 12h11M9 18h11', 'M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2'],
  money: ['M12 3v18', 'M17 7H9.5a3 3 0 0 0 0 6h5a3 3 0 0 1 0 6H6'],
  settings: ['M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z'],
  play: ['M7 4l13 8-13 8z'],
  upload: ['M12 16V4M7 9l5-5 5 5', 'M4 16v4h16v-4'],
  trash: ['M4 7h16M10 11v6M14 11v6', 'M6 7l1 13h10l1-13M9 7V4h6v3'],
  undo: ['M9 14L4 9l5-5', 'M4 9h11a5 5 0 0 1 0 10h-3'],
  dot: ['M12 12h.01'],
};
function Button({kind = 'secondary', size, icon, children, ...props}) {
  return h('button', {type: 'button', className: `btn btn-${kind}${size ? ' btn-' + size : ''}`, ...props},
    icon && h(Icon, {name: icon, size: size === 'sm' ? 15 : 17}), children);
}
function Page({title, subtitle, actions, children}) {
  return h('section', {className: 'page'},
    h('header', {className: 'page-head'}, h('div', null, h('h1', null, title), subtitle && h('p', null, subtitle)), actions && h('div', {className: 'page-actions'}, actions)),
    children);
}
function Card({title, extra, children, className = ''}) {
  return h('div', {className: 'card ' + className},
    (title || extra) && h('div', {className: 'card-head'}, h('h2', null, title), extra),
    children);
}
function Empty({icon = 'dot', children}) {
  return h('div', {className: 'empty'}, h(Icon, {name: icon, size: 28}), h('p', null, children));
}
function Badge({tone = 'neutral', children}) { return h('span', {className: 'badge badge-' + tone}, children); }
function Stat({label, value, hint}) {
  return h('div', {className: 'stat'}, h('div', {className: 'stat-label'}, label), h('div', {className: 'stat-value'}, value), hint && h('div', {className: 'stat-hint'}, hint));
}
function Field({label, children, grow}) {
  return h('label', {className: 'field' + (grow ? ' field-grow' : '')}, h('span', null, label), children);
}
function Table({columns, rows, empty = '暂无数据'}) {
  if (!rows.length) return h('p', {className: 'muted'}, empty);
  return h('div', {className: 'table-wrap'}, h('table', null,
    h('thead', null, h('tr', null, columns.map(column => h('th', {key: column}, column)))),
    h('tbody', null, rows.map((row, i) => h('tr', {key: i}, row.map((cell, j) => h('td', {key: j}, cell)))))));
}
// Where the conversation shows on a page: the dock is moved in on mount and back out on unmount, so the
// engine keeps the same elements, the same conversation and the same microphone whatever page is open.
function ChatSlot({compact = false}) {
  const holder = useRef(null);
  useEffect(() => {
    const dock = document.getElementById('chatDock');
    holder.current.append(dock);
    dock.classList.toggle('dock-compact', compact);
    dock.hidden = false;
    return () => { dock.hidden = true; document.getElementById('dockHome').append(dock); };
  }, [compact]);
  return h('div', {ref: holder, className: 'dock-slot'});
}
function useAsync(load, deps = []) {
  const [state, setState] = useState({data: null, error: '', loading: true});
  const reload = useCallback(() => load().then(data => setState({data, error: '', loading: false}),
    error => setState(previous => ({...previous, error: error.message, loading: false}))), deps);
  useEffect(() => { reload(); }, [reload]);
  return [state, reload];
}

// Background tasks, shared by the tasks page and every feature page that starts one.
const tasks = createStore({tasks: [], kinds: [], loaded: false});
let taskTimer = null;
async function refreshTasks() {
  const data = await api('/api/tasks').catch(() => null);
  if (!Array.isArray(data?.tasks)) return;
  tasks.set({tasks: data.tasks, kinds: data.kinds, loaded: true});
  for (const task of data.tasks) {
    if (!task.seen && (task.status === 'done' || task.status === 'failed')) {
      toast(`后台任务${task.status === 'done' ? '完成' : '失败'}：${task.title}`, {tone: task.status === 'done' ? 'success' : 'danger'});
      fetch(`/api/tasks/${task.id}/seen`, {method: 'POST'}).catch(() => {});
    }
  }
  clearTimeout(taskTimer);
  if (data.tasks.some(task => task.status === 'queued' || task.status === 'running')) taskTimer = setTimeout(refreshTasks, 3000);
}
UI.refreshTasks = refreshTasks;
const TASK_STATUS = {queued: ['排队中', 'neutral'], running: ['进行中', 'info'], done: ['已完成', 'success'], failed: ['失败', 'danger'], cancelled: ['已取消', 'neutral']};
UI.logic.taskPreview = task => {
  if (task.status === 'failed') return task.error;
  if (task.status === 'running' || task.status === 'queued') return task.progress;
  const result = task.result || {};
  return String(result.text || result.summary || '').slice(0, 200);
};
function TaskItem({task, open: startOpen = false}) {
  const [open, setOpen] = useState(startOpen);
  const [label, tone] = TASK_STATUS[task.status] || [task.status, 'neutral'];
  const Detail = task.status === 'done' && task.result && UI.taskDetails[task.kind];
  const active = task.status === 'queued' || task.status === 'running';
  return h('div', {className: 'task'},
    h('div', {className: 'task-head', onClick: () => setOpen(!open)},
      h('strong', null, task.title), h(Badge, {tone}, label), h('span', {className: 'muted small grow'}, format.when(task.created_at)),
      active && h(Button, {size: 'sm', onClick: event => { event.stopPropagation(); api(`/api/tasks/${task.id}/cancel`, {method: 'POST'}).then(refreshTasks).catch(error => toast(error.message, {tone: 'danger'})); }}, '取消'),
      Detail && h('span', {className: 'chevron' + (open ? ' open' : '')}, '›')),
    active && task.progress && h('div', {className: 'progress-line'}, h('span', {className: 'spinner'}), task.progress),
    (!Detail || !open) && !active && h('div', {className: 'muted small preview'}, UI.logic.taskPreview(task)),
    Detail && open && h('div', {className: 'task-body'}, h(Detail, {task})));
}
function TaskList({kinds, empty = '还没有任务'}) {
  const state = useStore(tasks);
  useEffect(() => { refreshTasks(); }, []);
  const list = state.tasks.filter(task => !kinds || kinds.includes(task.kind));
  if (!list.length) return h(Empty, {icon: 'tasks'}, state.loaded ? empty : '加载中…');
  return h('div', {className: 'task-list'}, list.map((task, index) => h(TaskItem, {key: task.id, task, open: index === 0})));
}
