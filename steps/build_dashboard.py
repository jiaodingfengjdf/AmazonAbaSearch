#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段二：把 SQLite 里的数据加工成前端可用的 JSON（meta / summary / keywords / trends 分片）。

产物（web/data/）：
    meta.json     运行元信息
    summary.json  KPI、类目聚合、各类榜单、大盘周趋势
    keywords.json 全部关键词的列表数据（紧凑数组格式）
    trends/shard-NN.json  逐关键词的趋势序列（32 分片，前端按需加载）
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import config


AMAZON_HOSTS = {
    "COM": "www.amazon.com", "JP": "www.amazon.co.jp", "UK": "www.amazon.co.uk",
    "DE": "www.amazon.de", "FR": "www.amazon.fr", "IT": "www.amazon.it",
    "ES": "www.amazon.es", "CA": "www.amazon.ca", "IN": "www.amazon.in",
    "MX": "www.amazon.com.mx", "AU": "www.amazon.com.au", "BR": "www.amazon.com.br",
    "AE": "www.amazon.ae", "SA": "www.amazon.sa", "NL": "www.amazon.nl",
    "SE": "www.amazon.se", "SG": "www.amazon.sg", "TR": "www.amazon.com.tr",
}


from steps.logger import log as jlog, progress as jprogress


# 当前正在生成的周目录（web/data/<table_date>/），由 run() 按周切换
OUT_DIR: Path = config.WEB_DATA


def log(level: str, msg: str) -> None:
    """输出中台可解析的 JSON Lines 日志（level: info | warn | error）。"""
    jlog(level, msg)


def progress(current: int, total: int) -> None:
    jprogress(current, total)


