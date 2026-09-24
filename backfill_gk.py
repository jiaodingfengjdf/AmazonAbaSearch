#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给已经抓过的数据期补 TOP10 商品快照（keyword.gk_asins）。

用途：老库 / 旧数据期没有这一列，看板详情抽屉只能显示 ABA 的 TOP3；
跑一次本脚本即可把「搜索位前 10 个 ASIN」补齐（只 UPDATE gk_asins，不动其它数据）。

    python backfill_gk.py                     # 最新数据期，全部页
    python backfill_gk.py --week ara_20260912 # 指定数据期
    python backfill_gk.py --pages 3           # 只补前 3 页（约 3000 个关键词，最快）
"""
from __future__ import annotations

import argparse
import sys

import config
from steps import asin_detail
from steps.store import connect


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="回填关键词 TOP10 商品快照（gk_asins）")
    parser.add_argument("--market", default=config.MARKET)
    parser.add_argument("--week", default="", help="数据期，如 ara_20260912；留空 = 库里最新一期")
    parser.add_argument("--pages", type=int, default=0, help="只回填前 N 页（默认全部；1 页 ≈ 1000 个关键词）")
    args = parser.parse_args(argv)

    conn = connect()
    try:
        week = args.week.strip()
        if not week:
            row = conn.execute(
                "SELECT MAX(table_date) AS d FROM keyword WHERE market=?", (args.market,)
            ).fetchone()
            week = (row and row["d"]) or ""
        if not week:
            print("库里还没有任何数据期，先跑一次抓取")
            return 1
        print(f"回填 {args.market} / {week} 的 TOP10 商品快照…")
        metrics = asin_detail.backfill_gk(
            conn, market=args.market, table_date=week,
            pages=(args.pages or None))
        print(f"完成：{metrics.get('gk_keywords', 0)} 个关键词补上 TOP10 快照"
              f"（{metrics.get('gk_pages')}/{metrics.get('gk_pages_total')} 页，"
              f"{metrics.get('gk_seconds')}s）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
