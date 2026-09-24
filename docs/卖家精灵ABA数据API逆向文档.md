# 卖家精灵（SellerSprite）ABA 数据研究接口逆向文档

> 目标接口：`POST https://www.sellersprite.com/v3/api/aba-research`
> 逆向时间：2026-09-16（前端构建时间 2026-09-15 22:28:19）
> 本文所有结论均在真实账号会话下**实测验证**通过（含请求体 225 字节的逐字节复现）。

---

## 0. 结论速览（TL;DR）

- 该接口是**纯 Cookie 鉴权**的 JSON POST 接口，**没有签名、没有 CSRF token、没有时间戳校验**；只要带登录态的 Cookie（关键是 `Sprite-X-Token` + `JSESSIONID` + `rank-login-user`）就能直接调用。
- 请求体是扁平 JSON，`order` 是唯一嵌套对象。**没有 `pageSize`、没有 `limit/skip`**，分页用 `page` + `size`。
- `size` 服务端会强制下限：请求 < 20 会被当成 50 返回；游客会话固定 20；实测 `size=2000` 也能返回。
- 深翻页没有 10000 条窗口限制，实测 `page=5000&size=50`（偏移 25 万）正常返回。
- 排序字段是**白名单**，传了不支持的字段（如 `w1RankGrowthRate`）会得到 `ERR_GLOBAL_500`，不是 400。
- 过滤用的百分比字段（`minRankGrowthRate` 等）用**百分比数值**（`20` = 20%），而响应里的同名比率是**小数**（`0.2`）。

最小可用请求（实测 HTTP 200 / `code=OK`）：

```bash
curl -s https://www.sellersprite.com/v3/api/aba-research \
  -H 'content-type: application/json;charset=UTF-8' \
  -H 'origin: https://www.sellersprite.com' \
  -H 'referer: https://www.sellersprite.com/v3/aba-research' \
  -H "cookie: $SELLERSPRITE_COOKIE" \
  -d '{"rankGrowthType":"W1","size":50,"page":1,"market":"COM","q":"","table":"ara_20260912","reverseType":"W","departments":[],"order":{"field":"searchfrequencyrank","desc":false},"keywordBidMatchType":"exact","movementMarket":""}'
```

---

## 1. 逆向路径（怎么找到的）

站点是 webpack 打包的 Vue SPA，接口定义全部在前端 bundle 里明文可见：

| 文件 | 作用 |
| --- | --- |
| `/v3/webapp/static/js/app.85491af1e273.js` | 公共模块：axios 实例、拦截器、站点映射表 |
| `/v3/webapp/static/js/chunk-95d370c2.56eef6ba6c0e.js` | **ABA 接口定义**（webpack 模块 `4109`） |
| `/v3/webapp/static/js/chunk-1cd0c6a1.e934c2d9c053.js` | ABA 页面：筛选表单、`getOptions()` 请求体拼装 |
| `/v3/webapp/static/js/runtime.476b0773a13e.js` | chunk 哈希映射表（用来自动定位懒加载 chunk） |

定位思路：
1. 抓 `GET /v3/aba-research` 的 HTML → 拿到 `app.*.js`；
2. 在 `app.js` 里搜 `"/v3/api/aba-research"` → 发现它来自模块 `a7b1` 的 `researchUrl` 常量（同时暴露了全部 `/v3/api/*` 业务接口）；
3. 在路由表里搜 `path:"aba-research"` → 得到懒加载 chunk 名 `chunk-95d370c2`、`chunk-1cd0c6a1`；
4. 用 runtime 的哈希表拼出真实文件名并下载；
5. `chunk-95d370c2` 模块 `4109` = 8 个 ABA 接口的完整定义；`chunk-1cd0c6a1` 的 `getOptions()` = 请求体的**精确**拼装规则。

请求体校验：按 `getOptions()` 重建出的 JSON 长度 **225 字节**，与你抓到的 `content-length: 225` 完全一致，说明参数模型已 100% 还原。

---

## 2. 鉴权机制

### 2.1 实际需要的东西

请求头里**没有任何**鉴权头，身份完全由 Cookie 承担：

