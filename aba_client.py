#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
卖家精灵（SellerSprite）ABA 数据研究 API 客户端
================================================

接口逆向自前端 bundle：
    https://www.sellersprite.com/v3/webapp/static/js/app.*.js
    https://www.sellersprite.com/v3/webapp/static/js/chunk-95d370c2.*.js   # api 定义
    https://www.sellersprite.com/v3/webapp/static/js/chunk-1cd0c6a1.*.js   # 页面 / 参数拼装

核心接口: POST /v3/api/aba-research

鉴权：只需要浏览器登录态的 Cookie（其中 Sprite-X-Token 是 RS256 JWT，24 小时过期；
      JSESSIONID + rank-login-user 一起用）。不带 Cookie 也能调，但被限制为游客口径。

用法（命令行）
--------------
    # 1) 准备 cookie（浏览器 F12 -> 网络 -> 任意请求 -> 复制 Cookie 整串）
    #    写入 cookie.txt，或设置环境变量 SELLERSPRITE_COOKIE
    python aba_api.py tables
    python aba_api.py depts --market-id 1
    python aba_api.py search --q "phone stand" --size 5
    python aba_api.py dump --market COM --departments kitchen \
        --min-searches 20000 --max-searches 50000 --min-rank-growth-rate 20 \
        --sort searches --desc --max-items 500 --out kitchen_w1.csv
    python aba_api.py trends --keyword "phone stand"

用法（作为库）
--------------
    from aba_api import AbaClient
    aba = AbaClient.from_cookie_file("cookie.txt")
    table = aba.latest_week_table()          # -> "ara_20260912"
    page = aba.search(q="phone stand", size=50)
    for item in aba.iter_search(departments=["kitchen"], max_items=2000):
        print(item["keyword"], item["searches"])
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

import requests

BASE_URL = "https://www.sellersprite.com"
ABA_PATH = "/v3/api/aba-research"

# marketId -> (code, publicCode, 中文名)
MARKETS: Dict[int, tuple] = {
    1: ("US", "COM", "美国站"),
    6: ("JP", "JP", "日本站"),
    3: ("UK", "UK", "英国站"),
    4: ("DE", "DE", "德国站"),
    5: ("FR", "FR", "法国站"),
    35691: ("IT", "IT", "意大利站"),
    44551: ("ES", "ES", "西班牙站"),
    7: ("CA", "CA", "加拿大站"),
    44571: ("IN", "IN", "印度站"),
    771770: ("MX", "MX", "墨西哥站"),
    111172: ("AU", "AU", "澳洲站"),
    15: ("BR", "BR", "巴西站"),
    9: ("AE", "AE", "阿联酋站"),
    13: ("SA", "SA", "沙特站"),
}
MARKET_ALIASES: Dict[str, int] = {}
for _mid, (_code, _pub, _cn) in MARKETS.items():
    MARKET_ALIASES[_code.lower()] = _mid
    MARKET_ALIASES[_pub.lower()] = _mid

# 实测可用的排序字段（其它字段会返回 ERR_GLOBAL_500）
SORT_FIELDS = [
    "searches",             # 月搜索量
    "searchfrequencyrank",  # ABA 搜索频率排名
    "impressions",          # 月展示量
    "clicks",               # 月点击量
    "purchases",            # 月购买量
    "purchaseRate",         # 转化率 / 购买率
    "products",             # 在售商品数
    "cprExact",             # SPR
    "titleDensityExact",    # 标题密度
    "adProducts1",          # 1 天广告商品数
    "adProducts7",
    "adProducts30",
    "bid",
    "bidMax",
    "bidMin",
]

# 排名增长口径：最近 1/2/3/4 个统计周期（W=周表，M=月表）
RANK_GROWTH_TYPES = ["W1", "W2", "W3", "W4"]

BID_MATCH_TYPES = ["exact", "phrase", "broad"]

