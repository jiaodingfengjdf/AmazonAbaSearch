#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段一：抓取 ABA 关键词数据并写入 SQLite（支持断点续传）。

策略：
  1. 先取最新周表 + 全部大类；
  2. 先跑一次「不限类目」全量（minSearches=5000），再逐个类目各跑一次；
  3. 每页抓完立刻写库并记 fetch_log，中断后重跑会自动跳过已完成分页。

用法：
    python etl_fetch.py                # 全量抓取（可重复执行，自动续跑）
    python etl_fetch.py --reset        # 清空抓取记录重新抓
    python etl_fetch.py --only kitchen pets
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import config
from aba_client import AbaClient, AbaApiError
from steps.asin_detail import compact_gk
from steps.store import connect as _store_connect, load_cookie as _store_load_cookie


# ----------------------------- 基础工具 -----------------------------


from steps.logger import log as jlog, progress as jprogress


def log(level: str, msg: str) -> None:
    """输出中台可解析的 JSON Lines 日志（level: info | warn | error）。"""
    jlog(level, msg)


def progress(current: int, total: int) -> None:
    jprogress(current, total)


def connect() -> sqlite3.Connection:
    """打开数据库（建表 + 老库自动补列，见 steps/store.py）。"""
    return _store_connect()


def load_cookie() -> str:
    return _store_load_cookie()


def g(item: Dict[str, Any], *names: str) -> Any:
    """按多个候选字段名取值（接口偶有命名差异）。"""
    for name in names:
        if name in item:
            return item[name]
    return None


UPSERT_KEYWORD = """
INSERT INTO keyword (
    keyword, market, table_date, station, keyword_cn, keyword_jp,
    searches, clicks, impressions, purchase_rate, purchases, products,
    search_rank, search_rank_growth_val, search_rank_growth_rate,
    w1_search_rank, w1_rank_growth_val, w1_rank_growth_rate,
    w4_search_rank, w4_rank_growth_val, w4_rank_growth_rate,
    w12_search_rank, w12_rank_growth_val, w12_rank_growth_rate,
    click_share_rate, cvs_share_rate, title_density, spr,
    bid, bid_min, bid_max, exact_ppc, phrase_ppc, broad_ppc,
    ad_products_1, ad_products_7, ad_products_30,
    top3_brands, top3_asins, gk_asins, updated_at
) VALUES (
    :keyword, :market, :table_date, :station, :keyword_cn, :keyword_jp,
    :searches, :clicks, :impressions, :purchase_rate, :purchases, :products,
    :search_rank, :search_rank_growth_val, :search_rank_growth_rate,
    :w1_search_rank, :w1_rank_growth_val, :w1_rank_growth_rate,
    :w4_search_rank, :w4_rank_growth_val, :w4_rank_growth_rate,
    :w12_search_rank, :w12_rank_growth_val, :w12_rank_growth_rate,
    :click_share_rate, :cvs_share_rate, :title_density, :spr,
    :bid, :bid_min, :bid_max, :exact_ppc, :phrase_ppc, :broad_ppc,
    :ad_products_1, :ad_products_7, :ad_products_30,
    :top3_brands, :top3_asins, :gk_asins, :updated_at
)
ON CONFLICT(keyword, market, table_date) DO UPDATE SET
    station=excluded.station, keyword_cn=excluded.keyword_cn, keyword_jp=excluded.keyword_jp,
    searches=excluded.searches, clicks=excluded.clicks, impressions=excluded.impressions,
    purchase_rate=excluded.purchase_rate, purchases=excluded.purchases, products=excluded.products,
    search_rank=excluded.search_rank,
    search_rank_growth_val=excluded.search_rank_growth_val,
    search_rank_growth_rate=excluded.search_rank_growth_rate,
    w1_search_rank=excluded.w1_search_rank,
    w1_rank_growth_val=excluded.w1_rank_growth_val,
    w1_rank_growth_rate=excluded.w1_rank_growth_rate,
    w4_search_rank=excluded.w4_search_rank,
    w4_rank_growth_val=excluded.w4_rank_growth_val,
    w4_rank_growth_rate=excluded.w4_rank_growth_rate,
    w12_search_rank=excluded.w12_search_rank,
    w12_rank_growth_val=excluded.w12_rank_growth_val,
    w12_rank_growth_rate=excluded.w12_rank_growth_rate,
    click_share_rate=excluded.click_share_rate, cvs_share_rate=excluded.cvs_share_rate,
    title_density=excluded.title_density, spr=excluded.spr,
    bid=excluded.bid, bid_min=excluded.bid_min, bid_max=excluded.bid_max,
    exact_ppc=excluded.exact_ppc, phrase_ppc=excluded.phrase_ppc, broad_ppc=excluded.broad_ppc,
    ad_products_1=excluded.ad_products_1, ad_products_7=excluded.ad_products_7,
    ad_products_30=excluded.ad_products_30,
    top3_brands=excluded.top3_brands, top3_asins=excluded.top3_asins,
    gk_asins=COALESCE(excluded.gk_asins, keyword.gk_asins),
    updated_at=excluded.updated_at
"""


