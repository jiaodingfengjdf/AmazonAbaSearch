# -*- coding: utf-8 -*-
"""ASIN 维度数据：关键词 TOP10 商品快照 + 商品档案 + 价格/BSR 趋势。

数据来源（全部实测可用，详见 docs/卖家精灵ABA数据API逆向文档.md）：

1. 关键词下 TOP10 ASIN 列表
   - ABA 列表接口每行自带 `gkDatas`：该词搜索位前 10 个商品快照
     （asin / position / rankPage / badges / 图片 / 价格 / 评分 / 评论数 / 标题），抓取时随关键词一起入库；
   - ABA 列表接口的 `top3AsinDtoList` 只有 3 个（后端写死），这里用 gkDatas 补足到 10 个。
2. ASIN 商品档案
   POST /v3/api/aba-research/asin/past-position
   body: {asins:[...], keyword, market, reverseType}  —— 一次可传 100+ 个 ASIN
   返回：品牌/卖家/配送/卖家数/变体数/分类 BSR/评分/评论数/上架时间/近30天销量/月销售额/
        FBA 费用/毛利率/尺寸重量/LQS/视频/EBC/该词历史周排名 等
3. ASIN 价格与销量趋势
   GET  /v3/api/trends/amz-unit-trend/{marketId}/{asin}   → 月均价/月销量/月销售额
   POST /v2/competitor-lookup/chart-monthly.json           → 月 BSR / 评分 / 评论数（可选补充）

缓存策略：
- `asin_keyword`（key = 关键词 + 数据期 + ASIN）：周更随关键词一起刷新，抽屉按需读取/实时补抓；
- `asin_trend`（key = ASIN + 站点）：与关键词无关，跨周复用，默认 7 天内不重复抓。
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import config
from aba_client import AbaApiError, AbaClient
from steps.logger import log as jlog, progress as jprogress

SESSION_CODES = ("ERR_GLOBAL_SESSION_EXPIRED", "ERR_USER_NOT_LOGIN", "ERR_REQUIRE_GUEST_ACCESS")

# asin/past-position 里真正要展示的字段（其余大字段不落库，控制体积）
DETAIL_FIELDS = [
    "title", "brand", "brandUrl", "imageUrl", "zoomImageUrl", "price", "averagePrice",
    "coupon", "primeExclusivePrice", "deliveryPrice",
    "bsrRank", "bsrLabel", "bsrId", "bsrRankCv", "bsrRankCr", "subcategories",
    "reviews", "rating", "reviewsRate", "reviewsIncreasement", "reviewsDelta", "questions",
    "availableDate", "availableDays", "publishDate", "firstReviewDate",
    "totalUnits", "totalAmount", "totalUnitsGrowth", "totalAmountGrowth", "amzUnit", "amzUnitDate",
    "sellerName", "sellerId", "sellerType", "sellerNation", "sellers",
    "variations", "parent", "sku", "fba", "profit", "lqs",
    "bestSeller", "newRelease", "amazonChoice", "video", "ebc",
    "dimensions", "weight", "pkgDimensions", "pkgWeight", "pkgVolumeWeights",
    "categoryId", "categoryName", "nodeIdPath", "nodeLabelPath", "nodeLabelLocale",
    "channel", "symbol", "alias",
]


class AsinDataError(RuntimeError):
    """ASIN 数据获取失败（含登录态失效），供看板服务转成可读提示。"""

    def __init__(self, message: str, code: str = "ERR_ASIN_FETCH"):
        super().__init__(message)
        self.code = code


def log(level: str, msg: str) -> None:
    jlog(level, msg)


def progress(current: int, total: int) -> None:
    jprogress(current, total)


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def make_client() -> AbaClient:
    from steps.store import load_cookie

    try:
        cookie = load_cookie()
    except SystemExit as exc:
        raise AsinDataError(SESSION_HINT, code="ERR_USER_NOT_LOGIN") from exc
    return AbaClient(cookie, min_interval=float(getattr(config, "ASIN_MIN_INTERVAL", 0.3)))


SESSION_HINT = "卖家精灵登录态失效，正在尝试自动恢复"


def _call_with_session_retry(fn, *, tries: int = 2, wait: float = 1.2):
    """会话类错误重试一次：服务端偶发中断时，重试往往就能成功。"""
    last: Optional[Exception] = None
    for attempt in range(max(1, tries)):
        try:
            return fn()
        except AbaApiError as exc:
            last = exc
            if exc.code not in SESSION_CODES or attempt >= tries - 1:
                raise
            time.sleep(wait * (attempt + 1))
    raise last if last else RuntimeError("unreachable")


# ---------------------------------------------------------------- 工具


def _to_json(value: Any) -> Any:
    """接口里部分字段是 JSON 字符串，前端直接用对象更省事。"""
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001
            return value
    return value


def slim_detail(raw: Dict[str, Any]) -> Dict[str, Any]:
    """裁剪 asin/past-position 单行数据。"""
    out: Dict[str, Any] = {}
    for key in DETAIL_FIELDS:
        if key in raw:
            out[key] = raw[key]
    out["salesTrend"] = _to_json(raw.get("salesTrend")) or {}
    out["amzUnitTrend"] = _to_json(raw.get("amzUnitTrend")) or {}
    positions = raw.get("pastPositions") or []
    out["pastPositions"] = [
        {"date": p.get("date"), "position": p.get("position")}
        for p in positions if isinstance(p, dict) and p.get("position") is not None
    ]
    return out


def _asins_of_keyword(conn: sqlite3.Connection, keyword: str, market: str,
                      table_date: str) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """整理该词的待展示 ASIN 列表：gkDatas 前 10（按搜索位）+ ABA TOP3（带点击占比）。"""
    row = conn.execute(
        "SELECT gk_asins, top3_asins FROM keyword WHERE keyword=? AND market=? AND table_date=?",
        (keyword, market, table_date),
    ).fetchone()
    gk: List[Dict[str, Any]] = json.loads((row and row["gk_asins"]) or "[]")
    top3: List[Dict[str, Any]] = json.loads((row and row["top3_asins"]) or "[]")
    top3_map = {a.get("asin"): a for a in top3 if a.get("asin")}

    ordered: List[Dict[str, Any]] = []
    seen = set()
    for idx, item in enumerate(gk, 1):
        asin = item.get("asin")
        if not asin or asin in seen:
            continue
        seen.add(asin)
        ordered.append({
            "asin": asin,
            "position": item.get("position") if item.get("position") is not None else idx,
            "rankPage": item.get("rankPage"),
            "rankIndex": item.get("rankIndex"),
            "badge": item.get("badges"),
            "imageUrl": item.get("asinImage"),
            "title": item.get("asinTitle") or item.get("asinUrl"),
            "price": item.get("asinPrice"),
            "reviews": item.get("asinReviews"),
            "rating": item.get("asinRating"),
        })
    # ABA TOP3 若不在搜索位前 10（新上榜/广告位），补在末尾但保留点击占比
    for asin, item in top3_map.items():
        if asin in seen:
            continue
        seen.add(asin)
        ordered.append({
            "asin": asin, "position": None, "rankPage": None, "rankIndex": None,
            "badge": None, "imageUrl": item.get("imageUrl"), "title": None,
            "price": None, "reviews": None, "rating": None,
        })
    return ordered, top3_map


# ---------------------------------------------------------------- 抓取


def fetch_keyword_details(conn: sqlite3.Connection, client: AbaClient, *,
                          keyword: str, market: str, table_date: str,
                          asins: Sequence[str], reverse_type: Optional[str] = None,
                          chunk_size: int = 40) -> int:
    """抓取并缓存这些 ASIN 在该关键词下的商品档案，返回写入条数。"""
    asins = [a for a in dict.fromkeys(asins) if a]
    if not asins:
        return 0
    reverse = (reverse_type or getattr(config, "REVERSE_TYPE", "W")).upper()
    written = 0
    for start in range(0, len(asins), chunk_size):
        batch = asins[start:start + chunk_size]
        try:
            resp = _call_with_session_retry(lambda: client._post(
                "/v3/api/aba-research/asin/past-position",
                {"asins": list(batch), "keyword": keyword,
                 "market": getattr(config, "MARKET", "COM"), "reverseType": reverse},
            ))
        except AbaApiError as exc:
            if exc.code in SESSION_CODES:
                raise AsinDataError(SESSION_HINT, code="ERR_USER_NOT_LOGIN") from exc
            raise AsinDataError(f"{exc.code}: {exc.message}", code=exc.code) from exc
        data = resp.get("data") or {}
        stamp = now_text()
        rows = []
        for asin in batch:
            detail = slim_detail(data.get(asin) or {})
            if not detail:
                continue
            rows.append((keyword, market, table_date, asin,
                         json.dumps(detail, ensure_ascii=False), stamp))
        if rows:
            conn.executemany(
                "INSERT INTO asin_keyword(keyword, market, table_date, asin, data, fetched_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(keyword, market, table_date, asin) DO UPDATE SET "
                "data=excluded.data, fetched_at=excluded.fetched_at",
                rows,
            )
            conn.commit()
            written += len(rows)
    return written


def fetch_asin_trend(client: AbaClient, asin: str, market: Optional[str] = None) -> Dict[str, Any]:
    """月均价 / 月销量 / 月销售额（+ 月 BSR / 评分 / 评论数）曲线。"""
    market_id = int(getattr(config, "MARKET_ID", 1))
    merged: Dict[str, Dict[str, Any]] = {}
    primary_ok = False
    chart_ok = False
    try:
        resp = _call_with_session_retry(
            lambda: client._request("GET", f"/v3/api/trends/amz-unit-trend/{market_id}/{asin}"))
        primary_ok = True
        for point in ((resp.get("data") or {}).get("trend") or []):
            date = point.get("date")
            if not date:
                continue
            item = merged.setdefault(date, {"date": date})
            item["unit"] = point.get("unit")
            item["price"] = point.get("price")
            item["amount"] = point.get("amount")
    except AbaApiError as exc:
        if exc.code in SESSION_CODES:
            raise AsinDataError(SESSION_HINT, code="ERR_USER_NOT_LOGIN") from exc
    except Exception:  # noqa: BLE001
        pass  # 第二个独立接口仍可能给出可用的月度数据

    # 月 BSR / 评分 / 评论数：接口较重（约 180KB），失败不影响价格趋势
    try:
        response = client.session.post(
            "https://www.sellersprite.com/v2/competitor-lookup/chart-monthly.json",
            data={"marketId": market_id, "asin": asin},
            headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") in SESSION_CODES:
            raise AsinDataError(SESSION_HINT, code="ERR_USER_NOT_LOGIN")
        if payload.get("code") not in (None, "OK", "ok", "SUCCESS", 0, 1, True):
            raise AsinDataError("卖家精灵月度图表接口暂不可用", code="ERR_TREND_UPSTREAM")
        chart_ok = True
        chart = ((payload.get("data") or {}).get("chartData") or {})
        for date, point in chart.items():
            if not isinstance(point, dict):
                continue
            item = merged.setdefault(date, {"date": date})
            for src, dst in (("bsrRank", "bsr"), ("rating", "rating"), ("reviews", "reviews"),
                             ("totalUnits", "unit"), ("averagePrice", "price")):
                if point.get(src) is not None and item.get(dst) is None:
                    item[dst] = point.get(src)
    except AsinDataError as exc:
        if exc.code == "ERR_USER_NOT_LOGIN":
            raise
    except Exception:  # noqa: BLE001
        pass

    usable = any(any(p.get(field) is not None for field in ("unit", "price", "amount", "bsr"))
                 for p in merged.values())
    if not usable and not (primary_ok and chart_ok):
        raise AsinDataError("卖家精灵月度接口暂不可用，稍后会自动重试", code="ERR_TREND_UPSTREAM")
    return {"asin": asin, "market": market or getattr(config, "MARKET", "COM"),
            "fetchedAt": now_text(), "availability": "available" if usable else "no_data",
            "trend": [merged[k] for k in sorted(merged)] if usable else []}


def _detail_rows(conn: sqlite3.Connection, keyword: str, market: str,
                 table_date: str, asins: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    asins = [a for a in asins if a]
    if not asins:
        return {}
    marks = ",".join("?" * len(asins))
    rows = conn.execute(
        f"SELECT asin, data, fetched_at FROM asin_keyword "
        f"WHERE keyword=? AND market=? AND table_date=? AND asin IN ({marks})",
        [keyword, market, table_date, *asins],
    )
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        try:
            item = json.loads(r["data"] or "{}")
        except Exception:  # noqa: BLE001
            item = {}
        item["_fetchedAt"] = r["fetched_at"]
        out[r["asin"]] = item
    return out


# ---------------------------------------------------------------- 对外读取（含按需补抓）


def keyword_payload(conn: sqlite3.Connection, *, keyword: str, market: str, table_date: str,
                    live: bool = True, client: Optional[AbaClient] = None) -> Dict[str, Any]:
    """抽屉用的关键词 ASIN 明细：优先读缓存，缺失的实时补抓。"""
    ordered, top3_map = _asins_of_keyword(conn, keyword, market, table_date)
    if not ordered:
        return {"ok": True, "keyword": keyword, "week": table_date, "market": market,
                "source": "empty", "asins": [],
                "message": "该数据期没有入库的 ASIN 快照（旧数据期需重抓）"}
    top_n = int(getattr(config, "ASIN_TOP_N", 10) or 10)
    ordered = ordered[:max(top_n, 1)]

    asins = [a["asin"] for a in ordered]
    cached = _detail_rows(conn, keyword, market, table_date, asins)
    missing = [a for a in asins if a not in cached]
    source = "cache"
    error = None
    error_code = None
    aba = None
    own_client = False
    if missing and live:
        try:
            aba = client or make_client()
            own_client = client is None
            fetch_keyword_details(conn, aba, keyword=keyword, market=market,
                                  table_date=table_date, asins=missing)
            cached = _detail_rows(conn, keyword, market, table_date, asins)
            source = "live"
        except AsinDataError as exc:
            error = str(exc)
            error_code = exc.code
            source = "cache"
        except Exception:  # noqa: BLE001
            error = "商品详情接口暂不可用，稍后重试"
            error_code = "ERR_DETAIL_UPSTREAM"
            source = "cache"
        finally:
            if own_client and aba is not None:
                try:
                    aba.session.close()
                except Exception:  # noqa: BLE001
                    pass

    out_asins = []
    for item in ordered:
        asin = item["asin"]
        detail = cached.get(asin) or {}
        merged = dict(item)
        merged.update({k: v for k, v in detail.items() if k != "_fetchedAt"})
        merged["asin"] = asin
        merged["imageUrl"] = detail.get("imageUrl") or item.get("imageUrl")
        merged["zoomImageUrl"] = detail.get("zoomImageUrl")
        merged["title"] = detail.get("title") or item.get("title")
        merged["price"] = detail.get("price") if detail.get("price") is not None else item.get("price")
        merged["reviews"] = detail.get("reviews") if detail.get("reviews") is not None else item.get("reviews")
        merged["rating"] = detail.get("rating") if detail.get("rating") is not None else item.get("rating")
        t3 = top3_map.get(asin)
        if t3:
            merged["clickRate"] = t3.get("clickRate")
            merged["conversionRate"] = t3.get("conversionRate")
        merged["hasDetail"] = bool(detail)
        out_asins.append(merged)

    aba_top3 = [
        {"asin": a.get("asin"), "imageUrl": a.get("imageUrl"),
         "clickRate": a.get("clickRate"), "conversionRate": a.get("conversionRate"),
         "inTop": a.get("asin") in {x["asin"] for x in ordered}}
        for a in top3_map.values() if a.get("asin")
    ]
    return {
        "ok": True, "keyword": keyword, "week": table_date, "market": market,
        "source": source, "detailMissing": [a for a in asins if a not in cached],
        "asins": out_asins, "abaTop3": aba_top3, "error": error,
        "errorCode": error_code, "updatedAt": now_text(),
    }


def local_trend_fallback(conn: sqlite3.Connection, *, asin: str,
                         market: str) -> Optional[Dict[str, Any]]:
    """实时抓不到时，用已缓存的 ASIN 商品档案拼一条「本地推算」趋势。

    只使用本地已有字段：`salesTrend`（月度销量）、`price` / `averagePrice` / `totalUnits`。
    价格只有一个当前值（没有历史），因此只补在最后一个月的点上，并在返回值里标注 partial。
    """
    row = conn.execute(
        "SELECT keyword, table_date, data FROM asin_keyword WHERE asin=? AND market=? "
        "ORDER BY table_date DESC LIMIT 1", (asin, market)).fetchone()
    if not row:
        return None
    try:
        detail = json.loads(row["data"] or "{}")
    except Exception:  # noqa: BLE001
        return None
    sales = _to_json(detail.get("salesTrend")) or {}
    points: List[Dict[str, Any]] = []
    for key in sorted(sales):
        text = str(key)
        if len(text) != 6 or not text.isdigit():
            continue
        points.append({"date": f"{text[:4]}-{text[4:]}", "unit": sales.get(key),
                       "price": None, "amount": None})
    if not points:
        return None
    raw_price = detail.get("price") or detail.get("averagePrice")
    try:
        price = float(raw_price) if raw_price is not None else None
        if price is not None and not math.isfinite(price):
            price = None
    except (TypeError, ValueError):
        price = None
    if price is not None:
        points[-1]["price"] = price
    return {
        "asin": asin, "market": market, "fetchedAt": now_text(), "trend": points,
        "partial": True,
        "note": (f"月销量来自本地商品档案（{row['table_date']}），"
                 f"价格仅当前值 ${price:.2f}，无历史价格曲线" if price is not None
                 else "月销量来自本地商品档案；价格缺失"),
        "detailKeyword": row["keyword"],
    }


def has_usable_trend(payload: Dict[str, Any]) -> bool:
    return any(isinstance(point, dict) and
               any(point.get(field) is not None for field in ("unit", "price", "amount", "bsr"))
               for point in (payload.get("trend") or []))


def trend_payload(conn: sqlite3.Connection, *, asin: str, market: str, live: bool = True,
                  max_age_hours: Optional[float] = None,
                  client: Optional[AbaClient] = None) -> Dict[str, Any]:
    """ASIN 价格/销量/BSR 趋势：缓存新鲜就直接给，否则实时抓。"""
    max_age = float(max_age_hours if max_age_hours is not None
                    else getattr(config, "ASIN_TREND_MAX_AGE_HOURS", 168))
    row = conn.execute(
        "SELECT data, fetched_at FROM asin_trend WHERE asin=? AND market=?", (asin, market)
    ).fetchone()
    fresh = False
    payload: Dict[str, Any] = {}
    if row and row["fetched_at"]:
        try:
            fresh = (datetime.now() - datetime.fromisoformat(row["fetched_at"])) < timedelta(hours=max_age)
        except Exception:  # noqa: BLE001
            fresh = False
        try:
            payload = json.loads(row["data"] or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        if not has_usable_trend(payload) and payload.get("trend"):
            payload["trend"] = []  # 旧缓存中的全 null 点不是可用的月度数据
        if payload.get("availability") == "no_data":
            try:
                fetched_at = datetime.fromisoformat(row["fetched_at"])
                fresh = (datetime.now() - fetched_at) < timedelta(hours=24)
                if Path(config.COOKIE_CANDIDATES[0]).stat().st_mtime > fetched_at.timestamp():
                    fresh = False  # 登录态更新后重新核查此前的空结果
            except Exception:  # noqa: BLE001
                fresh = False
    if fresh and (has_usable_trend(payload) or payload.get("availability") == "no_data"):
        if payload.get("availability") == "no_data":
            fallback = local_trend_fallback(conn, asin=asin, market=market)
            if fallback:
                fallback.update({"ok": True, "source": "local", "availability": "no_data"})
                return fallback
        payload.update({"ok": True, "source": "cache"})
        return payload
    if not live:
        # 不实时抓时，若连缓存都没有，用本地商品档案推一条月销量曲线，避免图表空着
        if not has_usable_trend(payload):
            fallback = local_trend_fallback(conn, asin=asin, market=market)
            if fallback:
                fallback.update({"ok": True, "source": "local"})
                return fallback
        payload.update({"ok": True, "source": "cache"})
        return payload

    aba = None
    own_client = False
    error = None
    try:
        aba = client or make_client()
        own_client = client is None
        payload = fetch_asin_trend(aba, asin, market)
        conn.execute(
            "INSERT INTO asin_trend(asin, market, data, fetched_at) VALUES (?,?,?,?) "
            "ON CONFLICT(asin, market) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
            (asin, market, json.dumps(payload, ensure_ascii=False),
             payload.get("fetchedAt") or now_text()),
        )
        conn.commit()
        payload.update({"ok": True, "source": "live"})
        if payload.get("availability") == "no_data":
            fallback = local_trend_fallback(conn, asin=asin, market=market)
            if fallback:
                fallback.update({"ok": True, "source": "local", "availability": "no_data"})
                return fallback
    except AsinDataError as exc:
        error = str(exc)
        payload = payload or {"asin": asin, "market": market, "trend": []}
        if not has_usable_trend(payload):
            fallback = local_trend_fallback(conn, asin=asin, market=market)
            if fallback:
                payload = fallback
        payload.update({"ok": True, "source": "local" if payload.get("partial") else "cache",
                        "error": error, "errorCode": exc.code})
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        payload = payload or {"asin": asin, "market": market, "trend": []}
        payload.update({"ok": True, "source": "cache", "error": "趋势接口暂不可用，稍后重试",
                        "errorCode": "ERR_TREND_UPSTREAM"})
    finally:
        if own_client and aba is not None:
            try:
                aba.session.close()
            except Exception:  # noqa: BLE001
                pass
    return payload


# ---------------------------------------------------------------- 周更预取


def backfill_gk(conn: sqlite3.Connection, *, market: str, table_date: str,
                client: Optional[AbaClient] = None, pages: Optional[int] = None,
                page_size: Optional[int] = None) -> Dict[str, Any]:
    """给已经抓过的数据期补 `gk_asins`（TOP10 商品快照）。

    老库/旧数据期没有这一列，这里按「不限类目」任务的分页原样再拉一遍，
    只 UPDATE gk_asins 一个字段，不动其它数据，约 1000 条/3 秒。
    """
    page_size = int(page_size or getattr(config, "PAGE_SIZE", 1000))
    row = conn.execute(
        "SELECT COUNT(*) AS pages, MAX(total) AS total FROM fetch_log "
        "WHERE task='__ALL__' AND market=? AND table_date=?",
        (market, table_date),
    ).fetchone()
    total = int((row and row["total"]) or 0)
    need = (total + page_size - 1) // page_size if total else 0
    if not need:
        return {"gk_pages": 0, "gk_keywords": 0, "gk_seconds": 0.0,
                "gk_message": "没有 __ALL__ 任务的分页记录，无法回填"}
    todo = int(pages) if pages and pages > 0 else need
    todo = min(todo, need)

    own_client = client is None
    aba = client or make_client()
    started = time.time()
    updated = 0
    try:
        for page in range(1, todo + 1):
            progress(page, todo)
            data = aba.search(
                market=market, table=table_date,
                reverse_type=getattr(config, "REVERSE_TYPE", "W"),
                rank_growth_type=getattr(config, "RANK_GROWTH_TYPE", "W1"),
                page=page, size=page_size, sort="searchfrequencyrank", desc=False,
                departments=[], extra={"minSearches": getattr(config, "MIN_SEARCHES", 0)},
            )
            items = data.get("items") or []
            rows = [(json.dumps(compact_gk(item), ensure_ascii=False), item["keyword"],
                     market, table_date) for item in items if compact_gk(item)]
            if rows:
                conn.executemany(
                    "UPDATE keyword SET gk_asins=? WHERE keyword=? AND market=? AND table_date=?",
                    rows,
                )
                conn.commit()
                updated += len(rows)
            log("info", f"  gk 回填 page {page}/{todo} +{len(rows)} 条（累计 {updated}）")
    finally:
        if own_client:
            try:
                aba.session.close()
            except Exception:  # noqa: BLE001
                pass
    elapsed = time.time() - started
    log("info", f"gk 回填完成：{todo}/{need} 页 / {updated} 个关键词补上 TOP10 快照，耗时 {elapsed:.1f}s")
    return {"gk_pages": todo, "gk_pages_total": need, "gk_keywords": updated,
            "gk_seconds": round(elapsed, 1)}


def compact_gk(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """列表接口 gkDatas -> 紧凑结构（与 fetch_aba.row_from_item 保持一致）。"""
    return [
        {
            "asin": a.get("asin"),
            "position": a.get("position"),
            "rankPage": a.get("rankPage"),
            "rankIndex": a.get("rankIndex"),
            "badges": a.get("badges"),
            "asinTitle": a.get("asinTitle") or a.get("asinUrl"),
            "asinImage": a.get("asinImage"),
            "asinPrice": a.get("asinPrice"),
            "asinReviews": a.get("asinReviews"),
            "asinRating": a.get("asinRating"),
        }
        for a in (item.get("gkDatas") or []) if a.get("asin")
    ]


def prefetch(conn: sqlite3.Connection, *, market: str, table_date: str, top_n: int,
             client: Optional[AbaClient] = None, min_interval: Optional[float] = None,
             force: bool = False) -> Dict[str, Any]:
    """把搜索量前 N 个关键词的 TOP10 ASIN 档案预先抓进缓存（离线也能看详情）。"""
    top_n = int(top_n or 0)
    if top_n <= 0:
        return {"prefetch_keywords": 0, "prefetch_asins": 0, "prefetch_seconds": 0.0}
    rows = conn.execute(
        "SELECT keyword, gk_asins, top3_asins FROM keyword "
        "WHERE market=? AND table_date=? AND gk_asins IS NOT NULL "
        "ORDER BY searches DESC LIMIT ?",
        (market, table_date, top_n),
    ).fetchall()
    if not rows:
        return {"prefetch_keywords": 0, "prefetch_asins": 0, "prefetch_seconds": 0.0}

    own_client = client is None
    aba = client or make_client()
    if min_interval is not None:
        aba.min_interval = float(min_interval)
    started = time.time()
    done_kw = done_asins = skipped = 0
    try:
        total = len(rows)
        for idx, row in enumerate(rows, 1):
            progress(idx, total)
            keyword = row["keyword"]
            targets = [a["asin"] for a in json.loads(row["gk_asins"] or "[]") if a.get("asin")]
            targets += [a.get("asin") for a in json.loads(row["top3_asins"] or "[]") if a.get("asin")]
            targets = [a for a in dict.fromkeys(targets) if a]
            if not targets:
                continue
            if not force:
                cached = _detail_rows(conn, keyword, market, table_date, targets)
                targets = [a for a in targets if a not in cached]
                if not targets:
                    skipped += 1
                    continue
            try:
                done_asins += fetch_keyword_details(
                    conn, aba, keyword=keyword, market=market,
                    table_date=table_date, asins=targets)
                done_kw += 1
            except AsinDataError as exc:
                log("warn", f"  ! ASIN 预取中断（{keyword}）：{exc}")
                break
            if idx % 50 == 0:
                log("info", f"  ASIN 预取 {idx}/{total}（已缓存 {done_asins} 条 ASIN 档案）")
    finally:
        if own_client:
            try:
                aba.session.close()
            except Exception:  # noqa: BLE001
                pass
    elapsed = time.time() - started
    log("info", f"ASIN 预取完成：{done_kw} 个关键词 / {done_asins} 条 ASIN 档案"
                f"（跳过已有 {skipped} 个），耗时 {elapsed:.1f}s")
    return {
        "prefetch_keywords": done_kw,
        "prefetch_asins": done_asins,
        "prefetch_skipped": skipped,
        "prefetch_seconds": round(elapsed, 1),
    }
