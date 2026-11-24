#!/usr/bin/env python3
"""打包脚本：使用 PyInstaller 把 BuddyBridge 打包成单文件可执行程序。

用法：
    python build.py            # 打包为单文件（默认用 BuddyBridge.spec）
    python build.py --onedir   # 打包为目录（启动更快）
    python build.py --no-install  # 缺少 PyInstaller 时只提示，不自动安装

产物在 dist/ 目录下。

注意：必须用「带 tkinter 的解释器」运行本脚本，否则打出来的程序会一闪就退。
Windows 上建议用系统安装的 Python（本机是 3.15），而不是精简版内置解释器。
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
SPEC = HERE / "BuddyBridge.spec"
DIST = HERE / "dist"
WORK = HERE / "build"


def log(msg: str) -> None:
    print(msg, flush=True)


def check_tkinter() -> bool:
    """tkinter 是 GUI 的硬依赖，缺失的话打出来的 exe 无法启动。"""
    try:
        import tkinter  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        log("=" * 68)
        log("✗ 当前解释器没有可用的 tkinter，无法打包 GUI 程序：")
        log(f"  解释器: {sys.executable}")
        log(f"  错误  : {exc}")
        log("")
        log("请改用系统安装的完整 Python 运行本脚本，例如：")
        log('  "C:\\Users\\<你>\\AppData\\Local\\Programs\\Python\\Python315\\python.exe" build.py')
        log("=" * 68)
        return False
    return True


def check_pyinstaller(auto_install: bool) -> bool:
    """PyInstaller 不在标准库里，缺了就装。"""
    probe = [sys.executable, "-c", "import PyInstaller, sys; "
             "print('PyInstaller', PyInstaller.__version__)"]
    ok = subprocess.run(probe, capture_output=True, text=True)
    if ok.returncode == 0:
        log(f"✓ {ok.stdout.strip()}  (py {sys.version.split()[0]})")
        return True

    if not auto_install:
        log("✗ 未检测到 PyInstaller。请先执行：")
        log(f'  "{sys.executable}" -m pip install --upgrade pyinstaller')
        return False

    log("未检测到 PyInstaller，正在为当前解释器安装……")
    log(f"  {sys.executable} -m pip install --upgrade pyinstaller")
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"],
        cwd=str(HERE),
    )
    if install.returncode != 0:
        log("✗ 安装失败。常见原因：网络不通 / 需要代理 / 权限不足。")
        log(f'  可手动重试："{sys.executable}" -m pip install --upgrade pyinstaller')
        log(f'  或换国内源："{sys.executable}" -m pip install -U pyinstaller '
            "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        return False

    verify = subprocess.run(probe, capture_output=True, text=True)
    if verify.returncode != 0:
        log("✗ 安装后仍无法导入 PyInstaller，请检查是否有多个 Python 混用。")
        log(verify.stderr.strip()[:500])
        return False
    log(f"✓ {verify.stdout.strip()}")
    return True


def main() -> int:
    argv = sys.argv[1:]
    onedir = "--onedir" in argv
    auto_install = "--no-install" not in argv

    log(f"解释器: {sys.executable}")
    log(f"Python : {sys.version.split()[0]}  |  平台: {sys.platform}")

    if not check_tkinter():
        return 2
    if not check_pyinstaller(auto_install):
        return 3

    common = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
    ]

    if onedir or not SPEC.exists():
        # 没有 spec 或显式要求 onedir 时走命令行模式（会生成新的 spec 到 build/ 下）
        cmd = common + [
            "--name", "BuddyBridge",
            "--windowed" if sys.platform == "win32" or sys.platform == "darwin" else "--noconsole",
            "--hidden-import", "tkinter",
            "--hidden-import", "tkinter.ttk",
            "--hidden-import", "tkinter.scrolledtext",
            "--collect-submodules", "buddy_bridge",
            "--specpath", str(WORK),
            "--onedir" if onedir else "--onefile",
            str(HERE / "run.py"),
        ]
        if onedir and SPEC.exists():
            log("提示：--onedir 走命令行模式，本次不使用 BuddyBridge.spec")
    else:
        # 用仓库里的 spec 作为唯一事实来源，便于复现且不污染工作区
        cmd = common + [str(SPEC)]

    log("执行命令:")
    log("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    log("")

    result = subprocess.run(cmd, cwd=str(HERE))
    if result.returncode != 0:
        log("")
        log("✗ 打包失败，返回码 %d" % result.returncode)
        return result.returncode

    if sys.platform == "win32":
        exe = DIST / "BuddyBridge.exe"
    elif sys.platform == "darwin":
        exe = DIST / "BuddyBridge.app"
    else:
        exe = DIST / "BuddyBridge"

    log("")
    if not exe.exists():
        log(f"✗ 打包流程结束但没找到产物: {exe}")
        return 4

    if exe.is_file():
        log(f"✅ 打包完成: {exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        log(f"✅ 打包完成: {exe}")
    log("   双击即可运行（首次启动会解压到临时目录，稍慢几秒）")
    log("   若双击没反应，用 python run.py 在控制台跑一遍看报错")
    return 0


if __name__ == "__main__":
    sys.exit(main())
