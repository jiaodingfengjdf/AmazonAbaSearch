const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/assets/agent.js'), 'utf8').replace(/\r\n/g, '\n');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.value = ''; this.hidden = false; this.attrs = {}; this.listeners = {}; this.classList = { add() {}, remove() {}, contains: () => false }; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(k, v) { this.listeners[k] = v; }
  querySelectorAll() { return []; }
  focus() {}
  click() { this.clicked = true; }
  setSelectionRange() {}
  scrollIntoView() {}
}
async function setup(extraFetch, saved = '') {
  const elements = new Map(); const writes = new Map(saved ? [['aba-research-chat-v3', saved]] : []); const calls = [];
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const context = vm.createContext({
    document: { getElementById: get, createElement: tag => new Element(tag), createDocumentFragment: () => new Element(), createTextNode: text => ({ textContent: text }), querySelector: () => new Element(), querySelectorAll: () => [], body: new Element(), addEventListener() {} },
    window: { matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, dispatchEvent() {}, getAbaAgentContext: () => ({ keyword: 'test' }), confirm: () => true },
    sessionStorage: { getItem: k => writes.get(k), setItem: (k,v) => writes.set(k,v) },
    location: { href: 'http://localhost/', origin: 'http://localhost' }, crypto: require('node:crypto').webcrypto,
    fetch: async (url, options) => { calls.push({ url, options }); if (url.endsWith('/status')) return { ok: true, json: async () => ({ ok: true, configured: true, data_available: true, skills: [{ id: 'market', title: '市场研究', description: '趋势与竞争', hint: '填写关键词或 ASIN' }] }) }; return extraFetch(url, options); },
    URL, Blob, AbortController, TextEncoder, TextDecoder, structuredClone, setTimeout, clearTimeout, Event, navigator: {}, console,
  });
  const instrumented = source.replace('  showConversation(); persist();\n  refreshContext(); checkStatus();', `  window.testing = { addFiles, send, createConversation, selectSkill, checkStatus, persist, getAttachments: () => attachments, getSkills: () => skills, getRecords: () => records, getUploads: () => uploads };\n  showConversation(); persist();\n  refreshContext(); checkStatus();`);
  vm.runInContext(instrumented, context);
  await context.window.testing.checkStatus();
  return { api: context.window.testing, get, calls, writes, context };
}
const attachment = { id: 'a'.repeat(32), name: 'report.csv', size: 80, kind: 'table', chunks: 1, warnings: [] };
const uploadResponse = { ok: true, json: async () => ({ ok: true, attachment }) };
const chatResponse = () => new Response(JSON.stringify({ type: 'done', text: '分析完成' }) + '\n');
function file(name = 'report.csv', size = 80) { return { name, size, type: 'text/csv' }; }

test('skills select on click and slash Enter/Tab without sending prematurely', async () => {
  const s = await setup(() => { throw new Error('unexpected send'); });
  assert.equal(s.get('agentSkills').children.length, 1);
  s.get('agentSkills').children[0].onclick();
  assert.equal(s.get('agentInput').value, '/market ');
  for (const key of ['Enter', 'Tab']) {
    s.get('agentInput').value = '/ma'; s.get('agentInput').oninput();
    let prevented = false;
    s.get('agentInput').onkeydown({ key, preventDefault: () => { prevented = true; } });
    assert.equal(prevented, true); assert.equal(s.get('agentInput').value, '/market ');
  }
  assert.equal(s.calls.filter(c => c.url.endsWith('/chat')).length, 0);
});

test('file-only send uses default message, follow-up retains IDs, metadata restores without blobs', async () => {
  const s = await setup(url => url.endsWith('/attachments') ? uploadResponse : chatResponse());
  await s.api.addFiles([file()]);
  assert.equal(s.get('agentSend').disabled, false);
  await s.api.send();
  let payload = JSON.parse(s.calls.find(c => c.url.endsWith('/chat')).options.body);
  assert.equal(payload.message, '分析上传的附件'); assert.deepEqual(payload.attachment_ids, [attachment.id]);
  s.get('agentInput').value = '继续分析之前的表格'; await s.api.send();
  payload = JSON.parse(s.calls.filter(c => c.url.endsWith('/chat')).at(-1).options.body);
  assert.deepEqual(payload.attachment_ids, [attachment.id]);
  const persisted = s.writes.get('aba-research-chat-v3');
  assert.equal(persisted.includes('"file"'), false); assert.equal(persisted.includes('previewUrl'), false);
  const restored = await setup(() => chatResponse(), persisted);
  assert.equal(restored.api.getAttachments()[0].name, 'report.csv');
  assert.equal(restored.api.getRecords()[0].attachments[0].id, attachment.id);
  restored.get('agentAttachments').children[0].children.at(-1).onclick();
  restored.get('agentInput').value = '按 ABA 分析'; await restored.api.send();
  assert.deepEqual(JSON.parse(restored.calls.find(c => c.url.endsWith('/chat')).options.body).attachment_ids, []);
});