def row_from_item(item: Dict[str, Any], market: str, table_date: str, now: str) -> Dict[str, Any]:
    top3_asins = [
        {
            "asin": a.get("asin"),
            "imageUrl": a.get("imageUrl"),
            "clickRate": a.get("clickRate"),
            "conversionRate": a.get("conversionRate"),
        }
        for a in (item.get("top3AsinDtoList") or [])
    ]
    # gkDatas：该关键词搜索位前 10 的商品快照（列表接口免费带的字段，用来把详情页从 TOP3 扩到 TOP10）
    gk_asins = compact_gk(item)
    return {
        "keyword": item.get("keyword"),
        "market": market,
        "table_date": table_date,
        "station": item.get("station"),
        "keyword_cn": item.get("keywordCn"),
        "keyword_jp": item.get("keywordJp"),
        "searches": item.get("searches"),
        "clicks": item.get("clicks"),
        "impressions": item.get("impressions"),
        "purchase_rate": item.get("purchaseRate"),
        "purchases": item.get("purchases"),
        "products": item.get("products"),
        "search_rank": item.get("searchRank"),
        "search_rank_growth_val": item.get("searchRankGrowthValue"),
        "search_rank_growth_rate": item.get("searchRankGrowthRate"),
        "w1_search_rank": item.get("w1SearchRank"),
        "w1_rank_growth_val": item.get("w1RankGrowthValue"),
        "w1_rank_growth_rate": item.get("w1RankGrowthRate"),
        "w4_search_rank": item.get("w4SearchRank"),
        "w4_rank_growth_val": item.get("w4RankGrowthValue"),
        "w4_rank_growth_rate": item.get("w4RankGrowthRate"),
        "w12_search_rank": item.get("w12SearchRank"),
        "w12_rank_growth_val": item.get("w12RankGrowthValue"),
        "w12_rank_growth_rate": item.get("w12RankGrowthRate"),
        "click_share_rate": item.get("clickShareRate"),
        "cvs_share_rate": item.get("cvsShareRate"),
        "title_density": item.get("titleDensityExact"),
        "spr": item.get("cprExact"),
        "bid": item.get("bid"),
        "bid_min": item.get("bidMin"),
        "bid_max": item.get("bidMax"),
        "exact_ppc": item.get("exactPpc"),
        "phrase_ppc": item.get("phrasePpc"),
        "broad_ppc": item.get("broadPpc"),
        "ad_products_1": item.get("adProducts1"),
        "ad_products_7": item.get("adProducts7"),
        "ad_products_30": item.get("adProducts30"),
        "top3_brands": json.dumps(item.get("top3Brands") or [], ensure_ascii=False),
        "top3_asins": json.dumps(top3_asins, ensure_ascii=False),
        "gk_asins": json.dumps(gk_asins, ensure_ascii=False) if gk_asins else None,
        "updated_at": now,
    }


