"""Bounded, read-only analysis tools over the weekly ABA database.

No model-generated SQL, filesystem paths, cookies, or arbitrary URLs are accepted.
Google Trends uses the project's bounded supplier query and cache service.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import config
from steps import google_trends
from steps.agent_market_data import MarketDataTools, KEYWORD_FIELDS, PRODUCT_FIELDS, pagination

METRICS = {
    "se": "searches", "rk": "search_rank", "gr": "w1_rank_growth_rate",
    "gv": "w1_rank_growth_val", "im": "impressions", "cl": "clicks",
    "cs": "click_share_rate", "cv": "cvs_share_rate", "spr": "spr",
    "td": "title_density", "pu": "purchases", "pr": "purchase_rate",
    "a30": "ad_products_30", "wc": "word_count",
}
SEARCH_FIELDS = ["keyword", "keyword_cn", "table_date", "searches", "search_rank",
          "w1_search_rank", "w1_rank_growth_val", "w1_rank_growth_rate", "purchases",
          "purchase_rate", "click_share_rate", "cvs_share_rate", "title_density",
          "spr", "products", "bid", "exact_ppc", "ad_products_30", "updated_at"]
FIELDS = KEYWORD_FIELDS
ASIN_FIELDS = PRODUCT_FIELDS
NOTES = [
    "keyword.searches / purchases 是每个周快照中的月度估算值，不是该周销量；不可跨周相加。",
    "w1_rank_growth_rate 是排名增幅，不是搜索量增长率；比例 0.2 表示 20%。",
    "keyword_trend 是供应商返回的历史序列，按 label 标注周期；月度快照与历史序列不能混算。",
    "click_share_rate / cvs_share_rate 是 ABA TOP3 集中度，不是全市场占有率。",
    "gk_asins 为关键词搜索位快照；asin_keyword/asin_trend 是按需缓存，以 fetched_at 为准，不是历史周的实测值。",
    "类目允许重叠；筛选前置条件为搜索量下限，样本不代表亚马逊完整市场。",
    "null 表示缺失，不表示 0；选品盈利、合规、真实差评和 Rufus 曝光未包含在此数据库。",
]


def text(value, limit=200):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("文本参数类型或长度不正确")
    return value.strip()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("筛选数值必须是有限数字")
    return value


def json_value(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def clean_json(value, depth=0, *, list_limit=24):
    """Bound supplier-controlled product text before it reaches the model."""
    if depth > 5:
        return None
    if isinstance(value, str):
        return value[:800]
    if isinstance(value, list):
        return [clean_json(v, depth + 1, list_limit=list_limit) for v in value[:list_limit]]
    if isinstance(value, dict):
        return {k: clean_json(v, depth + 1, list_limit=list_limit) for k, v in list(value.items())[:100]}
    return value


def validate_context(raw):
    if not isinstance(raw, dict):
        raise ValueError("页面上下文必须是对象")
    # The dashboard can only hand off a keyword, never its current period or filters.
    return {"keyword": text(raw.get("keyword"), 200)}


class DataTools(MarketDataTools):
    def __init__(self, context, db_path=None, web_data=None):
        self.context = validate_context(context)
        self.db_path = Path(db_path or config.DB_PATH)
        self.web_data = Path(web_data or config.WEB_DATA)
        self.conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")
        self.conn.create_function("word_count", 1, lambda s: len((s or "").replace("-", " ").split()))
        self.weeks = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT table_date FROM keyword WHERE market=? ORDER BY table_date DESC", (config.MARKET,))]
        if not self.weeks:
            self.close()
            raise ValueError("暂无 ABA 周数据，请先完成一次采集")
        self.week = self.weeks[0]
        meta = self.read_json("departments.json", [])
        self.official = [d["code"] for d in meta if d.get("code") not in (None, "any")]
        self.official = self.official or list(config.OFFICIAL_DEPTS)
        self.conn.create_function("dashboard_search", 5, self.search_match)

    def close(self):
        self.conn.close()

    def read_json(self, name, default):
        try:
            return json.loads((self.web_data / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def rows(self, sql, params=(), *, query_timeout=12):
        deadline = time.monotonic() + query_timeout
        self.conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        return [dict(r) for r in self.conn.execute(sql, params)]

    def resolve_week(self, week=None):
        week = text(week, 20) or getattr(self, "week", self.weeks[0])
        if not re.fullmatch(r"ara_\d{8}", week) or week not in self.weeks:
            raise ValueError("数据期不存在，请从已采集的周数据中选择")
        return week

    @staticmethod
    def search_match(keyword, cn, brands, asins, query):
        br = "|".join(str(x) for x in json_value(brands, []))
        aa = "|".join(str(x.get("asin", "")) for x in json_value(asins, []) if isinstance(x, dict))
        return any(query.lower() in (s or "").lower() for s in (keyword, cn, br, aa))

    def scope(self, week=None, args=None):
        args = args or {}
        conditions = ["k.market=?"]
        params = [config.MARKET]
        if week:
            conditions.append("k.table_date=?")
            params.append(week)
        department = text(args.get("department"))
        if department:
            relation = "d.keyword=k.keyword AND d.market=k.market AND d.table_date=k.table_date"
            official = ",".join("?" for _ in self.official)
            if department in (config.OTHER_DEPT_CODE, config.NONE_DEPT_CODE):
                has_dept = f"EXISTS (SELECT 1 FROM keyword_department d WHERE {relation})"
                if department == config.NONE_DEPT_CODE:
                    conditions.append("NOT " + has_dept)
                else:
                    conditions += [has_dept, f"NOT EXISTS (SELECT 1 FROM keyword_department d WHERE {relation} AND d.department IN ({official}))"]
                    params += self.official
            else:
                codes = [department] + [k for k, v in config.DEPT_ALIAS.items() if v == department]
                conditions.append(f"EXISTS (SELECT 1 FROM keyword_department d WHERE {relation} AND d.department IN ({','.join('?' for _ in codes)}))")
                params += codes
        if args.get("query"):
            conditions.append("(dashboard_search(k.keyword,k.keyword_cn,k.top3_brands,k.top3_asins,?) OR instr(lower(COALESCE(k.gk_asins,'')),lower(?))>0)")
            params += [text(args["query"])] * 2
        ranges = []
        for key, metric, bound in (("min_searches", "se", "min"), ("min_growth", "gr", "min"),
                                   ("max_click_share", "cs", "max"), ("max_title_density", "td", "max")):
            if args.get(key) is not None:
                ranges.append({"key": metric, bound: number(args[key])})
        for r in ranges:
            col = "word_count(k.keyword)" if r["key"] == "wc" else "k." + METRICS[r["key"]]
            # Match the dashboard's zero display for absent filter values; return raw nulls in evidence.
            for bound, op in (("min", ">="), ("max", "<=")):
                if r.get(bound) is not None:
                    conditions.append(f"COALESCE({col},0){op}?")
                    params.append(number(r[bound]))
        return " AND ".join(conditions), params

    def overview(self, **args):
        week = self.resolve_week(args["week"]) if args.get("week") else None
        where, params = self.scope(week, args)
        series = self.rows(f"""SELECT k.table_date week, COUNT(*) keywords, SUM(k.searches) monthly_searches_sum,
            AVG(k.searches) monthly_searches_mean, SUM(k.purchases) monthly_purchases_sum,
            SUM(CASE WHEN k.w1_rank_growth_rate>0 THEN 1 ELSE 0 END) rank_up_keywords,
            AVG(k.click_share_rate) avg_top3_click_share,
            SUM(CASE WHEN k.click_share_rate IS NULL THEN 1 ELSE 0 END) missing_click_share
            FROM keyword k WHERE {where} GROUP BY k.table_date ORDER BY k.table_date""", params)
        index = self.read_json("index.json", {})
        collection = [next((w for w in index.get("weeks", []) if w.get("table") == s["week"]), {}) for s in series]
        return {"period": "all_collected_weeks" if week is None else week, "market": config.MARKET,
                "available_weeks": self.weeks, "week_count": len(series),
                "scope": {"department": args.get("department") or "all", "collection_minimum_searches": config.MIN_SEARCHES, "query_scope": "all_stored_records"},
                "weekly_statistics": series, "collection_by_week": collection,
                "category_scope_note": "类目表为看板整期快照，不随筛选改变；多类目词重复归属，不可相加。",
                "metric_notes": NOTES}

    def search_keywords(self, **args):
        week = self.resolve_week(args["week"]) if args.get("week") else None
        where, params = self.scope(week, args)
        base = ("FROM keyword k" if week else
                "FROM keyword k JOIN (SELECT keyword, MAX(table_date) table_date FROM keyword WHERE market=? GROUP BY keyword) latest "
                "ON latest.keyword=k.keyword AND latest.table_date=k.table_date AND k.market=?")
        if not week:
            params = [config.MARKET, config.MARKET] + params
        sorts = {"searches": "k.searches DESC", "growth": "k.w1_rank_growth_rate DESC",
                 "purchases": "k.purchases DESC", "low_concentration": "k.click_share_rate ASC",
                 "low_title_density": "k.title_density ASC"}
        sort = args.get("sort", "searches")
        if sort not in sorts:
            raise ValueError("未知排序方式")
        sort_col = sorts[sort].split()[0]
        limit, offset = pagination(args.get("limit", 15), args.get("offset", 0))
        count = self.rows(f"SELECT COUNT(*) n {base} WHERE {where}", params)[0]["n"]
        rows = self.rows(f"SELECT {','.join('k.'+f for f in SEARCH_FIELDS)},k.top3_brands,k.top3_asins {base} WHERE {where} ORDER BY {sort_col} IS NULL,{sorts[sort]},k.keyword LIMIT ? OFFSET ?", params + [limit, offset])
        for r in rows:
            r["top3_brands"] = json_value(r["top3_brands"], [])
            r["top3_asins"] = clean_json(json_value(r["top3_asins"], []))
            r["all_week_snapshots"] = self.rows("SELECT table_date,searches,purchases,search_rank,click_share_rate,cvs_share_rate,title_density FROM keyword WHERE keyword=? AND market=? ORDER BY table_date", [r["keyword"], config.MARKET])
        return {"period": week or "all_collected_weeks", "matched_count": count,
                "returned_count": len(rows), "truncated": count > len(rows), "sort": sort,
                "offset": offset, "next_offset": offset + len(rows) if offset + len(rows) < count else None,
                "row_note": "每个词展示最近一次采集快照，all_week_snapshots 包含该词所有已采集周；消失词仍可见。", "rows": rows}

    def keyword_analysis(self, **args):
        keywords = args.get("keywords") or [self.context.get("keyword")]
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 5:
            raise ValueError("一次可深挖 1 至 5 个关键词")
        requested_week = self.resolve_week(args["week"]) if args.get("week") else None
        output = []
        for keyword in keywords:
            keyword = text(keyword)
            if not keyword:
                raise ValueError("请输入要分析的关键词")
            snapshots = self.rows(f"SELECT {','.join(FIELDS)} FROM keyword WHERE keyword=? AND market=? ORDER BY table_date", [keyword, config.MARKET])
            for snapshot in snapshots:
                for key in ('top3_brands', 'top3_asins', 'gk_asins'):
                    snapshot[key] = clean_json(json_value(snapshot[key], []))
                snapshot['departments'] = self.rows('SELECT department,primary_flag FROM keyword_department WHERE keyword=? AND market=? AND table_date=? ORDER BY department', [keyword, config.MARKET, snapshot['table_date']])
            week = requested_week or (snapshots[-1]["table_date"] if snapshots else self.week)
            trends = self.rows("""SELECT label,searches,rank,searches_growth_rate,rank_growth_rate FROM (
                SELECT label,searches,rank,searches_growth_rate,rank_growth_rate,
                ROW_NUMBER() OVER (PARTITION BY label ORDER BY table_date DESC,seq DESC) rn
                FROM keyword_trend WHERE keyword=? AND market=?) WHERE rn=1 ORDER BY label""", [keyword, config.MARKET])
            raw = self.rows("SELECT gk_asins,top3_asins FROM keyword WHERE keyword=? AND market=? AND table_date=?", [keyword, config.MARKET, week])
            products = self.rows("SELECT asin,data,fetched_at FROM asin_keyword WHERE keyword=? AND market=? AND table_date=? ORDER BY asin LIMIT 13", [keyword, config.MARKET, week])
            output.append({"keyword": keyword, "detail_week": week, "snapshots": snapshots,
                           "supplier_history_by_label": trends,
                           "search_position_snapshot": clean_json(json_value(raw[0]["gk_asins"], [])) if raw else [],
                           "aba_top3": clean_json(json_value(raw[0]["top3_asins"], [])) if raw else [],
                           "cached_product_details": [self.product(r) for r in products],
                           "missing_detail_week": not raw,
                           "detail_link": "/?" + urlencode({"week": week, "keyword": keyword})})
        return {"keywords": output, "cache_note": NOTES[4]}

    @staticmethod
    def product(row):
        data = json_value(row["data"], {})
        return {"asin": row["asin"], "fetched_at": row["fetched_at"],
                "data": clean_json({k: data.get(k) for k in ASIN_FIELDS}, list_limit=1000)}

    def asin_analysis(self, **args):
        asins = args.get("asins", [])
        if not isinstance(asins, list) or not 1 <= len(asins) <= 3:
            raise ValueError("一次可研究 1 至 3 个 ASIN")
        week = self.resolve_week(args["week"]) if args.get("week") else self.week
        output = []
        for asin in asins:
            asin = text(asin, 10).upper()
            if not re.fullmatch(r"[A-Z0-9]{10}", asin):
                raise ValueError("ASIN 应为 10 位字母数字")
            associated = self.rows("SELECT keyword,table_date,searches,search_rank FROM keyword WHERE market=? AND (instr(top3_asins,?)>0 OR instr(gk_asins,?)>0) ORDER BY table_date DESC,searches DESC LIMIT 60", [config.MARKET, asin, asin])
            coverage = self.rows("SELECT table_date,COUNT(*) keyword_mentions FROM keyword WHERE market=? AND (instr(top3_asins,?)>0 OR instr(gk_asins,?)>0) GROUP BY table_date ORDER BY table_date", [config.MARKET, asin, asin])
            products = self.rows("SELECT asin,data,fetched_at,table_date FROM asin_keyword WHERE market=? AND asin=? AND table_date<=? ORDER BY fetched_at DESC LIMIT 1", [config.MARKET, asin, week])
            trends = self.rows("SELECT data,fetched_at FROM asin_trend WHERE market=? AND asin=?", [config.MARKET, asin])
            trend = json_value(trends[0]["data"], {}) if trends else {}
            output.append({"asin": asin, "detail_week": week, "associated_keywords_sample": associated,
                           "associated_week_counts": coverage, "sample_note": "最多返回 60 条关联关键词快照；逐期总数见 associated_week_counts。",
                           "cached_detail": {**self.product(products[0]), "associated_week": products[0]["table_date"]} if products else None,
                           "monthly_trend": clean_json(trend.get("trend", []), list_limit=1000),
                           "trend_metadata": clean_json({k: trend.get(k) for k in ('source', 'fetchedAt', 'partial', 'reason', 'availability', 'provenance', 'error', 'errorCode') if k in trend}),
                           "trend_fetched_at": trends[0]["fetched_at"] if trends else None})
        return {"asins": output, "cache_note": NOTES[4]}

    def compare_weeks(self, **args):
        week = self.resolve_week(args.get("week"))
        older = [w for w in self.weeks if w < week]
        previous = args.get("previous_week") or (older[0] if older else "")
        if not previous:
            return {"week": week, "error": "该数据期之前没有可比较的周快照"}
        previous = self.resolve_week(previous)
        if previous >= week:
            raise ValueError("对比期必须早于当前数据期")
        where, params = self.scope(week, args)
        joined = f"FROM keyword k JOIN keyword p ON p.keyword=k.keyword AND p.market=k.market AND p.table_date=? WHERE {where}"
        params = [previous] + params
        stats = self.rows(f"""SELECT COUNT(*) matched_keywords,
            SUM(CASE WHEN k.searches IS NOT NULL AND p.searches IS NOT NULL THEN 1 ELSE 0 END) valid_search_pairs,
            SUM(CASE WHEN k.searches IS NOT NULL AND p.searches IS NOT NULL THEN k.searches END) current_monthly_searches,
            SUM(CASE WHEN k.searches IS NOT NULL AND p.searches IS NOT NULL THEN p.searches END) previous_monthly_searches
            {joined}""", params)[0]
        up = self.rows(f"""SELECT k.keyword,k.searches current_searches,p.searches previous_searches,
            k.searches-p.searches searches_delta,
            CASE WHEN p.searches>0 THEN 1.0*(k.searches-p.searches)/p.searches END searches_change_rate,
            k.w1_rank_growth_rate,k.click_share_rate {joined}
            AND k.searches IS NOT NULL AND p.searches IS NOT NULL ORDER BY searches_delta DESC LIMIT 15""", params)
        return {"week": week, "previous_week": previous, "matched_cohort": stats,
                "largest_search_increases": up,
                "comparison_note": "相同关键词的月搜索量快照变化；以当前期筛选定义样本，排除任一期缺失的搜索量。不是周搜索量增长。"}

    def execute(self, name, arguments):
        methods = {"data_overview": self.overview, "search_keywords": self.search_keywords,
                   "keyword_analysis": self.keyword_analysis, "asin_analysis": self.asin_analysis,
                   "compare_weeks": self.compare_weeks, "google_trends": self.google_trends}
        methods.update({name: getattr(self, name) for name in (
            'data_catalog', 'category_analysis', 'keyword_snapshots', 'keyword_history',
            'search_asins', 'product_history', 'keyword_asins', 'asin_trends', 'market_knowledge', 'dashboard_view', 'collection_history')})
        if name not in methods or not isinstance(arguments, dict):
            raise ValueError("未知工具或工具参数无效")
        try:
            return methods[name](**arguments)
        except sqlite3.OperationalError:
            raise ValueError('查询未完成，请按关键词、ASIN、类目或数据周缩小单次查询范围；其他历史记录仍可继续检索') from None

    def google_trends(self, *, keywords, years=5):
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 3:
            raise ValueError("一次可查询 1 至 3 个谷歌趋势关键词")
        if isinstance(years, bool) or not isinstance(years, int) or years not in (1, 3, 5):
            raise ValueError("谷歌趋势范围须为 1、3 或 5 年")
        validated = [text(keyword, 200) for keyword in keywords]
        if not all(validated):
            raise ValueError("谷歌趋势关键词不能为空")
        output = []
        for keyword in dict.fromkeys(validated):
            if getattr(self, 'market_loader', None) is not None:
                data = self.market_loader('google_trends', {'keyword': keyword})
            else:
                data = google_trends.load(keyword=keyword, market=config.MARKET, db_path=self.db_path)
            points = data.get("trend", [])
            if points and years != 5:
                end = datetime.fromtimestamp(points[-1]["time"] / 1000, timezone.utc)
                # 处理闰年 2 月 29 日。
                try:
                    cutoff = end.replace(year=end.year - years)
                except ValueError:
                    cutoff = end.replace(year=end.year - years, day=28)
                points = [point for point in points if point["time"] >= cutoff.timestamp() * 1000]
            output.append({
                **{key: data[key] for key in ("ok", "keyword", "station", "source", "fetchedAt", "stale", "error", "errorCode") if key in data},
                "years": years, "returned_points": len(points),
                "period_start": points[0]["date"] if points else None,
                "period_end": points[-1]["date"] if points else None,
                "trend": [{key: point[key] for key in ("date", "value", "label")} for point in points],
            })
        return {"keywords": output, "data_source": "SellerSprite /v2/keyword/google-trends.json · Google 网页搜索",
                "metric_note": "0–100 相对搜索指数，不是搜索次数或亚马逊月搜索量；1/3 年筛选保留近5年归一化口径，各关键词独立归一化，不能据此比较不同关键词绝对需求规模。null 是缺失；0 和 <1 是低热度。",
                "time_note": "截至缓存更新时间的最近五年周序列，不随所选 ABA 历史周截断；stale 表示缓存已过期，须披露。"}
