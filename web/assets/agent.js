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
  let clearConversationId = '';
  let attachments = [], uploads = 0, skills = [], suggestions = [], suggestionIndex = 0;
  let attachmentPolicy = { formats: ['png','jpg','jpeg','webp','gif','pdf','xlsx','csv','txt','md','docx'], max_file_bytes: 20971520, max_files: 10 };
  const welcome = el('agentWelcome');

  function attachmentMetadata(item) {
    return { id: item.id, name: item.name, size: item.size, kind: item.kind, chunks: item.chunks || 0,
      warnings: Array.isArray(item.warnings) ? item.warnings.filter(w => typeof w === 'string') : [] };
  }
  function savedAttachments(items) {
    return (Array.isArray(items) ? items : []).filter(a => a && /^[a-f0-9]{32}$/i.test(a.id) && typeof a.name === 'string')
      .slice(0, 10).map(a => ({ ...attachmentMetadata(a), state: 'ready' }));
  }
  function linkedAttachments() {
    const linked = new Map();
    for (const record of records) {
      if (record.role === 'user' && record.attachmentsSent !== false) for (const item of record.attachments || []) linked.set(item.id, attachmentMetadata(item));
    }
    for (const item of attachments) if (item.state === 'ready') linked.set(item.id, attachmentMetadata(item));
    return [...linked.values()];
  }
  function attachmentCount() {
    return linkedAttachments().length + attachments.filter(a => a.state !== 'ready').length;
  }
  function renderAttachments() {
    el('agentAttachments').replaceChildren(...attachments.map(item => {
      const chip = node('div', `agent-attachment ${item.state}`);
      if (item.previewUrl) {
        const img = node('img', 'agent-attachment-thumb'); img.src = item.previewUrl; img.alt = ''; chip.append(img);
      } else chip.append(node('span', 'agent-attachment-icon', item.kind === 'image' ? '▧' : item.kind === 'table' ? '▦' : '▤'));
      const info = node('div', 'agent-attachment-info');
      info.append(node('span', 'agent-attachment-name', item.name));
      const status = item.state === 'uploading' ? '正在上传…' : item.state === 'error' ? item.error : `${item.kind === 'image' ? '图片' : item.kind === 'table' ? '表格' : '文档'} · ${Math.max(1, Math.round(item.size / 1024))} KB${item.warnings?.length ? ' · 有提示' : ''}`;
      const detail = node('small', '', status); info.append(detail); chip.append(info);
      chip.title = [item.name, status, ...(item.warnings || [])].join('\n');
      if (item.state === 'error' && item.file) {
        const retry = node('button', 'agent-attachment-retry', '重试'); retry.type = 'button'; retry.disabled = !!active || uploads > 0;
        retry.onclick = () => uploadAttachment(item); chip.append(retry);
      }
      const remove = node('button', 'agent-attachment-remove', '×'); remove.type = 'button'; remove.setAttribute('aria-label', `移除附件 ${item.name}`);
      remove.disabled = !!active || uploads > 0; remove.onclick = () => {
        if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
        attachments = attachments.filter(a => a !== item); renderAttachments(); updateControls(); persist();
      }; chip.append(remove); return chip;
    }));
    el('agentAttachmentHelp').hidden = !attachments.length;
  }
  async function uploadAttachment(item) {
    if (active) return;
    const conversationId = activeConversationId;
    uploads++; item.state = 'uploading'; item.error = ''; renderAttachments(); updateControls();
    try {
      const response = await fetch('/api/agent/attachments', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-File-Name': encodeURIComponent(item.name) }, body: item.file });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !result.ok) throw new Error(result.error || `上传失败（${response.status}），请重试或移除附件`);
      const valid = savedAttachments([result.attachment]);
      if (!valid.length) throw new Error('上传响应无效，请重试');
      Object.assign(item, valid[0]); item.file = null;
      el('agentLive').textContent = `${item.name} 已上传${item.warnings?.length ? '，查看附件提示后发送' : '，可以发送或继续添加附件'}`;
    } catch (err) { item.state = 'error'; item.error = err.message; el('agentLive').textContent = `${item.name}：${err.message}`; }
    finally {
      uploads--;
      if (activeConversationId === conversationId) { renderAttachments(); updateControls(); persist(); }
      if (!uploads && pendingKeyword) startPendingKeyword();
    }
  }
  async function addFiles(files) {
    if (active || uploads) { el('agentLive').textContent = '请等当前上传或分析完成后添加附件。'; return; }
    const incoming = Array.from(files), remaining = attachmentPolicy.max_files - attachmentCount();
    if (incoming.length > remaining) el('agentLive').textContent = `每个对话最多 ${attachmentPolicy.max_files} 个附件，请移除待发送附件或新建对话后继续添加。`;
    const batch = incoming.slice(0, Math.max(0, remaining)).map(file => {
      const extension = file.name.split('.').pop().toLowerCase();
      const error = !attachmentPolicy.formats.includes(extension) ? '不支持此格式，请改用图片、PDF、XLSX、CSV、TXT、MD 或 DOCX' : file.size > attachmentPolicy.max_file_bytes ? '文件超过 20 MB，请压缩或拆分后重新添加' : file.size === 0 ? '文件为空，请重新选择' : '';
      const kind = ['png','jpg','jpeg','webp','gif'].includes(extension) ? 'image' : ['xlsx','csv'].includes(extension) ? 'table' : 'document';
      return { name: file.name, size: file.size, kind, file: error ? null : file, state: error ? 'error' : 'queued', error,
        previewUrl: kind === 'image' && !error ? URL.createObjectURL(file) : null };
    });
    // Hold a batch lock between files; the service limits concurrent parsers.
    uploads++;
    attachments.push(...batch); renderAttachments(); updateControls();
    try { for (const item of batch.filter(a => a.file)) await uploadAttachment(item); }
    finally {
      uploads--; renderAttachments(); updateControls(); persist();
      if (!uploads && pendingKeyword) startPendingKeyword();
    }
  }
  function renderSkills() {
    el('agentSkills').replaceChildren(...skills.map(skill => {
      const button = node('button', 'agent-skill', skill.title); button.type = 'button';
      button.title = `/${skill.id} · ${skill.description}${skill.hint ? '\n' + skill.hint : ''}`;
      button.onclick = () => selectSkill(skill); return button;
    }));
    updateSuggestions();
  }
  function selectSkill(skill) {
    const command = `/${skill.id} `;
    input.value = /^\/[^\s]*\s?/.test(input.value) ? input.value.replace(/^\/[^\s]*\s?/, command) : command + input.value;
    input.focus(); input.setSelectionRange(input.value.length, input.value.length);
    hideSuggestions(); updateControls();
  }
  function hideSuggestions() {
    suggestions = []; el('agentSlashSuggestions').hidden = true;
    input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant');
  }
  function updateSuggestions() {
    const match = input.value.match(/^\/([^\s]*)$/);
    suggestions = match && !active ? skills.filter(s => s.id.includes(match[1].toLowerCase()) || s.title.includes(match[1])) : [];
    suggestionIndex = 0; paintSuggestions();
  }
  function paintSuggestions() {
    const list = el('agentSlashSuggestions'); list.hidden = !suggestions.length;
    input.setAttribute('aria-expanded', String(!!suggestions.length));
    list.replaceChildren(...suggestions.map((skill, index) => {
      const option = node('button', 'agent-slash-option'); option.type = 'button'; option.id = `agentSkillOption${index}`;
      option.setAttribute('role', 'option'); option.setAttribute('aria-selected', String(index === suggestionIndex)); option.tabIndex = -1;
      option.append(node('strong', '', `/${skill.id} · ${skill.title}`), node('small', '', skill.hint || skill.description));
      option.onclick = () => selectSkill(skill); return option;
    }));
    if (suggestions.length) {
      input.setAttribute('aria-activedescendant', `agentSkillOption${suggestionIndex}`);
      list.children[suggestionIndex]?.scrollIntoView?.({ block: 'nearest' });
    } else input.removeAttribute('aria-activedescendant');
  }

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
        skills = (Array.isArray(data.skills) ? data.skills : []).filter(s => s && /^[a-z][a-z0-9_-]*$/.test(s.id) && typeof s.title === 'string')
          .map(s => ({ ...s, description: typeof s.description === 'string' ? s.description : '', hint: typeof s.hint === 'string' ? s.hint : '' }));
        if (data.attachments) {
          attachmentPolicy = { ...attachmentPolicy, ...data.attachments };
          const imageFormats = ['png','jpg','jpeg','webp','gif'];
          el('agentImageInput').accept = attachmentPolicy.formats.filter(f => imageFormats.includes(f)).map(f => `.${f}`).join(',');
          el('agentFileInput').accept = attachmentPolicy.formats.filter(f => !imageFormats.includes(f)).map(f => `.${f}`).join(',');
        }
        renderSkills();
        configured = data.ok && data.configured;
        if (!configured) el('agentLive').textContent = '请在服务端配置 DeepSeek 密钥后重试。';
        else if (!data.data_available) el('agentLive').textContent = '暂无 ABA 采集数据，可以先上传附件进行分析。';
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
    const uploading = uploads > 0;
    el('agentSend').disabled = !!active || uploading || !ready || attachments.some(a => a.state !== 'ready') || (!input.value.trim() && !attachments.some(a => a.state === 'ready'));
    el('agentSend').hidden = !!active;
    el('agentStop').hidden = !active;
    const emptyCurrent = !records.length && !attachments.length;
    el('agentNew').disabled = !!active || uploading || emptyCurrent;
    el('agentNew').title = emptyCurrent ? '当前已是空白对话，可以直接输入问题' : '新建对话';
    el('agentClear').disabled = !!active || uploading || (!records.length && !attachments.length);
    el('agentConversations').disabled = !!active || uploading;
    el('agentUpload').disabled = !!active || uploading || attachmentCount() >= attachmentPolicy.max_files;
    el('agentImageUpload').disabled = el('agentUpload').disabled;
    el('agentFileInput').disabled = !!active || uploading;
    el('agentImageInput').disabled = !!active || uploading;
    document.querySelectorAll('.agent-skill').forEach(b => { b.disabled = !!active; });
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
  function isBlankConversation(conversation) {
    return (!conversation.title || conversation.title === '新对话') && !conversation.records.length && !conversation.attachments?.length;
  }
  function pruneBlankConversations() {
    const blanks = conversations.filter(isBlankConversation);
    if (blanks.length < 2) return false;
    const keep = blanks.find(c => c.id === activeConversationId) || blanks.at(-1);
    conversations = conversations.filter(c => !isBlankConversation(c) || c === keep);
    return true;
  }
  function persist() {
    try {
      const current = conversations.find(c => c.id === activeConversationId);
      if (current) current.attachments = attachments.filter(a => a.state === 'ready').map(attachmentMetadata);
      if (current) current.records = records.slice(-20).map((r) => ({ role: r.role, content: r.content.slice(0,24000), context: r.context,
        attachments: (r.attachments || []).map(attachmentMetadata),
        attachmentsSent: r.attachmentsSent,
        status: r.status === 'pending' ? 'interrupted' : r.status, note: r.note,
        sources: r.sources.map(({ id, label, week, url, summary, scope, arguments: args }) => ({ id, label, week, url, summary, scope, arguments: args })) }));
      if (pruneBlankConversations()) updateConversationSelect();
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
    attachments.forEach(a => { if (a.previewUrl) URL.revokeObjectURL(a.previewUrl); });
    const current = conversations.find(c => c.id === activeConversationId);
    records = (current?.records || []).map(r => ({ ...r, sources: [...r.sources] }));
    // Older sessions kept sent files in the composer as well as message history.
    const sentIds = new Set(records.flatMap((r, i) => r.role === 'user' && records[i + 1]?.status === 'completed'
      ? (r.attachments || []).map(a => a.id) : []));
    attachments = savedAttachments(current?.attachments).filter(a => !sentIds.has(a.id));
    feed.querySelectorAll('.agent-message').forEach(n => n.remove());
    welcome.hidden = false;
    records.forEach(r => { append(r); renderSources(r); });
    input.value = '';
    hideSuggestions(); renderAttachments();
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
    if (record.attachments?.length) {
      const files = node('div', 'agent-message-attachments');
      record.attachments.forEach(a => files.append(node('span', '', `${a.kind === 'image' ? '▧' : a.kind === 'table' ? '▦' : '▤'} ${a.name}`)));
      article.append(files);
    }
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
      const period = source.scope?.period === 'live_review_sample' ? '评论样本'
        : source.scope?.period === 'user_uploaded_files' ? '上传附件'
        : source.week ? source.week.replace('ara_', '') : '全周期';
      row.append(a, node('span', '', period));
      if (source.summary) row.append(node('div', '', source.summary));
      if (source.scope?.department) row.append(node('div', '', `类目：${source.scope.department}`));
      const focus = source.arguments?.keywords || source.arguments?.asins || (source.arguments?.asin ? [source.arguments.asin] : null);
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
    let conversation = conversations.find(c => c.id === activeConversationId && isBlankConversation(c))
      || conversations.slice().reverse().find(isBlankConversation);
    if (!conversation) {
      conversation = { id: requestId(), title, records: [], attachments: [] };
      conversations.push(conversation);
    } else conversation.title = title;
    conversations = conversations.slice(-20);
    activeConversationId = conversation.id;
    showConversation(); persist();
  }
  async function startPendingKeyword() {
    if (keywordStarting || !pendingKeyword || active || uploads) return;
    keywordStarting = true;
    try {
      if (!configured || Date.now() - statusCheckedAt > 15000) await checkStatus();
      if (!pendingKeyword || active || uploads) return;
      const keyword = pendingKeyword;
      pendingKeyword = '';
      if (records.length || input.value.trim() || attachments.length) createConversation(`${keyword.slice(0, 20)} · 关键词分析`);
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
    const selected = attachments.filter(a => a.state === 'ready').map(attachmentMetadata);
    const question = input.value.trim() || (selected.length ? '分析上传的附件' : '');
    if (active || uploads || attachments.some(a => a.state !== 'ready') || !question || !configured) return;
    hideSuggestions();
    refreshContext();
    const snapshot = structuredClone(context), prior = history();
    const linked = linkedAttachments(), pendingAttachments = attachments;
    const user = { role: 'user', content: question, context: snapshot, attachments: selected, attachmentsSent: false, sources: [], status: 'completed' };
    const reply = { role: 'assistant', content: '', context: snapshot, sources: [], status: 'pending' };
    records.push(user, reply); append(user); append(reply);
    const current = conversations.find(c => c.id === activeConversationId);
    if (current && current.title === '新对话') { current.title = question.slice(0, 28); updateConversationSelect(); }
    const run = { id: requestId(), controller: new AbortController(), stopped: false };
    active = run; input.value = ''; attachments = []; renderAttachments(); updateControls(); persist();
    el('agentLive').classList.add('busy'); el('agentLive').textContent = '正在连接研究员…';
    let timer = null, completed = false, accepted = false;
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
      const payload = { request_id: run.id, message: question, history: prior, context: snapshot, attachment_ids: linked.map(a => a.id) };
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
      accepted = true;
      user.attachmentsSent = true; persist();
      pendingAttachments.forEach(a => { if (a.previewUrl) URL.revokeObjectURL(a.previewUrl); });
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
      if (!accepted) attachments = pendingAttachments;
      reply.status = 'interrupted';
      reply.note = run.stopped || err.name === 'AbortError' ? '已停止生成。可修改问题后重新发送。' : err.message;
      el('agentLive').textContent = reply.note;
      if (!run.stopped && !input.value) input.value = question;
    } finally {
      clearTimeout(timer); paint(); active = null;
      el('agentLive').classList.remove('busy'); renderAttachments(); updateControls(); persist();
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
  input.oninput = () => { updateSuggestions(); updateControls(); };
  input.addEventListener('paste', (event) => {
    const files = Array.from(event.clipboardData?.items || []).filter(item => item.kind === 'file' && item.type.startsWith('image/')).map(item => item.getAsFile()).filter(Boolean);
    if (files.length) { event.preventDefault(); addFiles(files); }
  });
  el('agentUpload').onclick = () => el('agentFileInput').click();
  el('agentImageUpload').onclick = () => el('agentImageInput').click();
  el('agentFileInput').onchange = (event) => { addFiles(event.target.files); event.target.value = ''; };
  el('agentImageInput').onchange = (event) => { addFiles(event.target.files); event.target.value = ''; };
  input.onkeydown = (event) => {
    if (event.isComposing) return;
    if (suggestions.length) {
      if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault(); suggestionIndex = (suggestionIndex + (event.key === 'ArrowDown' ? 1 : -1) + suggestions.length) % suggestions.length; paintSuggestions(); return;
      }
      if (['Enter', 'Tab'].includes(event.key) && !event.shiftKey) { event.preventDefault(); selectSkill(suggestions[suggestionIndex]); return; }
      if (event.key === 'Escape') { event.preventDefault(); hideSuggestions(); return; }
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); }
  };
  el('agentNew').onclick = () => {
    if (active || uploads) return;
    if (!records.length && !attachments.length) { input.focus(); return; }
    pendingKeyword = '';
    createConversation(); input.focus();
  };
  el('agentClear').onclick = () => {
    if (active || uploads || (!records.length && !attachments.length) || el('agentClearDialog').open) return;
    clearConversationId = activeConversationId;
    el('agentClearDialog').showModal();
    el('agentClearCancel').focus();
  };
  function dismissClearDialog() {
    el('agentClearDialog').close(); clearConversationId = '';
    el('agentClear').focus();
  }
  el('agentClearCancel').onclick = dismissClearDialog;
  el('agentClearDialog').oncancel = event => { event.preventDefault(); dismissClearDialog(); };
  el('agentClearConfirm').onclick = () => {
    if (active || uploads || clearConversationId !== activeConversationId) { dismissClearDialog(); return; }
    dismissClearDialog();
    const current = conversations.find(c => c.id === activeConversationId);
    if (current) { current.records = []; current.attachments = []; current.title = '新对话'; }
    showConversation(); persist(); input.focus();
  };
  el('agentConversations').onchange = (event) => {
    if (active || uploads) return;
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
    // The modal owns Escape and focus traversal while it is open.
    if (el('agentClearDialog').open) return;
    if (!panel.classList.contains('open')) return;
    if (event.key === 'Escape') { event.stopImmediatePropagation(); if (suggestions.length) hideSuggestions(); else close(); }
    if (event.key === 'Tab' && mobile.matches && !(document.activeElement === input && suggestions.length && !event.shiftKey)) {
      const focusable = [...panel.querySelectorAll('button:not([disabled]):not([tabindex="-1"]),a[href],textarea,select:not([disabled]),summary')].filter(n => n.getClientRects().length);
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
          attachments: savedAttachments(c.attachments).map(attachmentMetadata),
          records: c.records.filter(r => ['user','assistant'].includes(r.role) && typeof r.content === 'string' && r.context && Array.isArray(r.sources)).slice(-20)
            .map(r => ({ ...r, attachments: savedAttachments(r.attachments).map(attachmentMetadata) })) }));
      activeConversationId = conversations.some(c => c.id === saved.activeConversationId) ? saved.activeConversationId : conversations.at(-1)?.id;
    } else {
      const previous = JSON.parse(sessionStorage.getItem('aba-research-chat-v2') || '[]');
      if (Array.isArray(previous) && previous.length) conversations = [{ id: requestId(), title: previous.find(r => r.role === 'user')?.content?.slice(0, 28) || '已保存对话',
        records: previous.filter(r => ['user','assistant'].includes(r.role) && typeof r.content === 'string' && r.context && Array.isArray(r.sources)).slice(-20) }];
      activeConversationId = conversations[0]?.id || '';
    }
  } catch (_) { conversations = []; }
  pruneBlankConversations();
  if (!conversations.length) {
    const initial = { id: requestId(), title: '新对话', records: [] };
    conversations = [initial]; activeConversationId = initial.id;
  }
  showConversation(); persist();
  refreshContext(); checkStatus();
})();
