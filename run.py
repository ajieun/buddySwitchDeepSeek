#!/usr/bin/env python3
"""BuddyBridge 启动入口：双击或命令行运行此文件即可。"""

import sys

try:
    from buddy_bridge.gui import main
except ModuleNotFoundError as exc:
    if exc.name == "tkinter":
        print(
            "当前 Python 未包含 tkinter（精简版 Python 常见）。\n"
            "请改用完整版 Python（python.org 安装包默认包含），"
            "或直接运行 dist/BuddyBridge 打包程序。",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    raise

if __name__ == "__main__":
    sys.exit(main() or 0)