| Cookie | 作用 | 备注 |
| --- | --- | --- |
| `Sprite-X-Token` | **主鉴权**，RS256 JWT | `iat`→`exp` 恰好 24h，过期后所有接口变游客/未登录 |
| `JSESSIONID` | 会话绑定 | 与 Token 配套 |
| `rank-login-user` / `rank-login-user-info` | 用户名与账号信息 | 前端用来渲染，不是强校验项 |
| `ecookie` / `current_guest` / `_ga*` 等 | 风控/统计 | 建议原样带上，降低被风控概率 |

JWT 载荷示例（解码后）：

```json
{"jti":"xhA9tjqnBJU6Uf-nVvuXpA","iat":1789548211,"exp":1789634611,"nbf":1789548151,
 "sub":"yunyu","iss":"rank","aud":"sellerSpace","id":1767672,"pi":995397,
 "nn":"您锋哥","sys":"SS_CN","ed":"N","em":"xxx@sellersprite.com","ml":"A","end":1807260211979}
```

即 `iat = 2026-09-16 16:43:31 (+08)`，`exp = 2026-09-17 16:43:31 (+08)`。**每天需要重新获取一次 Cookie**（重新登录网页即可，`Sprite-X-Token` 会自动刷新）。

### 2.2 关于签名头 `Auth-Sign`

axios 请求拦截器里有这么一段：

```js
const t = Object(l["v"])();                      // 读取签名相关本地数据
t["_sign"] && (e.headers["Auth-Sign"] = t["_sign"]);
t["PARTNER_TOKEN"] && (e.headers["PARTNER-TOKEN"] = t["PARTNER_TOKEN"]);
```

即**部分模块**（如关键词反查/流量拓展）在客户端会带 `Auth-Sign`。但：
- 你抓到的 ABA 请求里没有 `Auth-Sign`；
- 本次实测在**完全不构造签名**的情况下，ABA 全部 8 个接口均返回 `code=OK`。

结论：ABA 数据模块当前**不需要**签名，只需 Cookie。

### 2.3 游客口径

不带任何 Cookie 直接 POST 也返回 `code=OK`，但：
- 返回行数被强制为 20；
- 部分高价值字段（`exactPpc`、`top3AsinDtoList` 的图片/价格等）为 `null`；
- `export` 类接口不可用。

---

## 3. 接口清单

### 3.1 ABA 模块（webpack 模块 `4109`）

| # | Method | Path | 关键参数 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | GET | `/v3/api/aba-research/tables` | — | 周表/月表清单 + 各站点数据起始期 |
| 2 | GET | `/v3/api/aba-research/{marketId}/departments` | marketId | 类目（department）枚举 |
| 3 | POST | `/v3/api/aba-research` | JSON body | **核心：关键词列表 / 选品** |
| 4 | POST | `/v3/api/aba-research/async-export?exportGkImages=false` | 同 3 + `keywordList` | 异步导出任务（消耗额度） |
| 5 | GET | `/v3/api/aba-research/trends` | `interval,keyword,table,market` | 单关键词历史趋势 |
| 6 | GET | `/v3/api/aba-research/yearly-trends` | 同上 | 按年分组的趋势 |
| 7 | POST | `/v3/api/aba-research/asin/past-position` | `asins,keyword,market,reverseType` | 关键词下 ASIN 历史排名 |
| 8 | POST | `/v3/api/aba-research/brand/past-position` | `brands,keyword,market,reverseType` | 关键词下品牌历史排名 |

### 3.2 页面图表抽屉用到的旁路接口（模块 `23ce`，非 ABA 前缀但同页使用）

| Method | Path |
| --- | --- |
| GET | `/v3/api/trends/keyword/history-trend/{keyword}` |
| GET | `/v3/api/trends/keyword/history-trend-month/{keyword}` |
| GET | `/v3/api/trends/keyword-stat/gkdata` |
| GET | `/v3/api/trends/keyword-stat/month` |
| GET | `/v3/api/trends/keywords/{a}/{b}` |
| GET | `/v2/keyword/google-trends.json` |
| GET | `/v2/keyword/ara-click-rate-trends.json` |
| GET | `/v2/keyword/ppc-trends.json` |

