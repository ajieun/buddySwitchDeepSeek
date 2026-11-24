"""ACP (Agent Client Protocol) 客户端。

把 CodeBuddy CLI 以 ``codebuddy --acp`` 方式拉起，作为"模型/智能体后端"使用。
协议为 JSON-RPC 2.0 over stdio，每行一条 JSON（NDJSON）。

设计取舍：
- 只声明最小客户端能力（不代理文件系统 / 终端），让 CLI 用它自己的本地能力，
  同时避免第三方请求借 ACP 反向读写本机文件。
- 线程模型：一个读线程 + 一个 stderr 线程；写操作用锁串行化；
  每个请求挂一个 Event 等待响应，超时给出可读错误。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = 1
CLIENT_INFO = {"name": "BuddyBridge", "version": "3.0"}

# session/update 中代表"模型输出正文"的变体
_TEXT_UPDATE_KINDS = ("agent_message_chunk", "agent_thought_chunk")


class AcpError(Exception):
    """ACP 协议层或链路层错误（消息面向最终用户）。"""

    def __init__(self, message: str, *, code: Any = None, data: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


def resolve_argv(command: str) -> List[str]:
    """把可执行名解析成可直接交给 Popen 的 argv 前缀。

    Windows 上 npm 全局安装的是 ``codebuddy.cmd``，不能直接 exec，
    需要经 COMSPEC 转一层。
    """
    path = shutil.which(command) or command
    if os.name == "nt":
        low = path.lower()
        if low.endswith((".cmd", ".bat")):
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            return [comspec, "/d", "/s", "/c", f'"{path}"']
        if low.endswith(".ps1"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
    return [path]


def extract_text(update: Dict[str, Any]) -> str:
    """从一条 session/update 里取出可展示的文本片段，取不到返回空串。"""
    kind = update.get("sessionUpdate") or ""
    if kind not in _TEXT_UPDATE_KINDS:
        return ""
    content = update.get("content") or {}
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return ""


def describe_update(update: Dict[str, Any]) -> str:
    """把非正文类 update 压缩成一行可读日志。"""
    kind = update.get("sessionUpdate") or "?"
    if kind == "tool_call":
        return f"tool_call: {update.get('title') or update.get('toolCallId')}"
    if kind == "tool_call_update":
        return f"tool_call_update: {update.get('toolCallId')} -> {update.get('status')}"
    if kind == "plan":
        return f"plan: {len(update.get('entries') or [])} 项"
    return kind


class AcpClient:
    """管理一个 ``codebuddy --acp`` 子进程的生命周期与请求。"""

    def __init__(
        self,
        *,
        command: str = "codebuddy",
        extra_args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        logger: Any = None,
        request_timeout: float = 180.0,
        permission_policy: str = "reject",
    ) -> None:
        self.command = command
        # 默认按 CodeBuddy 约定追加 --acp；显式传 extra_args 可整体接管（测试/自定义用）
        self.extra_args = ["--acp"] if extra_args is None else list(extra_args)
        self.cwd = cwd or os.getcwd()
        self.env = dict(os.environ)
        if env:
            self.env.update({k: str(v) for k, v in env.items()})
        self.logger = logger
        self.request_timeout = float(request_timeout)
        # reject：一律拒绝（默认）；allow：一律放行（仅在需要 agent 真改文件时用）
        self.permission_policy = permission_policy

        self._proc: Optional[subprocess.Popen] = None
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._closed = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._callbacks: Dict[str, Callable[[Dict[str, Any]], None]] = {}
        self._stderr_tail: List[str] = []

        self.agent_info: Dict[str, Any] = {}
        self.auth_methods: List[Dict[str, Any]] = []
        self.startup_log: List[str] = []

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._proc is not None:
            return
        argv = resolve_argv(self.command) + self.extra_args
        kwargs: Dict[str, Any] = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
        )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(argv, **kwargs)
        except FileNotFoundError:
            raise AcpError(
                f"找不到命令 {self.command!r}。请先安装：npm install -g @tencent-ai/codebuddy-code",
                code="command_not_found",
            )
        except OSError as exc:
            raise AcpError(f"启动 {self.command!r} 失败：{exc}", code="spawn_failed")

        self._reader_thread = threading.Thread(target=self._reader, name="acp-reader", daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader, name="acp-stderr", daemon=True
        )
        self._stderr_thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        proc = self._proc
        if proc is None:
            return
        self._closed.set()
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe is not None and not pipe.closed:
                    pipe.close()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None
        self._fail_all_pending("ACP 进程已退出")

    def __enter__(self) -> "AcpClient":
        self.start()
        self.initialize()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    # ---- 协议方法 ----

    def initialize(self, timeout: float = 60.0) -> Dict[str, Any]:
        """能力协商。若 agent 要求认证而本地没配 Key，这里会给出可读错误。"""
        try:
            res = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    # 不代理 fs / terminal：CLI 直接用本地能力，第三方也就无法借 ACP 读写文件
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                        "auth": {"terminal": False},
                    },
                    "clientInfo": CLIENT_INFO,
                },
                timeout=timeout,
            )
        except AcpError as exc:
            if exc.code == -32000:
                raise AcpError(
                    "CodeBuddy CLI 要求先认证。请设置 CODEBUDDY_API_KEY，"
                    "并按版本设置 CODEBUDDY_INTERNET_ENVIRONMENT（中国版为 internal）。",
                    code="auth_required",
                )
            raise
        self.agent_info = res.get("agentInfo") or {}
        self.auth_methods = res.get("authMethods") or []
        self.startup_log.append(
            "ACP 初始化完成：%s v%s，协议版本 %s"
            % (
                self.agent_info.get("name", "?"),
                self.agent_info.get("version", "?"),
                res.get("protocolVersion"),
            )
        )
        return res

    def new_session(self, cwd: Optional[str] = None, timeout: float = 60.0) -> str:
        res = self._request(
            "session/new",
            {"cwd": os.path.abspath(cwd or self.cwd), "mcpServers": []},
            timeout=timeout,
        )
        sid = res.get("sessionId")
        if not sid:
            raise AcpError("session/new 未返回 sessionId")
        return str(sid)

    def prompt(
        self,
        session_id: str,
        blocks: List[Dict[str, Any]],
        on_update: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """发送一轮 prompt，返回 stopReason 字符串。"""
        if on_update is not None:
            self._callbacks[session_id] = on_update
        try:
            res = self._request(
                "session/prompt",
                {"sessionId": session_id, "prompt": blocks},
                timeout=timeout or self.request_timeout,
            )
        finally:
            self._callbacks.pop(session_id, None)
        return str(res.get("stopReason") or "end_turn")

    def cancel(self, session_id: str) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
        except Exception:  # noqa: BLE001
            pass

    def close_session(self, session_id: str) -> None:
        """尽力关闭会话；agent 未声明 close 能力时静默忽略。"""
        try:
            self._request("session/close", {"sessionId": session_id}, timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # ---- 内部：收发 ----

    def _next_request_id(self) -> int:
        with self._pending_lock:
            self._next_id += 1
            return self._next_id

    def _send(self, obj: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AcpError("ACP 进程未启动")
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._write_lock:
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise AcpError(f"写入 ACP 进程失败：{exc}", code="write_failed")

    def _request(self, method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        rid = self._next_request_id()
        slot: Dict[str, Any] = {"event": threading.Event(), "result": None, "error": None}
        with self._pending_lock:
            self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise AcpError(f"{method} 超时（{int(timeout)}s 未收到响应）", code="timeout")
        if slot["error"] is not None:
            err = slot["error"]
            if isinstance(err, dict):
                raise AcpError(
                    str(err.get("message") or f"{method} 失败"),
                    code=err.get("code"),
                    data=err.get("data"),
                )
            raise AcpError(str(err))
        return slot["result"] or {}

    def _respond(self, req_id: Any, result: Any) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception:  # noqa: BLE001
            pass

    def _respond_error(self, req_id: Any, code: int, message: str) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})
        except Exception:  # noqa: BLE001
            pass

    def _reader(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                if self._closed.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # CLI 偶尔会在 stdout 打非协议日志，忽略但留痕
                    self.startup_log.append(f"非协议输出：{line[:200]}")
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        finally:
            self._fail_all_pending("ACP 进程输出流已关闭")

    def _stderr_reader(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                if len(self._stderr_tail) > 40:
                    del self._stderr_tail[:-40]
                if self.logger is not None:
                    self.logger.debug("[codebuddy] %s", line[:400])
        except Exception:  # noqa: BLE001
            pass

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        # agent → client 的请求
        if "method" in msg and "id" in msg:
            self._handle_agent_request(msg)
            return
        # 通知
        if "method" in msg:
            if msg.get("method") == "session/update":
                params = msg.get("params") or {}
                sid = str(params.get("sessionId") or "")
                update = params.get("update") or {}
                cb = self._callbacks.get(sid)
                if cb is not None:
                    try:
                        cb(update)
                    except Exception as exc:  # noqa: BLE001
                        if self.logger is not None:
                            self.logger.debug("session/update 回调异常：%s", exc)
            return
        # 响应
        rid = msg.get("id")
        if rid is None:
            return
        with self._pending_lock:
            slot = self._pending.pop(rid, None)
        if slot is None:
            return
        if "error" in msg:
            slot["error"] = msg.get("error")
        else:
            slot["result"] = msg.get("result")
        slot["event"].set()

    def _handle_agent_request(self, msg: Dict[str, Any]) -> None:
        """处理 agent 反向发来的请求。"""
        rid = msg.get("id")
        method = str(msg.get("method") or "")
        params = msg.get("params") or {}

        if method == "session/request_permission":
            self._respond(rid, self._permission_outcome(params))
            return

        # 没有声明这些能力，正常不会收到；收到就明确拒绝，别让 agent 挂住
        if method.startswith(("fs/", "terminal/")):
            self._respond_error(rid, -32601, f"客户端未提供 {method} 能力")
            return

        if method == "elicitation/create":
            self._respond(rid, {"action": "cancel"})
            return

        self._respond_error(rid, -32601, f"未实现的方法：{method}")

    def _permission_outcome(self, params: Dict[str, Any]) -> Dict[str, Any]:
        options = params.get("options") or []
        if self.permission_policy == "allow":
            for opt in options:
                if str(opt.get("kind") or "").startswith("allow"):
                    return {"outcome": {"outcome": "selected", "optionId": opt.get("optionId")}}
            if options:
                return {"outcome": {"outcome": "selected", "optionId": options[0].get("optionId")}}
        for opt in options:
            if str(opt.get("kind") or "").startswith("reject"):
                return {"outcome": {"outcome": "selected", "optionId": opt.get("optionId")}}
        return {"outcome": {"outcome": "cancelled"}}

    def _fail_all_pending(self, reason: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot["error"] = {"message": reason, "code": "closed"}
            slot["event"].set()

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail[-10:])
