# ABA 关键词趋势看板（全类目周更）

流程调度中台内的一个流程：**每周四自动登录卖家精灵刷新 Cookie → 全量抓取 ABA 全部 22 个大类 → 写入 SQLite → 重建看板 → 常驻 http://127.0.0.1:8766**

```
Chrome 自动登录（刷新 Cookie） → 卖家精灵 ABA API → SQLite（每周一期，永不覆盖） → 按周生成前端 JSON → ECharts 看板（8766，顶部下拉切换数据期）
```

## 流程信息

| 项 | 值 |
| --- | --- |
| 流程 ID | `aba_keyword_trend_dashboard` |
| 定时 | **每周四 06:00**（单次；周表周六标记、通常周二/周三才发布，周四取必然是最新一期） |
| 调度来源 | 中台数据库 `data/dashboard.db` 的 `process_configs.schedule_cron`（会同步写回 `process.json`）；只改 `process.json` 不生效 |
| cron | `0 6 * * 4`（Asia/Shanghai） |
| 超时 | 7200 秒 |
| 重试 | 失败重试 2 次，间隔 10 分钟 |
| 标签 | ABA / 选品 / 看板 / 周更 |
| 看板端口 | **8766**（中台本体在 8000，两者互不影响） |

详细说明见 [流程文档.md](流程文档.md)，接口逆向见 [docs/卖家精灵ABA数据API逆向文档.md](docs/卖家精灵ABA数据API逆向文档.md)。

> **类目级选品调研**：看板数据可直接喂给 Codex skill `aba-blueocean-report`（按大类目剔除品牌词/导航词、
> 跑蓝海指标与 TOP10 ASIN 画像、输出 PDF 调研报告）。用法见该 skill 的 `SKILL.md`，
> 分析口径见 `references/methodology.md`。

## AI 研究员（左侧聊天抽屉）

看板顶部「✦ AI 研究员」或左侧浮动入口打开聊天。关键词详情中的「让 AI 分析这个关键词」会直接提交该词的全周期分析请求。
聊天默认使用数据库中全部已采集周和全类目，独立于看板当前数据期与筛选条件；可在问题中明确指定单期或类目。
支持流式回答、停止生成、多轮追问、按证据链接回看数据、新对话、导出 Markdown。
对话暂存于当前浏览器标签页的 sessionStorage，刷新可恢复；关闭标签页后通常清除。
多轮输入保留最近 6 组完整问答（总长度有上限），较早问题需要重新提供研究对象。

后端使用 OpenAI Python SDK 的 `client.responses.create()`，固定调用
`https://api.deepseek.com` 的 `deepseek-flash`，不切换 Chat Completions。
DeepSeek Responses 无服务端会话存储，因此每次发送历史消息并重传本轮 function_call / function_call_output。

首次部署：

```powershell
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
# 在 config.yaml 中填写自己的卖家精灵账号和密码；采集后会生成本地 SQLite 数据库。
Copy-Item agent.example.json agent.local.json
# 在 agent.local.json 的 api_key 中填写 DeepSeek Key；也可设置 DEEPSEEK_API_KEY 环境变量。
python serve.py --host 0.0.0.0 --port 8766 --no-open
```

`agent.local.json` 位于流程根目录、已加入 Git 忽略，不在静态目录 `web/` 中。
仓库包含已生成的 `web/data/` 周数据供看板展示；SQLite 数据库位于本地 `db/`，不进入 Git。Agent 查询需先在本机完成 ABA 数据采集。
Key 只由服务端读取，状态接口不回传 Key；修改 Key 后下一次请求生效。
现有部署已配置本地 Key 时不要再次执行 Copy-Item 覆盖。
看板沿用既有局域网访问方式，访问者可使用分析额度；请在可信网络使用。

