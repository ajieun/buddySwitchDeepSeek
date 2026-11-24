#!/usr/bin/env python
"""bb —— 给终端套一个浏览器遥控器。

用法
----
::

    bb                      # 在 PTY 里开一个默认 shell
    bb codebuddy            # 在 PTY 里跑 codebuddy，浏览器里操作
    bb --port 7400 codebuddy
    bb --no-open --no-mirror codebuddy

它做的事
--------
1. 启动一个只监听 127.0.0.1 的 Web 服务；
2. 在**真正的伪终端**里跑你给的命令（不是管道模拟，TUI 正常）；
3. 把输出同时镜像到当前终端，并把画面推给浏览器；
4. 打开浏览器，你在页面上点按钮 = 往 PTY 写字符。

为什么必须用 ``bb`` 启动，而不是接管已经开着的窗口::

    Windows 的 ConPTY 句柄只属于创建它的进程，没有公开 API 让第三个进程
    插进去。tmux 能 attach 是因为它从终端创建那一刻就持有 PTY。
    所以「从创建时持有」是唯一可行的路径，这也正是 Claude Code、Aider
    这些工具的做法。
"""

from __future__ import annotations

import glob
import os
import socket
import sys
import time
import webbrowser
from pathlib import Path
from typing import List, Optional

from buddy_bridge.pty_host import IS_WINDOWS, which_command
from buddy_bridge.translate import Translator
from buddy_bridge.webterm import WebTermServer

DEFAULT_PORT = 7388
DEFAULT_SHELL = ["cmd.exe"] if IS_WINDOWS else ["bash"]

USAGE = f"""bb —— 浏览器遥控终端

用法：
  bb [选项] [命令...]

选项：
  --port N       指定端口（默认 {DEFAULT_PORT}，占用时自动顺延）
  --no-open      不自动打开浏览器
  --no-mirror    不在当前终端镜像输出（纯后台）
  --no-translate 关闭英译中
  --token T      指定访问令牌（非回环监听时自动生成）
  --host H       监听地址（默认 127.0.0.1；改成 0.0.0.0 会强制要令牌）
  -h, --help     显示本帮助

示例：
  bb                        # 默认 shell
  bb codebuddy              # 在伪终端里跑 codebuddy
  bb --port 7400 claude     # 换端口
"""


def parse_args(argv):
    opts = {
        "port": DEFAULT_PORT,
        "open": True,
        "mirror": True,
        "translate": True,
        "token": "",
        "host": "127.0.0.1",
        "command": [],
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE)
            raise SystemExit(0)
        elif a == "--port" and i + 1 < len(argv):
            opts["port"] = int(argv[i + 1])
            i += 2
        elif a == "--host" and i + 1 < len(argv):
            opts["host"] = argv[i + 1]
            i += 2
        elif a == "--token" and i + 1 < len(argv):
            opts["token"] = argv[i + 1]
            i += 2
        elif a == "--no-open":
            opts["open"] = False
            i += 1
        elif a == "--no-mirror":
            opts["mirror"] = False
            i += 1
        elif a == "--no-translate":
            opts["translate"] = False
            i += 1
        elif a.startswith("-") and a != "-":
            print(f"未知选项：{a}\n")
            print(USAGE)
            raise SystemExit(2)
        else:
            opts["command"] = argv[i:]
            break
    return opts


def pick_port(host: str, start: int, attempts: int = 20) -> int:
    for offset in range(attempts):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"从 {start} 起 {attempts} 个端口都被占用")


def _codebuddy_bins() -> List[Path]:
    """列出本机所有 codebuddy 入口（npm 全局 / WorkBuddy 内置 node）。"""
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", ""))
    localapp = Path(os.environ.get("LOCALAPPDATA", ""))
    patterns = [
        str(home / ".workbuddy/binaries/node/versions/*/node_modules/"
             "@tencent-ai/codebuddy-code/bin/codebuddy"),
        str(appdata / "npm/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy"),
        str(localapp / "Programs/nodejs/node_modules/"
            "@tencent-ai/codebuddy-code/bin/codebuddy"),
    ]
    found: List[Path] = []
    for pat in patterns:
        for hit in glob.glob(pat):
            p = Path(hit)
            if p.is_file():
                found.append(p)
    return found