---

## 4. 核心接口 `POST /v3/api/aba-research` 详解

### 4.1 请求体字段表

**固定字段**

| 字段 | 类型 | 说明 | 取值 |
| --- | --- | --- | --- |
| `market` | string | 站点 | `COM` `JP` `UK` `DE` `FR` `IT` `ES` `CA` `IN` `MX` `AU` `BR` `AE` `SA` |
| `table` | string | 数据期表名 | 周表 `ara_YYYYMMDD`；月表 `ara_YYYYMM` |
| `reverseType` | string | 周/月口径 | `W`（周）/ `M`（月） |
| `rankGrowthType` | string | 增长率对比周期 | `W1` `W2` `W3` `W4` |
| `q` | string | 关键词模糊搜索 | 可为空串 |
| `page` | int | 页码，从 1 开始 | 实测 5000 仍可用 |
| `size` | int | 每页条数 | 实测 20~2000；< 20 被服务端按 50 处理 |
| `order` | object | 排序 | `{"field":"searches","desc":true}` |
| `departments` | string[] | 类目过滤 | 可多选，见 4.4 |
| `keywordBidMatchType` | string | 竞价匹配方式 | `exact` / `phrase` / `broad` |
| `movementMarket` | bool \| `""` | 飙升市场口径 | `true` 或 `""` |
| `includeKeywords` | string | 包含词，逗号分隔 | 前端限制最多 20 个 |
| `excludeKeywords` | string | 排除词，逗号分隔 | 同上 |
| `minWordCount` / `maxWordCount` | int | 词数区间 | 仅 2024-09-14 之后的数据期生效 |

**数值过滤字段（均可选，不加即不限制）**

| 字段 | 单位 | 对应页面筛选项 |
| --- | --- | --- |
| `minSearches` / `maxSearches` | 次 | 月搜索量 |
| `minSearchRank` / `maxSearchRank` | 名次 | ABA 排名 |
| `minRankGrowthRate` / `maxRankGrowthRate` | **百分比** | 排名增长率 |
| `minRankGrowthValue` / `maxRankGrowthValue` | 名次 | 排名变化量（需配合 `rankGrowthType`） |
| `minImpressions` / `maxImpressions` | 次 | 月展示量 |
| `minClicks` / `maxClicks` | 次 | 月点击量 |
| `minMonopolyClickRate` / `maxMonopolyClickRate` | **百分比** | TOP3 点击集中度 |
| `minConversionRate` / `maxConversionRate` | **百分比** | TOP3 转化集中度 |
| `minSPR` / `maxSPR` | 整数 | SPR（`cprExact`） |
| `minTitleDensity` / `maxTitleDensity` | 整数 | 标题密度 |

> 实测：以上过滤字段全部被服务端接受且真实生效（对比 `total` 变化），例外是 `nodeIdPaths`——它属于商品研究模块的参数，ABA 接口接受但不参与过滤。

### 4.2 前端拼装规则（三个坑）

来自页面 `getOptions()`，复刻时要注意：

```js
const 数值白名单 = [];                       // 实际上为空数组，所以没有任何字段做 /100 换算
const 字符串字段 = ["q","order","rankGrowthType","departments","table","market",
                    "reverseType","orderBy","desc","nodeIdPaths","includeKeywords",
                    "excludeKeywords","movementMarket","keywordBidMatchType"];
for (const k in body) {
  if (字符串字段.includes(k)) continue;      // 字符串字段：原样保留，哪怕是空串
  if (body[k] === "") { delete body[k]; continue; }   // 其它字段：空串=删除
  body[k] = Number(body[k]);                 // 其余一律转数字
}
body.order = { field: body.orderBy, desc: body.desc };
delete body.orderBy; delete body.desc;
```

三个坑：
1. **空串语义不一致**：`movementMarket: ""`、`q: ""` 会被保留发送；而 `minSearches: ""`、`minWordCount: ""` 会被**删掉**。
2. 前端**不做**百分比换算，直接发 `20` 表示 20%。
3. `orderBy` / `desc` 只是前端字段，最终必须变成 `order: {field, desc}`。

