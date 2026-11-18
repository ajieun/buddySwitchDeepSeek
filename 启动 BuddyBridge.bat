@echo off
chcp 65001 >nul
title BuddyBridge
cd /d "%~dp0"

:: 检查 Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8+
    echo 下载地址: https://www.python.org/downloads/
    echo 安装时请勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo 正在启动 BuddyBridge...
python run.py
if %errorlevel% neq 0 (
    echo.
    echo [错误] 启动失败，请检查上方错误信息
    pause
)
