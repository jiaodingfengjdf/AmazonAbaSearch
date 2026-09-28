/* ABA 关键词趋势看板 —— 纯静态前端，数据来自 data/*.json */

const PALETTE = ['#fb8c1e', '#6ea8fe', '#34d399', '#b98cff', '#4dd4c8', '#f87171',
                 '#fbbf24', '#60a5fa', '#f472b6', '#a3e635', '#22d3ee', '#c084fc'];

const C = {};            // 列名 -> 下标
let DATA_BASE = 'data/';  // 当前数据期目录：data/<ara_YYYYMMDD>/
let INDEX = null;         // data/index.json：可用数据期列表
let META = null, SUMMARY = null, KW = null;
let view = [];           // 当前筛选/排序后的行下标
const shardCache = new Map();
let charts = {};
let agentKeyword = '';
const LIGHT_CHART_COLORS = {
  '#8b97b3': '#53657a', '#e8edf7': '#172338', '#cbd5f0': '#33455c',
  '#a7f3d0': '#087958', '#6ea8fe': '#2864c5', '#34d399': '#0d8960',
  '#fb8c1e': '#b9570c', '#b98cff': '#7542bd', '#4dd4c8': '#087f78',
  '#f87171': '#c53c48', '#0a1020': '#ffffff',
  'rgba(12,18,35,0.94)': '#ffffff', 'rgba(12,18,35,0.95)': '#ffffff',
  'rgba(255,255,255,0.12)': 'rgba(39,65,95,0.18)',
  'rgba(255,255,255,0.14)': 'rgba(39,65,95,0.28)',
  'rgba(255,255,255,0.05)': 'rgba(39,65,95,0.11)',
  'rgba(255,255,255,0.2)': 'rgba(39,65,95,0.32)',
  'rgba(255,255,255,0.25)': 'rgba(39,65,95,0.35)',
};
const chartColor = (value) => document.documentElement.dataset.theme === 'light'
  ? (LIGHT_CHART_COLORS[value] || value) : value;
function themeChartOption(value) {
  if (typeof value === 'string') return chartColor(value);
  if (Array.isArray(value)) return value.map(themeChartOption);
  if (value instanceof echarts.graphic.LinearGradient) {
    return new echarts.graphic.LinearGradient(value.x, value.y, value.x2, value.y2,
      value.colorStops.map(stop => ({ ...stop, color: chartColor(stop.color) })), value.global);
  }
  if (value && Object.getPrototypeOf(value) === Object.prototype) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, themeChartOption(item)]));
  }
  return value;
}
function themedChart(dom) {
  const chart = echarts.init(dom, null, { renderer: 'canvas' });
  const originalSetOption = chart.setOption.bind(chart);
  chart.setOption = (option, ...args) => {
    chart.rawThemeOption = option;
    originalSetOption(themeChartOption(option), ...args);
  };
  return chart;
}
function setupTheme() {
  const button = $('themeToggle');
  const sync = () => {
    const dark = document.documentElement.dataset.theme === 'dark';
    button.textContent = dark ? '☀ 浅色' : '☾ 深色';
    button.setAttribute('aria-pressed', String(dark));
    button.title = dark ? '切换到浅色' : '切换到深色';
  };
  sync();
  button.onclick = () => {
    document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('aba-theme', document.documentElement.dataset.theme); } catch (_) {}
    sync();
    Object.values(charts).forEach(chart => {
      if (chart && !chart.isDisposed() && chart.rawThemeOption) {
        chart.setOption(chart.rawThemeOption, { notMerge: true });
        chart.resize();
      }
    });
  };
}

const state = {
  q: '',              // 顶部全局搜索框
  sortKey: 'se', sortDir: 'desc', page: 1, size: 25,
};
// 生效中的筛选条件（每次筛选时从 DOM 现读，避免 DOM 与内存状态不同步）
let activeFilters = { deptIdx: -1, ranges: [], exclude: [], exact: [] };

/* ---------------- 筛选条件定义（区间口径与卖家精灵 ABA 页面一致） ---------------- */

const NUMERIC_FILTERS = [
  { key: 'se', col: 'se', label: '搜索量', tip: 'ABA 月搜索次数' },
  { key: 'rk', col: 'rk', label: '搜索排名', tip: 'ABA 搜索频率排名，数值越小越热门' },
  { key: 'gr', col: 'gr', label: '排名增长率', tip: '近一周（W1）ABA 排名上升幅度', unit: '%', scale: 100 },
  { key: 'im', col: 'im', label: '展示量', tip: 'ABA 月展示次数' },
  { key: 'cl', col: 'cl', label: '点击量', tip: 'ABA 月点击次数' },
  { key: 'cs', col: 'cs', label: '点击总占比', tip: 'TOP3 ASIN 点击占比之和（点击集中度）', unit: '%', scale: 100 },
  { key: 'cv', col: 'cv', label: '转化总占比', tip: 'TOP3 ASIN 转化占比之和（转化集中度）', unit: '%', scale: 100 },
  { key: 'spr', col: 'spr', label: 'SPR', tip: '上首页所需销量（cprExact）' },
  { key: 'td', col: 'td', label: '标题密度', tip: '标题含该关键词的商品数' },
  { key: 'wc', col: 'wc', label: '词组个数', tip: '关键词包含的单词个数' },
];

const SORT_OPTIONS = [
  ['se', 'desc', '搜索量 ↓'], ['se', 'asc', '搜索量 ↑'],
  ['gr', 'desc', '排名增长率 ↓'], ['gr', 'asc', '排名增长率 ↑'],
  ['rk', 'asc', '搜索排名 ↑'], ['gv', 'desc', '排名变化量 ↓'],
  ['im', 'desc', '展示量 ↓'], ['cl', 'desc', '点击量 ↓'],
  ['pu', 'desc', '月购买量 ↓'], ['cs', 'asc', '点击总占比 ↑'],
  ['cv', 'asc', '转化总占比 ↑'], ['td', 'asc', '标题密度 ↑'],
  ['spr', 'asc', 'SPR ↑'], ['wc', 'asc', '词组个数 ↑'],
  ['a30', 'asc', '广告商品数 ↑'],
];

/* ------------------------------ 工具 ------------------------------ */

const $ = (id) => document.getElementById(id);
const intFmt = (n) => (n === null || n === undefined || n === '') ? '-' : Number(n).toLocaleString('en-US');
const pct = (v, digits = 1) => (v === null || v === undefined) ? '-' : (v * 100).toFixed(digits) + '%';
const money = (v) => (v ? '$' + Number(v).toFixed(2) : '-');
const compact = (n) => {
  n = Number(n) || 0;
  if (n >= 1e8) return (n / 1e8).toFixed(2) + '亿';
  if (n >= 1e4) return (n / 1e4).toFixed(1) + '万';
  return n.toLocaleString('en-US');
};
const weekLabel = (s) => s && s.length === 8 ? `${s.slice(4, 6)}/${s.slice(6, 8)}` : s;

function setLoading(on, text) {
  if (text) $('loadingText').textContent = text;
  $('loading').classList.toggle('hidden', !on);
}

/* ------------------------------ 启动 ------------------------------ */

async function init() {
  setupTheme();
  setLoading(true, '加载数据…');
  await loadIndex();
  const wanted = new URLSearchParams(location.search).get('week') || INDEX.latest;
  await loadWeek(wanted);
  const wantedKeyword = new URLSearchParams(location.search).get('keyword');
  if (wantedKeyword) openDetailByKeyword(wantedKeyword);
  window.addEventListener('resize', () => Object.values(charts).forEach((c) => c && c.resize()));
  bindGlobalEvents();
}

/** 读取数据期索引，填充顶部「数据期」下拉框 */
async function loadIndex() {
  INDEX = await fetch('data/index.json').then((r) => r.json());
  const sel = $('weekSelect');
  const weeks = INDEX.weeks || [];
  sel.innerHTML = weeks.map((w) => {
    const label = w.display || w.table;
    const n = w.keywords ? ` · ${Number(w.keywords).toLocaleString('en-US')} 词` : '';
    const bad = w.complete === false ? ` · 未抓全(${w.pages}/${w.pagesNeeded}页)` : '';
    return `<option value="${w.table}">${label}${n}${bad}</option>`;
  }).join('');
  sel.value = INDEX.latest;
  const hint = $('weekHint');
  if (hint) hint.textContent = weeks.length > 1 ? `共 ${weeks.length} 周可切换` : '（每周自动新增一期）';
}

/** 该周抓取未完成时给出醒目标记（Cookie 过期会导致中途断抓） */
function renderWeekWarn(week) {
  const el = $('weekWarn');
  if (!el) return;
  const info = ((INDEX && INDEX.weeks) || []).find((w) => w.table === week) || {};
  if (info.complete === false) {
    el.style.display = 'block';
    el.innerHTML = `⚠ 该数据期抓取未完成（已抓 ${info.pages}/${info.pagesNeeded} 页，库里 ${Number(info.keywords || 0).toLocaleString('en-US')} 个关键词），`
      + `通常是卖家精灵 Cookie 过期导致中断。更新 cookie.txt 后重跑：<code>python run_all.py --fetch --week ${week}</code> 即可续抓（断点续传，不会重头再来）。`;
  } else {
    el.style.display = 'none';
    el.innerHTML = '';
  }
}