### 4.3 排序字段白名单（实测）

✅ 可用：`searches`、`searchfrequencyrank`、`impressions`、`clicks`、`purchases`、`purchaseRate`、`products`、`cprExact`、`titleDensityExact`、`adProducts1`、`adProducts7`、`adProducts30`、`bid`、`bidMax`、`bidMin`

❌ 返回 `ERR_GLOBAL_500`：`w1RankGrowthRate`、`w4RankGrowthRate`、`w12RankGrowthRate`、`searchRankGrowthRate`、`rankGrowthRate`、`searchRankGrowthValue`、`w1SearchRank`、`w4SearchRank`、`w12SearchRank`、`searchRank`、`exactPpc`、`phrasePpc`、`broadPpc`、`min/max*Ppc`、`clickShareRate`、`cvsShareRate`、`top3sumConversionRate`、`movementMarket`、`keyword`、`trends`

> 注意：页面表格里「排名增幅」列其实是**不可排序**的，UI 也没有开排序，这是后端白名单的真实边界。

### 4.4 类目 code（`departments`）

`GET /v3/api/aba-research/{marketId}/departments` 返回，常用值：

`any`(全部分类) `pets` `electronics` `beauty` `lawngarden` `baby-products` `handmade` `mi` `sporting` `arts-crafts` `automotive` `tools` `hpc` `kitchen` `computers` `office-products` `mobile` …

### 4.5 页面「快速选品模型」等价参数

页面上 6 个一键模型（`changeMode`）对应的参数组合：

| 模式 | 含义 | 等价参数 |
| --- | --- | --- |
| 1 | 整体市场 | `maxSearchRank` = 25万(US) / 10万(JP/DE/UK/IN) / 5万(其它) |
| 2 | 飙升市场 | `movementMarket: true` |
| 3 | 4 周持续增长 | `rankGrowthType:"W4"`, `rankGrowthValue:10000`, `minRankGrowthRate:10` |
| 4 | 上周增幅>50% | `rankGrowthType:"W1"`, `minRankGrowthRate:50` |
| 5 | 上周增幅>20% | `rankGrowthType:"W1"`, `minRankGrowthRate:20`, 叠加搜索排名区间 |
| 6 | 蓝海词 | `maxMonopolyClickRate:30`, `maxConversionRate:30`, 叠加 `maxSearchRank` |

---

## 5. 响应结构

```jsonc
{
  "code": "OK",
  "message": "成功",
  "success": true,
  "data": {
    "page": 1, "size": 50, "pages": 54365, "total": 2718204,
    "took": 9, "terminal": null, "hasNextPage": null, "guestVisited": false,
    "order": { "field": "", "desc": true },
    "items": [ { /* 见下表 */ } ]
  }
}
```

单行 `items[]` 字段字典：

| 字段 | 含义 | 备注 |
| --- | --- | --- |
| `keyword` / `keywordCn` / `keywordJp` | 关键词及中/日译名 | |
| `searches` | 月搜索量 | |
| `clicks` / `impressions` | 月点击量 / 月展示量 | |
| `purchases` / `purchaseRate` | 月购买量 / 购买率 | 购买率是**小数**（`0.007` = 0.7%） |
| `products` | 在售商品数 | |
| `searchRank` | 本期 ABA 搜索频率排名 | 越小越热门 |
| `searchRankGrowthValue` / `searchRankGrowthRate` | 排名变化量 / 变化率 | |
| `w1SearchRank` / `w1RankGrowthValue` / `w1RankGrowthRate` | 上一个周期的同名字段 | `W1` 口径；`w4*`、`w12*` 同理 |
| `clickShareRate` / `cvsShareRate` | TOP3 点击集中度 / 转化集中度 | 小数 |
| `top3AsinDtoList` | TOP3 ASIN | `{asin, imageUrl, clickRate, conversionRate}` |
| `top3Brands` | TOP3 品牌 | |
| `titleDensityExact` | 标题密度 | |
| `cprExact` | SPR | |
| `bid` / `bidMin` / `bidMax` | 建议竞价区间 | |
| `exactPpc` / `phrasePpc` / `broadPpc`（含 `min*`/`max*`） | 各匹配方式竞价 | 部分会话为 `null` |
| `adProducts1` / `adProducts7` / `adProducts30` | 近 1/7/30 天广告商品数 | |
| `departments` | 命中的类目 | |
| `gkDatas` | 关键词下的 ASIN 快照（自然位/广告位） | 体积大，含 `position`、`badges`、`rankPage` |
| `trends` | 该词的历史序列 | `{label, searches, rank, searchesGrowthRate, rankGrowthRate}` |