# 页面内置的 6 个「快速选品模型」对应的筛选组合（等价于页面上的模式按钮）
QUICK_MODES: Dict[int, Dict[str, Any]] = {
    1: {"_desc": "整体市场（限制搜索排名上限）"},
    2: {"movementMarket": True, "_desc": "飙升市场（新上榜 / 无历史基线关键词）"},
    3: {"rankGrowthType": "W4", "rankGrowthValue": 10000, "minRankGrowthRate": 10,
        "_desc": "4 周持续增长"},
    4: {"rankGrowthType": "W1", "minRankGrowthRate": 50, "_desc": "上周增幅 >50%"},
    5: {"rankGrowthType": "W1", "minRankGrowthRate": 20, "_desc": "上周增幅 >20% 且搜索排名区间过滤"},
    6: {"maxMonopolyClickRate": 30, "maxConversionRate": 30, "_desc": "点击/转化集中度低的蓝海词"},
}

CSV_COLUMNS = [
    ("keyword", "关键词"),
    ("keywordCn", "中文译名"),
    ("searches", "月搜索量"),
    ("searchRank", "ABA排名"),
    ("w1SearchRank", "上期排名"),
    ("w1RankGrowthRate", "上期排名增幅"),
    ("impressions", "月展示量"),
    ("clicks", "月点击量"),
    ("purchases", "月购买量"),
    ("purchaseRate", "购买率"),
    ("products", "在售商品数"),
    ("titleDensityExact", "标题密度"),
    ("cprExact", "SPR"),
    ("bid", "建议竞价"),
    ("exactPpc", "精准匹配竞价"),
    ("top3Brands", "TOP3品牌"),
    ("top3ClickRate", "TOP3点击集中度"),
    ("top3sumConversionRate", "TOP3转化集中度"),
    ("adProducts1", "1天广告商品数"),
    ("adProducts7", "7天广告商品数"),
    ("adProducts30", "30天广告商品数"),
]


