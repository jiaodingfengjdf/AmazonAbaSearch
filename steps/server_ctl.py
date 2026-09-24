# -*- coding: utf-8 -*-
"""看板静态服务（默认 0.0.0.0:8766）的启停与健康检查。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from steps.logger import log

ROOT = Path(__file__).resolve().parent.parent


def _health(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def api_ready(port: int) -> bool:
    """确认详情 API 和 Agent API 均已就绪，自动识别需要重启的旧版服务。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3.0) as resp:
            if not json.loads(resp.read().decode("utf-8")).get("ok"):
                return False
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/agent/status", timeout=3.0) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def find_pid(port: int) -> Optional[int]:
    """找出监听该端口的进程 PID：优先 psutil，失败再退回 netstat。

    注意：不能给 netstat 用 text=True —— 中文 Windows 的输出是 GBK，
    在子进程里会抛 UnicodeDecodeError 让 stdout 变成 None（中台环境已踩过）。
    """
    try:
        import psutil

        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                return conn.pid
        return None
    except Exception:
        pass

    try:
        raw = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, timeout=15).stdout or b""
    except Exception:
        return None
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                try:
                    return int(parts[4])
                except ValueError:
                    continue
    return None


def status(host: str, port: int) -> Dict[str, Any]:
    url = f"http://127.0.0.1:{port}/"
    pid = find_pid(port)
    return {"url": url, "pid": pid, "listening": pid is not None, "healthy": _health(url)}


def start(host: str, port: int, wait_seconds: int = 20) -> Dict[str, Any]:
    """后台启动看板服务（脱离中台进程，中台结束后仍可访问）。"""
    st = status(host, port)
    if st["healthy"] and api_ready(port):
        return st
    if st["listening"]:
        why = ("看板是旧版本（没有 /api/status 详情接口）" if st["healthy"]
               else f"端口 {port} 被 PID={st['pid']} 占用但没有响应")
        log("warn", f"{why}，先结束它再重启")
        stop(host, port)

    cmd = [sys.executable, str(ROOT / "serve.py"), "--host", host, "--port", str(port), "--no-open"]
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True,
    )
    log("info", f"已后台启动看板服务 PID={proc.pid}：{host}:{port}")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _health(f"http://127.0.0.1:{port}/"):
            log("info", f"看板已就绪：http://127.0.0.1:{port}/")
            return status(host, port)
        time.sleep(0.5)
    raise RuntimeError(f"看板服务启动后 {wait_seconds}s 内未响应，请检查端口 {port}")


def stop(host: str, port: int) -> bool:
    pid = find_pid(port)
    if not pid:
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True, timeout=15)
    log("info", f"已结束看板服务 PID={pid}")
    time.sleep(0.8)
    return True


def ensure(host: str, port: int, restart: bool = False) -> Dict[str, Any]:
    """确保看板在跑：已在跑则复用，否则启动；restart=True 强制重启。"""
    if restart:
        stop(host, port)
    return start(host, port)