> 客户端的 `search()` 额外补了 `marketId`、`website`、`tableHistoryDate`、`top3ClickRate`、`top3sumConversionRate`，与前端表格保持一致。

---

## 6. 站点映射（marketId ↔ code ↔ publicCode）

| marketId | code | publicCode | 站点 |
| --- | --- | --- | --- |
| 1 | US | COM | 美国（数据等同 GLOBAL） |
| 6 | JP | JP | 日本 |
| 3 | UK | UK | 英国 |
| 4 | DE | DE | 德国 |
| 5 | FR | FR | 法国 |
| 35691 | IT | IT | 意大利 |
| 44551 | ES | ES | 西班牙 |
| 7 | CA | CA | 加拿大 |
| 44571 | IN | IN | 印度 |
| 771770 | MX | MX | 墨西哥 |
| 111172 | AU | AU | 澳洲 |
| 15 | BR | BR | 巴西 |
| 9 | AE | AE | 阿联酋 |
| 13 | SA | SA | 沙特 |

注意两个参数用的是**不同口径**：`departments` 接口路径用 `marketId`（数字），列表接口的 `market` 用 `publicCode`（字符串）。

> `tables.restrictions` 显示目前只有 BR/MX/AU 有 2024-01 起的完整历史，SA/AE 从 2025-07-05 起，其余站点按页面默认区间提供。

### 数据期

| 口径 | 表名格式 | 实测条数 |
| --- | --- | --- |
| 周 | `ara_20260912` | 141 期（约回溯 3 年） |
| 月 | `ara_202608` | 44 期 |

---

## 7. 错误码

| code | 含义 | 触发场景 |
| --- | --- | --- |
| `OK` | 成功 | |
| `ERR_GLOBAL_400` | 参数错误 | 缺少 `keyword`（如 asin/past-position 少了关键字） |
| `ERR_GLOBAL_500` | 服务端异常 | **排序字段不在白名单**、趋势接口参数名写错 |
| `ERR_USER_NOT_LOGIN` / `ERR_GLOBAL_SESSION_EXPIRED` | 登录态失效 | Cookie / Token 过期 |
| `ERR_REQUIRE_GUEST_ACCESS` | 需要游客态 | |
| `ERR_ROBOT_CHECK` | 触发人机校验 | 高频请求 |
| `ERR_PAID_ONLY` | 套餐未覆盖 | 部分趋势/年表数据 |
| `ERR_LOGIN_ACCOUNT_INCONSISTENT` | 账号不一致 | 多账号串 Cookie |

---

## 8. 导出接口

```
POST /v3/api/aba-research/async-export?exportGkImages=false
```

- 请求体 = 列表接口的 body + `keywordList: ["关键词1","关键词2", ...]`（前端会 `delete body.limit; delete body.skip;`）。
- 返回后前端弹出「前往下载页」，实际产物在 `/v2/export-log` 页面下载。
- **会消耗账号的导出额度**（前端 `exportCount`、`NEKD` 配额接口），因此本次逆向**未实际触发导出**，避免扣减你的额度。需要时用 `AbaClient.export_async()` 调用即可。

---

## 9. 实测结果（本次验证）

### 9.1 请求体逐字节复现

你抓到的包 `content-length: 225`，重建结果：

```json
{"rankGrowthType":"W1","size":50,"page":1,"market":"COM","q":"","table":"ara_20260912","reverseType":"W","departments":[],"order":{"field":"searchfrequencyrank","desc":false},"keywordBidMatchType":"exact","movementMarket":""}
```

长度 **225 字节**，与抓包一致。