class AbaApiError(RuntimeError):
    """接口返回了非 OK 的 code。"""

    def __init__(self, code: str, message: str = "", payload: Any = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.payload = payload


def market_id_of(value: Any) -> int:
    """接受 marketId(1) / code('US') / publicCode('COM') / 中文名，统一转成 marketId。"""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        key = value.strip().lower()
        if key.isdigit():
            return int(key)
        if key in MARKET_ALIASES:
            return MARKET_ALIASES[key]
    raise ValueError(f"未知站点: {value!r}")


def public_code_of(value: Any) -> str:
    """转成接口使用的 market 字段（publicCode，例如 COM / JP / UK）。"""
    return MARKETS[market_id_of(value)][1]


@dataclass
class AbaClient:
    cookie: str
    timeout: float = 120.0
    retries: int = 2
    min_interval: float = 0.0  # 每次请求之间的最小间隔，建议 0.3~1 秒
    base_url: str = BASE_URL
    session: requests.Session = field(default_factory=requests.Session, repr=False)
    _last_call: float = field(default=0.0, repr=False)

    # ---------- 构造 ----------

    @classmethod
    def from_cookie_file(cls, path: str | os.PathLike, **kwargs) -> "AbaClient":
        text = Path(path).read_text(encoding="utf-8").strip()
        return cls(text, **kwargs)

    @classmethod
    def from_env(cls, **kwargs) -> "AbaClient":
        cookie = os.environ.get("SELLERSPRITE_COOKIE", "").strip()
        if not cookie:
            raise SystemExit("环境变量 SELLERSPRITE_COOKIE 为空；请先登录浏览器复制 Cookie")
        return cls(cookie, **kwargs)

    def __post_init__(self) -> None:
        self.session.headers.update(
            {
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "content-type": "application/json;charset=UTF-8",
                "origin": self.base_url,
                "referer": f"{self.base_url}/v3/aba-research",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
                ),
            }
        )
        if self.cookie:
            self.session.headers["cookie"] = self.cookie

    # ---------- 底层请求 ----------

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> Dict[str, Any]:
        url = path if path.startswith("http") else self.base_url + path
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            if self.min_interval:
                wait = self.min_interval - (time.time() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, timeout=self.timeout
                )
                self._last_call = time.time()
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # 网络层 / 解析层
                last_err = exc
                if attempt < self.retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise
            code = payload.get("code")
            if code in ("OK", "ok", 1, None):
                return payload
            # 业务错误：400/500 属于请求问题，重试无意义
            raise AbaApiError(code, payload.get("message", ""), payload)
        raise RuntimeError(f"请求失败: {last_err}")

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        return self._request("POST", path, json_body=body)

    # ---------- 元数据 ----------

    def tables(self) -> Dict[str, Any]:
        """GET /v3/api/aba-research/tables

        返回 {weekTables, monthTables, restrictions}；weekTables[0] 即最新周表。
        """
        return self._get(f"{ABA_PATH}/tables")["data"]

    def latest_week_table(self) -> str:
        return self.tables()["weekTables"][0]["table"]

    def latest_month_table(self) -> str:
        return self.tables()["monthTables"][0]["table"]

    def departments(self, market: Any = 1) -> List[Dict[str, Any]]:
        """GET /v3/api/aba-research/{marketId}/departments —— 类目（department）可选值。"""
        return self._get(f"{ABA_PATH}/{market_id_of(market)}/departments")["data"]

    # ---------- 核心：关键词列表 ----------

    def search(
        self,
        *,
        market: Any = "COM",
        table: Optional[str] = None,
        reverse_type: str = "W",
        q: str = "",
        page: int = 1,
        size: int = 50,
        sort: str = "searchfrequencyrank",
        desc: bool = False,
        rank_growth_type: str = "W1",
        departments: Optional[Sequence[str]] = None,
        keyword_bid_match_type: Optional[str] = "exact",
        movement_market: Any = None,
        include_keywords: Optional[str] = None,
        exclude_keywords: Optional[str] = None,
        min_word_count: Optional[int] = None,
        max_word_count: Optional[int] = None,
        extra: Optional[Mapping[str, Any]] = None,
        **filters: Any,
    ) -> Dict[str, Any]:
        """POST /v3/api/aba-research

        参数命名与前端 `getOptions()` 完全一致（除 sort/desc 会被包进 order 对象）。

        filters 里可直接给接口原生字段，例如：
            minSearches=20000, maxSearches=50000,
            minRankGrowthRate=20,                  # 百分比单位：20 => 20%
            maxMonopolyClickRate=30, minConversionRate=..., maxTitleDensity=...,
            minSearchRank=..., maxSearchRank=..., minCprExact/maxCprExact,
            minImpressions/maxImpressions, minClicks/maxClicks
        """
        if table is None:
            table = self.latest_week_table() if reverse_type.upper() == "W" else self.latest_month_table()

        body: Dict[str, Any] = {
            "rankGrowthType": rank_growth_type,
            "size": int(size),
            "page": int(page),
            "market": public_code_of(market),
            "q": q or "",
            "table": table,
            "reverseType": reverse_type.upper(),
            "departments": list(departments or []),
            "order": {"field": sort, "desc": bool(desc)},
        }
        if keyword_bid_match_type:
            body["keywordBidMatchType"] = keyword_bid_match_type
        if movement_market is not None:
            body["movementMarket"] = movement_market
        if include_keywords:
            body["includeKeywords"] = include_keywords
        if exclude_keywords:
            body["excludeKeywords"] = exclude_keywords
        if min_word_count:
            body["minWordCount"] = int(min_word_count)
        if max_word_count:
            body["maxWordCount"] = int(max_word_count)
        for key, value in filters.items():
            if value is not None and value != "":
                body[key] = value
        if extra:
            body.update({k: v for k, v in extra.items() if v is not None})

        data = self._post(ABA_PATH, body).get("data") or {}
        # 与页面一致：把关键派生字段补到每行上，方便直接落表
        website = MARKETS[market_id_of(market)][0]
        for item in data.get("items") or []:
            item["marketId"] = market_id_of(market)
            item["website"] = website
            item["tableHistoryDate"] = table
            item.setdefault("top3ClickRate", sum(
                (a.get("clickRate") or 0) for a in (item.get("top3AsinDtoList") or [])
            ))
            item.setdefault("top3sumConversionRate", sum(
                (a.get("conversionRate") or 0) for a in (item.get("top3AsinDtoList") or [])
            ))
        data["_request"] = body
        return data

    def iter_search(self, *, max_items: Optional[int] = None, page_size: int = 50, **kwargs) -> Iterator[Dict[str, Any]]:
        """按页迭代关键词，逐个 yield item。kwargs 同 search()。"""
        page = int(kwargs.pop("page", 1))
        kwargs.pop("size", None)  # 由 page_size 控制
        yielded = 0
        while True:
            data = self.search(page=page, size=page_size, **kwargs)
            items = data.get("items") or []
            if not items:
                return
            for item in items:
                yield item
                yielded += 1
                if max_items and yielded >= max_items:
                    return
            total = data.get("total") or 0
            if total and page * page_size >= total:
                return
            page += 1

    # ---------- 单关键词趋势 ----------

    def trends(self, *, keyword: str, market: Any = "COM", table: Optional[str] = None,
               interval: str = "w") -> List[Dict[str, Any]]:
        """GET /v3/api/aba-research/trends?interval=w|m&keyword=&table=&market="""
        table = table or (self.latest_week_table() if interval.lower() == "w" else self.latest_month_table())
        return self._get(
            f"{ABA_PATH}/trends",
            {"interval": interval.lower(), "keyword": keyword,
             "table": table, "market": public_code_of(market)},
        )["data"]

    def yearly_trends(self, *, keyword: str, market: Any = "COM", table: Optional[str] = None,
                      interval: str = "w") -> Dict[str, Any]:
        """GET /v3/api/aba-research/yearly-trends —— 按年分组的历史趋势。"""
        table = table or (self.latest_week_table() if interval.lower() == "w" else self.latest_month_table())
        return self._get(
            f"{ABA_PATH}/yearly-trends",
            {"interval": interval.lower(), "keyword": keyword,
             "table": table, "market": public_code_of(market)},
        )["data"]

    def asin_past_position(self, *, asins: Sequence[str], keyword: str, market: Any = "COM",
                           reverse_type: str = "W") -> Dict[str, Any]:
        """POST /v3/api/aba-research/asin/past-position —— 关键词下 ASIN 的历史排名。"""
        return self._post(
            f"{ABA_PATH}/asin/past-position",
            {"asins": list(asins), "keyword": keyword,
             "market": public_code_of(market), "reverseType": reverse_type.upper()},
        )["data"]

    def brand_past_position(self, *, brands: Sequence[str], keyword: str, market: Any = "COM",
                            reverse_type: str = "W") -> Dict[str, Any]:
        """POST /v3/api/aba-research/brand/past-position —— 关键词下品牌的历史排名。"""
        return self._post(
            f"{ABA_PATH}/brand/past-position",
            {"brands": [b for b in brands if b], "keyword": keyword,
             "market": public_code_of(market), "reverseType": reverse_type.upper()},
        )["data"]

    # ---------- 导出 ----------

    def export_async(self, *, body: Mapping[str, Any], export_gk_images: bool = False) -> Dict[str, Any]:
        """POST /v3/api/aba-research/async-export?exportGkImages=false

        会创建异步导出任务（走 /v2/export-log 下载），会消耗账号导出额度。
        body 与列表接口一致，另外可带 keywordList=[勾选的关键词]。
        """
        return self._post(
            f"{ABA_PATH}/async-export?exportGkImages={'true' if export_gk_images else 'false'}",
            body,
        )


