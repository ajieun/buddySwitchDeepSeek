#!/usr/bin/env python3
"""BuddyBridge 启动入口：双击或命令行运行此文件即可。"""

import sys

from buddy_bridge.gui import main

if __name__ == "__main__":
    sys.exit(main() or 0)
