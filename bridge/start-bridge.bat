@echo off
chcp 65001 >nul
title school-radar 微信推送桥
cd /d "%~dp0"
echo 启动推送桥（每 15 分钟抓取+推送，Ctrl+C 退出）…
python bridge.py
pause
