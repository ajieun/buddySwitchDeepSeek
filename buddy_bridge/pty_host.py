"""PTY 抽象层：统一的伪终端会话接口。

设计目标
--------
上层（Web 终端 / 命令行入口）只关心三件事：往里写、改尺寸、收到输出。
底层差异全部收敛在这里：

- Windows → ConPTY（通过 pywinpty，真伪终端，TUI 可正常渲染）
- POSIX   → pty.fork + os.read（标准库，零依赖）

两个必须注意的坑（已实测）
--------------------------
1. **必须先启动读取泵，再启动子进程**。ConPTY 在最后一个客户端断开时会丢弃
   未读缓冲，如果先启进程再起读线程，短命命令的输出会整段丢失。
2. **必须清理宿主注入的 ``CODEBUDDY_*`` 环境变量**。WorkBuddy 宿主会注入
   会话 ID、宿主类型等变量，独立 CLI 实例继承后会误判运行环境。
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence

IS_WINDOWS = os.name == "nt"

# 环境清理：这些前缀由宿主注入，会干扰独立 CLI 实例
_STRIP_PREFIXES = ("CODEBUDDY_", "WORKBUDDY_", "BUDDYBRIDGE_")
_STRIP_KEEP = {"CODEBUDDY_INTERNET_ENVIRONMENT"}

OutputCallback = Callable[[str], None]
ExitCallback = Callable[[Optional[int]], None]


def clean_env(base: Optional[Dict[str, str]] = None,
              keep_proxy: bool = True) -> Dict[str, str]:
    """构造干净的子进程环境。

    除了剥离宿主变量，还会把回环地址加入 NO_PROXY。原因很实际：
    CLI 启动阶段会拉本地配置，一旦系统代理不通就**静默卡死**（无日志、无端口）。
    """
    env = dict(base if base is not None else os.environ)
    for key in list(env):
        if key in _STRIP_KEEP:
            continue
        if key.startswith(_STRIP_PREFIXES):
            env.pop(key, None)

    loop = "127.0.0.1,localhost,::1"
    existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
    merged = ",".join([p for p in (loop, existing) if p])
    env["NO_PROXY"] = merged
    env["no_proxy"] = merged

    if not keep_proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            env.pop(k, None)
    return env


def which_command(command: str) -> Optional[str]:
    """把命令名解析成绝对路径，找不到返回 None。"""
    if not command:
        return None
    if os.path.isabs(command) and os.path.exists(command):
        return command
    return shutil.which(command)


class PtySession:
    """一条伪终端会话。

    生命周期：``start()`` → 读写 → ``close()``。输出通过回调异步推送，
    回调运行在独立的读线程里，**不要在里面做阻塞操作**。
    """

    def __init__(
        self,
        session_id: str,
        argv: Sequence[str],
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        cols: int = 100,
        rows: int = 30,
        title: str = "",
    ) -> None:
        if not argv:
            raise ValueError("argv 不能为空")
        self.session_id = session_id
        self.argv: List[str] = list(argv)
        self.cwd = cwd or os.getcwd()
        self.env = clean_env(env)
        self.cols = max(20, int(cols))
        self.rows = max(5, int(rows))
        self.title = title or " ".join(self.argv[:2])

        self._proc = None          # Windows: PtyProcess；POSIX: pid
        self._fd: Optional[int] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._alive = False
        self._exit_code: Optional[int] = None
        self._started_at = 0.0

        self.on_output: Optional[OutputCallback] = None
        self.on_exit: Optional[ExitCallback] = None

    # ------------------------------------------------------------------ 启动
    def start(self, on_output: OutputCallback,
              on_exit: Optional[ExitCallback] = None) -> None:
        if self._alive:
            raise RuntimeError("会话已在运行")
        self.on_output = on_output
        self.on_exit = on_exit
        self._stop.clear()

        if IS_WINDOWS:
            self._start_windows()
        else:
            self._start_posix()

        self._alive = True
        self._started_at = time.time()

    def _start_windows(self) -> None:
        try:
            from winpty import PtyProcess
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise RuntimeError(
                "Windows 下需要 pywinpty 提供真伪终端。请执行：\n"
                "  pip install pywinpty"
            ) from exc

        exe = which_command(self.argv[0])
        if not exe:
            raise RuntimeError(f"找不到命令：{self.argv[0]}")
        argv = [exe] + self.argv[1:]

        self._proc = PtyProcess.spawn(
            argv,
            cwd=self.cwd,
            env=self.env,
            dimensions=(self.rows, self.cols),
        )
        # 先起读泵，再让调用方返回（避免首屏输出丢失）
        self._reader = threading.Thread(
            target=self._read_loop_windows, name=f"pty-{self.session_id}", daemon=True
        )
        self._reader.start()

    def _start_posix(self) -> None:
        import fcntl
        import pty
        import struct
        import termios

        exe = which_command(self.argv[0])
        if not exe:
            raise RuntimeError(f"找不到命令：{self.argv[0]}")

        pid, fd = pty.fork()
        if pid == 0:  # 子进程
            try:
                os.chdir(self.cwd)
                os.execvpe(exe, [exe] + self.argv[1:], self.env)
            except BaseException:
                os._exit(127)

        self._proc = pid
        self._fd = fd
        # 设置初始窗口大小
        winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

        self._reader = threading.Thread(
            target=self._read_loop_posix, name=f"pty-{self.session_id}", daemon=True
        )
        self._reader.start()

    # ------------------------------------------------------------------ 读
    def _read_loop_windows(self) -> None:
        proc = self._proc
        code: Optional[int] = None
        while not self._stop.is_set():
            try:
                data = proc.read(8192)
            except EOFError:
                break
            except Exception:
                break
            if data:
                self._emit(data)
        try:
            code = proc.exitstatus if hasattr(proc, "exitstatus") else None
        except Exception:
            code = None
        self._finish(code)

    def _read_loop_posix(self) -> None:
        fd = self._fd
        code: Optional[int] = None
        while not self._stop.is_set():
            try:
                data = os.read(fd, 8192)
            except OSError:
                break
            if not data:
                break
            self._emit(data.decode("utf-8", "replace"))
        try:
            _, status = os.waitpid(self._proc, 0)
            code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else None
        except (ChildProcessError, OSError):
            code = None
        self._finish(code)

    def _emit(self, text: str) -> None:
        cb = self.on_output
        if cb is None:
            return
        try:
            cb(text)
        except Exception:
            # 回调异常不能杀死读线程，否则会话会假死
            pass

    def _finish(self, code: Optional[int]) -> None:
        with self._lock:
            if not self._alive:
                return
            self._alive = False
            self._exit_code = code
        cb = self.on_exit
        if cb is not None:
            try:
                cb(code)
            except Exception:
                pass

    # ------------------------------------------------------------------ 写
    def write(self, text: str) -> bool:
        """向终端写入字符。等同于用户敲键盘。"""
        if not text:
            return False
        if IS_WINDOWS:
            with self._lock:
                proc = self._proc
            if proc is None:
                return False
            try:
                proc.write(text)
                return True
            except Exception:
                return False

        fd = self._fd
        if fd is None:
            return False
        try:
            os.write(fd, text.encode("utf-8"))
            return True
        except OSError:
            return False

    def write_line(self, text: str, newline: str = "\r") -> bool:
        """写入一整行（模拟回车）。"""
        return self.write(text + newline)

    # -------------------------------------------------------------- 尺寸/终止
    def resize(self, cols: int, rows: int) -> bool:
        cols = max(20, int(cols))
        rows = max(5, int(rows))
        self.cols, self.rows = cols, rows
        if IS_WINDOWS:
            with self._lock:
                proc = self._proc
            if proc is None:
                return False
            try:
                proc.setwinsize(rows, cols)
                return True
            except Exception:
                return False

        fd = self._fd
        if fd is None:
            return False
        try:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.kill(self._proc, signal.SIGWINCH)
            return True
        except OSError:
            return False

    def close(self, force: bool = False) -> None:
        self._stop.set()
        if IS_WINDOWS:
            with self._lock:
                proc = self._proc
            if proc is not None:
                try:
                    proc.terminate(force=force)
                except Exception:
                    pass
        else:
            if self._proc:
                try:
                    os.kill(self._proc, signal.SIGKILL if force else signal.SIGTERM)
                except OSError:
                    pass
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        self._alive = False

    # ------------------------------------------------------------------ 状态
    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    @property
    def pid(self) -> Optional[int]:
        if IS_WINDOWS:
            return getattr(self._proc, "pid", None)
        return self._proc

    def info(self) -> Dict[str, object]:
        return {
            "id": self.session_id,
            "title": self.title,
            "command": " ".join(self.argv),
            "cwd": self.cwd,
            "cols": self.cols,
            "rows": self.rows,
            "alive": self._alive,
            "pid": self.pid,
            "exit_code": self._exit_code,
            "uptime": round(time.time() - self._started_at, 1) if self._started_at else 0,
        }
