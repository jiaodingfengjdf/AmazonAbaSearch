#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板服务：静态文件（带 gzip 压缩）+ 详情页 ASIN 数据接口。

默认监听 0.0.0.0:8766，同局域网其它设备可直接访问 http://<本机IP>:8766/

    python serve.py                     # 0.0.0.0:8766，自动打开浏览器
    python serve.py --port 9000         # 换端口
    python serve.py --host 127.0.0.1    # 只允许本机访问

详情接口（关键词抽屉用，按需实时抓 + 本地 SQLite 缓存）：

    GET /api/asin?kw=<关键词>&week=<ara_YYYYMMDD>   # TOP10 ASIN 商品档案
    GET /api/asin/trend?asin=<ASIN>                 # 月均价/月销量/BSR 趋势
    GET /api/status                                 # Cookie / 缓存状态自检
"""

from __future__ import annotations

import argparse
import gzip
import http.server
import json
import os
import queue
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
from steps import agent, asin_detail, store

HOST = "0.0.0.0"
PORT = 8766
COMPRESSIBLE = {".json", ".js", ".css", ".html", ".svg"}

_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()
_FETCH_GATE = threading.Lock()
_LAST_FETCH = [0.0]
_SESSION = {"until": 0.0, "reason": "", "cookie_mtime": 0.0}
SESSION_COOLDOWN_SECONDS = 600      # 登录态失效后的冷却：10 分钟内不再打卖家精灵
_RECOVERY_LOCK = threading.Lock()
_RECOVERY = {"last_attempt": 0.0, "last_error": ""}
RECOVERY_BACKOFF_SECONDS = 900      # 自动登录失败后至少 15 分钟才重试


def _lock_for(key: str) -> threading.Lock:
    """同一个关键词/ASIN 的并发请求只抓一次。"""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def throttle_fetch() -> None:
    """全局节流：相邻的实时抓取（不论关键词/ASIN）至少间隔 asin_min_interval 秒。"""
    gap = float(getattr(config, "ASIN_MIN_INTERVAL", 0.25) or 0)
    if gap <= 0:
        return
    with _FETCH_GATE:
        wait = gap - (time.time() - _LAST_FETCH[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_FETCH[0] = time.time()


def session_blocked() -> bool:
    """登录态失效后短时间内直接走缓存，避免每次点开都白跑一次请求。

    冷却期内如果 cookie.txt 被刷新过（流程运行或手动 `python refresh_cookie.py`），
    立即恢复实时抓取——不需要重启看板服务。
    """
    if time.time() >= _SESSION["until"]:
        return False
    if current_cookie_mtime() > _SESSION.get("cookie_mtime", 0.0):
        _SESSION["until"] = 0.0
        return False
    return True


def current_cookie_mtime() -> float:
    try:
        return Path(config.COOKIE_CANDIDATES[0]).stat().st_mtime
    except OSError:
        return 0.0


def mark_session_blocked(reason: str) -> None:
    _SESSION["until"] = time.time() + SESSION_COOLDOWN_SECONDS
    _SESSION["reason"] = reason[:200]
    _SESSION["cookie_mtime"] = current_cookie_mtime()


def recover_session() -> bool:
    """串行、限频地自动刷新登录态；成功后本次详情请求可立即重试。"""
    if not bool(getattr(config, "AUTO_LOGIN", True)):
        return False
    with _RECOVERY_LOCK:
        if (getattr(config, "COOKIE", "") or os.environ.get("SELLERSPRITE_COOKIE", "")).strip():
            _RECOVERY["last_error"] = "固定 Cookie 配置覆盖自动登录结果"
            return False
        if current_cookie_mtime() > _SESSION.get("cookie_mtime", 0.0):
            _SESSION["until"] = 0.0
            return True  # 另一线程或每周流程已更新 cookie.txt
        now = time.time()
        if now - _RECOVERY["last_attempt"] < RECOVERY_BACKOFF_SECONDS:
            return False
        _RECOVERY["last_attempt"] = now
        try:
            from refresh_cookie import load_login_cfg
            from steps.login_sellersprite import login

            cfg = load_login_cfg()
            if not cfg.get("account") or not cfg.get("password"):
                _RECOVERY["last_error"] = "未配置自动登录账号"
                return False
            result = login(str(cfg["account"]), str(cfg["password"]),
                           headless=True, browser_channel=str(cfg.get("browser_channel", "chrome")),
                           timeout_ms=int(cfg.get("login_timeout_ms", 45000)),
                           cookie_path=Path(config.COOKIE_CANDIDATES[0]))
            if not result.get("ok"):
                _RECOVERY["last_error"] = "自动登录未成功"
                return False
            _SESSION.update({"until": 0.0, "reason": "", "cookie_mtime": current_cookie_mtime()})
            _RECOVERY["last_error"] = ""
            return True
        except Exception:
            _RECOVERY["last_error"] = "自动登录暂不可用"
            return False


def maintain_session(stop: threading.Event) -> None:
    """看板常驻时定期发现会话失效；只对明确的登录错误触发重登。"""
    while not stop.is_set():
        try:
            if bool(getattr(config, "AUTO_LOGIN", True)):
                from steps.login_sellersprite import verify_cookie
                try:
                    cookie = store.load_cookie()
                except SystemExit:
                    cookie = ""
                ok, why = verify_cookie(cookie) if cookie else (False, "ERR_USER_NOT_LOGIN")
                if not ok and (any(code in why for code in asin_detail.SESSION_CODES) or "游客口径" in why):
                    mark_session_blocked("登录态失效")
                    recover_session()
        except Exception:
            pass  # 网络抖动不应触发重新登录
        stop.wait(1800)


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "AbaDashboard/1.2"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api(parsed)
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path.rstrip("/")
        if route not in ("/api/agent/chat", "/api/agent/cancel"):
            return self.send_json({"ok": False, "error": "未知接口"}, 404)
        origin = self.headers.get("Origin")
        if (origin and urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower()) or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self.send_json({"ok": False, "error": "不接受跨站分析请求"}, 403)
        if self.headers.get_content_type() != "application/json":
            return self.send_json({"ok": False, "error": "请求须为 application/json"}, 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 128 * 1024:
                return self.send_json({"ok": False, "error": "请求为空或超过 128 KB"}, 413)
            self.connection.settimeout(15)
            payload = json.loads(self.rfile.read(length))
            self.connection.settimeout(None)
            if not isinstance(payload, dict):
                raise ValueError("请求必须是对象")
            if route == "/api/agent/cancel":
                return self.send_json({"ok": True, "cancelled": agent.cancel(payload.get("request_id"))})
            run = agent.start(payload)
        except BlockingIOError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 429)
        except (ValueError, UnicodeError, TimeoutError):
            return self.send_json({"ok": False, "error": "请求或 Agent 配置无效，请检查问题长度、数据期与本地配置"}, 400)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                try:
                    event = run.events.get(timeout=8)
                except queue.Empty:
                    event = {"type": "heartbeat"}
                if event is None:
                    break
                self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            agent.cancel(run.id)

    # ------------------------------ 详情数据接口 ------------------------------

    def handle_api(self, parsed) -> None:
        query = parse_qs(parsed.query)
        route = parsed.path.rstrip("/")
        started = time.time()
        try:
            if route == "/api/asin":
                payload = self.api_keyword_asins(query)
            elif route == "/api/asin/trend":
                payload = self.api_asin_trend(query)
            elif route == "/api/status":
                payload = self.api_status()
            elif route == "/api/agent/status":
                payload = agent.status()
            else:
                return self.send_json({"ok": False, "error": f"未知接口 {route}"}, 404)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        payload["tookMs"] = int((time.time() - started) * 1000)
        self.send_json(payload)

    def first(self, query: dict, key: str, default: str = "") -> str:
        values = query.get(key) or []
        return (values[0] if values else default) or default

    def latest_week(self, conn) -> str:
        row = conn.execute(
            "SELECT MAX(table_date) AS d FROM keyword WHERE market=?", (config.MARKET,)
        ).fetchone()
        return (row and row["d"]) or ""

    def api_keyword_asins(self, query: dict) -> dict:
        keyword = self.first(query, "kw").strip()
        if not keyword:
            return {"ok": False, "error": "缺少参数 kw（关键词）"}
        week = self.first(query, "week").strip()
        live = self.first(query, "live", "1") not in ("0", "false", "False")
        live = live and bool(getattr(config, "ASIN_LIVE_FETCH", True))
        conn = store.connect()
        try:
            if not week:
                week = self.latest_week(conn)
            if not week:
                return {"ok": False, "error": "库里还没有任何数据期，请先跑一次流程"}
            with _lock_for(f"kw::{week}::{keyword}"):
                blocked = live and session_blocked()
                if blocked:
                    live = recover_session()
                if live:
                    throttle_fetch()
                payload = asin_detail.keyword_payload(
                    conn, keyword=keyword, market=config.MARKET, table_date=week, live=live)
                if blocked and not live and payload.get("detailMissing"):
                    payload.update({"errorCode": "ERR_USER_NOT_LOGIN",
                                    "error": "自动恢复登录态暂未成功，稍后重试"})
                if payload.get("errorCode") == "ERR_USER_NOT_LOGIN":
                    mark_session_blocked("登录态失效")
                    if recover_session():
                        throttle_fetch()
                        payload = asin_detail.keyword_payload(
                            conn, keyword=keyword, market=config.MARKET, table_date=week, live=True)
                    if payload.get("errorCode") == "ERR_USER_NOT_LOGIN":
                        mark_session_blocked("登录态失效")
            return payload
        finally:
            conn.close()

    def api_asin_trend(self, query: dict) -> dict:
        asin = self.first(query, "asin").strip().upper()
        if not asin:
            return {"ok": False, "error": "缺少参数 asin"}
        live = self.first(query, "live", "1") not in ("0", "false", "False")
        live = live and bool(getattr(config, "ASIN_LIVE_FETCH", True))
        conn = store.connect()
        try:
            with _lock_for(f"asin::{config.MARKET}::{asin}"):
                blocked = live and session_blocked()
                if blocked:
                    live = recover_session()
                if live:
                    throttle_fetch()
                payload = asin_detail.trend_payload(
                    conn, asin=asin, market=config.MARKET, live=live)
                if blocked and not live and not asin_detail.has_usable_trend(payload):
                    payload.update({"errorCode": "ERR_USER_NOT_LOGIN",
                                    "error": "自动恢复登录态暂未成功，稍后重试"})
                if payload.get("errorCode") == "ERR_USER_NOT_LOGIN":
                    mark_session_blocked("登录态失效")
                    if recover_session():
                        throttle_fetch()
                        payload = asin_detail.trend_payload(
                            conn, asin=asin, market=config.MARKET, live=True)
                    if payload.get("errorCode") == "ERR_USER_NOT_LOGIN":
                        mark_session_blocked("登录态失效")
            payload["asin"] = asin
            return payload
        finally:
            conn.close()

    def api_status(self) -> dict:
        session_blocked()          # 顺便刷新一次冷却状态（Cookie 被刷新过会自动解除）
        conn = store.connect()
        try:
            week = self.latest_week(conn)
            stats = {
                "week": week,
                "keywords": conn.execute(
                    "SELECT COUNT(*) c FROM keyword WHERE market=? AND table_date=?",
                    (config.MARKET, week)).fetchone()["c"] if week else 0,
                "keywordsWithGk": conn.execute(
                    "SELECT COUNT(*) c FROM keyword WHERE market=? AND table_date=? "
                    "AND gk_asins IS NOT NULL", (config.MARKET, week)).fetchone()["c"] if week else 0,
                "asinDetailCached": conn.execute(
                    "SELECT COUNT(*) c FROM asin_keyword").fetchone()["c"],
                "asinTrendCached": conn.execute(
                    "SELECT COUNT(*) c FROM asin_trend").fetchone()["c"],
            }
        finally:
            conn.close()
        return {
            "ok": True,
            "market": config.MARKET,
            "liveFetch": bool(getattr(config, "ASIN_LIVE_FETCH", True)),
            "sessionBlockedSeconds": max(0, int(_SESSION["until"] - time.time())),
            "sessionReason": _SESSION["reason"],
            "autoRecovery": bool(getattr(config, "AUTO_LOGIN", True)),
            "autoRecoveryError": _RECOVERY["last_error"],
            "cookie": store.cookie_status(),
            "cache": stats,
        }

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
        if "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024:
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------ 静态文件 ------------------------------

    def serve_static(self, raw_path: str) -> None:
        path = self.translate_path(raw_path)
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        if not os.path.isfile(path):
            self.send_error(404, "Not found")
            return

        ctype = self.guess_type(path)
        with open(path, "rb") as fh:
            body = fh.read()

        headers = {"Content-Type": ctype}
        suffix = Path(path).suffix
        if suffix == ".json":
            headers["Cache-Control"] = "no-store"
        elif suffix in {".html", ".css", ".js"}:
            # 改样式/脚本后刷新即生效，避免拿到旧缓存
            headers["Cache-Control"] = "no-cache"
        else:
            headers["Cache-Control"] = "max-age=600"
        if Path(path).suffix in COMPRESSIBLE and "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024:
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"

        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        if "/data/" not in self.path:  # 数据请求太多，不打印
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def lan_ip() -> str:
    """取本机局域网 IP，用于打印可直接分享的访问地址。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def apply_yaml_config() -> None:
    """看板服务是被中台独立拉起的进程，这里读一遍 config.yaml，保证和流程用同一份口径。"""
    path = Path(config.ROOT) / "config.yaml"
    if not path.exists():
        return
    try:
        import yaml

        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 读取 config.yaml 失败（用 config.py 默认值）：{exc}")
        return
    for key, value in cfg.items():
        name = str(key).upper()
        if hasattr(config, name):
            setattr(config, name, value)