/** 切换到某个数据期：重新加载该周全部数据并重绘 */
async function loadWeek(week) {
  agentKeyword = '';
  closeDetail();
  setLoading(true, `加载 ${week} 数据…`);
  const sel = $('weekSelect');
  if (sel) sel.value = week;
  DATA_BASE = `data/${week}/`;
  shardCache.clear();
  Object.values(charts).forEach((c) => { try { c && c.dispose(); } catch (e) {} });
  charts = {};

  const [meta, summary, kw] = await Promise.all([
    fetch(DATA_BASE + 'meta.json').then((r) => r.json()),
    fetch(DATA_BASE + 'summary.json').then((r) => r.json()),
    fetch(DATA_BASE + 'keywords.json').then((r) => r.json()),
  ]);
  META = meta; SUMMARY = summary; KW = kw;
  Object.keys(C).forEach((k) => { delete C[k]; });
  kw.cols.forEach((name, i) => { C[name] = i; });

  state.q = ''; state.page = 1;
  state.sortKey = 'se'; state.sortDir = 'desc';
  const gs = $('globalSearch'); if (gs) gs.value = '';

  renderMeta();
  renderKpis();
  renderFilters();
  renderTableHead();
  renderCharts();
  applyFilters(1);
  renderWeekWarn(week);
  setLoading(false);
  document.title = `ABA 关键词趋势看板 · ${META.tableDate}`;
}

function renderMeta() {
  $('metaMin').textContent = intFmt(META.minSearches);
  $('metaChips').innerHTML = [
    ['站点', META.market === 'COM' ? '美国站 (COM)' : META.market],
    ['口径', (META.reverseType === 'W' ? '周表' : '月表') + ' · ' + META.rankGrowthType],
    ['关键词', intFmt(META.keywordCount)],
    ['生成', (META.generatedAt || '').replace('T', ' ')],
  ].map(([k, v]) => `<span class="chip">${k} <b>${v}</b></span>`).join('');
}

function renderKpis() {
  const k = SUMMARY.kpis;
  const cards = [
    { label: '入榜关键词', value: intFmt(k.keywords), extra: `搜索量 ≥ ${intFmt(META.minSearches)}`, accent: '#fb8c1e' },
    { label: '月搜索量合计', value: compact(k.totalSearches), extra: `均值 ${compact(k.avgSearches)}`, accent: '#6ea8fe' },
    {
      label: '平均排名增幅', value: pct(k.avgGrowth),
      extra: `中位数 ${pct(k.medianGrowth)}`, accent: '#34d399',
    },
    {
      label: '排名上升词占比', value: pct(k.upRatio),
      extra: `${intFmt(k.upCount)} 个词环比上升`, accent: '#4dd4c8',
    },
    { label: '强增长词（≥50%）', value: intFmt(k.strongCount), extra: '近一周排名增幅 ≥ 50%', accent: '#b98cff' },
    {
      label: '覆盖大类', value: intFmt(k.deptCount),
      extra: `另有 其它细分类目 ${intFmt(k.deptOtherCount)} 词 · 未归类 ${intFmt(k.deptNoneCount)} 词`,
      accent: '#fbbf24',
    },
  ];
  $('kpis').innerHTML = cards.map((c) => `
    <div class="kpi" style="--accent:${c.accent}">
      <div class="label">${c.label}</div>
      <div class="value">${c.value}</div>
      <div class="extra">${c.extra}</div>
    </div>`).join('');
}

function fillDeptSelect() {
  const items = SUMMARY.deptIndex.map((d, i) => ({ i, ...d }));
  const official = items.filter((d) => d.official);
  const extra = items.filter((d) => !d.official);
  let html = `<option value="-1">全部类目</option>`;
  html += `<optgroup label="ABA 大类（${official.length}）">` +
    official.map((d) => `<option value="${d.i}">${d.name} (${d.code})${d.note ? ' · ' + d.note : ''}</option>`).join('') +
    `</optgroup>`;
  if (extra.length) {
    html += `<optgroup label="其它">` +
      extra.map((d) => `<option value="${d.i}">${d.name}</option>`).join('') +
      `</optgroup>`;
  }
  $('fDept').innerHTML = html;
}

/* ------------------------------ 筛选面板 ------------------------------ */

function renderFilters() {
  const rows = [];

  rows.push(`<div class="frow top">
    <span class="flabel">类目</span>
    <select id="fDept"></select>
    <select id="fMode" title="按 ABA 归类：关键词只要被 ABA 归到该类目就命中（多类目词会出现在多个类目下）；只看主类目：仅当该词的主类目就是所选类目时命中。">
      <option value="any">按 ABA 归类</option>
      <option value="primary">只看主类目</option>
    </select>
    <span class="fsep"></span>
    <span class="flabel">排序</span>
    <select id="fSort">${SORT_OPTIONS.map(([k, d, t]) => `<option value="${k}:${d}">${t}</option>`).join('')}</select>
    <span class="flabel">每页</span>
    <select id="fSize">
      <option>25</option><option>50</option><option>100</option><option>200</option>
    </select>
  </div>`);

  for (const f of NUMERIC_FILTERS) {
    const unit = f.unit === '%' ? '<span class="unit">%</span>' : '';
    rows.push(`<div class="frow" data-key="${f.key}">
      <span class="flabel">${f.label}<span class="info-dot" title="${f.tip}">?</span></span>
      <input class="fnum ${f.unit === '%' ? 'percent' : ''}" data-role="min" type="number" placeholder="最小值" />
      <span class="tilde">~</span>
      <input class="fnum ${f.unit === '%' ? 'percent' : ''}" data-role="max" type="number" placeholder="最大值" />
      ${unit}
    </div>`);
  }

  rows.push(`<div class="frow">
    <span class="flabel">排除关键词<span class="info-dot" title="关键词包含其中任意一个词就被剔除，多个用英文逗号分隔">?</span></span>
    <input class="wide" id="fExclude" placeholder="输入关键词，多个以英文逗号区分" />
    <span class="flabel">精确匹配<span class="info-dot" title="只保留与输入完全一致的关键词，多个用英文逗号分隔">?</span></span>
    <input class="wide" id="fExact" placeholder="输入关键词，多个以英文逗号区分" />
  </div>`);

  $('filterPanel').innerHTML = rows.join('');
  fillDeptSelect();
  bindFilterEvents();
}

function bindFilterEvents() {
  // 数值范围 + 快捷区间
  $('filterPanel').querySelectorAll('.frow[data-key]').forEach((row) => {
    const key = row.dataset.key;
    const minInput = row.querySelector('[data-role="min"]');
    const maxInput = row.querySelector('[data-role="max"]');
    const onChange = () => applyFilters(1);
    minInput.addEventListener('input', onChange);
    maxInput.addEventListener('input', onChange);
  });

  // 排除 / 精确匹配（各自独立防抖，真正的取值在 applyFilters 里现读 DOM）
  const textTimers = {};
  const bindText = (id, key) => {
    const handler = () => {
      clearTimeout(textTimers[key]);
      textTimers[key] = setTimeout(() => applyFilters(1), 250);
    };
    // input 覆盖即时输入，change 作为兜底（失焦/清空/粘贴等场景）
    $(id).addEventListener('input', handler);
    $(id).addEventListener('change', handler);
  };
  bindText('fExclude', 'exclude');
  bindText('fExact', 'exact');

  // 排序 / 每页
  $('fSort').addEventListener('change', (e) => {
    const [key, dir] = e.target.value.split(':');
    state.sortKey = key; state.sortDir = dir;
    sortView(); state.page = 1; renderTable(); renderTableHead();
  });
  $('fSize').addEventListener('change', (e) => {
    state.size = Number(e.target.value); state.page = 1; renderTable();
  });
  $('fDept').addEventListener('change', () => {
    applyFilters(1);
    renderSeriesChart();
    renderGrowthChart();
    renderScatterChart();
  });
  $('fMode').addEventListener('change', () => applyFilters(1));
}

function resetFilters() {
  state.q = '';
  state.sortKey = 'se'; state.sortDir = 'desc'; state.page = 1;
  $('globalSearch').value = '';
  $('fDept').value = '-1';
  if ($('fMode')) $('fMode').value = 'any';
  $('fSort').value = 'se:desc';
  $('fSize').value = String(state.size);
  ['fExclude', 'fExact'].forEach((id) => { const el = $(id); if (el) el.value = ''; });
  $('filterPanel').querySelectorAll('.frow[data-key] input').forEach((i) => { i.value = ''; });
  applyFilters(1);
  refreshDeptCharts();
  renderTableHead();
}

/** 从筛选面板现读条件（DOM 是唯一事实来源） */
function readFilters() {
  const parseList = (v) => (v || '').split(/[,，]/).map((s) => s.trim().toLowerCase()).filter(Boolean);
  const ranges = [];
  $('filterPanel').querySelectorAll('.frow[data-key]').forEach((row) => {
    const spec = NUMERIC_FILTERS.find((f) => f.key === row.dataset.key);
    const minV = row.querySelector('[data-role="min"]').value.trim();
    const maxV = row.querySelector('[data-role="max"]').value.trim();
    if (!spec || (minV === '' && maxV === '')) return;
    ranges.push([spec, {
      min: minV === '' ? null : Number(minV),
      max: maxV === '' ? null : Number(maxV),
    }]);
  });
  return {
    deptIdx: Number($('fDept').value),
    mode: ($('fMode') || {}).value || 'any',
    ranges,
    exclude: parseList($('fExclude').value),
    exact: parseList($('fExact').value),
  };
}

