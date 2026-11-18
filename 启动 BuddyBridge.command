#!/usr/bin/env bash
# BuddyBridge 启动脚本 (macOS / Linux)
cd "$(dirname "$0")"

if ! command -v python3 &> /dev/null; then
    echo "[错误] 未检测到 python3，请先安装 Python 3.8+"
    echo "macOS: brew install python3"
    echo "Linux: sudo apt install python3  或  sudo yum install python3"
    exit 1
fi

echo "正在启动 BuddyBridge..."
python3 run.py
