#!/usr/bin/env python3
"""打包脚本：使用 PyInstaller 把 BuddyBridge 打包成单文件可执行程序。

用法：
    python build.py            # 打包为单文件
    python build.py --onedir   # 打包为目录（启动更快）

产物在 dist/ 目录下。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    onedir = "--onedir" in sys.argv
    here = Path(__file__).parent.resolve()
    entry = here / "run.py"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", "BuddyBridge",
        "--windowed" if os.name != "posix" or sys.platform == "darwin" else "--noconsole",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.scrolledtext",
        "--collect-submodules", "buddy_bridge",
        str(entry),
    ]
    if onedir:
        cmd.append("--onedir")
    else:
        cmd.append("--onefile")

    print("执行命令:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(here))
    if result.returncode != 0:
        print("打包失败", file=sys.stderr)
        return result.returncode

    dist = here / "dist"
    print(f"\n✅ 打包完成！产物在: {dist}")
    if sys.platform == "win32":
        exe = dist / "BuddyBridge.exe"
    elif sys.platform == "darwin":
        exe = dist / "BuddyBridge.app"
    else:
        exe = dist / "BuddyBridge"
    print(f"   可执行文件: {exe}")
    print("   双击即可运行（首次启动可能稍慢）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