/* ------------------------------ 图表 ------------------------------ */

const baseAxis = {
  axisLine: { lineStyle: { color: 'rgba(255,255,255,0.14)' } },
  axisLabel: { color: '#8b97b3', fontSize: 11 },
  splitLine: { lineStyle: { color: 'rgba(255,255,255,0.05)' } },
};

function chartOf(id) {
  if (!charts[id]) charts[id] = themedChart($(id));
  return charts[id];
}

function renderCharts() {
  renderMarketChart();
  renderDeptChart();
  renderPieChart();
  renderGrowthChart();      // 跟随类目
  renderScatterChart();     // 跟随类目
  renderSeriesChart();      // 跟随类目
}

/** 类目相关的三张图一起刷新（下拉、柱状图/玫瑰图点击、重置都走这里） */
function refreshDeptCharts() {
  renderGrowthChart();
  renderScatterChart();
  renderSeriesChart();
}

function renderMarketChart() {
  const w = SUMMARY.weekly;
  chartOf('chartMarket').setOption({
    grid: { left: 62, right: 62, top: 42, bottom: 34 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(12,18,35,0.94)', borderColor: 'rgba(255,255,255,0.12)',
      textStyle: { color: '#e8edf7', fontSize: 12 },
      valueFormatter: (v) => Number(v).toLocaleString('en-US'),
    },
    legend: { data: ['周搜索量合计', '有效词量'], top: 4, textStyle: { color: '#8b97b3' }, icon: 'roundRect', itemWidth: 10, itemHeight: 10 },
    xAxis: { type: 'category', data: w.labels.map(weekLabel), boundaryGap: false, ...baseAxis },
    yAxis: [
      { type: 'value', name: '搜索量', nameTextStyle: { color: '#8b97b3' }, ...baseAxis, axisLabel: { ...baseAxis.axisLabel, formatter: (v) => compact(v) } },
      { type: 'value', name: '词量', nameTextStyle: { color: '#8b97b3' }, ...baseAxis, splitLine: { show: false } },
    ],
    series: [
      {
        name: '周搜索量合计', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
        data: w.searches, yAxisIndex: 0,
        lineStyle: { width: 3, color: '#fb8c1e' },
        itemStyle: { color: '#fb8c1e' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(251,140,30,0.42)' },
            { offset: 1, color: 'rgba(251,140,30,0.02)' },
          ]),
        },
      },
      {
        name: '有效词量', type: 'line', smooth: true, symbol: 'none', yAxisIndex: 1,
        data: w.keywords, lineStyle: { width: 2, color: '#6ea8fe', type: 'dashed' },
        itemStyle: { color: '#6ea8fe' },
      },
    ],
  });
}

function renderDeptChart() {
  const data = SUMMARY.departments.filter((d) => d.official).slice(0, 12).reverse();
  const max = Math.max(...data.map((d) => d.searches));
  chartOf('chartDept').setOption({
    grid: { left: 126, right: 78, top: 8, bottom: 8 },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(12,18,35,0.94)', borderColor: 'rgba(255,255,255,0.12)',
      textStyle: { color: '#e8edf7', fontSize: 12 },
      formatter: (p) => {
        const d = data[p[0].dataIndex];
        return `<b>${d.name}</b><br/>关键词 ${intFmt(d.count)} 个<br/>搜索量 ${intFmt(d.searches)}<br/>平均增幅 ${pct(d.avgGrowth)}`;
      },
    },
    xAxis: { type: 'value', ...baseAxis, axisLabel: { ...baseAxis.axisLabel, formatter: (v) => compact(v) } },
    yAxis: { type: 'category', data: data.map((d) => d.name + (d.note ? '（同样本）' : '')), ...baseAxis, splitLine: { show: false } },
    series: [{
      type: 'bar', data: data.map((d) => d.searches), barWidth: '62%',
      itemStyle: {
        borderRadius: [0, 6, 6, 0],
        color: (p) => {
          const ratio = data[p.dataIndex].searches / max;
          const c = new echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: 'rgba(251,140,30,0.35)' },
            { offset: 1, color: `rgba(251,140,30,${0.45 + ratio * 0.55})` },
          ]);
          return c;
        },
      },
      label: { show: true, position: 'right', color: '#cbd5f0', fontSize: 11, formatter: (p) => compact(p.value) },
    }],
  });
  chartOf('chartDept').off('click');
  chartOf('chartDept').on('click', (p) => {
    const dept = data[p.dataIndex];
    const i = SUMMARY.deptIndex.findIndex((d) => d.code === dept.code);
    $('fDept').value = String(i);
    applyFilters(1);
    refreshDeptCharts();
  });
}

function renderPieChart() {
  const all = SUMMARY.departments.filter((d) => d.official);
  const top = all.slice(0, 9);
  const rest = all.slice(9).reduce((s, d) => s + d.count, 0);
  const data = top.map((d, i) => ({
    name: d.name + (d.note ? '（同样本）' : ''), value: d.count,
    itemStyle: { color: PALETTE[i % PALETTE.length] },
  }));
  if (rest) data.push({ name: '其它类目', value: rest, itemStyle: { color: 'rgba(255,255,255,0.25)' } });
  chartOf('chartPie').setOption({
    tooltip: {
      backgroundColor: 'rgba(12,18,35,0.94)', borderColor: 'rgba(255,255,255,0.12)',
      textStyle: { color: '#e8edf7', fontSize: 12 },
      formatter: (p) => `<b>${p.name}</b><br/>关键词 ${intFmt(p.value)} 个（${p.percent}%）`,
    },
    legend: { type: 'scroll', bottom: 0, textStyle: { color: '#8b97b3', fontSize: 11 }, icon: 'circle', itemWidth: 8, itemHeight: 8 },
    series: [{
      type: 'pie', radius: ['42%', '72%'], center: ['50%', '44%'], roseType: 'radius',
      data, label: { color: '#cbd5f0', fontSize: 11, formatter: '{b}\n{d}%' },
      labelLine: { lineStyle: { color: 'rgba(255,255,255,0.2)' } },
      itemStyle: { borderColor: '#0a1020', borderWidth: 2 },
      emphasis: { scale: true, scaleSize: 6 },
    }],
  });
  chartOf('chartPie').off('click');
  chartOf('chartPie').on('click', (p) => {
    if (p.name === '其它类目') return;
    const hit = all.find((d) => (d.name + (d.note ? '（同样本）' : '')) === p.name);
    if (!hit) return;
    const i = SUMMARY.deptIndex.findIndex((d) => d.code === hit.code);
    if (i >= 0) { $('fDept').value = String(i); applyFilters(1); refreshDeptCharts(); }
  });
}

function renderGrowthChart() {
  const code = currentDeptCode();
  const bank = SUMMARY.deptTopGrowth || {};
  const list = bank[code] || bank.__all__ || [];
  const hint = $('chartGrowthHint');
  if (hint) {
    const name = code === '__all__' ? '全部类目' : ((SUMMARY.deptIndex.find((d) => d.code === code) || {}).name || code);
    hint.textContent = `当前类目：${name} · 近一周 ABA 排名上升幅度（增幅 >0 且搜索量 ≥5,000）`;
  }
  const data = list.slice().reverse();
  chartOf('chartGrowth').setOption({
    grid: { left: 132, right: 70, top: 10, bottom: 12 },
    tooltip: {
      backgroundColor: 'rgba(12,18,35,0.94)', borderColor: 'rgba(255,255,255,0.12)',
      textStyle: { color: '#e8edf7', fontSize: 12 },
      formatter: (p) => {
        const d = data[p.dataIndex];
        return `<b>${d.keyword}</b><br/>搜索量 ${intFmt(d.searches)}<br/>排名 ${d.rank}（上期 ${d.rank - (d.growthValue || 0)}）<br/>` +
               `增幅 <span style="color:#34d399">+${(d.growth * 100).toFixed(1)}%</span><br/>点击图表查看趋势`;
      },
    },
    xAxis: { type: 'value', ...baseAxis, axisLabel: { ...baseAxis.axisLabel, formatter: (v) => (v * 100).toFixed(0) + '%' } },
    yAxis: { type: 'category', data: data.map((d) => d.keyword.length > 16 ? d.keyword.slice(0, 16) + '…' : d.keyword), ...baseAxis, splitLine: { show: false } },
    series: [{
      type: 'bar', data: data.map((d) => d.growth), barWidth: '62%',
      itemStyle: {
        borderRadius: [0, 6, 6, 0],
        color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
          { offset: 0, color: 'rgba(52,211,153,0.35)' },
          { offset: 1, color: 'rgba(52,211,153,0.95)' },
        ]),
      },
      label: { show: true, position: 'right', color: '#a7f3d0', fontSize: 11, formatter: (p) => '+' + (p.value * 100).toFixed(0) + '%' },
    }],
  }, true);   // notMerge：切换类目时清掉旧条形
  chartOf('chartGrowth').off('click');
  chartOf('chartGrowth').on('click', (p) => openDetailByKeyword(data[p.dataIndex].keyword));
}

