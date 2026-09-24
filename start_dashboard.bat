@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 ABA 关键词趋势看板 (0.0.0.0:8766) ...
start "ABA Dashboard 8766" /min python serve.py --host 0.0.0.0 --port 8766
timeout /t 2 >nul
start http://127.0.0.1:8766/