def save_page(
    conn: sqlite3.Connection,
    items: Sequence[Dict[str, Any]],
    market: str,
    table_date: str,
    dept_label_map: Dict[str, str],
    task_dept: Optional[str],
    primary_dept: Optional[str],
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    rows = [row_from_item(i, market, table_date, now) for i in items]
    conn.executemany(UPSERT_KEYWORD, rows)

    # 类目关联：优先用接口返回的英文类目名映射成 code，其次用本次任务类目兜底
    dept_pairs: List[Tuple[str, str, int]] = []
    seen = set()
    for item in items:
        keyword = item.get("keyword")
        raw = item.get("departments") or []
        codes = [dept_label_map.get(name, name) for name in raw]
        if task_dept and task_dept not in codes:
            codes.append(task_dept)
        for idx, code in enumerate(codes):
            if not code:
                continue
            key = (keyword, code)
            if key in seen:
                continue
            seen.add(key)
            dept_pairs.append((keyword, code, 1 if idx == 0 else 0))
    if dept_pairs:
        conn.executemany(
            "INSERT INTO keyword_department(keyword, market, table_date, department, primary_flag) "
            "VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            [(k, market, table_date, d, f) for k, d, f in dept_pairs],
        )

    trend_rows = []
    for item in items:
        for seq, point in enumerate(item.get("trends") or []):
            trend_rows.append(
                (
                    item.get("keyword"),
                    market,
                    table_date,
                    seq,
                    point.get("label"),
                    point.get("searches"),
                    point.get("rank"),
                    point.get("searchesGrowthRate"),
                    point.get("rankGrowthRate"),
                )
            )
    if trend_rows:
        conn.executemany(
            "INSERT INTO keyword_trend(keyword, market, table_date, seq, label, searches, rank, "
            "searches_growth_rate, rank_growth_rate) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(keyword, market, table_date, seq) DO UPDATE SET "
            "label=excluded.label, searches=excluded.searches, rank=excluded.rank, "
            "searches_growth_rate=excluded.searches_growth_rate, "
            "rank_growth_rate=excluded.rank_growth_rate",
            trend_rows,
        )
    return len(rows)


def relogin_client(box: dict, login_cfg: Optional[dict]) -> bool:
    """会话失效时用 Chrome 重新登录，成功则替换 box 里的客户端。"""
    cfg = login_cfg or {}
    if not (cfg.get("account") and cfg.get("password")):
        log("error", "登录态失效，但 config.yaml 未配置 login.account / login.password，无法自动重登")
        return False
    try:
        from steps.login_sellersprite import login

        log("warn", "会话已失效，正在用 Chrome 重新登录卖家精灵…")
        res = login(
            str(cfg.get("account")), str(cfg.get("password")),
            headless=bool(cfg.get("headless", False)),
            browser_channel=str(cfg.get("browser_channel", "chrome")),
            timeout_ms=int(cfg.get("login_timeout_ms", 45000)),
            keep_browser_open=bool(cfg.get("keep_browser_open", False)),
        )
        if not res.get("ok"):
            log("error", f"重新登录失败：{res.get('message')}")
            return False
        box["client"] = AbaClient(res["cookie"], min_interval=config.MIN_INTERVAL)
        SESSION_LOST["flag"] = False
        log("info", "重新登录成功，继续抓取")
        return True
    except Exception as exc:
        log("error", f"重新登录异常：{exc}")
        return False


def fetch_task(
    box: dict,
    conn: sqlite3.Connection,
    task: str,
    departments: Optional[Sequence[str]],
    market: str,
    table_date: str,
    dept_label_map: Dict[str, str],
    reset: bool = False,
    login_cfg: Optional[dict] = None,
) -> int:
    done = {
        r["page"]
        for r in conn.execute(
            "SELECT page FROM fetch_log WHERE task=? AND market=? AND table_date=?",
            (task, market, table_date),
        )
    }
    row = conn.execute(
        "SELECT MAX(total) t FROM fetch_log WHERE task=? AND market=? AND table_date=?",
        (task, market, table_date),
    ).fetchone()
    known_total = (row["t"] if row else None) or 0
    pages = (known_total + config.PAGE_SIZE - 1) // config.PAGE_SIZE if known_total else None

    page = 1
    relogins_left = 3
    total_saved = 0
    total = known_total
    if pages and all(p in done for p in range(1, pages + 1)):
        log("info", f"  {task:<18} 已完成（{pages} 页），跳过")
        return 0
    while True:
        if page in done and (pages is None or page <= pages):
            page += 1
            if pages and page > pages:
                break
            continue
        aba = box["client"]
        try:
            data = aba.search(
                market=market,
                table=table_date,
                reverse_type=config.REVERSE_TYPE,
                rank_growth_type=config.RANK_GROWTH_TYPE,
                page=page,
                size=config.PAGE_SIZE,
                sort="searchfrequencyrank",
                desc=False,
                departments=list(departments or []),
                extra={"minSearches": config.MIN_SEARCHES},
            )
        except AbaApiError as exc:
            if exc.code in SESSION_CODES:
                SESSION_LOST["flag"] = True
                if relogins_left > 0 and relogin_client(box):
                    relogins_left -= 1
                    continue          # 重新登录后重试本页，不推进页码
                log("error", f"  ! {task} 第 {page} 页会话失效且重登失败，中止本任务（可稍后续抓）")
                break
            log("warn", f"  ! {task} 第 {page} 页失败: {exc}（跳过，稍后重跑本任务可续抓）")
            break
        items = data.get("items") or []
        total = data.get("total") or 0
        if not items:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_log(task, market, table_date, page, item_count, total, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (task, market, table_date, page, 0, total, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            break
        saved = save_page(conn, items, market, table_date, dept_label_map, task if task != "__ALL__" else None,
                          task if task != "__ALL__" else None)
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log(task, market, table_date, page, item_count, total, fetched_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (task, market, table_date, page, len(items), total, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        total_saved += saved
        pages = (total + config.PAGE_SIZE - 1) // config.PAGE_SIZE if total else page
        log("info", f"  {task:<18} page {page:>4}/{pages:<4} +{len(items):>4} 条 (累计 {total_saved})")
        if page >= pages:
            break
        page += 1
    return total_saved


# 抓取过程中是否遇到登录态失效（Cookie 过期）
SESSION_LOST = {"flag": False}
# 服务端会话失效的错误码（会自动重新登录续抓）
SESSION_CODES = ("ERR_GLOBAL_SESSION_EXPIRED", "ERR_USER_NOT_LOGIN", "ERR_REQUIRE_GUEST_ACCESS")


def run(reset: bool = False, only: Optional[Sequence[str]] = None,
        skip_all: bool = False, table_date: Optional[str] = None,
        login_cfg: Optional[dict] = None) -> Dict[str, Any]:
    """抓取最新周表的全类目 ABA 数据入库（可重复执行，断点续传）。

    返回 metrics 字典，供中台写入执行记录。
    """
    box = {"client": AbaClient(load_cookie(), min_interval=config.MIN_INTERVAL)}
    conn = connect()
    if reset:
        conn.execute("DELETE FROM fetch_log")
        conn.commit()
        log("info", "已清空 fetch_log（本次强制重抓）")

    def _guard(fn, *args, **kwargs):
        """登录态失效时给出可操作的错误信息，而不是裸抛 API 错误。"""
        try:
            return fn(*args, **kwargs)
        except AbaApiError as exc:
            if exc.code in ("ERR_GLOBAL_SESSION_EXPIRED", "ERR_USER_NOT_LOGIN", "ERR_REQUIRE_GUEST_ACCESS"):
                raise RuntimeError(
                    "卖家精灵登录态已失效（Sprite-X-Token 24 小时过期）。"
                    "请在浏览器重新登录 sellersprite.com，复制整串 Cookie 覆盖写入 "
                    f"{config.ROOT / 'cookie.txt'} 后重跑（已抓数据不会丢，断点续传）。"
                ) from exc
            raise

    tables = _guard(box["client"].tables)

    if not table_date:
        # 默认取最新周表；指定 table_date 可回填历史周（如 ara_20260905）
        table_date = tables["weekTables"][0]["table"] if config.REVERSE_TYPE == "W" else tables["monthTables"][0]["table"]
    else:
        weeks = [w["table"] for w in tables["weekTables"]]
        if table_date not in weeks:
            raise ValueError(f"数据期 {table_date} 不在可用周表列表中（最近：{weeks[:3]}）")
    raw_depts = _guard(box["client"].departments, config.MARKET_ID)
    dept_label_map = {d["label"]: d["code"] for d in raw_depts}
    dept_label_map.update({d.get("translation"): d["code"] for d in raw_depts if d.get("translation")})
    dept_codes = [d["code"] for d in raw_depts if d["code"] != "any"]
    if config.DEPARTMENTS:
        dept_codes = [c for c in dept_codes if c in config.DEPARTMENTS]
    if only:
        dept_codes = [c for c in dept_codes if c in set(only)]

    do_all = config.INCLUDE_ALL_PASS and not skip_all
    log("info", f"站点 {config.MARKET} / 数据期 {table_date} / 搜索量≥{config.MIN_SEARCHES} / 增长口径 {config.RANK_GROWTH_TYPE}")
    log("info", f"待抓任务：{'__ALL__ + ' if do_all else ''}{len(dept_codes)} 个大类")

    grand_total = 0
    started = time.time()
    tasks = (["__ALL__"] if do_all else []) + list(dept_codes)
    total_tasks = len(tasks)

    for i, task in enumerate(tasks, 1):
        progress(i, total_tasks)
        label = "不限类目全量" if task == "__ALL__" else f"大类 {task}"
        log("info", f"[{i}/{total_tasks}] 抓取 {label}")
        grand_total += fetch_task(
            box, conn, task, None if task == "__ALL__" else [task],
            config.MARKET, table_date, dept_label_map, login_cfg=login_cfg,
        )
        time.sleep(1.0)   # 任务之间稍作停顿，降低被风控概率

    kw_count = conn.execute(
        "SELECT COUNT(*) c FROM keyword WHERE market=? AND table_date=?", (config.MARKET, table_date)
    ).fetchone()["c"]
    dept_count = conn.execute(
        "SELECT COUNT(DISTINCT department) c FROM keyword_department WHERE market=? AND table_date=?",
        (config.MARKET, table_date),
    ).fetchone()["c"]
    trend_count = conn.execute(
        "SELECT COUNT(*) c FROM keyword_trend WHERE market=? AND table_date=?", (config.MARKET, table_date)
    ).fetchone()["c"]
    conn.execute(
        "INSERT INTO run_log(stage, message, created_at) VALUES (?,?,?)",
        ("fetch", f"table={table_date} keywords={kw_count} depts={dept_count} trends={trend_count}",
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    # 完整性校验：__ALL__ 任务必须抓完，否则视为失败（中台会按 notify_on 发通知）
    row = conn.execute(
        "SELECT COUNT(*) AS pages, MAX(total) AS total FROM fetch_log "
        "WHERE task='__ALL__' AND market=? AND table_date=?",
        (config.MARKET, table_date),
    ).fetchone()
    pages, total = int(row["pages"] or 0), int(row["total"] or 0)
    need = (total + config.PAGE_SIZE - 1) // config.PAGE_SIZE if total else 0
    complete = bool(need) and pages >= need
    conn.close()

    elapsed = time.time() - started
    log("info", f"抓取完成：本轮写入 {grand_total} 行；库中关键词 {kw_count:,} 个 / 趋势点 {trend_count:,} 条 / 类目 {dept_count} 个")
    log("info", f"耗时 {elapsed:.1f}s，数据库 {config.DB_PATH}")
    if not complete:
        reason = "卖家精灵登录态失效（Cookie 过期）" if SESSION_LOST["flag"] else "部分分页抓取失败"
        raise RuntimeError(
            f"{reason}：数据期 {table_date} 只抓了 {pages}/{need} 页（库里 {kw_count:,} 个关键词）。"
            f"更新 {config.ROOT / 'cookie.txt'} 后重跑本流程即可续抓（断点续传，已抓分页自动跳过）。"
        )

    return {
        "table_date": table_date,
        "rows_written": grand_total,
        "pages": pages,
        "pages_needed": need,
        "complete": complete,
        "keywords": kw_count,
        "trend_points": trend_count,
        "departments": dept_count,
        "fetch_seconds": round(elapsed, 1),
        "tasks": total_tasks,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="抓取 ABA 关键词数据入库")
    parser.add_argument("--reset", action="store_true", help="清空 fetch_log 重新抓取")
    parser.add_argument("--only", nargs="*", help="只抓指定类目 code")
    parser.add_argument("--skip-all", action="store_true", help="跳过「不限类目」全量")
    args = parser.parse_args(argv)
    run(reset=args.reset, only=args.only, skip_all=args.skip_all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