function renderScatterChart() {
  const code = currentDeptCode();
  const bank = SUMMARY.deptScatter || {};
  const pts = bank[code] || bank.__all__ || [];
  const isAll = code === '__all__';
  const oneName = isAll ? '' : (((SUMMARY.deptIndex.find((d) => d.code === code)) || {}).name || code);
  const hint = $('chartScatterHint');
  if (hint) {
    const colorBy = isAll ? '按类目分色' : '按 TOP3 点击集中度分色（绿=分散偏蓝海，红=高度集中）';
    hint.textContent = `X=月搜索量（对数） Y=排名增幅（截断 ±150%） 气泡=月购买量 · ${colorBy} · 当前类目：${isAll ? '全部类目' : oneName}（${pts.length} 个点）`;
  }

  // 数据格式 [关键词, 搜索量, 增幅, 月购买量, 类目下标, 点击集中度]
  // 整体视图：按类目分色；单类目视图：按 TOP3 点击集中度分色（绿=分散偏蓝海，红=高度集中）
  const TIERS = [
    { name: '点击集中度 <20%', test: (v) => v !== null && v < 0.2, color: '#34d399' },
    { name: '20%~40%', test: (v) => v !== null && v >= 0.2 && v < 0.4, color: '#6ea8fe' },
    { name: '40%~60%', test: (v) => v !== null && v >= 0.4 && v < 0.6, color: '#fbbf24' },
    { name: '≥60%（高度集中）', test: (v) => v !== null && v >= 0.6, color: '#f87171' },
    { name: '无数据', test: (v) => v === null || v === undefined, color: '#94a3b8' },
  ];
  const groups = new Map();   // 名称 -> { color, points }
  pts.forEach((p) => {
    const key = isAll ? ((SUMMARY.deptIndex[p[4]] || {}).name || '其它') : (TIERS.find((t) => t.test(p[5])) || TIERS[4]).name;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push([p[1], p[2] * 100, p[3], p[0], p[5]]);
  });
  const tierColor = (v) => (TIERS.find((t) => t.test(v)) || TIERS[4]).color;
  const series = [...groups.entries()].map(([name, data], i) => ({
    name, type: 'scatter', data: data.map((d) => ({
      value: d, name: d[3],
      itemStyle: {
        color: isAll ? PALETTE[i % PALETTE.length] : tierColor(d[4]),
        opacity: 0.78,
      },
    })),
    symbolSize: (v) => Math.max(6, Math.min(30, 6 + Math.log10(v[2] + 10) * 7)),
    emphasis: { itemStyle: { opacity: 1, borderColor: '#fff', borderWidth: 1 } },
  }));
  chartOf('chartScatter').setOption({
    grid: { left: 56, right: 20, top: 46, bottom: 42 },
    legend: { top: 2, textStyle: { color: '#8b97b3', fontSize: 11 }, icon: 'circle', itemWidth: 8, itemHeight: 8, type: 'scroll' },
    tooltip: {
      backgroundColor: 'rgba(12,18,35,0.94)', borderColor: 'rgba(255,255,255,0.12)',
      textStyle: { color: '#e8edf7', fontSize: 12 },
      formatter: (p) => `<b>${p.data.name}</b><br/>搜索量 ${intFmt(p.value[0])}<br/>排名增幅 ${p.value[1].toFixed(1)}%`
        + `<br/>月购买量 ${intFmt(p.value[2])}<br/>TOP3 点击集中度 ${p.value[4] === null || p.value[4] === undefined ? '-' : (p.value[4] * 100).toFixed(1) + '%'}`,
    },
    xAxis: {
      type: 'log', name: '月搜索量（对数）', nameLocation: 'middle', nameGap: 26,
      nameTextStyle: { color: '#8b97b3' }, ...baseAxis,
      axisLabel: { ...baseAxis.axisLabel, formatter: (v) => compact(v) },
    },
    yAxis: { type: 'value', name: '排名增幅 %', nameTextStyle: { color: '#8b97b3' }, ...baseAxis, axisLabel: { ...baseAxis.axisLabel, formatter: '{value}%' } },
    series,
  }, true);   // notMerge：切换类目时清掉旧气泡
  chartOf('chartScatter').off('click');
  chartOf('chartScatter').on('click', (p) => openDetailByKeyword(p.data.name));
}

/** 当前选中的类目 code（未选=__all__） */
function currentDeptCode() {
  const idx = Number($('fDept').value);
  if (idx < 0) return '__all__';
  const d = SUMMARY.deptIndex[idx];
  return d ? d.code : '__all__';
}

function renderSeriesChart() {
  const code = currentDeptCode();
  const bank = SUMMARY.deptTrendSeries || {};
  const list = bank[code] || bank.__all__ || [];
  const hint = $('chartSeriesHint');
  if (hint) {
    const name = code === '__all__' ? '全部类目' : ((SUMMARY.deptIndex.find((d) => d.code === code) || {}).name || code);
    hint.textContent = `当前类目：${name} · 近一周增幅 ≥20% 且搜索量 ≥3万 中挑 10 个有完整历史曲线的词（无可选词时退回搜索量榜）`;
  }
  const labels = (list[0] ? list[0].labels : SUMMARY.weekly.labels).map(weekLabel);
  chartOf('chartSeries').setOption({
    color: PALETTE,
    grid: { left: 62, right: 26, top: 46, bottom: 30 },
    legend: { top: 2, type: 'scroll', textStyle: { color: '#8b97b3', fontSize: 11 }, icon: 'roundRect', itemWidth: 12, itemHeight: 8 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(12,18,35,0.94)', borderColor: 'rgba(255,255,255,0.12)',
      textStyle: { color: '#e8edf7', fontSize: 12 },
      valueFormatter: (v) => Number(v).toLocaleString('en-US'),
    },
    xAxis: { type: 'category', data: labels, boundaryGap: false, ...baseAxis },
    yAxis: [{ type: 'value', name: '搜索量', nameTextStyle: { color: '#8b97b3' }, ...baseAxis, axisLabel: { ...baseAxis.axisLabel, formatter: (v) => compact(v) } }],
    series: list.map((s) => ({
      name: s.keyword, type: 'line', smooth: true, symbol: 'circle', symbolSize: 4,
      data: s.searches, lineStyle: { width: 2 }, emphasis: { focus: 'series' },
    })),
  }, true);   // notMerge：切换类目时清掉旧序列
}

/* ------------------------------ 数据表 ------------------------------ */

const COLUMNS = [
  { key: 'idx', label: '#', cls: 'num', sort: false },
  { key: 'kw', label: '关键词' },
  { key: 'dept', label: '类目', sort: false },
  // col 指 keywords.json 的列名，排序时按名字取下标，避免以后加列导致错位
  { key: 'se', label: '月搜索量', cls: 'num', col: 'se' },
  { key: 'rk', label: 'ABA排名', cls: 'num', col: 'rk' },
  { key: 'w1rk', label: '上期排名', cls: 'num', col: 'w1rk' },
  { key: 'gv', label: '排名变化', cls: 'num', col: 'gv' },
  { key: 'gr', label: '排名增幅', cls: 'num', col: 'gr' },
  { key: 'pu', label: '月购买量', cls: 'num', col: 'pu' },
  { key: 'pr', label: '购买率', cls: 'num', col: 'pr' },
  { key: 'np', label: '在售商品', cls: 'num', col: 'np' },
  { key: 'spr', label: 'SPR', cls: 'num', col: 'spr' },
  { key: 'td', label: '标题密度', cls: 'num', col: 'td' },
  { key: 'cs', label: '点击集中度', cls: 'num', col: 'cs' },
  { key: 'cv', label: '转化集中度', cls: 'num', col: 'cv' },
  { key: 'bid', label: '建议竞价', cls: 'num', col: 'bid' },
  { key: 'a30', label: '30天广告商品', cls: 'num', col: 'a30' },
  { key: 'spark', label: '近 14 期趋势', sort: false },
];

function renderTableHead() {
  $('kwTable').querySelector('thead').innerHTML = '<tr>' + COLUMNS.map((c) => {
    const cls = [c.cls, c.sort === false ? 'no-sort' : ''].filter(Boolean).join(' ');
    const arrow = state.sortKey === c.key ? (state.sortDir === 'desc' ? ' ▾' : ' ▴') : '';
    return `<th class="${cls}" data-key="${c.key}">${c.label}${arrow}</th>`;
  }).join('') + '</tr>';
  $('kwTable').querySelectorAll('th').forEach((th) => {
    const key = th.dataset.key;
    const col = COLUMNS.find((c) => c.key === key);
    if (!col || col.sort === false) return;
    th.onclick = () => {
      if (state.sortKey === key) state.sortDir = state.sortDir === 'desc' ? 'asc' : 'desc';
      else { state.sortKey = key; state.sortDir = 'desc'; }
      $('fSort').value = `${key}:${state.sortDir}`;
      sortView();
      state.page = 1;
      renderTableHead();   // 刷新表头 ▾ / ▴ 指示
      renderTable();
    };
  });
}

