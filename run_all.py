# -*- coding: utf-8 -*-
"""本地命令行入口（等价于中台「立即执行」，方便不开中台时手动跑）。

    python run_all.py            # 抓取 + 重建看板 + 确保 8766 在跑
    python run_all.py --build    # 只重建前端数据（离线可用）
    python run_all.py --fetch    # 只抓取入库
    python run_all.py --serve    # 只启动看板（8766）
    python run_all.py --stop     # 停掉看板
    python run_all.py --reset    # 清空断点强制重抓
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

import config


def load_cfg() -> dict:
    path = Path(config.ROOT) / "config.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="只重建前端数据")
    parser.add_argument("--fetch", action="store_true", help="只抓取入库")
    parser.add_argument("--serve", action="store_true", help="只启动看板服务")
    parser.add_argument("--stop", action="store_true", help="停掉看板服务")
    parser.add_argument("--reset", action="store_true", help="抓取前清空断点记录")
    parser.add_argument("--week", help="指定数据期（如 ara_20260905），用于回填/续抓历史周")
    args = parser.parse_args()

    if args.serve or args.stop:
        from steps.server_ctl import ensure, stop
        if args.stop:
            stop(config.HOST, config.PORT)
            print("看板已停止")
        else:
            st = ensure(config.HOST, config.PORT)
            print(f"看板地址：{st['url']}  (局域网 http://<本机IP>:{config.PORT}/)")
        return 0

    if args.build:
        from steps.build_dashboard import run as build_run
        print(build_run(table_date=args.week))
        return 0

    if args.fetch:
        from steps.fetch_aba import run as fetch_run
        print(fetch_run(reset=args.reset, table_date=args.week))
        return 0

    from main import run as process_run
    cfg = load_cfg()
    if args.reset:
        cfg["force_full"] = True
    if args.week:
        cfg["table_date"] = args.week
    result = process_run(cfg)
    print(f"\n[{'OK' if result.success else 'FAIL'}] {result.message}")
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
