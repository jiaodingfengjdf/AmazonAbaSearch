const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../web/assets/app.js'), 'utf8');
const start = source.indexOf('const googleTrendCache =');
const end = source.indexOf('function closeDetail()', start);

function setup(fetch) {
  const context = vm.createContext({ fetch, META: { market: 'COM' }, detailToken: 1,
    charts: {}, Date, Map, Number, String, encodeURIComponent });
  vm.runInContext(source.slice(start, end), context);
  const tip = { textContent: '' };
  const retry = {};
  const panel = { hidden: true, dataset: {}, innerHTML: '',
    querySelector: selector => selector === '.asin-tip' ? tip : retry };
  const button = { setAttribute() {} };
  const card = { isConnected: true,
    querySelector: selector => selector === '.google-panel' ? panel : button };
  return { context, card, panel, tip, retry };
}

test('special characters are URL encoded and concurrent cards share one request', async () => {
  const urls = [];
  const { context } = setup(async url => {
    urls.push(url);
    return { ok: true, json: async () => ({ ok: true, trend: [] }) };
  });
  await Promise.all([context.fetchGoogleTrend('a & b'), context.fetchGoogleTrend('a & b')]);
  assert.deepEqual(urls, ['api/keyword/google-trends?kw=a%20%26%20b']);
});

test('failed requests can be retried and empty results are explained', async () => {
  let calls = 0;
  const { context, card, panel, tip } = setup(async () => {
    if (++calls === 1) throw new Error('network offline');
    return { ok: true, json: async () => ({ ok: true, trend: [] }) };
  });
  await context.toggleGoogleTrend(card, 'owala');
  assert.equal(tip.textContent, 'network offline');
  assert.match(panel.innerHTML, /重试/);
  await context.toggleGoogleTrend(card, 'owala');
  await context.toggleGoogleTrend(card, 'owala');
  assert.equal(calls, 2);
  assert.match(panel.innerHTML, /暂无谷歌搜索趋势数据/);
});

test('collapsing while loading prevents late response rendering', async () => {
  let finish;
  const { context, card, panel } = setup(() => new Promise(resolve => { finish = resolve; }));
  const loading = context.toggleGoogleTrend(card, 'owala');
  await context.toggleGoogleTrend(card, 'owala');
  finish({ ok: true, json: async () => ({ ok: true, trend: [] }) });
  await loading;
  assert.equal(panel.hidden, true);
  assert.match(panel.innerHTML, /加载谷歌趋势/);
});

test('switching keywords invalidates the previous pending render', async () => {
  let finish;
  const { context, card, panel } = setup(() => new Promise(resolve => { finish = resolve; }));
  const loading = context.toggleGoogleTrend(card, 'owala');
  context.detailToken += 1;
  finish({ ok: true, json: async () => ({ ok: true, trend: [] }) });
  await loading;
  assert.match(panel.innerHTML, /加载谷歌趋势/);
});

test('keyword drawer places one automatically loaded Google chart directly after history', async () => {
  const { context } = setup(async () => ({ ok: true, json: async () => ({ ok: true, trend: [] }) }));
  const button = {}, section = { querySelector: () => button };
  const body = { innerHTML: '', querySelector: () => ({}), insertBefore() {} };
  const elements = { drawerBody: body, drawer: { classList: { add() {} }, setAttribute() {} }, detailChart: {}, keywordGoogleTrend: section };
  const calls = [];
  Object.assign(context, {
    KW: { rows: [{ kw: 'owala', dps: [], br: '', gr: 0 }] },
    C: new Proxy({}, { get: (_, key) => key }), SUMMARY: { deptIndex: [] },
    META: { market: 'COM', tableDate: 'ara_20260919' }, trendPair: async () => null,
    $: id => elements[id], intFmt: String, pct: String, money: String, weekLabel: String, compact: String,
    document: { createElement: () => ({}) }, window: { dispatchEvent() {} }, Event: class {},
    themedChart: () => ({ setOption() {} }), baseAxis: {},
    echarts: { graphic: { LinearGradient: class {} } }, renderAsinSection() {},
    toggleGoogleTrend: (host, keyword) => calls.push({ host, keyword })
  });
  const detailStart = source.indexOf('async function openDetail(rowIdx)');
  const detailEnd = source.indexOf('/* ---------------- 详情抽屉：ASIN TOP10', detailStart);
  vm.runInContext(source.slice(detailStart, detailEnd), context);
  await context.openDetail(0);
  assert.equal((body.innerHTML.match(/id="keywordGoogleTrend"/g) || []).length, 1);
  assert.ok(body.innerHTML.indexOf('id="detailChart"') < body.innerHTML.indexOf('id="keywordGoogleTrend"'));
  assert.ok(body.innerHTML.indexOf('id="keywordGoogleTrend"') < body.innerHTML.indexOf('TOP3 品牌'));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].host, section);
  assert.equal(calls[0].keyword, 'owala');
  assert.equal(typeof button.onclick, 'function');
});

test('ASIN cards and missing-product fallback do not contain duplicate keyword trend controls', () => {
  const cards = source.slice(source.indexOf('function asinCardHtml('), source.indexOf('async function toggleAsinTrend('));
  const asins = source.slice(source.indexOf('async function renderAsinSection('), source.indexOf('function asinCardHtml('));
  assert.doesNotMatch(cards, /google-panel|google-toggle|googleTrendSectionHtml/);
  assert.doesNotMatch(asins, /google-panel|google-toggle|toggleGoogleTrend/);
  assert.match(cards, /价格趋势/);
});
