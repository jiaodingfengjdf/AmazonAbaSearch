# -*- coding: utf-8 -*-
"""中台 JSON Lines 日志协议（见 docs/流程自动化开发规范.md 第 6 节）。"""

from __future__ import annotations

import json


def log(level: str, msg: str) -> None:
    """level: info | warn | error —— 中台按行解析后推送到 Dashboard。"""
    print(json.dumps({"type": "log", "level": level, "msg": msg}, ensure_ascii=False), flush=True)


def progress(current: int, total: int) -> None:
    """驱动 Dashboard 进度条，current 从 1 开始。"""
    print(json.dumps({"type": "progress", "current": current, "total": total}), flush=True)