### 9.2 行为边界实测

| 用例 | 结果 |
| --- | --- |
| `size` = 1/5/19 | 服务端按 50 返回（`data.size=50`） |
| `size` = 20 / 21 / 100 / 1000 / 2000 | 按请求值返回 |
| 无 Cookie | `code=OK`，固定 20 条（游客口径） |
| `page` = 200 / 201 / 1000 / 5000 | 全部正常，无 10000 偏移限制 |
| `sort=w1RankGrowthRate` | `ERR_GLOBAL_500` |
| `sort=searches&desc=true` | 首位 `reacher`，2,529,786 次 |
| `q=phone stand` | `total=373` |
| `departments=["kitchen"]` | `total=272,045` |
| `minRankGrowthRate=50` | 返回行 `w1RankGrowthRate` 均 ≥ 0.5，印证百分比→小数语义 |
| `movementMarket=true` | 返回行 `w1RankGrowthRate=null`（新上榜词，无历史基线） |
| `minWordCount=2&maxWordCount=4` | `total=2,075,052` |

### 9.3 端到端选品示例

条件：美国站 / Home & Kitchen / 周表 `ara_20260912` / 月搜索量 2 万~5 万 / 排名增幅 ≥ 20% / 按搜索量降序

```bash
python aba_api.py dump --market COM --departments kitchen \
  --min-searches 20000 --max-searches 50000 --min-rank-growth-rate 20 \
  --sort searches --desc --max-items 200 --out aba选品示例.csv
```

命中 **96 条**，Top3：`carhartt`(46,762) / `boo basket`(44,832) / `halloween party decorations`(44,683)，产物见同目录 CSV 与 XLSX。

---

## 10. 选品实战配方

```python
from aba_api import AbaClient

aba = AbaClient.from_cookie_file("cookie.txt", min_interval=0.5)

# A. 蓝海低竞争：月搜索 1万~10万、点击集中度<30%、转化集中度<30%、标题密度低
aba.search(market="COM", table=aba.latest_week_table(),
           minSearches=10000, maxSearches=100000,
           maxMonopolyClickRate=30, maxConversionRate=30, maxTitleDensity=20,
           sort="searches", desc=True, size=100)

# B. 上升期关键词：上周排名增幅 >50%
aba.search(market="COM", rank_growth_type="W1", extra={"minRankGrowthRate": 50},
           sort="searches", desc=True, size=100)

# C. 新上榜（飙升市场）：movementMarket
aba.search(market="COM", movement_market=True, size=100)

# D. 类目 + 词数 + SPR 区间（适合找可做的长尾）
aba.search(market="COM", departments=["kitchen"],
           include_keywords="bamboo", exclude_keywords="case",
           min_word_count=2, max_word_count=4,
           minSPR=20, maxSPR=200, sort="cprExact", desc=False)

# E. 月表口径（月度对比/季节性）
aba.search(market="COM", reverse_type="M", table=aba.latest_month_table(),
           rank_growth_type="W1", sort="searches", desc=True)

# F. 批量落表（自动翻页）
rows = list(aba.iter_search(departments=["pets"], minSearches=20000, max_items=2000))
```

---

## 11. 风险与注意事项

1. **Token 24 小时过期**：`Sprite-X-Token` 是唯一硬鉴权，过期后表现为返回游客数据（行数变 20、竞价字段为 `null`）或 `ERR_USER_NOT_LOGIN`。生产脚本务必监听这两种信号并重新取 Cookie。
2. **配额**：账号对查询次数/导出次数有配额（前端有 `KTS`/`NEKD`、`ERR_PAID_ONLY` 分支）。逆向不等于可以无限刷，建议 `min_interval ≥ 0.5s` 并做总量上限。
3. **风控**：`ERR_ROBOT_CHECK` 会出现；保持 UA/Referer/Origin 与浏览器一致，避免并发轰炸。
4. **Cookie 即密码**：`Sprite-X-Token` 泄露等于账号被接管，不要提交到 Git、不要跨环境传播（本次验证用的 Cookie 落在 `work/cookie.txt`，建议用完删除，并因为已在会话中明文传输过而考虑重新登录一次轮换 Token）。
5. **字段兼容性**：`exactPpc` 等竞价字段在部分会话/站点可能为 `null`；`gkDatas` 体积大（单行可达数 KB），批量抓取建议先剔除再落表。
6. **合规**：该接口属于 SellerSprite 商业服务的数据出口，请在自己账号授权范围内使用，遵守其服务条款，避免对外二次分发原始数据。