test('upload locks conversation switches and blocks send; failed upload retries', async () => {
  let release, count = 0;
  const s = await setup(() => ++count === 1 ? new Promise(resolve => { release = resolve; }) : uploadResponse);
  const pending = s.api.addFiles([file()]);
  assert.ok(s.api.getUploads() > 0); assert.equal(s.get('agentNew').disabled, true);
  assert.equal(s.get('agentConversations').disabled, true); assert.equal(s.get('agentSend').disabled, true);
  release({ ok: false, json: async () => ({ error: '连接中断，请重试' }) }); await pending;
  assert.equal(s.api.getAttachments()[0].state, 'error'); assert.equal(s.get('agentSend').disabled, true);
  await s.get('agentAttachments').children[0].children.at(-2).onclick();
  assert.equal(s.api.getAttachments()[0].state, 'ready'); assert.equal(s.get('agentSend').disabled, false);
});

test('invalid format and oversize files explain errors and do not upload', async () => {
  const s = await setup(() => { throw new Error('unexpected upload'); });
  await s.api.addFiles([file('bad.exe'), file('large.pdf', 20971521)]);
  assert.equal(s.api.getAttachments().length, 2); assert.equal(s.get('agentSend').disabled, true);
  assert.match(s.api.getAttachments()[0].error, /不支持/); assert.match(s.api.getAttachments()[1].error, /20 MB/);
  assert.equal(s.calls.filter(c => c.url.endsWith('/attachments')).length, 0);
});

test('failed chat retains selected attachments for retry', async () => {
  const s = await setup(url => url.endsWith('/attachments') ? uploadResponse : { ok: false, json: async () => ({ error: '稍后重试' }) });
  await s.api.addFiles([file()]); await s.api.send();
  assert.equal(s.api.getAttachments()[0].id, attachment.id);
  assert.equal(s.get('agentInput').value, '分析上传的附件'); assert.equal(s.get('agentSend').disabled, false);
});



test('clipboard images upload with thumbnail and escaped filename header', async () => {
  const image = new Blob(['pixels'], { type: 'image/png' }); image.name = '商品 截图.png';
  const s = await setup(() => ({ ok: true, json: async () => ({ ok: true, attachment: { ...attachment, name: image.name, kind: 'image', size: image.size } }) }));
  let prevented = false;
  s.get('agentInput').listeners.paste({ preventDefault: () => { prevented = true; }, clipboardData: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => image }] } });
  for (let n = 0; n < 10 && s.api.getUploads(); n++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(prevented, true); assert.equal(s.api.getAttachments()[0].kind, 'image');
  assert.match(s.api.getAttachments()[0].previewUrl, /^blob:/);
  assert.equal(s.calls.find(c => c.url.endsWith('/attachments')).options.headers['X-File-Name'], encodeURIComponent(image.name));
  s.get('agentAttachments').children[0].children.at(-1).onclick();
  assert.equal(s.api.getAttachments().length, 0);
});

test('multi-file batch is serialized and stays locked through queued uploads', async () => {
  const releases = [];
  const s = await setup(() => new Promise(resolve => { releases.push(resolve); }));
  const pending = s.api.addFiles([file('one.csv'), file('two.csv'), file('three.csv')]);
  assert.equal(releases.length, 1); assert.equal(s.get('agentConversations').disabled, true);
  releases[0](uploadResponse);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(releases.length, 2); assert.equal(s.get('agentConversations').disabled, true);
  releases[1](uploadResponse);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(releases.length, 3); assert.equal(s.get('agentSend').disabled, true);
  releases[2](uploadResponse); await pending;
  assert.equal(s.get('agentConversations').disabled, false);
});