def run(host: str = HOST, port: int = PORT, open_browser: bool = True) -> None:
    apply_yaml_config()
    socketserver.TCPServer.allow_reuse_address = True
    handler = lambda *a, **kw: Handler(*a, directory=str(config.ROOT / "web"), **kw)  # noqa: E731
    with socketserver.ThreadingTCPServer((host, port), handler) as httpd:
        maintenance_stop = threading.Event()
        threading.Thread(target=maintain_session, args=(maintenance_stop,), daemon=True).start()
        local = f"http://127.0.0.1:{port}/"
        print(f"看板已启动：{local}")
        print(f"接口口径：站点 {config.MARKET}(id={config.MARKET_ID}) · "
              f"TOP{config.ASIN_TOP_N} · 实时抓取 {'开' if config.ASIN_LIVE_FETCH else '关'}")
        if host not in ("127.0.0.1", "localhost"):
            print(f"局域网访问：http://{lan_ip()}:{port}/   (监听 {host}:{port}，同网段设备可直接打开)")
        print("Ctrl+C 退出")
        if open_browser:
            import webbrowser

            threading.Timer(0.8, lambda: webbrowser.open(local)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("已停止")
        finally:
            maintenance_stop.set()


def main() -> int:
    parser = argparse.ArgumentParser(description="ABA 看板本地服务")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    run(host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