---

## 12. 详情页 / ASIN 维度接口（2026-09-20 补充实测）

> 背景：看板详情抽屉要从「TOP3」升级到「TOP10 ASIN + 商品图片 / 价格趋势 / BSR / 评分数 / 上架时间」。
> 以下接口全部在登录态下实测通过（`market=COM`、`marketId=1`）。

### 12.1 关键词下的 TOP10 商品从哪来

`POST /v3/api/aba-research` 每一行自带两组 ASIN 字段：

| 字段 | 条数 | 内容 | 能否扩到 10 |
| --- | --- | --- | --- |
| `top3AsinDtoList` | **固定 3 条**（后端写死，无任何参数可调） | asin / imageUrl / clickRate / conversionRate | ❌ |
| `gkDatas` | **固定 10 条** | asin / position（自然位）/ rankIndex（页面位次，含广告位）/ rankPage / badges / asinImage / asinPrice / asinReviews / asinRating / asinTitle | ✅ 本身就是 10 条 |

实测 100 个关键词，`gkDatas` **全部是 10 条**；`position` 取值 1~9（同一自然位可能对应「广告位 + 自然位」两条），
`badges`：`AC` = Amazon's Choice，`R` = 其它推荐位。

**结论**：TOP10 可以做到 —— 用 `gkDatas` 的前 10 条；但 ABA 只对 TOP3 提供点击/转化占比，
接口层面拿不到 TOP4~10 的点击占比（页面上的「TOP3 ASIN」列同理）。所以看板把 TOP10 定义为
「该词自然位前 10 个商品」，另外把 TOP3 点击占比作为附加信息展示。

### 12.2 `POST /v3/api/aba-research/asin/past-position` —— ASIN 完整商品档案

```json
{"asins": ["B07BGLT25K", "B0CSPTWZYH"], "keyword": "toilet paper", "market": "COM", "reverseType": "W"}
```

- 返回 `data` = `{asin: 商品对象}`；**实测一次传 120 个 ASIN 正常返回 120 个**（适合批量）。
- 商品对象共 124 个字段，常用的：

| 字段 | 含义 |
| --- | --- |
| `imageUrl` / `zoomImageUrl` | 主图（200 / 600） |
| `title` / `brand` / `brandUrl` / `categoryName` / `nodeLabelPath` | 标题 / 品牌 / 类目路径 |
| `price` / `averagePrice` / `coupon` / `primeExclusivePrice` / `deliveryPrice` | 现价 / 期间均价 / 券 / Prime 价 / 配送价 |
| `bsrRank` / `bsrLabel` / `bsrRankCv` / `bsrRankCr` / `subcategories[]` | BSR 排名 / 类目 / 变化量 / 变化率 / 子类目排名 |
| `rating` / `reviews` / `reviewsRate` / `reviewsIncreasement` / `reviewsDelta` | 评分 / 评论数 / 留评率 / 新增评论数 |
| `availableDate` / `publishDate` / `firstReviewDate` / `availableDays` | 上架时间 / 首次评论 / 已上架天数 |
| `totalUnits` / `totalAmount` / `amzUnit` / `amzUnitTrend` / `salesTrend` | 近 30 天销量 / 销售额 / 估算月销量 / 月度曲线（JSON 字符串） |
| `fba` / `profit` / `lqs` | FBA 费用 / 毛利率(%) / Listing 质量分 |
| `sellerName` / `sellerType` / `sellerNation` / `sellers` / `variations` / `parent` / `sku` | 卖家 / 配送方式(AMZ·FBA·FBM) / 卖家数 / 变体数 / 父 ASIN / SKU |
| `dimensions` / `weight` / `pkgDimensions` / `pkgWeight` / `pkgVolumeWeights` | 商品与包装尺寸重量 |
| `bestSeller` / `amazonChoice` / `newRelease` / `video` / `ebc` | 榜单与标记 |
| `pastPositions[{date, position}]` | **该关键词下的历史周排名**（实测 5 期） |