def roundn(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    return round(float(value), digits)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_codes(dept_list: Optional[str], official: set) -> List[str]:
    """把一行关键词的类目列表规整成展示口径。

    - 互为别名的类目先合并去重（mobile / wireless）；
    - OFFICIAL_DEPTS_ONLY=True 时只保留 22 个大类，只有细分小类的词进「其它细分类目」；
    - 完全没有类目信息的词进「未归类」。
    """
    codes = list(dict.fromkeys(
        config.DEPT_ALIAS.get(c, c) for c in (dept_list or "").split(",") if c
    ))
    if not config.OFFICIAL_DEPTS_ONLY:
        return codes or [config.NONE_DEPT_CODE]
    official_hit = [c for c in codes if c in official]
    if official_hit:
        return official_hit
    return [config.OTHER_DEPT_CODE] if codes else [config.NONE_DEPT_CODE]


def alias_notes(dept_meta: Mapping[str, Mapping[str, str]], official: set) -> Dict[str, str]:
    """两个大类若共用同一个 Amazon 类目名（如 mobile / wireless），互相标注「同一批词」。"""
    label_to_codes: Dict[str, List[str]] = {}
    for code in official:
        label = (dept_meta.get(code) or {}).get("label") or code
        label_to_codes.setdefault(label, []).append(code)
    notes: Dict[str, str] = {}
    for codes in label_to_codes.values():
        if len(codes) > 1:
            for code in codes:
                notes[code] = "与 " + "/".join(c for c in codes if c != code) + " 同一批词"
    return notes


def fetch_rows(conn: sqlite3.Connection, market: str, table_date: str) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT k.*,
               (SELECT department FROM keyword_department d
                 WHERE d.keyword=k.keyword AND d.market=k.market AND d.table_date=k.table_date
                 ORDER BY primary_flag DESC, department LIMIT 1) AS primary_dept, (
                SELECT GROUP_CONCAT(department) FROM (
                    SELECT department FROM keyword_department d
                    WHERE d.keyword=k.keyword AND d.market=k.market AND d.table_date=k.table_date
                    ORDER BY primary_flag DESC, department
                )
            ) AS dept_list
            FROM keyword k
            WHERE k.market=? AND k.table_date=? AND k.searches >= ?
            ORDER BY k.searches DESC
            """,
            (market, table_date, config.MIN_SEARCHES),
        )
    )


def brief_row(r: sqlite3.Row) -> Dict[str, Any]:
    """榜单/榜单卡片用的紧凑字段（前端 topGrowth 结构与之一致）。"""
    return {
        "keyword": r["keyword"],
        "keywordCn": r["keyword_cn"],
        "dept": (r["dept_list"] or "未归类").split(",")[0],
        "searches": r["searches"],
        "rank": r["search_rank"],
        "growth": roundn(r["w1_rank_growth_rate"]),
        "growthValue": r["w1_rank_growth_val"],
        "purchases": r["purchases"],
        "purchaseRate": roundn(r["purchase_rate"]),
        "products": r["products"],
        "spr": r["spr"],
        "bid": roundn(r["bid"], 2),
    }


def build_dept_charts(rows, official, dept_index: Dict[str, int]) -> tuple:
    """按类目拆分「排名增幅榜」与「蓝海四象限」数据：整体 + 每个大类 + 两个兜底桶。

    - 榜单：每类目取 增幅>0 且搜索量≥5000 的前 TOP_N
    - 散点：每类目取搜索量靠前的 600 个点（整体视图 6000 个），截断 |增幅| ≤150%
    """
    groups: Dict[str, List[sqlite3.Row]] = {}
    for r in rows:
        for code in normalize_codes(r["dept_list"], official):
            groups.setdefault(code, []).append(r)

    def top_growth_of(pool):
        cand = [r for r in pool if (r["w1_rank_growth_rate"] or 0) > 0 and (r["searches"] or 0) >= 5000]
        return [brief_row(r) for r in sorted(cand, key=lambda r: -r["w1_rank_growth_rate"])[: config.TOP_N]]

    def scatter_of(pool, limit: int):
        """[关键词, 搜索量, 增幅, 月购买量, 类目下标(-1=当前类目), TOP3点击集中度]"""
        pts = []
        for r in pool[:limit]:
            g = roundn(r["w1_rank_growth_rate"], 4) or 0
            if abs(g) > 1.5:
                continue
            pts.append([r["keyword"], r["searches"] or 0, g, r["purchases"] or 0, -1,
                        roundn(r["click_share_rate"], 4)])
        return pts

    all_scatter = []
    for r in rows[:6000]:
        g = roundn(r["w1_rank_growth_rate"], 4) or 0
        if abs(g) > 1.5:
            continue
        first = normalize_codes(r["dept_list"], official)[0]
        all_scatter.append([r["keyword"], r["searches"] or 0, g, r["purchases"] or 0,
                            dept_index.get(first, dept_index.get(config.NONE_DEPT_CODE, 0)),
                            roundn(r["click_share_rate"], 4)])

    growth: Dict[str, Any] = {"__all__": top_growth_of(rows)}
    scatter: Dict[str, Any] = {"__all__": all_scatter}
    for code, pool in groups.items():
        growth[code] = top_growth_of(pool)
        scatter[code] = scatter_of(pool, 600)
    return growth, scatter


def build_summary(rows: Sequence[sqlite3.Row], market: str, table_date: str,
                  dept_meta: Dict[str, Dict[str, str]], official: set) -> Dict[str, Any]:
    total_searches = sum(r["searches"] or 0 for r in rows)
    growth_rates = [r["w1_rank_growth_rate"] for r in rows if r["w1_rank_growth_rate"] is not None]
    up = sum(1 for g in growth_rates if g > 0)
    strong = sum(1 for g in growth_rates if g is not None and g >= 0.5)

    # 类目聚合
    dept_stats: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        codes = normalize_codes(r["dept_list"], official)
        for code in codes:
            st = dept_stats.setdefault(
                code, {"code": code, "count": 0, "searches": 0, "purchases": 0,
                       "growth_sum": 0.0, "growth_n": 0, "up": 0}
            )
            st["count"] += 1
            st["searches"] += r["searches"] or 0
            st["purchases"] += r["purchases"] or 0
            if r["w1_rank_growth_rate"] is not None:
                st["growth_sum"] += r["w1_rank_growth_rate"]
                st["growth_n"] += 1
                if r["w1_rank_growth_rate"] > 0:
                    st["up"] += 1
    def dept_name(code: str) -> str:
        if code == config.OTHER_DEPT_CODE:
            return config.OTHER_DEPT_NAME
        if code == config.NONE_DEPT_CODE:
            return config.NONE_DEPT_NAME
        meta = dept_meta.get(code, {})
        return meta.get("translation") or meta.get("label") or config.DEPT_NAME_EXTRA.get(code) or code

    alias_note = alias_notes(dept_meta, official)

    dept_list = []
    for code, st in dept_stats.items():
        meta = dept_meta.get(code, {})
        dept_list.append(
            {
                "code": code,
                "name": dept_name(code),
                "label": meta.get("label") or code,
                "official": code in official,
                "note": alias_note.get(code),
                "count": st["count"],
                "searches": st["searches"],
                "purchases": st["purchases"],
                "avgGrowth": roundn(st["growth_sum"] / st["growth_n"], 4) if st["growth_n"] else None,
                "upRatio": roundn(st["up"] / st["growth_n"], 4) if st["growth_n"] else None,
            }
        )
    # 22 个大类按搜索量排序，兜底桶固定放最后
    buckets = {config.OTHER_DEPT_CODE, config.NONE_DEPT_CODE}
    dept_list.sort(key=lambda x: (x["code"] in buckets, -x["searches"]))
    official_count = sum(1 for d in dept_list if d["code"] in official)

    brief = brief_row

    growth_pool = [r for r in rows if (r["w1_rank_growth_rate"] or 0) > 0 and (r["searches"] or 0) >= 5000]
    top_growth = [brief(r) for r in sorted(growth_pool, key=lambda r: -r["w1_rank_growth_rate"])[: config.TOP_N]]
    top_searches = [brief(r) for r in rows[: config.TOP_N]]
    # 机会词：搜索量够大、点击/转化集中度低、标题密度低
    opportunities = [
        r for r in rows
        if (r["searches"] or 0) >= 10000
        and (r["cvs_share_rate"] or 0) <= 0.3
        and (r["title_density"] or 0) <= 20
        and (r["w1_rank_growth_rate"] or 0) > 0
    ]
    top_opportunity = [
        brief(r) for r in sorted(opportunities, key=lambda r: -((r["searches"] or 0) * (r["w1_rank_growth_rate"] or 0)))[
            : config.TOP_N
        ]
    ]
    # 泡沫词：搜索暴涨但集中度极高
    top_monopoly = [
        brief(r) for r in sorted(
            [r for r in rows if (r["searches"] or 0) >= 20000 and (r["click_share_rate"] or 0) > 0],
            key=lambda r: -(r["click_share_rate"] or 0),
        )[: config.TOP_N]
    ]

    return {
        "meta": {"market": market, "tableDate": table_date, "generatedAt": datetime.now().isoformat(timespec="seconds")},
        "kpis": {
            "keywords": len(rows),
            "totalSearches": total_searches,
            "avgSearches": int(total_searches / len(rows)) if rows else 0,
            "avgGrowth": roundn(statistics.fmean(growth_rates)) if growth_rates else None,
            "medianGrowth": roundn(statistics.median(growth_rates)) if growth_rates else None,
            "upCount": up,
            "upRatio": roundn(up / len(growth_rates)) if growth_rates else None,
            "strongCount": strong,
            "totalPurchases": sum(r["purchases"] or 0 for r in rows),
            "deptCount": official_count,
            "deptOtherCount": sum(d["count"] for d in dept_list if d["code"] == config.OTHER_DEPT_CODE),
            "deptNoneCount": sum(d["count"] for d in dept_list if d["code"] == config.NONE_DEPT_CODE),
        },
        "departments": dept_list,
        "topGrowth": top_growth,
        "topSearches": top_searches,
        "topOpportunity": top_opportunity,
        "topMonopoly": top_monopoly,
    }


def build_weekly(conn: sqlite3.Connection, market: str, table_date: str) -> Dict[str, Any]:
    """大盘周趋势：每个统计周期下的关键词数、总搜索量、平均排名。"""
    rows = list(
        conn.execute(
            """
            SELECT t.label AS label,
                   COUNT(*) AS kw,
                   SUM(t.searches) AS searches,
                   AVG(t.rank) AS avg_rank
            FROM keyword_trend t
            JOIN keyword k
              ON k.keyword=t.keyword AND k.market=t.market AND k.table_date=t.table_date
            WHERE t.market=? AND t.table_date=? AND t.searches IS NOT NULL AND k.searches >= ?
            GROUP BY t.label
            ORDER BY t.label
            """,
            (market, table_date, config.MIN_SEARCHES),
        )
    )
    labels = [r["label"] for r in rows]
    return {
        "labels": labels,
        "searches": [r["searches"] or 0 for r in rows],
        "keywords": [r["kw"] for r in rows],
        "avgRank": [roundn(r["avg_rank"], 1) for r in rows],
    }


def build_keywords(rows: Sequence[sqlite3.Row], dept_index: Dict[str, int],
                   official: set) -> Dict[str, Any]:
    data = []
    for r in rows:
        codes = normalize_codes(r["dept_list"], official)
        dept = codes[0]
        # 一个关键词可能同属多个类目：
        #   dp  = 展示用的主类目（首个官方类目）
        #   dps = 归属的全部官方类目下标（宽松筛选：ABA 归类）
        #   dpm = 主类目桶下标（严格筛选：只看主类目；主类目是小类则归入「其它细分类目」）
        dept_ids = [dept_index.get(c, dept_index[config.NONE_DEPT_CODE]) for c in codes]
        primary = r["primary_dept"]
        if primary and primary in official:
            primary_bucket = config.DEPT_ALIAS.get(primary, primary)
        elif primary:
            primary_bucket = config.OTHER_DEPT_CODE
        else:
            primary_bucket = config.NONE_DEPT_CODE
        primary_idx = dept_index.get(primary_bucket, dept_index[config.NONE_DEPT_CODE])
        brands = json.loads(r["top3_brands"] or "[]")
        asins = json.loads(r["top3_asins"] or "[]")
        word_count = len([w for w in (r["keyword"] or "").replace("-", " ").split() if w])
        data.append(
            [
                r["keyword"],
                dept_ids[0],
                dept_ids,
                r["searches"] or 0,
                r["search_rank"] or 0,
                roundn(r["w1_rank_growth_val"], 2) or 0,
                roundn(r["w1_rank_growth_rate"], 4) or 0,
                r["w1_search_rank"] or 0,
                r["purchases"] or 0,
                roundn(r["purchase_rate"], 4) or 0,
                r["products"] or 0,
                r["spr"] or 0,
                r["title_density"] or 0,
                roundn(r["bid"], 2) or 0,
                roundn(r["exact_ppc"], 2) or 0,
                r["ad_products_1"] or 0,
                r["ad_products_7"] or 0,
                r["ad_products_30"] or 0,
                roundn(r["click_share_rate"], 4) or 0,
                roundn(r["cvs_share_rate"], 4) or 0,
                "|".join([b for b in brands if b]),
                # ASIN:点击占比%，抽屉里用来画真实占比条
                "|".join(
                    f"{a_['asin']}:{round((a_.get('clickRate') or 0) * 100, 2)}"
                    for a_ in asins if a_.get("asin")
                ),
                r["keyword_cn"] or "",
                r["impressions"] or 0,
                r["clicks"] or 0,
                word_count,
                primary_idx,
            ]
        )
    cols = ["kw", "dp", "dps", "se", "rk", "gv", "gr", "w1rk", "pu", "pr", "np", "spr", "td",
            "bid", "ep", "a1", "a7", "a30", "cs", "cv", "br", "as", "cn", "im", "cl", "wc", "dpm"]
    return {"cols": cols, "rows": data}


def build_trend_shards(conn: sqlite3.Connection, market: str, table_date: str,
                       keywords: Sequence[str]) -> Dict[str, Any]:
    """把关键词趋势按 keywords.json 的行序切片，前端按「行号 // shardSize」定位分片。

    这样同一屏（相邻行）通常只命中 1~2 个分片，翻页时几乎不用重复下载。
    """
    out_dir = OUT_DIR / "trends"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("shard-*.json"):
        old.unlink()

    labels = [r["label"] for r in conn.execute(
        "SELECT DISTINCT label FROM keyword_trend WHERE market=? AND table_date=? ORDER BY label",
        (market, table_date),
    )]

    trends: Dict[str, Tuple[List[int], List[int]]] = {}
    for keyword, searches, rank in conn.execute(
        "SELECT keyword, searches, rank FROM keyword_trend "
        "WHERE market=? AND table_date=? ORDER BY keyword, seq",
        (market, table_date),
    ):
        pair = trends.setdefault(keyword, ([], []))
        pair[0].append(searches or 0)
        pair[1].append(rank or 0)

    n = len(keywords)
    shard_size = max(1, math.ceil(n / config.TREND_SHARDS))
    kept = 0
    for i in range(config.TREND_SHARDS):
        chunk = keywords[i * shard_size:(i + 1) * shard_size]
        if not chunk:
            continue
        payload = {"labels": labels, "size": shard_size, "offset": i * shard_size,
                   "s": [trends.get(kw, ([], []))[0] for kw in chunk],
                   "r": [trends.get(kw, ([], []))[1] for kw in chunk]}
        path = out_dir / f"shard-{i:02d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        kept += 1
    return {"shards": kept, "labels": len(labels), "shardSize": shard_size}


def list_db_weeks(conn) -> list:
    """库里已有的数据期（新→旧）。"""
    return [r["table_date"] for r in conn.execute(
        "SELECT DISTINCT table_date FROM keyword WHERE market=? ORDER BY table_date DESC",
        (config.MARKET,))]


def list_built_weeks() -> list:
    """web/data 下已经生成好的周目录（新→旧）。"""
    return sorted(
        [d.name for d in config.WEB_DATA.iterdir() if d.is_dir() and d.name.startswith("ara_")],
        reverse=True,
    )


def write_index(conn) -> dict:
    """写 web/data/index.json：前端数据期下拉框的数据源。"""
    import json as _json
    built = list_built_weeks()
    db_weeks = list_db_weeks(conn)
    items = []
    for t in built:
        meta_path = config.WEB_DATA / t / "meta.json"
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        # 抓取完整性：__ALL__ 任务的已抓页数 vs 应抓页数（Cookie 过期时会中途断掉）
        row = conn.execute(
            "SELECT COUNT(*) AS pages, MAX(total) AS total FROM fetch_log "
            "WHERE task='__ALL__' AND market=? AND table_date=?",
            (config.MARKET, t),
        ).fetchone()
        pages = int(row["pages"] or 0)
        total = int(row["total"] or 0)
        need = (total + config.PAGE_SIZE - 1) // config.PAGE_SIZE if total else 0
        items.append({
            "table": t,
            "label": t.replace("ara_", ""),
            "keywords": meta.get("keywordCount"),
            "generatedAt": meta.get("generatedAt"),
            "complete": bool(need) and pages >= need,
            "pages": pages,
            "pagesNeeded": need,
        })
    # 20260912 → 2026-09-12（周六）+ 覆盖周期
    for it in items:
        lab = it["label"]
        if len(lab) == 8:
            it["rangeText"] = f"{lab[0:4]}-{lab[4:6]}-{lab[6:8]} 当周"
            it["display"] = f"{lab[0:4]}-{lab[4:6]}-{lab[6:8]}"
        elif len(lab) == 6:
            it["display"] = f"{lab[0:4]}-{lab[4:6]}"
    index = {
        "latest": items[0]["table"] if items else None,
        "weeks": items,
        "dbWeeks": db_weeks,
        "builtWeeks": built,
        "market": config.MARKET,
    }
    config.WEB_DATA.mkdir(parents=True, exist_ok=True)
    (config.WEB_DATA / "index.json").write_text(
        _json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return index


def load_dept_meta() -> Dict[str, Dict[str, str]]:
    """类目元数据（中文名），跨周共用，取一次并缓存到 web/data/departments.json。"""
    raw_dept_cache = config.WEB_DATA / "departments.json"
    try:
        import os

        from aba_client import AbaClient

        cookie = ""
        for path in config.COOKIE_CANDIDATES:
            if Path(path).exists():
                cookie = Path(path).read_text(encoding="utf-8").strip()
                break
        cookie = os.environ.get("SELLERSPRITE_COOKIE", "").strip() or cookie
        raw = AbaClient(cookie).departments(config.MARKET_ID)
        raw_dept_cache.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return {d["code"]: d for d in raw}
    except Exception as exc:
        log("info", f"  类目元数据在线获取失败（{exc}），使用缓存/裸 code")
        if raw_dept_cache.exists():
            raw = json.loads(raw_dept_cache.read_text(encoding="utf-8"))
            return {d["code"]: d for d in raw}
        return {}


def build_one_week(conn, market: str, table_date: str, dept_meta: dict) -> Dict[str, Any]:
    """为单个数据期生成前端数据到 web/data/<table_date>/。"""
    global OUT_DIR
    OUT_DIR = config.WEB_DATA / table_date
    log("info", f"── 生成 {table_date} 的前端数据")
    official = set(config.OFFICIAL_DEPTS)
    if dept_meta:
        official = {code for code in dept_meta if code != "any"} or official
    dept_meta = dict(dept_meta)
    dept_meta[config.OTHER_DEPT_CODE] = {"code": config.OTHER_DEPT_CODE, "name": config.OTHER_DEPT_NAME}
    dept_meta[config.NONE_DEPT_CODE] = {"code": config.NONE_DEPT_CODE, "name": config.NONE_DEPT_NAME}
    log("info", f"  大类口径: {'仅 departments 接口的 ' + str(len(official)) + ' 个大类' if config.OFFICIAL_DEPTS_ONLY else '全部类目'}")

    rows = fetch_rows(conn, market, table_date)
    log("info", f"  关键词 {len(rows):,} 个")

    summary = build_summary(rows, market, table_date, dept_meta, official)
    summary["weekly"] = build_weekly(conn, market, table_date)

    # 类目索引（含未归类兜底）
    dept_codes = [d["code"] for d in summary["departments"]]
    for fallback in (config.OTHER_DEPT_CODE, config.NONE_DEPT_CODE):
        if fallback not in dept_codes:
            dept_codes.append(fallback)
    dept_index = {code: i for i, code in enumerate(dept_codes)}
    alias_note = alias_notes(dept_meta, official)
    summary["deptIndex"] = [
        {"code": c,
         "name": (config.OTHER_DEPT_NAME if c == config.OTHER_DEPT_CODE
                  else config.NONE_DEPT_NAME if c == config.NONE_DEPT_CODE
                  else dept_meta.get(c, {}).get("translation")
                  or dept_meta.get(c, {}).get("label")
                  or config.DEPT_NAME_EXTRA.get(c) or c),
         "official": c in official and c not in (config.OTHER_DEPT_CODE, config.NONE_DEPT_CODE),
         "note": alias_note.get(c)}
        for c in dept_codes
    ]

    # 跟随类目切换的两张图：整体 + 每个类目各一套
    dept_growth, dept_scatter = build_dept_charts(rows, official, dept_index)
    summary["deptTopGrowth"] = dept_growth
    summary["deptScatter"] = dept_scatter
    log("info", f"  榜单/散点已按 {len(dept_growth)} 个口径拆分（整体 + 各类目）")

    keywords = build_keywords(rows, dept_index, official)

    # 多折线图：整体 + 每个类目各挑一套「有真实历史曲线」的重点增长词，前端按类目切换
    def history_of(keyword: str):
        return list(
            conn.execute(
                "SELECT label, searches, rank FROM keyword_trend "
                "WHERE market=? AND table_date=? AND keyword=? ORDER BY seq",
                (market, table_date, keyword),
            )
        )

    def nonzero(points) -> int:
        return sum(1 for p in points if (p["searches"] or 0) > 0)

    def pick_series(pool, limit: int = 10):
        """优先取「近一周增幅≥20% 且搜索量≥3万」且有完整历史曲线的词，不够再退回搜索量榜。"""
        candidates = [r["keyword"] for r in pool
                      if (r["w1_rank_growth_rate"] or 0) >= 0.2 and (r["searches"] or 0) >= 30000]
        fallback = [r["keyword"] for r in pool]
        out, used = [], set()
        for kw in candidates + fallback:
            if len(out) >= limit:
                break
            if kw in used:
                continue
            points = history_of(kw)
            if nonzero(points) < 6:
                continue
            used.add(kw)
            out.append(
                {"keyword": kw,
                 "labels": [p["label"] for p in points],
                 "searches": [p["searches"] or 0 for p in points],
                 "rank": [p["rank"] or 0 for p in points]}
            )
        return out

    dept_pool: Dict[str, List[sqlite3.Row]] = {}
    for r in rows:
        for code in normalize_codes(r["dept_list"], official):
            dept_pool.setdefault(code, []).append(r)

    trend_series = {"__all__": pick_series(rows)}
    for code, pool in dept_pool.items():
        trend_series[code] = pick_series(pool)
    summary["deptTrendSeries"] = trend_series

    # （四象限/榜单数据已按类目在 build_summary 里生成：deptScatter / deptTopGrowth）

    meta = {
        **summary["meta"],
        "minSearches": config.MIN_SEARCHES,
        "amazonHost": AMAZON_HOSTS.get(config.MARKET, "www.amazon.com"),
        "rankGrowthType": config.RANK_GROWTH_TYPE,
        "reverseType": config.REVERSE_TYPE,
        "marketId": config.MARKET_ID,
        "keywordCount": len(rows),
        "trendShards": config.TREND_SHARDS,
        "source": "/v3/api/aba-research",
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (OUT_DIR / "keywords.json").write_text(
        json.dumps(keywords, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    shard_info = build_trend_shards(conn, market, table_date, [r["keyword"] for r in rows])
    meta["trendLabels"] = shard_info["labels"]
    meta["shardSize"] = shard_info["shardSize"]
    (OUT_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    size_mb = sum(f.stat().st_size for f in OUT_DIR.rglob("*.json")) / 1024 / 1024
    log("info", f"完成：类目 {len(summary['departments'])} 个 / 榜单 {len(summary['topGrowth'])} 条 / "
                f"趋势分片 {shard_info['shards']} 个 / 数据合计 {size_mb:.1f}MB")
    return {
        "table_date": table_date,
        "keywords": len(rows),
        "departments": len(summary["departments"]),
        "trend_shards": shard_info["shards"],
        "web_data_mb": round(size_mb, 1),
    }


def run(table_date: Optional[str] = None, keep_weeks: Optional[int] = None,
        all_weeks: bool = False, force: bool = False) -> Dict[str, Any]:
    """生成前端数据（每周一个目录，历史周只生成一次）。

    - table_date 指定：只生成该周
    - all_weeks=True：库里所有数据期都生成
    - 默认：最近 keep_weeks 周（config.KEEP_WEEKS，默认 8），已生成过的历史周自动跳过
    """
    conn = connect()
    market = config.MARKET
    db_weeks = list_db_weeks(conn)
    if not db_weeks:
        raise SystemExit("库里没有数据，请先运行抓取")

    if table_date:
        targets = [t for t in [table_date] if t in db_weeks] or [db_weeks[0]]
    elif all_weeks:
        targets = db_weeks
    else:
        keep = keep_weeks or getattr(config, "KEEP_WEEKS", 8)
        targets = db_weeks[:max(1, int(keep))]

    exist = [t for t in targets if (config.WEB_DATA / t / "keywords.json").exists()]
    pending = [t for t in targets if t not in exist]
    log("info", f"库里共 {len(db_weeks)} 个数据期；本次目标 {len(targets)} 周，已生成 {len(exist)} 周，待生成 {pending or '无'}")

    dept_meta = load_dept_meta()

    built = []
    for i, week in enumerate(targets, 1):
        is_latest = (week == targets[0])
        already = (config.WEB_DATA / week / "keywords.json").exists() and not force
        if already and not is_latest:
            # 与库里实际词量比对：不一致说明上次是半成品（抓取中断时生成过），需要重建
            try:
                have = int(json.loads((config.WEB_DATA / week / "meta.json").read_text(encoding="utf-8"))["keywordCount"])
            except Exception:
                have = -1
            db_have = conn.execute(
                "SELECT COUNT(*) FROM keyword WHERE market=? AND table_date=?", (market, week)
            ).fetchone()[0]
            if have == db_have:
                log("info", f"[{i}/{len(targets)}] {week} 已生成且与库一致（{have:,} 词），跳过")
                continue
            log("info", f"[{i}/{len(targets)}] {week} 前端数据已过期（{have:,} → {db_have:,} 词），重建")
        log("info", f"[{i}/{len(targets)}] 生成 {week}" + ("（最新周，每次刷新）" if is_latest else "（force 重建）" if force else ""))
        progress(i, len(targets))
        built.append(build_one_week(conn, market, week, dept_meta))

    index = write_index(conn)
    conn.close()
    log("info", f"数据期索引已更新：共 {len(index['weeks'])} 周可切换（最新 {index['latest']}）")
    latest = built[0] if built else {"table_date": targets[0] if targets else None}
    return {
        "weeks_built": [b["table_date"] for b in built],
        "weeks_available": [w["table"] for w in index["weeks"]],
        "table_date": latest.get("table_date"),
        "keywords": latest.get("keywords", 0),
        "departments": latest.get("departments", 0),
        "trend_shards": latest.get("trend_shards", 0),
        "web_data_mb": latest.get("web_data_mb", 0),
    }


CSV_COLUMNS = [
    ("keyword", "关键词"), ("keyword_cn", "中文译名"), ("dept", "类目"),
    ("searches", "月搜索量"), ("search_rank", "ABA排名"), ("w1_search_rank", "上期排名"),
    ("rank_growth_value", "排名变化量"), ("rank_growth_rate", "排名增幅%"),
    ("purchases", "月购买量"), ("purchase_rate", "购买率%"), ("products", "在售商品数"),
    ("spr", "SPR"), ("title_density", "标题密度"), ("click_share_rate", "点击集中度%"),
    ("cvs_share_rate", "转化集中度%"), ("bid", "建议竞价"), ("ad_products_30", "30天广告商品"),
]


def export_top_csv(path, limit: int = 2000) -> int:
    """导出当周选品清单（按搜索量排序的前 N 个关键词）为 CSV。"""
    import csv as _csv
    conn = connect()
    row = conn.execute(
        "SELECT table_date FROM keyword WHERE market=? GROUP BY table_date "
        "ORDER BY table_date DESC LIMIT 1", (config.MARKET,)
    ).fetchone()
    if not row:
        raise RuntimeError("库中没有数据，请先执行抓取")
    table_date = row["table_date"]
    official = set(config.OFFICIAL_DEPTS)
    rows = fetch_rows(conn, config.MARKET, table_date)[:limit]
    conn.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = _csv.writer(fh)
        writer.writerow([label for _, label in CSV_COLUMNS])
        for r in rows:
            dept = normalize_codes(r["dept_list"], official)[0]
            growth = r["w1_rank_growth_rate"]
            writer.writerow([
                r["keyword"], r["keyword_cn"] or "", dept,
                r["searches"] or 0, r["search_rank"] or 0, r["w1_search_rank"] or 0,
                r["w1_rank_growth_val"] or 0, round((growth or 0) * 100, 2),
                r["purchases"] or 0, round((r["purchase_rate"] or 0) * 100, 2),
                r["products"] or 0, r["spr"] or 0, r["title_density"] or 0,
                round((r["click_share_rate"] or 0) * 100, 2),
                round((r["cvs_share_rate"] or 0) * 100, 2),
                round(r["bid"] or 0, 2), r["ad_products_30"] or 0,
            ])
    return len(rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = [a for a in (argv if argv is not None else sys.argv[1:])]
    flags = {a for a in args if a.startswith("--")}
    tables = [a for a in args if not a.startswith("--")]
    run(tables[0] if tables else None, all_weeks="--all" in flags, force="--force" in flags)
    return 0


if __name__ == "__main__":
    sys.exit(main())