# ------------------------- 落表 / 命令行 -------------------------


def flatten(item: Mapping[str, Any], columns: Sequence[tuple] = CSV_COLUMNS) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    for key, label in columns:
        value = item.get(key)
        if isinstance(value, list):
            value = " | ".join(str(v) for v in value)
        if key in ("w1RankGrowthRate", "purchaseRate", "top3ClickRate", "top3sumConversionRate"):
            if isinstance(value, (int, float)):
                value = round(value * 100, 4)
        row[label] = value
    return row


def write_csv(path: str | os.PathLike, items: Iterable[Mapping[str, Any]]) -> int:
    rows = [flatten(i) for i in items]
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=[label for _, label in CSV_COLUMNS])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _client_from_args(args: argparse.Namespace) -> AbaClient:
    if args.cookie:
        return AbaClient(args.cookie, min_interval=args.interval)
    if args.cookie_file:
        return AbaClient.from_cookie_file(args.cookie_file, min_interval=args.interval)
    here = Path(__file__).resolve().parent
    candidates = [
        "cookie.txt",
        "work/cookie.txt",
        "creds/cookie.txt",
        here / "cookie.txt",
        here.parent / "work/cookie.txt",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return AbaClient.from_cookie_file(candidate, min_interval=args.interval)
    return AbaClient.from_env(min_interval=args.interval)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="卖家精灵 ABA 数据研究 API 客户端")
    p.add_argument("--cookie", help="Cookie 字符串")
    p.add_argument("--cookie-file", help="Cookie 文件路径")
    p.add_argument("--interval", type=float, default=0.5, help="请求最小间隔秒数（默认 0.5）")
    p.add_argument("--json", action="store_true", help="打印原始 JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tables", help="可用周表 / 月表列表")

    d = sub.add_parser("depts", help="类目枚举")
    d.add_argument("--market-id", default="1")

    def add_query_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--market", default="COM", help="COM/JP/UK/DE/FR/IT/ES/CA/IN 或 marketId")
        sp.add_argument("--table", default=None, help="ara_YYYYMMDD（周）或 ara_YYYYMM（月），默认最新")
        sp.add_argument("--reverse-type", default="W", choices=["W", "M", "w", "m"])
        sp.add_argument("--q", default="", help="关键词（模糊匹配）")
        sp.add_argument("--size", type=int, default=50)
        sp.add_argument("--page", type=int, default=1)
        sp.add_argument("--sort", default="searchfrequencyrank", choices=SORT_FIELDS)
        sp.add_argument("--desc", action="store_true")
        sp.add_argument("--rank-growth-type", default="W1", choices=RANK_GROWTH_TYPES)
        sp.add_argument("--rank-growth-value", type=float, default=None)
        sp.add_argument("--departments", nargs="*", default=[], help="类目 code，如 kitchen pets")
        sp.add_argument("--bid-match-type", default="exact", choices=BID_MATCH_TYPES)
        sp.add_argument("--include-keywords", default=None)
        sp.add_argument("--exclude-keywords", default=None)
        sp.add_argument("--min-word-count", type=int, default=None)
        sp.add_argument("--max-word-count", type=int, default=None)
        sp.add_argument("--movement-market", action="store_true", help="飙升市场口径")
        sp.add_argument("--min-searches", type=float, default=None)
        sp.add_argument("--max-searches", type=float, default=None)
        sp.add_argument("--min-search-rank", type=float, default=None)
        sp.add_argument("--max-search-rank", type=float, default=None)
        sp.add_argument("--min-rank-growth-rate", type=float, default=None, help="排名增长率下限，百分比")
        sp.add_argument("--max-rank-growth-rate", type=float, default=None)
        sp.add_argument("--max-monopoly-click-rate", type=float, default=None)
        sp.add_argument("--max-conversion-rate", type=float, default=None)
        sp.add_argument("--min-title-density", type=float, default=None)
        sp.add_argument("--max-title-density", type=float, default=None)
        sp.add_argument("--min-spr", type=float, default=None)
        sp.add_argument("--max-spr", type=float, default=None)

    s = sub.add_parser("search", help="查询一页关键词")
    add_query_flags(s)

    dmp = sub.add_parser("dump", help="翻页抓取并落地 CSV")
    add_query_flags(dmp)
    dmp.add_argument("--max-items", type=int, default=500)
    dmp.add_argument("--out", required=True)

    t = sub.add_parser("trends", help="单关键词历史趋势")
    t.add_argument("--keyword", required=True)
    t.add_argument("--market", default="COM")
    t.add_argument("--table", default=None)
    t.add_argument("--mode", default="w", choices=["w", "m"], help="interval")
    t.add_argument("--yearly", action="store_true")

    return p