> 新品（例如 2026-09 才上架的 ASIN）在卖家精灵商品库里还没有档案，返回里除 `pastPositions` 外基本都是 `null`，
> 前端必须按「-」兜底。

### 12.3 `GET /v3/api/trends/amz-unit-trend/{marketId}/{asin}` —— 价格 / 销量 / 销售额趋势

```
GET /v3/api/trends/amz-unit-trend/1/B07BGLT25K
→ {"data": {"asin": "...", "image": "...", "title": "...", "sku": "...",
            "trend": [{"date": "2024-07", "unit": null, "price": 5.99, "amount": null}, …],
            "lastMonthUnit": 100000}}
```

- 返回最近 **26 个月**的「月销量 unit / 月均价 price / 月销售额 amount」，正是页面「销量趋势」表里的三行数据。
- 响应仅约 3KB，适合按需实时抓。

### 12.4 `POST /v2/competitor-lookup/chart-monthly.json` —— 月度 BSR / 评分 / 评论数（补充）

- 表单参数：`marketId=1&asin=B07BGLT25K`（`application/x-www-form-urlencoded`）。
- 返回 `data.chartData`：**按 27 个月**给出 `bsrRank` / `price` / `rating` / `reviews` / `totalUnits` / `averagePrice`，
  另有 `data.asinObj`（商品档案，字段与 12.2 类似）。
- 约 180KB/次，本项目只取其中 6 个字段后落缓存。

### 12.5 其它实测可用的相关接口

| 接口 | 说明 |
| --- | --- |
| `GET /v3/api/ai-analysis/daily-remaining-quota` | AI 分析当日剩余额度（实测 `{"data": 100}`）；与 TOP10/商品数据无关，仅供 AI 分析功能使用 |
| `POST /v3/api/competing-lookup` | 「查竞品」，body `{"marketId":1,"asins":[...]}`；返回商品档案 + `trends[{dk,sales}]` 月销量 |
| `GET /v2/keepa/subTrend/{marketId}/{asin}` | Keepa 全量历史：58,447 个日点（price / bsr / rating / reviews / fba / sellers…），**单次 17.6MB**，太重，本项目不采用 |
| `GET /v2/competitor-lookup/{marketId}/{asin}/export-history-monthly` | 页面上的「导出月度历史」链接 |

### 12.6 回填与节奏建议

- `gkDatas` 是列表接口**免费附带**的字段，不额外消耗请求；但旧数据期没有这一列，用
  `python backfill_gk.py --week ara_20260912` 按「不限类目」分页重拉一遍即可补齐（实测 1000 条 / 约 3 秒，50 页 ≈ 2.5 分钟）。
- ASIN 档案按关键词抓（1 次请求覆盖 10 个 ASIN）、价格趋势按 ASIN 抓（1 次请求 1 个 ASIN），
  建议请求间隔 ≥0.25s，并对同一关键词/ASIN 做本地缓存（本项目落到 `asin_keyword` / `asin_trend` 两张表）。

---

## 13. 附件

| 文件 | 说明 |
| --- | --- |
| `aba_api.py` | 可直接使用的 Python 客户端（含 CLI，内置全部字段与站点映射） |
| `aba选品示例_US_HomeKitchen_W1_20260912.csv` | 实测选品结果（96 行） |
| `aba选品示例_US_HomeKitchen_W1_20260912.xlsx` | 同上 Excel 版（已冻结表头/首列） |

命令行速查：

```bash
python aba_api.py tables                       # 数据期与站点覆盖
python aba_api.py depts --market-id 1          # 类目枚举
python aba_api.py search --q "phone stand" --size 20 --sort searches --desc
python aba_api.py dump --market COM --departments kitchen --max-items 1000 --out result.csv
python aba_api.py trends --keyword "phone stand" --mode w
python aba_api.py trends --keyword "phone stand" --mode m --yearly
```