function applyFilters(page = 1) {
  const rows = KW.rows;
  const q = state.q.trim().toLowerCase();
  activeFilters = readFilters();
  const { deptIdx, ranges, exclude, mode } = activeFilters;
  const exact = activeFilters.exact.length ? new Set(activeFilters.exact) : null;
  const out = [];
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    // 类目命中：any=ABA 归类（多类目词都算），primary=只看主类目
    if (deptIdx >= 0) {
      const hitDept = mode === 'primary' ? r[C.dpm] === deptIdx : (r[C.dps] || []).includes(deptIdx);
      if (!hitDept) continue;
    }

    let skip = false;
    for (let n = 0; n < ranges.length; n++) {
      const [f, range] = ranges[n];
      const value = r[C[f.col]] * (f.scale || 1);
      if (range.min !== null && value < range.min) { skip = true; break; }
      if (range.max !== null && value > range.max) { skip = true; break; }
    }
    if (skip) continue;

    const kw = r[C.kw];
    const kwLower = kw.toLowerCase();
    if (exact && !exact.has(kwLower)) continue;
    if (exclude.length) {
      let hit = false;
      for (let n = 0; n < exclude.length; n++) {
        if (kwLower.includes(exclude[n])) { hit = true; break; }
      }
      if (hit) continue;
    }
    if (q) {
      const hit = kwLower.includes(q) || (r[C.cn] || '').toLowerCase().includes(q) ||
                  (r[C.br] || '').toLowerCase().includes(q) || (r[C.as] || '').toLowerCase().includes(q);
      if (!hit) continue;
    }
    out.push(i);
  }
  view = out;
  sortView();
  state.page = page;
  renderTable();
  window.dispatchEvent(new Event('aba-context-change'));
}

function sortView() {
  const col = COLUMNS.find((c) => c.key === state.sortKey);
  if (!col || !col.col) return;
  const i = C[col.col], dir = state.sortDir === 'desc' ? -1 : 1;
  view.sort((a, b) => (KW.rows[a][i] - KW.rows[b][i]) * dir);
}