def search_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    extra = {
        "minSearches": args.min_searches,
        "maxSearches": args.max_searches,
        "minSearchRank": args.min_search_rank,
        "maxSearchRank": args.max_search_rank,
        "minRankGrowthRate": args.min_rank_growth_rate,
        "maxRankGrowthRate": args.max_rank_growth_rate,
        "maxMonopolyClickRate": args.max_monopoly_click_rate,
        "maxConversionRate": args.max_conversion_rate,
        "minTitleDensity": args.min_title_density,
        "maxTitleDensity": args.max_title_density,
        "minSPR": args.min_spr,
        "maxSPR": args.max_spr,
    }
    if args.rank_growth_value is not None:
        extra["rankGrowthValue"] = args.rank_growth_value
    return {
        "market": args.market,
        "table": args.table,
        "reverse_type": args.reverse_type.upper(),
        "q": args.q,
        "size": args.size,
        "page": args.page,
        "sort": args.sort,
        "desc": args.desc,
        "rank_growth_type": args.rank_growth_type,
        "departments": args.departments,
        "keyword_bid_match_type": args.bid_match_type,
        "movement_market": True if args.movement_market else None,
        "include_keywords": args.include_keywords,
        "exclude_keywords": args.exclude_keywords,
        "min_word_count": args.min_word_count,
        "max_word_count": args.max_word_count,
        "extra": {k: v for k, v in extra.items() if v is not None},
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    aba = _client_from_args(args)

    if args.cmd == "tables":
        data = aba.tables()
        print("最新周表:", data["weekTables"][0]["table"], f"(共 {len(data['weekTables'])} 期)")
        print("最新月表:", data["monthTables"][0]["table"], f"(共 {len(data['monthTables'])} 期)")
        print("站点可用起始:", json.dumps(data["restrictions"], ensure_ascii=False))
        return 0

    if args.cmd == "depts":
        for row in aba.departments(args.market_id):
            print(f"{row['code']:<16} {row['label']:<36} {row.get('translation') or ''}")
        return 0

    if args.cmd == "search":
        data = aba.search(**search_kwargs(args))
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"total={data.get('total')} pages={data.get('pages')} "
                  f"items={len(data.get('items') or [])} took={data.get('took')}ms")
            for row in (data.get("items") or [])[: args.size]:
                print(f"{row['keyword'][:48]:<50} searches={row['searches']:<10} "
                      f"rank={row['searchRank']:<9} growth={row.get('w1RankGrowthRate')}")
        return 0

    if args.cmd == "dump":
        items = aba.iter_search(max_items=args.max_items, page_size=min(args.size, 1000),
                                **search_kwargs(args))
        n = write_csv(args.out, items)
        print(f"已写入 {args.out}（{n} 行）")
        return 0

    if args.cmd == "trends":
        if args.yearly:
            data = aba.yearly_trends(keyword=args.keyword, market=args.market,
                                     table=args.table, interval=args.mode)
        else:
            data = aba.trends(keyword=args.keyword, market=args.market,
                              table=args.table, interval=args.mode)
        print(json.dumps(data, ensure_ascii=False)[:4000])
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
