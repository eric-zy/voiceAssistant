// The app frame: a menu of pages grouped by purpose, the current page, and toasts.
const GROUPS = ['助手', '我的资料', '智能体', '账户'];
UI.logic.menu = pages => GROUPS.map(group => ({group, pages: pages.filter(page => page.group === group).sort((a, b) => (a.order || 0) - (b.order || 0))}))
  .filter(section => section.pages.length);

function Sidebar({current, open, onClose}) {
  const state = useStore(account);
  return h('aside', {className: 'sidebar' + (open ? ' open' : '')},
    h('div', {className: 'brand'}, h('span', {className: 'brand-mark'}, h(Icon, {name: 'voice', size: 20})), h('div', null, h('strong', null, '语音助手'), h('small', null, '本地识别 · DeepSeek'))),
    h('nav', null, UI.logic.menu(UI.pages).map(section => h('div', {key: section.group, className: 'nav-group'},
      h('div', {className: 'nav-title'}, section.group),
      section.pages.map(page => h('a', {key: page.id, href: '#/' + page.id, className: 'nav-item' + (page.id === current ? ' active' : ''), onClick: onClose},
        h(Icon, {name: page.icon}), h('span', null, page.title)))))),
    h('div', {className: 'sidebar-foot'}, h('div', {className: 'who', title: state.email}, state.email || '…'),
      h('button', {className: 'link', onClick: logout}, '退出')));
}
function App() {
  const {page} = useStore(route);
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => { refreshHealth(); refreshTasks(); }, []);
  const current = UI.pages.find(item => item.id === page) || UI.pages.find(item => item.id === 'chat');
  useEffect(() => { document.title = `${current.title} · 语音助手`; }, [current]);
  return h('div', {className: 'app'},
    h('header', {className: 'topbar'}, h('button', {className: 'menu-button', 'aria-label': '菜单', onClick: () => setMenuOpen(!menuOpen)}, '☰'),
      h('strong', null, current.title)),
    h(Sidebar, {current: current.id, open: menuOpen, onClose: () => setMenuOpen(false)}),
    menuOpen && h('div', {className: 'scrim', onClick: () => setMenuOpen(false)}),
    h('main', {className: 'content'}, h(current.component, {key: current.id})),
    h(Toasts));
}
ReactDOM.createRoot(document.getElementById('root')).render(h(App));
