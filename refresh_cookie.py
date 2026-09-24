#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动刷新卖家精灵登录态（Cookie），用于看板详情页「实时抓取」失效时立刻恢复。

为什么需要它：
1. `Sprite-X-Token` 24 小时到期；
2. **同一账号在别处登录会顶掉旧会话**——实测「看板 8 小时前抓到的会话」在用户于自己浏览器
   登录卖家精灵后立刻失效（同一 Cookie 连试 3 次都返回 `ERR_GLOBAL_SESSION_EXPIRED`）。

两种恢复方式（看板**无需重启**，每个请求都重新读 cookie.txt）：

    # A. 让看板用「你浏览器里的同一个会话」——最推荐：两边同时在线、互不顶号
    #    浏览器 F12 → Network → 任意 sellersprite 请求 → 复制 Cookie 整串 → Ctrl+C
    python refresh_cookie.py --from-clipboard

    # B. 让看板自己登录一次（会把你浏览器那边的会话顶掉，浏览器需要重新登录）
    python refresh_cookie.py                 # 用 config.yaml 里的账号密码
    python refresh_cookie.py --headless      # 无界面模式（默认可见地打开 Chrome）

    python refresh_cookie.py --force         # B 模式下：即使现有 Cookie 有效也重新登录
    python refresh_cookie.py --from-string "…"   # 直接传 Cookie 字符串（注意会进命令历史）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

import config
from steps.login_sellersprite import login, verify_cookie


def read_clipboard() -> str:
    """读取 Windows 剪贴板文本（浏览器里 Ctrl+C 复制的那串 Cookie）。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        return (proc.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def apply_cookie(text: str) -> dict:
    """校验一段 Cookie 字符串，有效则写入 cookie.txt。"""
    cookie = (text or "").strip().strip('"').strip("'")
    if not cookie:
        return {"ok": False, "message": "Cookie 为空（剪贴板里没有文本？）"}
    if "Sprite-X-Token" not in cookie:
        return {"ok": False, "message": "这段文本里没有 Sprite-X-Token，不像卖家精灵的 Cookie 整串"}
    ok, why = verify_cookie(cookie)
    if not ok:
        return {"ok": False, "message": f"Cookie 校验失败：{why}"}
    path = Path(config.COOKIE_CANDIDATES[0])
    path.write_text(cookie, encoding="utf-8")
    return {"ok": True, "message": f"已写入 {path.name}（{why}）",
            "cookie_file": str(path), "bytes": len(cookie)}


def load_login_cfg() -> dict:
    path = Path(config.ROOT) / "config.yaml"
    if not path.exists():
        return {}
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return dict(cfg.get("login") or {})


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="手动刷新卖家精灵 Cookie")
    parser.add_argument("--force", action="store_true", help="即使现有 Cookie 有效也重新登录")
    parser.add_argument("--headless", action="store_true", help="无界面运行 Chrome")
    parser.add_argument("--from-clipboard", action="store_true",
                        help="从剪贴板读取整串 Cookie（推荐：与浏览器同一会话，互不顶号）")
    parser.add_argument("--from-string", default="", help="直接传入 Cookie 字符串")
    args = parser.parse_args(argv)

    # A 模式：直接用浏览器里的 Cookie，避免两边互相顶号
    if args.from_clipboard or args.from_string:
        text = args.from_string or read_clipboard()
        result = apply_cookie(text)
        result["next"] = ("看板详情页的实时抓取会自动恢复（无需重启服务），刷新页面重新展开即可"
                          if result.get("ok") else "请重新复制整串 Cookie（F12 → Network → 任意请求 → Request Headers → Cookie）")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    cfg = load_login_cfg()
    account = str(cfg.get("account") or "")
    password = str(cfg.get("password") or "")
    if not (account and password):
        print("config.yaml 的 login.account / login.password 未配置，无法自动登录。", file=sys.stderr)
        return 2

    cookie_path = Path(config.COOKIE_CANDIDATES[0])
    if cookie_path.exists() and not args.force:
        ok, why = verify_cookie(cookie_path.read_text(encoding="utf-8").strip())
        if ok:
            print(json.dumps({"ok": True, "refreshed": False, "message": f"现有 Cookie 仍有效（{why}）"},
                             ensure_ascii=False))
            return 0
        print(f"现有 Cookie 已失效（{why}），开始重新登录…")

    result = login(
        account, password,
        headless=args.headless or bool(cfg.get("headless", False)),
        browser_channel=str(cfg.get("browser_channel", "chrome")),
        timeout_ms=int(cfg.get("login_timeout_ms", 45000)),
        keep_browser_open=bool(cfg.get("keep_browser_open", False)),
        cookie_path=cookie_path,
    )
    out = {
        "ok": bool(result.get("ok")),
        "refreshed": bool(result.get("ok")),
        "cookie_file": str(cookie_path),
        "message": result.get("message"),
        "next": ("看板详情页的实时抓取会自动恢复（无需重启服务），直接刷新页面再点「价格趋势」即可"
                 if result.get("ok") else "登录失败，请查看 logs/login_failed_*.png 截图后重试"),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