test('separate image and file buttons open their pickers and image picker shares upload pipeline', async () => {
  const image = new Blob(['pixels'], { type: 'image/png' }); image.name = 'image.png';
  const s = await setup(() => ({ ok: true, json: async () => ({ ok: true, attachment: { ...attachment, name: image.name, kind: 'image', size: image.size } }) }));
  s.get('agentImageUpload').onclick(); assert.equal(s.get('agentImageInput').clicked, true);
  s.get('agentUpload').onclick(); assert.equal(s.get('agentFileInput').clicked, true);
  const target = { files: [image], value: image.name };
  s.get('agentImageInput').onchange({ target });
  assert.equal(target.value, ''); assert.equal(s.get('agentImageUpload').disabled, true); assert.equal(s.get('agentUpload').disabled, true);
  for (let n = 0; n < 10 && s.api.getUploads(); n++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(s.api.getAttachments()[0].kind, 'image'); assert.equal(s.get('agentImageUpload').disabled, false);
  assert.equal(s.get('agentSend').disabled, false);
  s.get('agentAttachments').children[0].children.at(-1).onclick();
});

test('repeated new clicks on an empty conversation preserve one draft and its typed question', async () => {
  const s = await setup(() => { throw new Error('unexpected send'); });
  const initial = JSON.parse(s.writes.get('aba-research-chat-v3')).activeConversationId;
  assert.equal(s.get('agentNew').disabled, true);
  s.get('agentInput').value = '/competition 铁箱'; s.get('agentInput').oninput();
  for (let n = 0; n < 30; n++) s.get('agentNew').onclick();
  assert.equal(s.get('agentConversations').children.length, 1);
  assert.equal(s.get('agentInput').value, '/competition 铁箱');
  assert.equal(JSON.parse(s.writes.get('aba-research-chat-v3')).activeConversationId, initial);
});

test('new after a completed conversation creates one empty draft and retains history', async () => {
  const s = await setup(() => chatResponse());
  s.get('agentInput').value = '研究 owala'; await s.api.send();
  const original = JSON.parse(s.writes.get('aba-research-chat-v3')).activeConversationId;
  assert.equal(s.get('agentNew').disabled, false);
  for (let n = 0; n < 30; n++) s.get('agentNew').onclick();
  assert.equal(s.get('agentConversations').children.length, 2);
  const saved = JSON.parse(s.writes.get('aba-research-chat-v3'));
  assert.equal(saved.conversations.find(c => c.id === original).records.length, 2);
  assert.equal(saved.conversations.filter(c => !c.records.length && !c.attachments?.length).length, 1);
  assert.equal(s.get('agentNew').disabled, true);
});

test('restore merges duplicate empty chats while preserving active empty, meaningful titles and files', async () => {
  const record = {role:'user', content:'竞争分析', context:{}, sources:[], status:'completed'};
  const saved = JSON.stringify({ activeConversationId:'blank-active', conversations:[
    {id:'history', title:'/competition 铁箱', records:[record]},
    {id:'blank-old', title:'新对话', records:[]},
    {id:'blank-active', title:'新对话', records:[]},
    {id:'blank-newer', title:'新对话', records:[]},
    {id:'files', title:'新对话', records:[], attachments:[attachment]},
    {id:'keyword-draft', title:'owala · 关键词分析', records:[]}
  ] });
  const s = await setup(() => chatResponse(), saved);
  const restored = JSON.parse(s.writes.get('aba-research-chat-v3'));
  assert.equal(restored.activeConversationId, 'blank-active');
  assert.deepEqual(restored.conversations.map(c => c.id), ['history','blank-active','files','keyword-draft']);
  assert.equal(restored.conversations[0].records[0].content, '竞争分析');
  assert.equal(restored.conversations[2].attachments[0].id, attachment.id);
});

test('new reuses a saved empty conversation and clear removes duplicate blanks', async () => {
  const record = {role:'user', content:'研究问题', context:{}, sources:[], status:'completed'};
  const saved = JSON.stringify({activeConversationId:'history', conversations:[
    {id:'blank', title:'新对话', records:[]}, {id:'history', title:'研究问题', records:[record]}
  ]});
  const s = await setup(() => chatResponse(), saved);
  s.get('agentNew').onclick();
  let state = JSON.parse(s.writes.get('aba-research-chat-v3'));
  assert.equal(state.activeConversationId, 'blank'); assert.equal(state.conversations.length, 2);
  s.get('agentConversations').onchange({target:{value:'history'}});
  s.get('agentClear').onclick();
  state = JSON.parse(s.writes.get('aba-research-chat-v3'));
  assert.equal(state.activeConversationId, 'history'); assert.equal(state.conversations.length, 1);
  assert.equal(s.get('agentConversations').children.length, 1);
});
