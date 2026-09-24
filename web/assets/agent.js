/* Left research drawer. Only the local service talks to DeepSeek. */
(() => {
  'use strict';
  const el = (id) => document.getElementById(id);
  const panel = el('agentPanel'), feed = el('agentMessages'), input = el('agentInput');
  const storageKey = 'aba-research-chat-v3';
  const mobile = window.matchMedia('(max-width: 1099px)');
  let records = [], active = null, returnFocus = null, configured = false;
  let conversations = [], activeConversationId = '', context = {}, statusCheckedAt = 0;
  let statusPromise = null, pendingKeyword = '', keywordStarting = false;
  const welcome = el('agentWelcome');

  function node(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function safeUrl(value) {
    try {
      const url = new URL(value, location.href);
      return ['https:', 'http:'].includes(url.protocol) ? url : null;
    } catch (_) { return null; }
  }
  function inline(parent, value) {
    // Build text nodes, never evaluate HTML from a model or supplier.
    const pattern = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^\s)]+)\))/g;
    let end = 0, match;
    while ((match = pattern.exec(value))) {
      parent.append(document.createTextNode(value.slice(end, match.index)));
      if (match[2]) parent.append(node('strong', '', match[2]));
      else if (match[3]) parent.append(node('code', '', match[3]));
      else {
        const url = safeUrl(match[5]);
        const a = node(url ? 'a' : 'span', '', match[4]);
        if (url) { a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; }
        parent.append(a);
      }
      end = pattern.lastIndex;
    }
    parent.append(document.createTextNode(value.slice(end)));
  }
  function markdown(value) {
    const root = document.createDocumentFragment();
    const lines = value.replace(/\r/g, '').split('\n');
    const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((s) => s.trim());
    const separator = (line) => line && line.includes('|') && cells(line).every((s) => /^:?-{3,}:?$/.test(s));
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (!line.trim()) continue;
      if (line.startsWith('```')) {
        const code = [];
        while (++i < lines.length && !lines[i].startsWith('```')) code.push(lines[i]);
        root.append(node('pre', '', code.join('\n')));
      } else if (separator(lines[i + 1])) {
        const wrap = node('div', 'agent-table-wrap'), table = node('table');
        const header = node('tr');
        cells(line).forEach((v) => { const cell = node('th'); inline(cell, v); header.append(cell); });
        const head = node('thead'); head.append(header); table.append(head);
        const body = node('tbody'); i++;
        while (i + 1 < lines.length && lines[i + 1].trim().startsWith('|')) {
          const tr = node('tr');
          cells(lines[++i]).forEach((v) => { const td = node('td'); inline(td, v); tr.append(td); });
          body.append(tr);
        }
        table.append(body); wrap.append(table); root.append(wrap);
      } else if (/^#{1,6}\s/.test(line)) {
        const h = node(line.startsWith('###') ? 'h4' : 'h3'); inline(h, line.replace(/^#+\s*/, '')); root.append(h);
      } else if (/^\s*([-*]|\d+[.)])\s/.test(line)) {
        const ordered = /^\s*\d/.test(line), list = node(ordered ? 'ol' : 'ul');
        do {
          const item = node('li'); inline(item, lines[i].replace(/^\s*([-*]|\d+[.)])\s+/, '')); list.append(item); i++;
        } while (i < lines.length && (ordered ? /^\s*\d+[.)]\s/ : /^\s*[-*]\s/).test(lines[i]));
        i--; root.append(list);
      } else if (/^---+$/.test(line.trim())) root.append(node('hr'));
      else {
        const p = node(line.startsWith('>') ? 'blockquote' : 'p'); inline(p, line.replace(/^>\s?/, '')); root.append(p);
      }
    }
    return root;
  }
  function refreshContext() {
    context = { keyword: window.getAbaAgentContext?.()?.keyword || '' };
    updateControls();
  }
  function checkStatus() {
    if (statusPromise) return statusPromise;
    statusCheckedAt = Date.now();
    statusPromise = (async () => {
      try {
        const res = await fetch('/api/agent/status');
        if (!res.ok) throw new Error('service');
        const data = await res.json();
        configured = data.ok && data.configured && data.data_available;
        if (!configured) el('agentLive').textContent = !data.configured ? '请在服务端配置 DeepSeek 密钥后重试。' : '请先完成一次 ABA 数据采集。';
      } catch (_) {
        configured = false;
        el('agentLive').textContent = '分析服务未连接，请确认看板服务已更新并启动。';
      }
      refreshContext();
      updateControls();
    })().finally(() => { statusPromise = null; });
    return statusPromise;
  }
  function updateControls() {
    const ready = configured;
    el('agentSend').disabled = !!active || !ready || !input.value.trim();
    el('agentSend').hidden = !!active;
    el('agentStop').hidden = !active;
    el('agentNew').disabled = !!active;
    el('agentClear').disabled = !!active || !records.length;
    el('agentConversations').disabled = !!active;
    document.querySelectorAll('[data-agent-prompt]').forEach((b) => { b.disabled = !!active || !ready; });
  }
  function syncLayout() {
    const opened = panel.classList.contains('open');
    el('agentBackdrop').hidden = !opened || !mobile.matches;
    panel.setAttribute('aria-modal', String(mobile.matches));
    // Keep desktop charts interactive; isolate the modal on small screens.
    for (const target of [document.querySelector('main'), document.querySelector('.topbar'), el('drawer')]) {
      target.inert = opened && mobile.matches;
    }
    window.dispatchEvent(new Event('resize'));
  }
  function open(keyword) {
    returnFocus = document.activeElement;
    panel.inert = false;
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    document.body.classList.add('agent-is-open');
    el('agentLauncher').hidden = true;
    el('agentLauncher').setAttribute('aria-expanded', 'true');
    el('agentOpenTop').setAttribute('aria-expanded', 'true');
    refreshContext(); syncLayout(); input.focus();
    if (typeof keyword === 'string' && keyword.trim()) {
      pendingKeyword = keyword.trim().slice(0, 200);
      startPendingKeyword();
    } else if (Date.now() - statusCheckedAt > 15000) checkStatus();
  }
  function close() {
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('agent-is-open');
    el('agentLauncher').hidden = false;
    el('agentLauncher').setAttribute('aria-expanded', 'false');
    el('agentOpenTop').setAttribute('aria-expanded', 'false');
    syncLayout();
    if (returnFocus?.isConnected) returnFocus.focus();
    else el('agentLauncher').focus();
    panel.inert = true;
  }
  function persist() {
    try {
      const current = conversations.find(c => c.id === activeConversationId);
      if (current) current.records = records.slice(-20).map((r) => ({ role: r.role, content: r.content.slice(0,24000), context: r.context,
        status: r.status === 'pending' ? 'interrupted' : r.status, note: r.note,
        sources: r.sources.map(({ id, label, week, url, summary, scope, arguments: args }) => ({ id, label, week, url, summary, scope, arguments: args })) }));
      sessionStorage.setItem(storageKey, JSON.stringify({ activeConversationId, conversations: conversations.slice(-20) }));
    } catch (_) { /* Storage may be full or disabled; the current conversation remains available. */ }
  }
  function updateConversationSelect() {
    const select = el('agentConversations');
    select.replaceChildren(...conversations.slice().reverse().map(c => {
      const option = node('option', '', c.title || '新对话');
      option.value = c.id;
      return option;
    }));
    select.value = activeConversationId;
  }
  function showConversation() {
    const current = conversations.find(c => c.id === activeConversationId);
    records = (current?.records || []).map(r => ({ ...r, sources: [...r.sources] }));
    feed.querySelectorAll('.agent-message').forEach(n => n.remove());
    welcome.hidden = false;
    records.forEach(r => { append(r); renderSources(r); });
    input.value = '';
    el('agentLive').textContent = '';
    updateConversationSelect(); updateControls();
  }
  function append(record) {
    welcome.hidden = true;
    const article = node('article', `agent-message ${record.role}`);
    const meta = node('div', 'agent-message-meta');
    meta.append(node('strong', '', record.role === 'user' ? '你' : '✦ ABA 研究员'));
    const body = node('div', 'agent-message-body');
    const sources = node('details', 'agent-sources'); sources.hidden = true;
    const note = node('div', 'agent-message-note');
    article.append(meta, body, sources, note);
    if (record.role === 'assistant') {
      const copy = node('button', 'agent-copy', '复制回答'); copy.type = 'button';
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(record.content); copy.textContent = '已复制'; }
        catch (_) { copy.textContent = '复制不可用，可导出对话'; }
      };
      article.append(copy);
    }
    feed.append(article);
    record.elements = { article, body, sources, note };
    render(record);
    feed.scrollTop = feed.scrollHeight;
  }
  function render(record) {
    const { body, note } = record.elements;
    if (record.role === 'user') body.textContent = record.content;
    else body.replaceChildren(markdown(record.content || (record.status === 'pending' ? '正在读取数据…' : '')));
    note.textContent = record.note || (record.status === 'interrupted' ? '上次生成未完成，可以重新提问。' : '');
  }
  function renderSources(record) {
    const details = record.elements.sources;
    const opened = details.open;
    details.replaceChildren(node('summary', '', `已核对 ${record.sources.length} 组数据 · 查看来源`));
    details.hidden = !record.sources.length;
    record.sources.forEach((source) => {
      const row = node('div', 'agent-source'), a = node('a', '', `[${source.id}] ${source.label}`);
      const url = safeUrl(source.url);
      if (url && url.origin === location.origin) {
        a.href = url.href;
        a.onclick = async (event) => {
          event.preventDefault();
          if (active) { el('agentLive').textContent = '请等本轮完成后查看来源。'; return; }
          close();
          try { await window.navigateAbaEvidence?.(url.searchParams.get('week'), url.searchParams.get('keyword')); }
          catch (_) { location.href = url.href; }
        };
      }
      row.append(a, node('span', '', source.week ? source.week.replace('ara_', '') : '全周期'));
      if (source.summary) row.append(node('div', '', source.summary));
      if (source.scope?.department) row.append(node('div', '', `类目：${source.scope.department}`));
      const focus = source.arguments?.keywords || source.arguments?.asins;
      if (focus) row.append(node('div', '', focus.join(' · ')));
      details.append(row);
    });
    details.open = opened;
  }
  function history() {
    const items = [];
    for (let i = 0; i + 1 < records.length; i++) {
      if (records[i].role === 'user' && records[i+1].role === 'assistant' && records[i+1].status === 'completed') {
        items.push(...records.slice(i,i+2).map((r) => ({ role: r.role, content: r.content.slice(0,16000) }))); i++;
      }
    }
    const recent = items.slice(-12);
    while (recent.reduce((n,r) => n+r.content.length,0) > 45000) recent.splice(0,2);
    return recent;
  }
  function requestId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2,'0')).join('');
  }
  function createConversation(title = '新对话') {
    persist();
    const conversation = { id: requestId(), title, records: [] };
    conversations.push(conversation);
    conversations = conversations.slice(-20);
    activeConversationId = conversation.id;
    showConversation(); persist();
  }
  async function startPendingKeyword() {
    if (keywordStarting || !pendingKeyword || active) return;
    keywordStarting = true;
    try {
      if (!configured || Date.now() - statusCheckedAt > 15000) await checkStatus();
      if (!pendingKeyword || active) return;
      const keyword = pendingKeyword;
      pendingKeyword = '';
      if (records.length || input.value.trim()) createConversation(`${keyword.slice(0, 20)} · 关键词分析`);
      else {
        const current = conversations.find(c => c.id === activeConversationId);
        if (current) { current.title = `${keyword.slice(0, 20)} · 关键词分析`; updateConversationSelect(); persist(); }
      }
      input.value = `请分析亚马逊关键词 ${JSON.stringify(keyword)}。基于全部已采集周的 ABA 关键词和 ASIN 数据，研究全周期需求趋势、季节性、竞争格局、相关商品表现及选品机会与风险；给出有数据依据的结论，标注来源，并明确哪些判断缺少数据支持。`;
      updateControls();
      if (configured) send();
      else input.focus();
    } finally { keywordStarting = false; }
  }
  async function send() {
    const question = input.value.trim();
    if (active || !question || !configured) return;
    refreshContext();
    const snapshot = structuredClone(context), prior = history();
    const user = { role: 'user', content: question, context: snapshot, sources: [], status: 'completed' };
    const reply = { role: 'assistant', content: '', context: snapshot, sources: [], status: 'pending' };
    records.push(user, reply); append(user); append(reply);
    const current = conversations.find(c => c.id === activeConversationId);
    if (current && current.title === '新对话') { current.title = question.slice(0, 28); updateConversationSelect(); }
    const run = { id: requestId(), controller: new AbortController(), stopped: false };
    active = run; input.value = ''; updateControls();
    el('agentLive').classList.add('busy'); el('agentLive').textContent = '正在连接研究员…';
    let timer = null, completed = false;
    const paint = () => {
      const bottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 140;
      render(reply); if (bottom) feed.scrollTop = feed.scrollHeight; timer = null;
    };
    const receive = (event) => {
      if (event.type === 'status') el('agentLive').textContent = event.text;
      if (event.type === 'delta') {
        reply.content += event.text;
        if (!timer) timer = setTimeout(paint, 70);
      }
      if (event.type === 'source') {
        // Evidence stays in the service; persist only a small, navigable source descriptor.
        const { details, ...source } = event.source;
        reply.sources.push(source); renderSources(reply);
      }
      if (event.type === 'done') {
        completed = true; reply.content = event.text; reply.status = 'completed'; reply.note = event.note || '';
        el('agentLive').textContent = event.incomplete ? '本次回答未完整输出，可继续追问' : '分析完成 · 可继续追问';
      }
      if (event.type === 'error') throw new Error(event.message);
    };
    try {
      const payload = { request_id: run.id, message: question, history: prior, context: snapshot };
      let encoded = JSON.stringify(payload);
      while (payload.history.length && new TextEncoder().encode(encoded).length > 120 * 1024) {
        payload.history.splice(0,2); encoded = JSON.stringify(payload);
      }
      const response = await fetch('/api/agent/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: encoded, signal: run.controller.signal });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.error || `分析服务返回 ${response.status}`);
      }
      if (!response.body) throw new Error('当前浏览器不支持流式响应');
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let pos;
        while ((pos = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0,pos); buffer = buffer.slice(pos+1);
          if (line.trim()) receive(JSON.parse(line));
        }
        if (done) { if (buffer.trim()) receive(JSON.parse(buffer)); break; }
      }
      if (!completed) throw new Error('连接已中断，请重试本轮问题');
    } catch (err) {
      reply.status = 'interrupted';
      reply.note = run.stopped || err.name === 'AbortError' ? '已停止生成。可修改问题后重新发送。' : err.message;
      el('agentLive').textContent = reply.note;
      if (!run.stopped && !input.value) input.value = question;
    } finally {
      clearTimeout(timer); paint(); active = null;
      el('agentLive').classList.remove('busy'); updateControls(); persist();
      if (pendingKeyword) startPendingKeyword();
    }
  }
  function stop() {
    if (!active) return;
    active.stopped = true;
    active.controller.abort();
    fetch('/api/agent/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: active.id }), keepalive: true }).catch(() => {});
  }
  el('agentLauncher').onclick = () => open();
  el('agentOpenTop').onclick = () => open();
  el('agentClose').onclick = close;
  el('agentBackdrop').onclick = close;
  el('agentStop').onclick = stop;
  el('agentForm').onsubmit = (event) => { event.preventDefault(); send(); };
  input.oninput = updateControls;
  input.onkeydown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); }
  };
  el('agentNew').onclick = () => {
    if (active) return;
    pendingKeyword = '';
    createConversation(); input.focus();
  };
  el('agentClear').onclick = () => {
    if (active || !records.length || !window.confirm('清除当前对话的所有消息？')) return;
    const current = conversations.find(c => c.id === activeConversationId);
    if (current) { current.records = []; current.title = '新对话'; }
    showConversation(); persist(); input.focus();
  };
  el('agentConversations').onchange = (event) => {
    if (active) return;
    persist(); activeConversationId = event.target.value;
    showConversation(); persist(); input.focus();
  };
  document.querySelectorAll('[data-agent-prompt]').forEach((button) => {
    button.onclick = () => { input.value = button.dataset.agentPrompt; updateControls(); send(); };
  });
  el('agentExport').onclick = () => {
    const content = '# ABA 研究对话\n\n' + records.map((r) => `## ${r.role === 'user' ? '问题' : '分析'}\n\n${r.content}\n\n${r.note || ''}\n\n` +
      r.sources.map(s => `- [${s.id}] ${s.label} · ${s.week} · ${new URL(s.url, location.href).href}`).join('\n')).join('\n\n');
    const a = node('a'); a.href = URL.createObjectURL(new Blob([content], {type:'text/markdown;charset=utf-8'}));
    a.download = `ABA研究-${new Date().toISOString().slice(0,10)}.md`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  document.addEventListener('keydown', (event) => {
    if (!panel.classList.contains('open')) return;
    if (event.key === 'Escape') { event.stopImmediatePropagation(); close(); }
    if (event.key === 'Tab' && mobile.matches) {
      const focusable = [...panel.querySelectorAll('button:not([disabled]),a[href],textarea,summary')].filter(n => n.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length-1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }, true);
  mobile.addEventListener('change', syncLayout);
  window.addEventListener('aba-context-change', refreshContext);
  window.addEventListener('pagehide', stop);
  window.abaAgent = { open };
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    if (saved && Array.isArray(saved.conversations)) {
      conversations = saved.conversations.filter(c => c && typeof c.id === 'string' && Array.isArray(c.records))
        .slice(-20).map(c => ({ id: c.id, title: typeof c.title === 'string' ? c.title.slice(0, 28) : '新对话',
          records: c.records.filter(r => ['user','assistant'].includes(r.role) && typeof r.content === 'string' && r.context && Array.isArray(r.sources)).slice(-20) }));
      activeConversationId = conversations.some(c => c.id === saved.activeConversationId) ? saved.activeConversationId : conversations.at(-1)?.id;
    } else {
      const previous = JSON.parse(sessionStorage.getItem('aba-research-chat-v2') || '[]');
      if (Array.isArray(previous) && previous.length) conversations = [{ id: requestId(), title: previous.find(r => r.role === 'user')?.content?.slice(0, 28) || '已保存对话',
        records: previous.filter(r => ['user','assistant'].includes(r.role) && typeof r.content === 'string' && r.context && Array.isArray(r.sources)).slice(-20) }];
      activeConversationId = conversations[0]?.id || '';
    }
  } catch (_) { conversations = []; }
  if (!conversations.length) {
    const initial = { id: requestId(), title: '新对话', records: [] };
    conversations = [initial]; activeConversationId = initial.id;
  }
  showConversation(); persist();
  refreshContext(); checkStatus();
})();
