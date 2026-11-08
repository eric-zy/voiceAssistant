// Spend panel: what this account's DeepSeek calls and web searches cost, from the server's local ledger.
function yuan(value) { return '¥' + (value < 0.01 && value > 0 ? value.toFixed(4) : value.toFixed(2)); }
function tokens(n) { return n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n); }
function usageLine(t) {
  const parts = [`${yuan(t.yuan)}`, `${t.calls - t.searches} 次对话`, `${t.searches} 次搜索`,
    `输入 ${tokens(t.hit_tokens + t.miss_tokens)}（缓存命中 ${t.cache_rate === null ? '—' : Math.round(t.cache_rate * 100) + '%'}）`,
    `输出 ${tokens(t.output_tokens)}`];
  return parts.join(' · ');
}
function usageNotes(t) {
  const notes = [];
  if (t.incomplete) notes.push(`${t.incomplete} 次被打断的对话没有拿到用量，实际花费会略高`);
  if (t.unpriced) notes.push(`${t.unpriced} 次调用的模型没有价格，未计入金额`);
  return notes;
}
function usageTable(headers, rows) {
  const table = document.createElement('table');
  table.style.cssText = 'width:100%;border-collapse:collapse;font-size:14px;margin-top:8px';
  const head = table.insertRow();
  for (const text of headers) { const th = document.createElement('th'); th.textContent = text; th.style.textAlign = 'left'; head.append(th); }
  for (const row of rows) { const tr = table.insertRow(); for (const text of row) tr.insertCell().textContent = text; }
  return table;
}
function usageRows(items, key) {
  return items.map(item => [item[key], yuan(item.yuan), String(item.calls - item.searches), String(item.searches), tokens(item.hit_tokens + item.miss_tokens + item.output_tokens)]);
}
const USAGE_HEADERS = ['', '金额', '对话', '搜索', 'token'];
async function refreshUsage() {
  const response = await fetch('/api/usage');
  if (!response.ok) { $('usageBody').textContent = '读取花费失败'; return; }
  const data = await response.json();
  const parts = [];
  const line = (label, totals) => { const p = document.createElement('p'); p.style.margin = '6px 0'; p.textContent = `${label}：${usageLine(totals)}`; return p; };
  parts.push(line('今天', data.today), line('本月', data.month));
  for (const note of usageNotes(data.month)) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = note; parts.push(p); }
  if (data.days.length) parts.push(usageTable(['日期', ...USAGE_HEADERS.slice(1)], usageRows([...data.days].reverse(), 'day')));
  if (data.agents.length) parts.push(usageTable(['本月按 agent', ...USAGE_HEADERS.slice(1)], usageRows(data.agents, 'agent')));
  if (data.all) {
    parts.push(line('全站本月', data.all.month));
    parts.push(usageTable(['本月按账号', ...USAGE_HEADERS.slice(1)], usageRows(data.all.users, 'email')));
  }
  $('usageBody').replaceChildren(...parts);
}
$('usagePanel').addEventListener('toggle', () => { if ($('usagePanel').open) refreshUsage().catch(() => {}); });
$('usageRefresh').onclick = () => refreshUsage().catch(() => {});