数据工具为 `data_overview`、`search_keywords`、`keyword_analysis`、`asin_analysis`、`compare_weeks`。
Agent 默认覆盖数据库中全部已采集周和全类目；不会继承看板选择的数据期、类目、搜索或筛选。看板关键词详情的「让 AI 分析」仅传递关键词线索。用户可在对话中明确要求单期或单类目。关键词筛选每词返回最近一次采集快照并附全部已采集周序列，单词深挖返回完整周快照；ASIN 关联提供逐期总数和最多 60 条样本。
界面默认浅色，顶部按钮可切换深色并在本机浏览器保留偏好。Agent 抽屉的「新建」会保留旧对话供下拉列表切换；「清除对话」只清空当前对话。对话保存在当前浏览器标签的会话存储中。
通过 SQLite `mode=ro` / `query_only` 和参数化固定查询读取数据库，模型不能执行自由 SQL 或触发实时爬取。
每轮最多 12 次工具检索、6 轮工具交互后收束回答，同时最多 2 个分析请求；模型连接超时 10 秒、读取超时 90 秒，流开始前最多重试 1 次，总分析时限 300 秒。
返回 `[D1]` 等证据编号、数据期和来源链接。TOP10 快照、按需缓存、月度估计量和历史序列分别标注；缺失数据保持空值。
Agent 人设、证据边界在 `agent_prompt.md` 中维护；从用户资料提炼的全流程研究方法与证据等级在 `agent_research_playbook.md` 中维护。Agent 无实时政策联网检索能力，资料中未核实的算法权重、政策日期及数字不能作为平台事实。
API 会把问题、有限历史与相关采集数据发送给 DeepSeek 进行分析。

接口：`GET /api/agent/status`、`POST /api/agent/chat`（NDJSON 流）、`POST /api/agent/cancel`。
聊天请求含 `request_id`（随机 UUID）、`message`、`history`（user/assistant 文本）、`context`。
流事件含 `status`、`source`、`delta`、`done`、`error` 与心跳；停止时关闭模型响应流，已经发出的请求可能已产生费用。

回归检查（从项目根目录运行，不调用真实模型）：

```powershell
python -m unittest discover -s tests -v
```