/* 分片按行号切片，因此同一屏只会命中 1~2 个分片 */
async function ensureShards(rowIdxList) {
  const size = META.shardSize || 1;
  const ids = [...new Set(rowIdxList.map((i) => Math.floor(i / size)))];
  await Promise.all(ids.map(async (id) => {
    if (shardCache.has(id)) return;
    const name = String(id).padStart(2, '0');
    const p = fetch(`${DATA_BASE}trends/shard-${name}.json`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    shardCache.set(id, p);
  }));
  return Promise.all(ids.map((id) => shardCache.get(id)));
}

async function trendPair(rowIdx) {
  const size = META.shardSize || 1;
  const shardId = Math.floor(rowIdx / size);
  const shard = await shardCache.get(shardId);
  if (!shard) return null;
  const local = rowIdx - shardId * size;
  return { searches: (shard.s || [])[local] || [], ranks: (shard.r || [])[local] || [], labels: shard.labels || [] };
}

function sparkline(values, width = 110, height = 26) {
  const pts = (values || []).map((v, i) => [i, Number(v) || 0]).filter((p) => p[1] > 0);
  if (pts.length < 2) return '<span class="flat">—</span>';
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const scaleX = (x) => ((x - minX) / Math.max(1, maxX - minX)) * (width - 4) + 2;
  const scaleY = (y) => height - 3 - ((y - minY) / Math.max(1, maxY - minY)) * (height - 8);
  const d = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${scaleX(p[0]).toFixed(1)},${scaleY(p[1]).toFixed(1)}`).join(' ');
  const rising = ys[ys.length - 1] >= ys[0];
  const color = rising ? '#34d399' : '#f87171';
  return `<svg class="spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round"/>
    <circle cx="${scaleX(pts[pts.length - 1][0]).toFixed(1)}" cy="${scaleY(pts[pts.length - 1][1]).toFixed(1)}" r="2.2" fill="${color}"/>
  </svg>`;
}

async function renderTable() {
  const total = view.length;
  const pages = Math.max(1, Math.ceil(total / state.size));
  state.page = Math.min(Math.max(1, state.page), pages);
  const start = (state.page - 1) * state.size;
  const slice = view.slice(start, start + state.size);
  await ensureShards(slice);

  const tbody = $('kwTable').querySelector('tbody');
  const rowsHtml = [];
  for (let n = 0; n < slice.length; n++) {
    const rowIdx = slice[n];
    const r = KW.rows[rowIdx];
    const deptNames = (r[C.dps] || []).map((i) => (SUMMARY.deptIndex[i] || {}).name).filter(Boolean);
    const primary = deptNames[0] || '-';
    const filterDept = activeFilters.deptIdx >= 0 ? SUMMARY.deptIndex[activeFilters.deptIdx] : null;
    const hitFilter = !!filterDept && deptNames.includes(filterDept.name);
    const deptLabel = hitFilter ? filterDept.name : primary;
    const deptCls = hitFilter ? 'tag active' : 'tag';
    const tr = await trendPair(rowIdx);
    const trendHtml = tr ? sparkline(tr.searches) : '<span class="flat">…</span>';
    const growth = r[C.gr];
    const gv = r[C.gv];
    const cls = growth > 0 ? 'up' : growth < 0 ? 'down' : 'flat';
    const arrow = growth > 0 ? '▲' : growth < 0 ? '▼' : '–';
    rowsHtml.push(`<tr data-row="${rowIdx}">
      <td class="num">${start + n + 1}</td>
      <td class="kw" title="${r[C.kw]}">
        <span class="kw-line">
          <span class="kw-text">${r[C.kw]}</span>
          <a class="kw-link" href="${amazonUrl(r[C.kw])}" target="_blank" rel="noreferrer" title="在 Amazon 搜索该关键词">↗</a>
        </span>
        <small>${r[C.cn] || ''}</small>
      </td>
      <td><span class="${deptCls}" title="${deptNames.join(' / ')}">${deptLabel}</span></td>
      <td class="num">${intFmt(r[C.se])}</td>
      <td class="num">${intFmt(r[C.rk])}</td>
      <td class="num">${intFmt(r[C.w1rk])}</td>
      <td class="num ${cls}">${arrow} ${intFmt(Math.abs(gv))}</td>
      <td class="num ${cls}">${growth ? (growth * 100).toFixed(1) + '%' : '0%'}</td>
      <td class="num">${intFmt(r[C.pu])}</td>
      <td class="num">${pct(r[C.pr], 2)}</td>
      <td class="num">${intFmt(r[C.np])}</td>
      <td class="num">${intFmt(r[C.spr])}</td>
      <td class="num">${intFmt(r[C.td])}</td>
      <td class="num">${pct(r[C.cs])}</td>
      <td class="num">${pct(r[C.cv])}</td>
      <td class="num">${money(r[C.bid])}</td>
      <td class="num">${intFmt(r[C.a30])}</td>
      <td>${trendHtml}</td>
    </tr>`);
  }
  tbody.innerHTML = rowsHtml.join('') || `<tr><td colspan="${COLUMNS.length}" style="text-align:center;color:#8b97b3;padding:32px">没有符合条件的关键词</td></tr>`;
  tbody.querySelectorAll('tr[data-row]').forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.closest('.kw-link')) return;   // 点跳转图标不打开详情
      openDetail(Number(tr.dataset.row));
    };
  });

  const modeText = activeFilters.deptIdx >= 0
    ? (activeFilters.mode === 'primary' ? ' · 只看主类目' : ' · 按 ABA 归类（含多类目词）')
    : '';
  $('resultHint').textContent = `命中 ${intFmt(total)} 个关键词 · 第 ${state.page}/${pages} 页 · 共 ${intFmt(KW.rows.length)} 条入库数据${modeText}`;
  renderPager(pages);
}

function renderPager(pages) {
  const p = state.page;
  const nums = [];
  const push = (n) => nums.push(`<button class="${n === p ? 'active' : ''}" data-page="${n}">${n}</button>`);
  const around = 2;
  push(1);
  if (p - around > 2) nums.push('<span class="info">…</span>');
  for (let n = Math.max(2, p - around); n <= Math.min(pages - 1, p + around); n++) push(n);
  if (p + around < pages - 1) nums.push('<span class="info">…</span>');
  if (pages > 1) push(pages);
  $('pager').innerHTML =
    `<span class="info">共 ${intFmt(view.length)} 条 / ${intFmt(pages)} 页</span>` +
    `<button data-page="${p - 1}" ${p === 1 ? 'disabled' : ''}>上一页</button>` +
    nums.join('') +
    `<button data-page="${p + 1}" ${p === pages ? 'disabled' : ''}>下一页</button>`;
  $('pager').querySelectorAll('button[data-page]').forEach((b) => {
    b.onclick = () => {
      const n = Number(b.dataset.page);
      // 翻页只重绘表格，页面滚动位置保持不动
      if (n >= 1 && n <= pages) { state.page = n; renderTable(); }
    };
  });
}

/** Amazon 关键词搜索页地址（按当前站点域名） */
function amazonUrl(keyword) {
  const host = (META && META.amazonHost) || 'www.amazon.com';
  return `https://${host}/s?k=${encodeURIComponent(keyword)}`;
}

function openDetailByKeyword(keyword) {
  const i = KW.rows.findIndex((r) => r[C.kw] === keyword);
  if (i >= 0) openDetail(i);
}

async function openDetail(rowIdx) {
  const r = KW.rows[rowIdx];
  const deptNames = (r[C.dps] || []).map((i) => (SUMMARY.deptIndex[i] || {}).name).filter(Boolean);
  const tr = await trendPair(rowIdx);
  const brands = (r[C.br] || '').split('|').filter(Boolean);
  const growth = r[C.gr];
  const keyword = r[C.kw];
  const week = (META && META.tableDate) || '';
  detailToken += 1;
  const token = detailToken;
  disposeGoogleCharts();

  const box = (l, v) => `<div class="box"><div class="l">${l}</div><div class="v">${v}</div></div>`;
  $('drawerBody').innerHTML = `
    <h2>${keyword}</h2>
    <div class="drawer-sub">${r[C.cn] || ''} · ${deptNames.map((d) => `<span class="tag">${d}</span>`).join(' ')} · 数据期 ${META.tableDate}</div>
    <div class="mini">
      ${box('月搜索量', intFmt(r[C.se]))}
      ${box('ABA 排名', intFmt(r[C.rk]))}
      ${box('上期排名', intFmt(r[C.w1rk]))}
      ${box('排名增幅', `<span class="${growth > 0 ? 'up' : growth < 0 ? 'down' : 'flat'}">${growth ? (growth * 100).toFixed(1) + '%' : '0%'}</span>`)}
      ${box('排名变化量', intFmt(r[C.gv]))}
      ${box('月购买量', intFmt(r[C.pu]))}
      ${box('购买率', pct(r[C.pr], 2))}
      ${box('在售商品', intFmt(r[C.np]))}
      ${box('SPR', intFmt(r[C.spr]))}
      ${box('标题密度', intFmt(r[C.td]))}
      ${box('点击集中度', pct(r[C.cs]))}
      ${box('转化集中度', pct(r[C.cv]))}
      ${box('建议竞价', money(r[C.bid]))}
      ${box('精准竞价', money(r[C.ep]))}
      ${box('1天广告商品', intFmt(r[C.a1]))}
      ${box('30天广告商品', intFmt(r[C.a30]))}
    </div>
    <div class="section-title">历史趋势（搜索量 / ABA 排名）</div>
    <div id="detailChart" style="height:300px"></div>
    ${googleTrendSectionHtml()}
    <div class="section-title">TOP3 品牌</div>
    <div class="brand-list">${brands.length ? brands.map((b) => `<span class="tag">${b}</span>`).join('') : '<span class="flat">—</span>'}</div>
    <div class="section-title">ASIN TOP10（该词自然位前 10 个商品）
      <span class="asin-source" id="asinSource">加载中…</span>
    </div>
    <div id="asinList" class="asin-list"><div class="asin-loading"><span class="spinner small"></span>正在读取商品数据…</div></div>`;

  $('drawer').classList.add('open');
  $('drawer').setAttribute('aria-hidden', 'false');
  agentKeyword = keyword;
  const agentButton = document.createElement('button');
  agentButton.className = 'btn agent-open';
  agentButton.textContent = '✦ 让 AI 分析这个关键词';
  agentButton.onclick = () => window.abaAgent?.open(keyword);
  $('drawerBody').insertBefore(agentButton, $('drawerBody').querySelector('.mini'));
  window.dispatchEvent(new Event('aba-context-change'));

  const el = $('detailChart');
  if (charts.detail) charts.detail.dispose();
  charts.detail = themedChart(el);
  const labels = (tr ? tr.labels : []).map(weekLabel);
  charts.detail.setOption({
    grid: { left: 58, right: 58, top: 40, bottom: 30 },
    tooltip: {
      trigger: 'axis', backgroundColor: 'rgba(12,18,35,0.95)', borderColor: 'rgba(255,255,255,0.12)',
      textStyle: { color: '#e8edf7', fontSize: 12 },
    },
    legend: { top: 2, textStyle: { color: '#8b97b3' }, icon: 'roundRect', itemWidth: 12, itemHeight: 8 },
    xAxis: { type: 'category', data: labels, ...baseAxis },
    yAxis: [
      { type: 'value', name: '搜索量', nameTextStyle: { color: '#8b97b3' }, ...baseAxis, axisLabel: { ...baseAxis.axisLabel, formatter: (v) => compact(v) } },
      { type: 'value', name: 'ABA排名', inverse: true, nameTextStyle: { color: '#8b97b3' }, ...baseAxis, splitLine: { show: false }, axisLabel: { ...baseAxis.axisLabel, formatter: (v) => compact(v) } },
    ],
    series: [
      {
        name: '搜索量', type: 'bar', data: tr ? tr.searches : [], barWidth: '46%',
        itemStyle: { borderRadius: [4, 4, 0, 0], color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(251,140,30,0.95)' }, { offset: 1, color: 'rgba(251,140,30,0.25)' }]) },
      },
      {
        name: 'ABA排名', type: 'line', yAxisIndex: 1, data: tr ? tr.ranks : [],
        smooth: true, symbol: 'circle', symbolSize: 5,
        lineStyle: { width: 2.4, color: '#6ea8fe' }, itemStyle: { color: '#6ea8fe' },
      },
    ],
  });

  const googleSection = $('keywordGoogleTrend');
  googleSection.querySelector('.google-toggle').onclick = () => toggleGoogleTrend(googleSection, keyword);
  toggleGoogleTrend(googleSection, keyword);
  renderAsinSection(keyword, week, token, rowIdx);
}

/* ---------------- 详情抽屉：ASIN TOP10 ---------------- */

let detailToken = 0;

/** 后端 /api/asin 返回的商品档案（缺失字段一律显示「-」） */
async function renderAsinSection(keyword, week, token, rowIdx) {
  const host = $('asinList');
  if (!host) return;
  let payload = null;
  let error = '';
  try {
    payload = await fetchAsinData(keyword, week);
  } catch (e) {
    error = e && e.message ? e.message : String(e);
  }
  if (token !== detailToken) return;              // 抽屉已切换到别的词，丢弃过期响应
  const box = $('asinList');
  if (!box) return;

  const asins = (payload && payload.asins) || [];
  if (!asins.length) {
    box.innerHTML = fallbackAsinHtml(rowIdx, error || (payload && payload.message));
    return;
  }
  box.innerHTML = asins.map((a, i) => asinCardHtml(a, i)).join('');
  box.querySelectorAll('.asin-card').forEach((card) => {
    const btn = card.querySelector('.ac-toggle');
    if (btn) btn.onclick = () => toggleAsinTrend(card);
  });
  // ABA 接口只给 TOP3 点击占比：不在搜索位前 10 的，单独列一行
  const outside = (payload.abaTop3 || []).filter((a) => !a.inTop);
  if (outside.length) {
    const line = document.createElement('div');
    line.className = 'ac-foot asin-top3';
    line.innerHTML = `<span class="ac-label">ABA 点击 TOP3（不在搜索位前 10）</span>` +
      outside.map((a) => `<span class="ac-chip click">${a.asin} ${a.clickRate
        ? (a.clickRate * 100).toFixed(2) + '%' : '-'}</span>`).join('');
    box.prepend(line);
  }
  const hint = $('asinSource');
  if (hint) {
    const live = payload.source === 'live';
    const missing = (payload.detailMissing || []).length;
    hint.textContent = live
      ? `· 已实时抓取 ${asins.length} 个商品`
      : (missing ? `· ${asins.length} 个商品（部分为缓存快照）` : '· 来自本地缓存');
    if (payload.error) hint.textContent += ` · ${payload.error}`;
  }
}

const asinCache = new Map();       // 周+关键词 -> 商品档案
const asinTrendCache = new Map();  // ASIN -> 价格趋势

function fetchAsinData(keyword, week) {
  const key = `${week}|${keyword}`;
  if (asinCache.has(key)) return Promise.resolve(asinCache.get(key));
  const url = `api/asin?kw=${encodeURIComponent(keyword)}&week=${encodeURIComponent(week)}`;
  return fetch(url).then((r) => r.json()).then((data) => {
    if (data && data.ok && !data.error && !(data.detailMissing || []).length) asinCache.set(key, data);
    return data;
  });
}

/** 接口不可用时退回到库里自带的 TOP3 点击占比（老数据期只有这些） */
function fallbackAsinHtml(rowIdx, reason) {
  const r = KW.rows[rowIdx];
  const list = (r[C.as] || '').split('|').filter(Boolean).map((s) => {
    const [asin, rate] = s.split(':');
    return { asin, rate: Number(rate) || 0 };
  });
  const tip = reason ? `<div class="asin-tip">ASIN 明细接口不可用：${reason}</div>` : '';
  if (!list.length) return `${tip}<div class="flat">该数据期没有 ASIN 快照（旧周数据需重抓后才有）</div>`;
  return `${tip}<div class="asin-tip">以下为库里自带的 ABA TOP3（点击占比），完整 TOP10 需要看板服务提供 /api/asin</div>` +
    list.map((a, i) => `
      <div class="asin-item">
        <span class="rank">#${i + 1}</span>
        <a href="https://www.amazon.com/dp/${a.asin}" target="_blank" rel="noreferrer">${a.asin}</a>
        <span class="bar"><span style="width:${Math.max(2, Math.min(100, a.rate))}%"></span></span>
        <span class="flat">${a.rate ? a.rate.toFixed(2) + '%' : '-'}</span>
      </div>`).join('');
}

const BADGE_LABEL = { AC: "Amazon's Choice", R: '推荐位' };

function fmtDate(ms) {
  if (!ms) return '-';
  const d = new Date(Number(ms));
  if (Number.isNaN(d.getTime())) return '-';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function metric(label, value, cls) {
  return `<span class="ac-metric"><i>${label}</i><b class="${cls || ''}">${value}</b></span>`;
}

function asinCardHtml(a, idx) {
  const prices = `$${Number(a.price).toFixed(2)}`;
  const avg = a.averagePrice ? `$${Number(a.averagePrice).toFixed(2)}` : '-';
  const price = a.price != null ? prices : '-';
  const bsr = a.bsrRank ? `#${Number(a.bsrRank).toLocaleString('en-US')}` : '-';
  const bsrDelta = a.bsrRankCv ? `${a.bsrRankCv > 0 ? '+' : ''}${a.bsrRankCv}` : '';
  const rating = a.rating != null ? Number(a.rating).toFixed(1) : '-';
  const reviews = a.reviews != null ? Number(a.reviews).toLocaleString('en-US') : '-';
  const amazonHost = (META && META.amazonHost) || 'www.amazon.com';
  const img = a.zoomImageUrl || a.imageUrl;
  const title = (a.title || '').replace(/</g, '&lt;');
  const clickRate = a.clickRate ? `<span class="ac-chip click">点击 ${(a.clickRate * 100).toFixed(2)}%</span>` : '';
  const pos = a.position ? `<span class="ac-chip">自然位 ${a.position}</span>` : '<span class="ac-chip">广告 / 新上榜</span>';
  const badge = a.badge ? `<span class="ac-chip badge">${BADGE_LABEL[a.badge] || a.badge}</span>` : '';
  const sell = [
    a.sellerName ? `${a.sellerName}${a.sellerType ? ' · ' + a.sellerType : ''}` : '',
    a.sellers ? `${a.sellers} 个卖家` : '',
    a.variations ? `${a.variations} 个变体` : '',
  ].filter(Boolean).join(' / ') || '-';
  const positions = (a.pastPositions || []).slice(0, 6)
    .map((p) => `<span class="ac-chip">${fmtDate(p.date).slice(5)} #${p.position}</span>`).join('');
  const subs = (a.subcategories || []).map((s) => `#${s.rank} ${s.label}`).join(' / ');
  const profit = a.profit != null ? `${a.profit}%` : '-';
  const fba = a.fba ? `$${Number(a.fba).toFixed(2)}` : '-';
  const units30 = a.totalUnits != null ? Number(a.totalUnits).toLocaleString('en-US') : '-';
  const revenue30 = a.totalAmount ? `$${Number(a.totalAmount).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '-';
  const dims = [a.dimensions, a.weight].filter(Boolean).join(' / ') || '-';

  return `
  <div class="asin-card" data-asin="${a.asin}">
    <div class="ac-head">
      <a class="ac-thumb" href="https://${amazonHost}/dp/${a.asin}" target="_blank" rel="noreferrer">
        ${img ? `<img loading="lazy" src="${img}" alt="${a.asin}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'ac-noimg',textContent:'无图'}))">` : '<span class="ac-noimg">无图</span>'}
      </a>
      <div class="ac-main">
        <div class="ac-line1">
          <span class="rank">#${idx + 1}</span>
          <a class="ac-asin" href="https://${amazonHost}/dp/${a.asin}" target="_blank" rel="noreferrer">${a.asin}</a>
          ${pos}${clickRate}${badge}
        </div>
        <div class="ac-title" title="${title}">${title || '（无标题）'}</div>
        <div class="ac-metrics">
          ${metric('价格', price)}
          ${metric('平均单价', avg)}
          ${metric('BSR', bsr + (bsrDelta ? ` <em>${bsrDelta}</em>` : ''))}
          ${metric('评分', rating)}
          ${metric('评分数', reviews)}
          ${metric('上架时间', fmtDate(a.availableDate))}
        </div>
        <div class="ac-metrics second">
          ${metric('近30天销量', units30)}
          ${metric('近30天销售额', revenue30)}
          ${metric('FBA 费用', fba)}
          ${metric('毛利率', profit)}
          ${metric('卖家', sell)}
          ${metric('LQS', a.lqs != null ? a.lqs : '-')}
        </div>
        <div class="ac-foot">
          ${a.brand ? `<span class="ac-chip brand">${a.brand}</span>` : ''}
          ${subs ? `<span class="ac-chip">${subs}</span>` : ''}
          ${dims !== '-' ? `<span class="ac-chip">${dims}</span>` : ''}
        </div>
        ${positions ? `<div class="ac-foot"><span class="ac-label">该词历史排名</span>${positions}</div>` : ''}
      </div>
      <div class="ac-actions">
        <button class="ac-toggle" type="button">价格趋势</button>
      </div>
    </div>
    <div class="ac-chart" hidden></div>
  </div>`;
}

async function toggleAsinTrend(card) {
  const asin = card.dataset.asin;
  const panel = card.querySelector('.ac-chart:not(.google-panel)');
  const btn = card.querySelector('.ac-toggle');
  if (!panel.hidden) {
    panel.hidden = true;
    btn.textContent = '价格趋势';
    if (charts[`asin_${asin}`]) { charts[`asin_${asin}`].dispose(); delete charts[`asin_${asin}`]; }
    return;
  }
  panel.hidden = false;
  btn.textContent = '收起';
  panel.innerHTML = '<div class="asin-loading"><span class="spinner small"></span>加载价格趋势…</div>';
  let data = asinTrendCache.get(asin);
  if (!data) {
    try {
      data = await fetch(`api/asin/trend?asin=${encodeURIComponent(asin)}`).then((r) => r.json());
      if (data && data.ok && !data.error && data.availability !== 'no_data') asinTrendCache.set(asin, data);
    } catch (e) {
      panel.innerHTML = `<div class="asin-tip">价格趋势加载失败：${e}</div>`;
      return;
    }
  }
  const trend = (data && data.trend) || [];
  if (!trend.length) {
    const message = data?.errorCode === 'ERR_USER_NOT_LOGIN'
      ? '卖家精灵登录态自动恢复暂未成功，请稍后重新展开；若持续失败，请检查自动登录配置。'
      : data?.errorCode === 'ERR_TREND_UPSTREAM'
        ? '卖家精灵月度接口暂不可用，稍后重新展开会重试。'
        : '卖家精灵目前未提供该 ASIN 的可用月度数据；系统会定期重新检查。';
    panel.replaceChildren(Object.assign(document.createElement('div'), { className: 'asin-tip', textContent: message }));
    return;
  }
  const hasValue = trend.some((p) => p.price != null || p.unit != null || p.amount != null || p.bsr != null);
  if (!hasValue) {
    panel.innerHTML = '<div class="asin-tip">当前缓存没有可用月度数值，系统会重新获取。</div>';
    return;
  }
  const srcLabel = data.source === 'live' ? '实时抓取'
    : (data.source === 'local' ? '本地商品档案推算' : '本地缓存');
  const warn = data.error
    ? `<div class="asin-tip">${data.errorCode === 'ERR_USER_NOT_LOGIN'
      ? '登录态自动恢复暂未成功' : '实时更新暂不可用'}；当前展示「${srcLabel}」数据，稍后重新展开会重试。</div>`
    : '';
  panel.innerHTML = `${warn}<div class="ac-chart-box" id="chart_${asin}"></div>
    <div class="ac-chart-tip">月销量 / 月均价来自卖家精灵 ASIN 月度趋势（${srcLabel}）`
    + `${data.note ? ' · ' + data.note : ''}</div>`;
  const dom = document.getElementById(`chart_${asin}`);
  if (!dom) return;
  if (charts[`asin_${asin}`]) charts[`asin_${asin}`].dispose();
  const chart = themedChart(dom);
  charts[`asin_${asin}`] = chart;
  const labels = trend.map((p) => p.date);
  chart.setOption({
    grid: { left: 58, right: 104, top: 34, bottom: 28 },
    tooltip: {
      trigger: 'axis', backgroundColor: 'rgba(12,18,35,0.95)', borderColor: 'rgba(255,255,255,0.12)',
      textStyle: { color: '#e8edf7', fontSize: 12 },
    },
    legend: { top: 0, textStyle: { color: '#8b97b3' }, icon: 'roundRect', itemWidth: 12, itemHeight: 8 },
    xAxis: { type: 'category', data: labels, ...baseAxis, axisLabel: { ...baseAxis.axisLabel, rotate: 40 } },
    yAxis: [
      { type: 'value', name: '月销量', nameTextStyle: { color: '#8b97b3' }, ...baseAxis, axisLabel: { ...baseAxis.axisLabel, formatter: (v) => compact(v) } },
      { type: 'value', name: '均价 $', nameTextStyle: { color: '#8b97b3' }, ...baseAxis, splitLine: { show: false } },
      {
        type: 'value', name: 'BSR', inverse: true, offset: 56,
        nameTextStyle: { color: '#8b97b3' }, ...baseAxis, splitLine: { show: false },
        axisLabel: { ...baseAxis.axisLabel, formatter: (v) => compact(v) },
      },
    ],
    series: [
      {
        name: '月销量', type: 'bar', data: trend.map((p) => p.unit), barWidth: '46%',
        itemStyle: { borderRadius: [3, 3, 0, 0], color: 'rgba(251,140,30,0.75)' },
      },
      {
        name: '月均价', type: 'line', yAxisIndex: 1, data: trend.map((p) => p.price), smooth: true,
        symbolSize: 5, lineStyle: { width: 2.2, color: '#34d399' }, itemStyle: { color: '#34d399' },
      },
      {
        name: 'BSR', type: 'line', yAxisIndex: 2, data: trend.map((p) => p.bsr), smooth: true,
        symbolSize: 5, lineStyle: { width: 2, color: '#6ea8fe' }, itemStyle: { color: '#6ea8fe' },
      },
    ],
  });
}

const googleTrendCache = new Map();
let googleChartId = 0;

function googleTrendSectionHtml() {
  return `<section id="keywordGoogleTrend" class="keyword-google-trend" aria-label="关键词谷歌趋势">
    <div class="section-title">谷歌趋势（Google 网页搜索）<button class="ac-toggle google-toggle" type="button" aria-expanded="false" aria-controls="keywordGooglePanel">展开</button></div>
    <div id="keywordGooglePanel" class="google-panel" hidden></div>
  </section>`;
}

function disposeGoogleCharts() {
  Object.keys(charts).filter(key => key.startsWith('google_')).forEach(key => {
    charts[key].dispose();
    delete charts[key];
  });
}

function fetchGoogleTrend(keyword) {
  const key = `${META?.market || 'COM'}|${keyword}`;
  const cached = googleTrendCache.get(key);
  if (cached && Date.now() - cached.at < 24 * 3600 * 1000) return cached.promise;
  const entry = { at: Date.now(), promise: null };
  entry.promise = fetch(`api/keyword/google-trends?kw=${encodeURIComponent(keyword)}`)
    .then(async response => {
      if (!response.ok) throw new Error('谷歌趋势服务暂不可用，请稍后重试');
      const data = await response.json();
      if (!data.ok) throw new Error(data.error || '谷歌趋势加载失败，请稍后重试');
      if (data.stale || data.error) googleTrendCache.delete(key);
      return data;
    }).catch(error => { googleTrendCache.delete(key); throw error; });
  googleTrendCache.set(key, entry);
  return entry.promise;
}

async function toggleGoogleTrend(card, keyword) {
  const panel = card.querySelector('.google-panel');
  const button = card.querySelector('.google-toggle');
  const request = (Number(panel.dataset.request) || 0) + 1;
  panel.dataset.request = request;
  if (!panel.hidden) {
    panel.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    button.textContent = '展开';
    if (charts[panel.dataset.chart]) {
      charts[panel.dataset.chart].dispose();
      delete charts[panel.dataset.chart];
    }
    return;
  }
  const token = detailToken;
  panel.hidden = false;
  button.setAttribute('aria-expanded', 'true');
  button.textContent = '收起';
  panel.innerHTML = '<div class="asin-loading" role="status"><span class="spinner small"></span>加载谷歌趋势…</div>';
  const current = () => card.isConnected && !panel.hidden && token === detailToken && Number(panel.dataset.request) === request;
  try {
    const data = await fetchGoogleTrend(keyword);
    if (!current()) return;
    const points = data.trend || [];
    if (!points.some(point => point.value != null)) {
      panel.innerHTML = '<div class="asin-tip" role="status">该关键词暂无谷歌搜索趋势数据</div>';
      return;
    }
    panel.innerHTML = `<div class="google-toolbar"><span class="google-keyword"></span><div class="google-ranges" aria-label="谷歌趋势时间范围">
      <button class="ac-toggle" type="button" data-years="1" aria-pressed="false">近1年</button>
      <button class="ac-toggle" type="button" data-years="3" aria-pressed="false">近3年</button>
      <button class="ac-toggle" type="button" data-years="5" aria-pressed="true">全部</button>
    </div></div><div class="ac-chart-box google-chart-box" role="img"></div><div class="ac-chart-tip"></div>`;
    panel.querySelector('.google-keyword').textContent = `${keyword} · Google 网页搜索 · ${data.station}`;
    panel.querySelector('.ac-chart-tip').textContent = `搜索指数为 0–100 的相对热度，范围切换保留近5年口径；${data.stale ? '缓存已过期 · ' + data.error : '更新于 ' + fmtDate(data.fetchedAt * 1000)}。`;
    const dom = panel.querySelector('.google-chart-box');
    dom.setAttribute('aria-label', `${keyword} 谷歌搜索指数趋势，支持下方缩放和时间范围按钮`);
    const id = `google_${++googleChartId}`;
    panel.dataset.chart = id;
    const chart = charts[id] = themedChart(dom);
    const render = years => {
      const end = new Date(points[points.length - 1].time);
      const start = new Date(end);
      start.setUTCFullYear(start.getUTCFullYear() - years);
      const selected = years === 5 ? points : points.filter(point => point.time >= start.getTime());
      panel.querySelectorAll('[data-years]').forEach(btn => btn.setAttribute('aria-pressed', String(Number(btn.dataset.years) === years)));
      chart.setOption({
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        grid: { left: 48, right: 20, top: 30, bottom: 78 },
        tooltip: { trigger: 'axis', renderMode: 'richText', formatter: params => {
          const point = selected[params[0]?.dataIndex];
          return point ? `${point.date}\n谷歌搜索指数：${point.value == null ? '无数据' : point.label}` : '';
        } },
        xAxis: { type: 'category', boundaryGap: false, data: selected.map(point => point.date), axisLabel: { color: '#8b97b3', rotate: 35 } },
        yAxis: { type: 'value', name: '搜索指数', min: 0, max: 100, axisLabel: { color: '#8b97b3' }, nameTextStyle: { color: '#8b97b3' }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.05)' } } },
        dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 4, height: 20 }],
        series: [{ name: '谷歌搜索指数', type: 'line', showSymbol: false, connectNulls: false,
          data: selected.map(point => ({ value: point.value, label: point.label })),
          lineStyle: { width: 2, color: '#6ea8fe' }, itemStyle: { color: '#6ea8fe' } }],
      }, { notMerge: true });
    };
    render(5);
    panel.querySelectorAll('[data-years]').forEach(btn => { btn.onclick = () => render(Number(btn.dataset.years)); });
  } catch (error) {
    if (!current()) return;
    panel.innerHTML = '<div class="asin-tip" role="status"></div><button class="ac-toggle" type="button">重试</button>';
    panel.querySelector('.asin-tip').textContent = error.message || '谷歌趋势加载失败';
    panel.querySelector('button').onclick = () => { panel.hidden = true; toggleGoogleTrend(card, keyword); };
  }
}

function closeDetail() {
  detailToken += 1;
  disposeGoogleCharts();
  $('drawer').classList.remove('open');
  $('drawer').setAttribute('aria-hidden', 'true');
  agentKeyword = '';
  window.dispatchEvent(new Event('aba-context-change'));
}

window.getAbaAgentContext = () => {
  return { keyword: agentKeyword };
};
window.navigateAbaEvidence = async (week, keyword) => {
  if (week && META?.tableDate !== week) await loadWeek(week);
  const params = new URLSearchParams({week: META.tableDate});
  if (keyword) { params.set('keyword', keyword); openDetailByKeyword(keyword); }
  history.replaceState(null, '', '?' + params.toString());
};

/* ------------------------------ 交互绑定 ------------------------------ */

function bindGlobalEvents() {
  let timer = null;
  $('globalSearch').oninput = (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.q = e.target.value; applyFilters(1); }, 220);
  };
  $('resetBtn').onclick = resetFilters;
  $('weekSelect').onchange = (e) => {
    const w = e.target.value;
    try { history.replaceState(null, '', `?week=${w}`); } catch (err) {}
    loadWeek(w);
  };
  $('toggleFilter').onclick = () => {
    const panel = $('filterPanel');
    const collapsed = panel.classList.toggle('collapsed');
    $('toggleFilter').textContent = collapsed ? '展开筛选 ▼' : '收起筛选 ▲';
  };
  $('drawerClose').onclick = closeDetail;
  $('drawer').onclick = (e) => { if (e.target === $('drawer')) closeDetail(); };
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDetail(); });
  $('exportBtn').onclick = exportCsv;
  renderTableHead();
}

function exportCsv() {
  const headers = COLUMNS.filter((c) => c.key !== 'spark').map((c) => c.label);
  const lines = [headers.join(',')];
  const max = Math.min(view.length, 20000);
  for (let n = 0; n < max; n++) {
    const r = KW.rows[view[n]];
    const dept = (r[C.dps] || []).map((i) => (SUMMARY.deptIndex[i] || {}).name).filter(Boolean).join(' / ');
    const cells = [
      n + 1, r[C.kw], dept, r[C.se], r[C.rk], r[C.w1rk], r[C.gv], (r[C.gr] * 100).toFixed(2) + '%',
      r[C.pu], (r[C.pr] * 100).toFixed(2) + '%', r[C.np], r[C.spr], r[C.td],
      (r[C.cs] * 100).toFixed(2) + '%', (r[C.cv] * 100).toFixed(2) + '%', r[C.bid], r[C.a30],
    ];
    lines.push(cells.map((c) => {
      const s = String(c ?? '');
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    }).join(','));
  }
  const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `aba-${META.tableDate}-筛选结果.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

init().catch((err) => {
  console.error(err);
  setLoading(true, '加载失败：' + err.message);
});
