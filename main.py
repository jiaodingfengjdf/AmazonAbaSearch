# -*- coding: utf-8 -*-
"""流程：ABA 关键词趋势看板（全类目周更）

中台定时触发（见 process.json）：每周一 06:00 全量抓取，周三 06:00 补抓（应对周表延迟发布）。
执行链路：抓取 22 个大类 → 写入 SQLite → 重建前端数据 → 确保 8766 看板在跑 → 导出当周选品 CSV。
本文件只做编排，具体逻辑在 steps/。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import config as appconfig


@dataclass
class ProcessResult:
    success: bool
    message: str
    artifacts: list[Path]
    metrics: dict


def log(level: str, msg: str) -> None:
    print(json.dumps({"type": "log", "level": level, "msg": msg}, ensure_ascii=False), flush=True)


def progress(current: int, total: int) -> None:
    print(json.dumps({"type": "progress", "current": current, "total": total}), flush=True)


def apply_overrides(cfg: dict) -> None:
    """把 config.yaml 的键覆盖到 config.py 常量（key.upper() 即常量名）。"""
    # 中台会把其它流程的默认配置一起传进来（紫鸟/店铺等），这些与本次无关，直接跳过
    runtime_only = {
        "force_full", "table_date", "serve", "restart_server", "export_csv",
        "login",
        "stores", "store_platform", "stores_local", "stores_crossborder", "browser_host",
        "ziniu_host", "ziniu_exe_path", "ziniu_socket_port", "ziniu_cdp_agent_port",
        "ziniu_remote_start", "ziniu_login", "ziniu_ipc_type",
    }
    unknown: list[str] = []
    for key, value in (cfg or {}).items():
        name = str(key).upper()
        if str(key).lower() in runtime_only:
            continue
        if not hasattr(appconfig, name):
            unknown.append(str(key))
            continue
        setattr(appconfig, name, value)
    if unknown:
        log("warn", f"配置里有 {len(unknown)} 个未知项已忽略：{', '.join(unknown)}")
    log("info", "生效口径：站点=%s 表=%s 搜索量≥%s 增长口径=%s 类目=%s" % (
        appconfig.MARKET, appconfig.REVERSE_TYPE, appconfig.MIN_SEARCHES,
        appconfig.RANK_GROWTH_TYPE, "全部" if not appconfig.DEPARTMENTS else appconfig.DEPARTMENTS))


def run(config: dict) -> ProcessResult:
    sys.stdout.reconfigure(line_buffering=True)
    apply_overrides(config)

    auto_login = bool(config.get("auto_login", getattr(appconfig, "AUTO_LOGIN", True)))
    login_cfg = dict(config.get("login") or {})
    serve = bool(config.get("serve", True))
    force_full = bool(config.get("force_full", False))
    restart_server = bool(config.get("restart_server", False))
    table_date = (config.get("table_date") or "").strip() or None
    export_csv = bool(config.get("export_csv", True))

    artifacts: list[Path] = []
    metrics: dict = {}

    # ── 第 0 步：登录态（Sprite-X-Token 24 小时过期，过期就用 Chrome 自动登录刷新）──
    if auto_login:
        log("info", "=== 0/5 检查卖家精灵登录态 ===")
        try:
            from steps.login_sellersprite import ensure_cookie

            login_res = ensure_cookie(login_cfg)
            metrics["login_reused"] = bool(login_res.get("reused"))
            metrics["login_ok"] = bool(login_res.get("ok"))
            if not login_res.get("ok"):
                msg = f"卖家精灵登录失败：{login_res.get('message')}"
                log("error", msg)
                return ProcessResult(False, msg, artifacts, metrics)
        except Exception as exc:
            log("error", f"自动登录异常：{exc}")
            return ProcessResult(False, f"自动登录异常：{exc}", artifacts, metrics)
    else:
        log("info", "=== 0/5 跳过自动登录（config: auto_login=false），使用现有 cookie.txt ===")

    log("info", "=== 1/5 抓取 ABA 全类目数据（搜索量≥%s，增长量口径 %s）===" % (
        appconfig.MIN_SEARCHES, appconfig.RANK_GROWTH_TYPE))
    try:
        from steps.fetch_aba import run as fetch_run
        metrics.update(fetch_run(reset=force_full, table_date=table_date, login_cfg=login_cfg))
        log("info", "抓取完成：%(keywords)s 个关键词 / %(trend_points)s 条趋势点 / %(departments)s 个类目 / 数据期 %(table_date)s" % metrics)
        log("info", "本轮写入 %(rows_written)s 行，耗时 %(fetch_seconds)s 秒（已存在的数据期会自动跳过）" % metrics)
    except Exception as exc:
        log("error", f"抓取阶段失败：{exc}")
        return ProcessResult(False, f"抓取失败：{exc}", artifacts, metrics)

    log("info", "=== 2/5 预取详情页 ASIN 数据（搜索量前 %s 个关键词）===" % appconfig.ASIN_PREFETCH_TOP_N)
    try:
        from steps import asin_detail
        from steps.fetch_aba import connect as open_db

        top_n = int(config.get("asin_prefetch_top_n", getattr(appconfig, "ASIN_PREFETCH_TOP_N", 0)) or 0)
        if top_n > 0:
            conn = open_db()
            try:
                asin_metrics = asin_detail.prefetch(
                    conn, market=appconfig.MARKET,
                    table_date=metrics.get("table_date") or table_date or "",
                    top_n=top_n)
            finally:
                conn.close()
            metrics.update(asin_metrics)
            log("info", "ASIN 预取：%(prefetch_keywords)s 个关键词 / %(prefetch_asins)s 条商品档案 / 耗时 %(prefetch_seconds)s 秒" % {
                "prefetch_keywords": asin_metrics.get("prefetch_keywords", 0),
                "prefetch_asins": asin_metrics.get("prefetch_asins", 0),
                "prefetch_seconds": asin_metrics.get("prefetch_seconds", 0),
            })
        else:
            log("info", "ASIN 预取已关闭（asin_prefetch_top_n=0），详情页完全按需实时抓取")
    except Exception as exc:
        # 预取只是「让详情页开箱即快」，失败不影响主流程（详情页仍会按需实时抓）
        log("warn", f"ASIN 预取失败（不影响看板）：{exc}")

    log("info", "=== 3/5 重建看板数据（KPI / 榜单 / 趋势分片）===")
    try:
        from steps.build_dashboard import run as build_run
        build_metrics = build_run(table_date)
        metrics.update({f"build_{k}": v for k, v in build_metrics.items()})
        log("info", "构建完成：%(keywords)s 个关键词入榜 / %(departments)s 个类目 / 趋势分片 %(trend_shards)s 个 / 前端数据 %(web_data_mb)sMB" % build_metrics)
    except Exception as exc:
        log("error", f"构建阶段失败：{exc}")
        return ProcessResult(False, f"构建失败：{exc}", artifacts, metrics)
    progress(1, 1)

    artifacts.append(appconfig.DB_PATH)
    artifacts.append(appconfig.WEB_DATA / "summary.json")

    if serve:
        log("info", "=== 4/5 检查看板服务（%s:%s）===" % (appconfig.HOST, appconfig.PORT))
        try:
            from steps.server_ctl import ensure
            st = ensure(appconfig.HOST, appconfig.PORT, restart=restart_server)
            metrics["dashboard_url"] = st["url"]
            metrics["dashboard_pid"] = st["pid"]
            log("info", f"看板地址：{st['url']}（局域网 http://<本机IP>:{appconfig.PORT}/）")
        except Exception as exc:
            log("error", f"看板服务启动失败：{exc}")
            return ProcessResult(False, f"数据已更新，但看板服务启动失败：{exc}", artifacts, metrics)
    else:
        log("info", "=== 4/5 跳过看板服务（config: serve=false）===")

    if export_csv:
        try:
            out_dir = Path(config.get("output_dir") or appconfig.OUTPUT_DIR)
            out_dir.mkdir(parents=True, exist_ok=True)
            csv_path = out_dir / f"ABA选品_{appconfig.MARKET}_{metrics.get('table_date', 'latest')}.csv"
            from steps.build_dashboard import export_top_csv
            rows = export_top_csv(csv_path)
            artifacts.append(csv_path)
            metrics["csv_rows"] = rows
            log("info", f"已导出选品清单：{csv_path}（{rows} 行）")
        except Exception as exc:
            log("warn", f"导出 CSV 失败（不影响主流程）：{exc}")

    msg = ("ABA 全类目数据已更新：%(keywords)s 个关键词 / %(departments)s 个类目 / 数据期 %(table_date)s，"
           "看板 %(dashboard_url)s") % {
        "keywords": metrics.get("keywords", 0),
        "departments": metrics.get("departments", 0),
        "table_date": metrics.get("table_date"),
        "dashboard_url": metrics.get("dashboard_url", "-"),
    }
    log("info", msg)
    return ProcessResult(True, msg, artifacts, metrics)