参考：[DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/)、
[OpenAI 工具调用](https://developers.openai.com/api/docs/guides/function-calling)、
[Amazon 官方 Rufus 机制说明](https://www.aboutamazon.com/news/retail/amazon-rufus-ai-assistant-personalized-shopping-features)。

## 目录结构

```
aba_keyword_trend_dashboard/
├── process.json          中台元数据（名称/cron/超时/重试/标签）
├── config.example.yaml   业务参数模板（复制为本地 config.yaml 并填写账号）
├── main.py               中台入口 run(config) → ProcessResult
├── run_all.py            本地命令行入口（--fetch/--build/--serve/--stop/--reset）
├── backfill_gk.py        给旧数据期补「TOP10 商品快照」（只 UPDATE keyword.gk_asins）
├── refresh_cookie.py     自动恢复失败时的手动登录态修复工具（看板无需重启）
├── start_dashboard.bat   一键启动看板并打开浏览器
├── aba_client.py         卖家精灵 ABA 接口客户端
├── config.py             默认参数（被 config.yaml 覆盖）
├── db_schema.sql         SQLite 表结构
├── serve.py              看板服务（静态 gzip 文件 + /api/asin 详情接口，默认 0.0.0.0:8766）
├── steps/
│   ├── logger.py         中台 JSON Lines 日志协议
│   ├── login_sellersprite.py  第 0 步：Chrome 自动登录卖家精灵，刷新 cookie.txt
│   ├── fetch_aba.py      阶段一：抓取入库（断点续传）
│   ├── asin_detail.py    阶段二：TOP10 ASIN 商品档案 / 价格趋势（缓存 + 按需实时抓 + 预取）
│   ├── build_dashboard.py阶段三：生成前端数据 + 导出选品 CSV
│   ├── store.py          SQLite 连接 / 建表迁移 / Cookie 读取
│   └── server_ctl.py     看板服务启停/健康检查
├── db/aba.sqlite         数据库（约 165MB，按数据期留档）
├── web/                  前端（index.html + assets + data）
├── output/               每周选品 CSV
└── cookie.txt            卖家精灵登录态（24h 过期，需定期更新）
```

## 数据口径

| 项 | 默认值 | 说明 |
| --- | --- | --- |
| 站点 | `COM` 美国站 | ABA 数据为 GLOBAL 口径 |
| 数据表 | 最新周表 `ara_YYYYMMDD` | 月表 `ara_YYYYMM` 需把 `reverse_type` 改 `M` |
| 搜索量下限 | 5000 | `min_searches` |
| 排名增长量口径 | 近一周 `W1` | 可选 `W1/W2/W3/W4` |
| 抓取范围 | 不限类目全量 + 22 个大类各一次 | `include_all_pass` + `departments` |

实测规模：**49,267 个关键词 / 689,738 条趋势点 / 22 个大类 + 2 个兜底桶**，首次全量抓取约 7 分钟（间隔 0.4s、每页 1000 条）。

## 数据库表

| 表 | 说明 |
| --- | --- |
| `keyword` | 关键词主表，主键 `(keyword, market, table_date)`，含搜索量、排名、增长率、竞价、购买率、SPR、标题密度、TOP3 品牌/ASIN |
| `keyword.gk_asins` | **该词自然位前 10 个商品快照**（asin / 自然位 / 页面位次 / 标记 / 图片 / 价格 / 评分 / 评论数 / 标题），详情抽屉的 TOP10 来源 |
| `keyword_department` | 关键词 ↔ 类目（多对多），主键含 `table_date` |
| `keyword_trend` | 逐关键词 14 期趋势（搜索量 / 排名 / 增长率），主键含 `table_date` |
| `asin_keyword` | ASIN 商品档案缓存（key = 关键词 + 数据期 + ASIN）：图片 / 品牌 / 价格 / 均价 / BSR / 评分 / 评论数 / 上架时间 / 近30天销量 / FBA / 毛利率 / 卖家 / 变体 / 尺寸重量 / 该词历史周排名 |
| `asin_trend` | ASIN 月度趋势缓存（key = ASIN + 站点）：月均价 / 月销量 / 月销售额 / 月度 BSR / 评分 / 评论数，默认 7 天有效 |
| `fetch_log` | 抓取断点（按数据期记录），保证同一周同一分页不重复抓 |
| `run_log` | 运行日志 |

> **多周存储**：所有表主键都带 `table_date`，每周抓取只新增一期、绝不覆盖历史，可以直接做周与周对比；
> `fetch_log` 同样按数据期记录 —— 新一周自动全量抓取，已在库的周自动跳过。

## 多周数据与前端切换

### 类目口径（表格「范围」下拉）

> 以下三张图都跟随筛选面板的「类目」联动（构建时按「整体 + 每个类目」各生成一套，切换时本地即换、无额外请求）：
>
> | 图 | 数据字段 | 每类目取数规则 |
> | --- | --- | --- |
> | 排名增幅榜 TOP20 | `summary.deptTopGrowth` | 增幅 >0 且搜索量 ≥5000，按增幅降序取前 20 |
> | 蓝海四象限 | `summary.deptScatter` | 搜索量前 600 个点（整体 6000 个），\|增幅\| ≤150% |
> | 重点增长词周搜索量走势 | `summary.deptTrendSeries` | 增幅 ≥20% 且搜索量 ≥3万、且有完整历史曲线的前 10 |

ABA 是**多类目归属**：`drill` 同时属于 Beauty 与 Tools，`gummy bears` 同时属于 Grocery 与 Beauty。
所以「选某个类目 → 出现的词」有两种合理口径，看板在「类目」旁边给了「范围」下拉：

| 口径 | 依据字段 | 含义 | 美容化妆示例 |
| --- | --- | --- | --- |
| **按 ABA 归类**（默认） | `dps` 全部归属 | 只要被 ABA 归到该类目就命中，多类目词会出现在多个类目下 | 9,230 词 |
| **只看主类目** | `dpm` 主类目 | 仅当该词主类目就是所选类目时命中 | 5,117 词 |

另外，主类目落在 ABA 细分小类（如 `Home / Furniture`）的词，在严格口径下会归入「其它细分类目」，
不会被误标成 22 大类之一（例如 `bar stools set of 3` 主类目是家具，之前会被显示成美容化妆，现已修正）。

> 数据没有抓错：抽查 12 个 beauty 标签词，ABA 接口返回的 `departments` 全部确实含 `Beauty & Personal Care`。

```
web/data/
├── index.json                 数据期索引（下拉框数据源）
├── departments.json           类目元数据（跨周共用）
├── ara_20260912/              每周一个目录
│   ├── meta.json / summary.json / keywords.json
│   └── trends/shard-00..31.json
└── ara_20260905/              上一周
```

- 构建时**只为缺失的周生成**（历史周只建一次），最新周每次重建；`keep_weeks` 控制保留几周（默认 8 周 ≈ 150MB），**数据库里的历史不受影响**
- 看板顶部「数据期」下拉框列出所有已生成周（含词量），切换即加载该周数据，URL 形如 `?week=ara_20260912`
- 回填历史周：`config.yaml` 填 `table_date: "ara_20260905"` 跑一次，或
  `python -c "from steps.fetch_aba import run; run(table_date='ara_20260905')"`，再 `python run_all.py --build`

## 常用命令

```powershell
cd processes\aba_keyword_trend_dashboard

python run_all.py            # 抓取 + 重建 + 确保看板在跑
python run_all.py --fetch    # 只抓取
python run_all.py --build    # 只重建前端数据（离线可用）
python run_all.py --serve    # 只启动看板
python run_all.py --stop     # 停掉看板
python run_all.py --reset    # 清空断点强制重抓

# 给旧数据期补 TOP10 商品快照（详情抽屉不再只有 TOP3）
python backfill_gk.py                                   # 最新数据期，全部页（约 2.5 分钟）
python backfill_gk.py --week ara_20260912 --pages 3      # 只补前 3 页（约 3000 个热门词）

# 详情页「实时抓取」提示登录态失效时，一条命令恢复（打开 Chrome 登录 → 写回 cookie.txt）
python refresh_cookie.py                      # B：看板自己登录（会把浏览器那边的会话顶掉）
python refresh_cookie.py --from-clipboard     # A：用你浏览器里的 Cookie，两边同时在线互不顶号

# 本地按中台方式调用（中台就是用这个）
python -c "import yaml; from main import run; print(run(yaml.safe_load(open('config.yaml',encoding='utf-8'))))"
```

## 详情抽屉：ASIN TOP10 与商品维度数据

抽屉打开时前端调用看板服务的三个接口（都在 `serve.py` 里，同端口 8766）：

| 接口 | 内容 | 数据来源 |
| --- | --- | --- |
| `GET /api/asin?kw=<关键词>&week=<数据期>` | 自然位前 10 个 ASIN 的完整商品档案 | 先读 `asin_keyword` 缓存，缺失的实时调用卖家精灵 `asin/past-position`（1 次请求 = 10 个 ASIN） |
| `GET /api/asin/trend?asin=<ASIN>` | 最近 26 个月月均价 / 月销量 / 月销售额 + 月度 BSR / 评分 / 评论数 | `asin_trend` 缓存（7 天内直接复用），否则调 `trends/amz-unit-trend` + `comparison chart-monthly` |
| `GET /api/status` | Cookie 状态、当前数据期、已缓存 ASIN 数 | 本地 |

- **TOP10 口径**：ABA 接口只给 TOP3 的点击占比（`top3AsinDtoList` 固定 3 条），所以 TOP10 用的是列表接口
  `gkDatas` 的「自然位前 10 个商品」；TOP3 点击占比作为附加信息展示，不在前 10 的会单独列一行。
- **实时/离线**：`asin_prefetch_top_n`（默认 300）会在每周流程里预取最热关键词的档案；没预取到的词点开时实时抓（约 0.3~1 秒）。
  看板服务会定期检查登录态；详情请求遇到登录错误时自动串行重登并重试。已缓存的商品仍可显示；把 `asin_live_fetch: false` 可关闭实时抓取（纯看缓存）。

### 登录态自动恢复与数据缺失

卖家精灵登录态可能提前失效。看板服务每 30 分钟校验一次；详情请求遇到明确的登录错误时会立即尝试自动重登，并重试本次请求。多个并发请求只触发一次重登；失败后 15 分钟内不重复尝试。自动重登使用无界面 Chrome，需本机可用的 Playwright/Chrome 与 `config.yaml` 中已配置的账号信息。若设置了固定 `cookie` 或 `SELLERSPRITE_COOKIE`，该值会覆盖文件更新，须先清除固定覆盖才能让自动重登生效。

只有自动恢复不成功时才需要人工处理（看板无需重启）：
   - `python refresh_cookie.py --from-clipboard` —— **推荐**：你把浏览器里的 Cookie 整串复制一下（F12 → Network → 任意请求 → Cookie），看板与你浏览器共用同一条会话，互不顶号
   - `python refresh_cookie.py` —— 让看板自己登录一次（会把你浏览器那边的会话顶掉）
月度趋势请求区分登录失败、接口暂时不可用和上游确实没有数据。失败时继续展示可用的本地历史；不会把失败响应长期留在浏览器缓存。上游明确返回空趋势时仅缓存 24 小时，之后重新获取。

## 关键配置（config.yaml）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `market` / `market_id` | COM / 1 | 站点 |
| `min_searches` | 5000 | 搜索量下限 |
| `rank_growth_type` | W1 | 排名增长量口径 |
| `departments` | `[]` | 留空=全部 22 个大类；也可指定 `["kitchen","beauty"]` |
| `force_full` | false | true=清空断点强制重抓 |
| 触发时间 | 每周四 06:00 | 由中台「定时」配置决定（DB 的 `schedule_cron`，同步到 `process.json`），本流程不再有时间门槛 |
| `table_date` | "" | 指定数据期（回填历史周用），留空=最新周 |
| `keep_weeks` | 8 | 前端保留多少周的数据目录 |
| `serve` | true | 流程跑完是否确保看板在跑 |
| `restart_server` | false | 改了前端要立即生效可设 true |
| `export_csv` | true | 是否导出当周选品 CSV |
| `asin_top_n` | 10 | 详情抽屉展示的 ASIN 个数（自然位前 N） |
| `asin_prefetch_top_n` | 300 | 每周预取搜索量前 N 个关键词的 ASIN 档案（0 = 全部按需实时抓） |
| `asin_trend_max_age_hours` | 168 | 价格/销量趋势缓存有效期（小时） |
| `asin_min_interval` | 0.25 | 抓 ASIN 数据时的请求间隔（秒） |
| `asin_live_fetch` | true | 抽屉是否允许实时抓取（false = 只展示缓存） |
| `port` | 8766 | 看板端口 |
| `auto_login` | true | 触发时自动检查/刷新登录态 |
| `login.account` / `login.password` | — | 在本地 config.yaml 中填写卖家精灵账号密码 |
| `login.headless` | false | false=可见地打开 Chrome；无人值守可改 true |
| `login.browser_channel` | chrome | 用本机安装的 Chrome（也可用 chromium） |
| `cookie` | 空 | 直接填整串 Cookie（优先于 cookie.txt） |

## 排版与性能

- 数据总量：SQLite 约 165MB **每周一期**（49,267 词 / 689,738 趋势点）；前端每周约 18.6MB（keywords 10MB + 32 个趋势分片）
- 趋势数据按 `keywords.json` 行序切成 32 个分片按需加载，翻页通常只命中 1~2 个分片
- 静态服务带 gzip（keywords.json 10MB → 约 5.7MB 传输），JS/CSS 走 no-cache 避免改版不生效
- 筛选面板 `grid auto-fit`，733px 宽 2 列 / 1600px 宽 4 列

## 登录态（已自动化）

每次触发流程的第 0 步会：

1. 用现有 `cookie.txt` 调 ABA 接口验证（登录态返回 50 条，游客口径只返回 20 条）；
2. 有效 → 跳过；失效 → **自动打开 Chrome**，点击「登录/注册」→ 填 `input[name="email"]` / `input[type="password"]` → 点「立即登录」→ 抓 `Sprite-X-Token` 写回 `cookie.txt`；
3. 失败会截图 `logs/login_failed_*.png` 并让流程失败告警（中台按 `notify_on` 通知）。

所以正常情况下**不需要人工维护 Cookie**；只在登录失败（如需图形验证码）时按截图处理，或临时把 `auto_login` 设为 false 手动维护。

## 注意

- `config.yaml`、`agent.local.json`、`cookie.txt` 与 `db/*.sqlite` 已加入 `.gitignore`，不要提交；首次使用可复制 `config.example.yaml` 并填写本地配置
- 看板是无鉴权服务，且监听 `0.0.0.0`，公共网络环境建议把 `config.yaml` 的 `host` 改成 `127.0.0.1`
