# -*- coding: utf-8 -*-
"""SQLite 连接 / 建表迁移 / Cookie 读取 —— 抓取、构建、看板服务共用。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Dict, List

import config


_SCHEMA_READY = {"done": False}


def connect() -> sqlite3.Connection:
    """打开数据库并保证表结构是最新的（老库自动补列）。"""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    # 建表 / 补列只在每个进程首次连接时做一次，避免看板每个请求都跑一遍 DDL
    if not _SCHEMA_READY["done"]:
        conn.executescript((config.ROOT / "db_schema.sql").read_text(encoding="utf-8"))
        migrate(conn)
        _SCHEMA_READY["done"] = True
    return conn


def columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def migrate(conn: sqlite3.Connection) -> List[str]:
    """给老库补上新增的列/索引，返回本次实际执行的语句（便于日志）。"""
    done: List[str] = []
    if "keyword" in {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        cols = columns(conn, "keyword")
        if "gk_asins" not in cols:
            conn.execute("ALTER TABLE keyword ADD COLUMN gk_asins TEXT")
            done.append("ALTER TABLE keyword ADD COLUMN gk_asins TEXT")
    conn.commit()
    return done


def load_cookie() -> str:
    """Cookie 读取优先级：config.COOKIE > 环境变量 > cookie.txt 等候选路径。"""
    inline = (getattr(config, "COOKIE", "") or "").strip()
    if inline:
        return inline
    env = os.environ.get("SELLERSPRITE_COOKIE", "").strip()
    if env:
        return env
    for path in config.COOKIE_CANDIDATES:
        if Path(path).exists():
            text = Path(path).read_text(encoding="utf-8").strip()
            if text:
                return text
    raise SystemExit(
        "未找到 Cookie。请把浏览器登录后的 Cookie 写入 "
        f"{config.COOKIE_CANDIDATES[0]} 或设置环境变量 SELLERSPRITE_COOKIE"
    )


def cookie_status() -> Dict[str, object]:
    """给看板服务用：Cookie 是否存在/多久没更新（不校验有效性）。"""
    path = Path(config.COOKIE_CANDIDATES[0])
    if not path.exists():
        return {"exists": False, "path": str(path), "mtime": None}
    return {
        "exists": True,
        "path": str(path),
        "mtime": path.stat().st_mtime,
        "size": path.stat().st_size,
    }
