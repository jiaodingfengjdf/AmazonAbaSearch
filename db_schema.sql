PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- 关键词主表：一个数据期一行
CREATE TABLE IF NOT EXISTS keyword (
    keyword                 TEXT NOT NULL,
    market                  TEXT NOT NULL,
    table_date              TEXT NOT NULL,
    station                 TEXT,
    keyword_cn              TEXT,
    keyword_jp              TEXT,
    searches                INTEGER,
    clicks                  INTEGER,
    impressions             INTEGER,
    purchase_rate           REAL,
    purchases               INTEGER,
    products                INTEGER,
    search_rank             INTEGER,
    search_rank_growth_val  REAL,
    search_rank_growth_rate REAL,
    w1_search_rank          INTEGER,
    w1_rank_growth_val      REAL,
    w1_rank_growth_rate     REAL,
    w4_search_rank          INTEGER,
    w4_rank_growth_val      REAL,
    w4_rank_growth_rate     REAL,
    w12_search_rank         INTEGER,
    w12_rank_growth_val     REAL,
    w12_rank_growth_rate    REAL,
    click_share_rate        REAL,
    cvs_share_rate          REAL,
    title_density           INTEGER,
    spr                     INTEGER,
    bid                     REAL,
    bid_min                 REAL,
    bid_max                 REAL,
    exact_ppc               REAL,
    phrase_ppc              REAL,
    broad_ppc               REAL,
    ad_products_1           INTEGER,
    ad_products_7           INTEGER,
    ad_products_30          INTEGER,
    top3_brands             TEXT,
    top3_asins              TEXT,
    -- 关键词下搜索位 TOP10 商品快照（来自列表接口的 gkDatas，存精简 JSON）
    gk_asins                TEXT,
    updated_at              TEXT,
    PRIMARY KEY (keyword, market, table_date)
);

CREATE INDEX IF NOT EXISTS idx_keyword_searches ON keyword(market, table_date, searches DESC);
CREATE INDEX IF NOT EXISTS idx_keyword_growth   ON keyword(market, table_date, w1_rank_growth_rate DESC);

-- 关键词 <-> 类目（多对多）
CREATE TABLE IF NOT EXISTS keyword_department (
    keyword    TEXT NOT NULL,
    market     TEXT NOT NULL,
    table_date TEXT NOT NULL,
    department TEXT NOT NULL,
    primary_flag INTEGER DEFAULT 0,
    PRIMARY KEY (keyword, market, table_date, department)
);
CREATE INDEX IF NOT EXISTS idx_kwdept_dept ON keyword_department(market, table_date, department);

-- 关键词历史趋势（来自列表接口每行的 trends 数组）
CREATE TABLE IF NOT EXISTS keyword_trend (
    keyword              TEXT NOT NULL,
    market               TEXT NOT NULL,
    table_date           TEXT NOT NULL,
    seq                  INTEGER NOT NULL,
    label                TEXT,
    searches             INTEGER,
    rank                 INTEGER,
    searches_growth_rate REAL,
    rank_growth_rate     REAL,
    PRIMARY KEY (keyword, market, table_date, seq)
);
CREATE INDEX IF NOT EXISTS idx_trend_label ON keyword_trend(market, table_date, label);

-- 抓取断点记录：同一分页不会重复抓
CREATE TABLE IF NOT EXISTS fetch_log (
    task       TEXT NOT NULL,
    market     TEXT NOT NULL,
    table_date TEXT NOT NULL,
    page       INTEGER NOT NULL,
    item_count INTEGER,
    total      INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (task, market, table_date, page)
);

CREATE TABLE IF NOT EXISTS run_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stage      TEXT,
    message    TEXT,
    created_at TEXT
);

-- ASIN 深度数据缓存：按「关键词 + 数据期」存 asin/past-position 返回的商品档案
-- （图片、价格、BSR、评分/评论数、上架时间、销量趋势、该词历史排名 …），供看板详情抽屉按需读取
CREATE TABLE IF NOT EXISTS asin_keyword (
    keyword    TEXT NOT NULL,
    market     TEXT NOT NULL,
    table_date TEXT NOT NULL,
    asin       TEXT NOT NULL,
    data       TEXT,
    fetched_at TEXT,
    PRIMARY KEY (keyword, market, table_date, asin)
);
CREATE INDEX IF NOT EXISTS idx_asin_keyword_kw ON asin_keyword(market, table_date, keyword);

-- ASIN 维度趋势缓存：月均价 / 月销量 / 月销售额 / BSR / 评分 / 评论数 曲线
-- 与关键词无关，key 只有 (asin, market)，跨周复用
CREATE TABLE IF NOT EXISTS asin_trend (
    asin       TEXT NOT NULL,
    market     TEXT NOT NULL,
    data       TEXT,
    fetched_at TEXT,
    PRIMARY KEY (asin, market)
);