def resolve_command(name: str) -> Optional[List[str]]:
    """把命令名解析成可直接执行的 argv。

    优先 PATH；``codebuddy`` 这类 npm 包常因 bin shim 缺失而不在 PATH，
    这里回退到已知安装位置，直接用 node 执行入口脚本。
    """
    exe = which_command(name)
    if exe:
        return [exe]

    if name.lower() in ("codebuddy", "cbc"):
        for entry in _codebuddy_bins():
            # .../node_modules/@tencent-ai/codebuddy-code/bin/codebuddy
            node = entry.parents[4] / "node.exe"
            if node.is_file():
                return [str(node), str(entry)]
            node_cmd = which_command("node")
            if node_cmd:
                return [node_cmd, str(entry)]
    return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts = parse_args(argv)

    command = opts["command"] or DEFAULT_SHELL
    resolved = resolve_command(command[0])
    if not resolved:
        print(f"[bb] 找不到命令：{command[0]}")
        print("     请确认它已安装并在 PATH 里（Windows 可用 where，类 Unix 用 which）")
        if command[0].lower() == "codebuddy":
            print("     已尝试搜索 npm 全局目录与 WorkBuddy 内置 node，均未找到。")
            print("     可先安装：npm install -g @tencent-ai/codebuddy-code")
        return 2
    command = resolved + command[1:]

    if IS_WINDOWS:
        try:
            import winpty  # noqa: F401
        except ImportError:
            print("[bb] Windows 下需要 pywinpty 提供真伪终端（TUI 才能正常渲染）。")
            print("     请执行：pip install pywinpty")
            return 2

    port = pick_port(opts["host"], opts["port"])

    # 翻译：词典始终可用；配了模型端点才会对长句发请求
    translator = Translator(
        base_url=os.environ.get("BB_TRANSLATE_URL", ""),
        api_key=os.environ.get("BB_TRANSLATE_KEY", ""),
        model=os.environ.get("BB_TRANSLATE_MODEL", ""),
    ) if opts["translate"] else Translator()

    server = WebTermServer(
        host=opts["host"],
        port=port,
        translator=translator,
        token=opts["token"],
        on_log=lambda msg: print(f"[bb] {msg}", flush=True) if not opts["mirror"] else None,
    )

    # 先接管输出：镜像到当前终端（保证"程序确实跑在终端里"）
    mirror = opts["mirror"] and sys.stdout is not None

    ok, info = server.start()
    if not ok:
        print(f"[bb] 启动失败：{info}")
        return 1

    try:
        session = server.hub.create(command, cwd=os.getcwd(), cols=100, rows=30)
    except Exception as exc:  # noqa: BLE001
        print(f"[bb] 启动终端失败：{exc}")
        server.stop()
        return 1

    if mirror:
        # 把 PTY 输出同时写到当前终端，让用户"看得见程序在里面跑"
        original = session.pty.on_output

        def tee(text: str) -> None:
            try:
                sys.stdout.write(text)
                sys.stdout.flush()
            except Exception:
                pass
            if original:
                original(text)

        session.pty.on_output = tee

    url = info if not server.token else f"{info}"
    print()
    print("  ┌──────────────────────────────────────────────────────────")
    print(f"  │  浏览器遥控器： {url}")
    print(f"  │  会话 ID：     {session.id}")
    print(f"  │  运行命令：    {' '.join(command)}")
    print("  │")
    print("  │  交互请在浏览器进行；此处仅镜像显示输出。")
    print("  │  按 Ctrl+C 结束会话并关闭服务。")
    print("  └──────────────────────────────────────────────────────────")
    print()

    if opts["open"]:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        while session.pty.is_alive:
            time.sleep(0.4)
        # 进程退出后再等一会儿，让浏览器把最后的输出画完
        time.sleep(0.8)
    except KeyboardInterrupt:
        print("\n[bb] 收到中断，正在关闭……")
    finally:
        try:
            session.pty.close(force=True)
        except Exception:
            pass
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
