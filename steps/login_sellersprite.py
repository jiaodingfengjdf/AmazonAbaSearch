# -*- coding: utf-8 -*-
"""卖家精灵自动登录：用 Chrome 打开官网 → 填账号密码 → 登录 → 抓 Cookie 写入 cookie.txt。

触发流程时自动执行，用来解决 Sprite-X-Token 24 小时过期的问题。
登录成功后会用 ABA 接口验证：游客口径只会返回 20 条，登录态会返回请求的条数。
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from steps.logger import log

ROOT = Path(__file__).resolve().parent.parent
HOME_URL = "https://www.sellersprite.com/"
LOGIN_URL = "https://www.sellersprite.com/cn/w/user/login"
LOG_DIR = ROOT / "logs"


def cookie_header(cookies) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def verify_cookie(cookie: str, want_items: int = 50) -> Tuple[bool, str]:
    """调 ABA 接口验证登录态：游客固定 20 条，登录态按请求条数返回。"""
    client = None
    try:
        from aba_client import AbaApiError, AbaClient

        client = AbaClient(cookie, min_interval=0)
        data = client.search(
            market="COM", size=want_items, sort="searches", desc=True
        )
    except AbaApiError as exc:
        return False, f"{exc.code}: {exc.message}"
    except Exception as exc:  # 网络等异常
        return False, str(exc)
    finally:
        if client is not None:
            client.session.close()
    got = len(data.get("items") or [])
    if got >= want_items:
        return True, f"登录态有效（返回 {got} 条）"
    return False, f"疑似游客口径（只返回 {got} 条）"


def login(
    account: str,
    password: str,
    *,
    headless: bool = False,
    browser_channel: str = "chrome",
    timeout_ms: int = 45000,
    cookie_path: Optional[Path] = None,
    keep_browser_open: bool = False,
    want_items: int = 50,
) -> Dict[str, Any]:
    """执行登录并返回 {ok, cookie, message}；ok=True 时已写入 cookie.txt。"""
    if not account or not password:
        return {"ok": False, "cookie": "", "message": "config.yaml 未配置 login.account / login.password"}

    from playwright.sync_api import sync_playwright

    cookie_path = Path(cookie_path or (ROOT / "cookie.txt"))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    shot = LOG_DIR / f"login_failed_{datetime.now():%Y%m%d_%H%M%S}.png"

    log("info", f"① 打开 Chrome 登录卖家精灵（账号 {account}）…")
    with sync_playwright() as pw:
        launch_kwargs: Dict[str, Any] = {"headless": headless, "args": ["--start-maximized"]}
        if browser_channel:
            launch_kwargs["channel"] = browser_channel
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(locale="zh-CN", viewport=None)
        page = context.new_page()
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            # 点「登录/注册」，点不到就直接进登录页
            try:
                page.click("a.login-btn", timeout=8000)
                log("info", "② 已点击「登录/注册」")
            except Exception:
                log("warn", "② 未找到「登录/注册」按钮，直接打开登录页")
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout_ms)

            # 页面上可能有隐藏的同名表单，这里只取「可见」的那个
            email = page.locator('input[name="email"]:visible').first
            email.wait_for(state="visible", timeout=timeout_ms)
            email.fill(account)
            page.locator('input[type="password"]:visible').first.fill(password)
            log("info", "③ 已填写账号与密码，提交登录…")
            page.locator('button[type="submit"].login-btn:visible').first.click()

            # 等 Sprite-X-Token 落地并验证可用
            deadline = time.time() + 40
            cookie = ""
            last = "等待登录响应…"
            while time.time() < deadline:
                cookies = context.cookies("https://www.sellersprite.com")
                if any(c["name"] == "Sprite-X-Token" for c in cookies):
                    cookie = cookie_header(cookies)
                    ok, why = verify_cookie(cookie, want_items)
                    last = why
                    if ok:
                        cookie_path.write_text(cookie, encoding="utf-8")
                        log("info", f"④ 登录成功，Cookie 已写入 {cookie_path.name}（{why}）")
                        return {"ok": True, "cookie": cookie, "message": why}
                time.sleep(2)

            page.screenshot(path=str(shot), full_page=True)
            log("error", f"④ 登录未成功：{last}；已截图 {shot}")
            return {"ok": False, "cookie": cookie, "message": last}
        finally:
            if not keep_browser_open:
                context.close()
                browser.close()


def ensure_cookie(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """流程入口调用：先看现有 Cookie 还能不能用，不行就自动登录刷新。"""
    from steps.fetch_aba import load_cookie  # 复用 Cookie 读取优先级

    try:
        existing = load_cookie()
    except SystemExit:
        existing = ""
    if existing:
        ok, why = verify_cookie(existing)
        if ok:
            log("info", f"现有 Cookie 仍有效（{why}），跳过自动登录")
            return {"ok": True, "cookie": existing, "message": why, "reused": True}
        log("warn", f"现有 Cookie 已失效（{why}），开始自动登录刷新")

    result = login(
        str(cfg.get("account") or ""),
        str(cfg.get("password") or ""),
        headless=bool(cfg.get("headless", False)),
        browser_channel=str(cfg.get("browser_channel", "chrome")),
        timeout_ms=int(cfg.get("login_timeout_ms", 45000)),
        keep_browser_open=bool(cfg.get("keep_browser_open", False)),
    )
    result["reused"] = False
    return result
