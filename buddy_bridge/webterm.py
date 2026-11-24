"""Web 终端服务：HTTP 静态托管 + WebSocket 双向通道 + 翻译流水线。

为什么用 WebSocket 而不是 SSE
-----------------------------
终端输入是高频小包（vim 里按 hjkl、TUI 里的方向键），SSE 只能服务端推送，
每次按键都要发一个 HTTP 请求，延迟和开销都不可接受。WebSocket 一条连接
双向复用，帧开销只有 2~6 字节。

协议（JSON 文本帧）
------------------
客户端 → 服务端::

    {"type": "input",  "data": "ls\\r"}
    {"type": "resize", "cols": 120, "rows": 30}
    {"type": "ping"}

服务端 → 客户端::

    {"type": "ready",   "session": {...}}
    {"type": "output",  "data": "..."}
    {"type": "translation", "items": [{"src": "...", "dst": "..."}]}
    {"type": "exit",    "code": 0}
    {"type": "error",   "message": "..."}

安全边界
--------
- 默认只监听 ``127.0.0.1``，局域网不可达。
- 监听非回环地址时**强制**生成访问令牌，且校验 ``Host`` 头。
- 静态文件只从 ``web/`` 目录读，路径做规范化防穿越。
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import queue
import secrets
import socket
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .pty_host import PtySession
from .translate import LineCollector, Translator, is_translatable

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 翻译批处理：攒够行数或超时即发送，减少请求次数
_TRANSLATE_BATCH = 6
_TRANSLATE_WINDOW = 0.35


class Session:
    """一条终端会话 + 它的订阅者与翻译缓冲。"""

    def __init__(self, sid: str, pty: PtySession) -> None:
        self.id = sid
        self.pty = pty
        self.clients: List["_WsClient"] = []
        self.lock = threading.RLock()
        self.collector = LineCollector()
        self.created_at = time.time()
        self.last_output = time.time()

    def broadcast(self, payload: dict) -> None:
        dead = []
        with self.lock:
            clients = list(self.clients)
        data = json.dumps(payload, ensure_ascii=False)
        for c in clients:
            if not c.send_json(data):
                dead.append(c)
        if dead:
            with self.lock:
                for d in dead:
                    if d in self.clients:
                        self.clients.remove(d)


class _WsClient:
    """一个 WebSocket 连接的发送封装（带锁，避免多线程写帧交错）。"""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._lock = threading.Lock()
        self.closed = False

    def send_frame(self, opcode: int, payload: bytes) -> bool:
        if self.closed:
            return False
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack("!H", n)
        else:
            header.append(127)
            header += struct.pack("!Q", n)
        try:
            with self._lock:
                self.sock.sendall(bytes(header) + payload)
            return True
        except OSError:
            self.closed = True
            return False

    def send_json(self, data: str) -> bool:
        return self.send_frame(0x1, data.encode("utf-8"))

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class TerminalHub:
    """会话仓库 + 翻译流水线。"""

    def __init__(self, translator: Optional[Translator] = None,
                 on_log=None) -> None:
        self.sessions: Dict[str, Session] = {}
        self.lock = threading.RLock()
        self.translator = translator or Translator()
        self.translate_enabled = True
        self._tr_queue: "queue.Queue[tuple]" = queue.Queue()
        self._log = on_log or (lambda *a, **k: None)
        self._tr_thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._running = True
        self._tr_thread = threading.Thread(target=self._translate_worker,
                                           name="translate", daemon=True)
        self._tr_thread.start()

    def shutdown(self) -> None:
        self._running = False
        self._tr_queue.put(None)
        with self.lock:
            for s in list(self.sessions.values()):
                try:
                    s.pty.close(force=True)
                except Exception:
                    pass
            self.sessions.clear()

    # ------------------------------------------------------------ 会话管理
    def create(self, argv: List[str], cwd: Optional[str] = None,
               cols: int = 100, rows: int = 30, title: str = "") -> Session:
        sid = uuid.uuid4().hex[:12]
        pty = PtySession(sid, argv, cwd=cwd, cols=cols, rows=rows, title=title)
        session = Session(sid, pty)

        def on_output(text: str) -> None:
            session.last_output = time.time()
            session.broadcast({"type": "output", "data": text})
            self._feed_translate(session, text)

        def on_exit(code: Optional[int]) -> None:
            session.broadcast({"type": "exit", "code": code})
            self._log(f"会话 {sid} 已退出，code={code}")

        pty.start(on_output, on_exit)
        with self.lock:
            self.sessions[sid] = session
        self._log(f"会话 {sid} 已启动：{' '.join(argv)}")
        return session

    def get(self, sid: str) -> Optional[Session]:
        with self.lock:
            return self.sessions.get(sid)

    def close(self, sid: str) -> bool:
        with self.lock:
            session = self.sessions.pop(sid, None)
        if not session:
            return False
        try:
            session.pty.close(force=True)
        except Exception:
            pass
        session.broadcast({"type": "exit", "code": None})
        return True

    def list_info(self) -> List[dict]:
        with self.lock:
            return [s.pty.info() for s in self.sessions.values()]

    # ------------------------------------------------------------ 翻译流水
    def _feed_translate(self, session: Session, text: str) -> None:
        if not self.translate_enabled:
            return
        try:
            lines = session.collector.feed(text)
        except Exception:
            return
        for line in lines:
            if is_translatable(line):
                self._tr_queue.put((session.id, line))

    def _translate_worker(self) -> None:
        while self._running:
            try:
                first = self._tr_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if first is None:
                break

            batch = [first]
            deadline = time.time() + _TRANSLATE_WINDOW
            while len(batch) < _TRANSLATE_BATCH and time.time() < deadline:
                try:
                    item = self._tr_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    self._running = False
                    break
                batch.append(item)

            # 同一会话相邻的重复行去掉，避免刷屏
            seen = set()
            unique = []
            for sid, line in batch:
                key = (sid, line)
                if key in seen:
                    continue
                seen.add(key)
                unique.append((sid, line))

            if not unique:
                continue
            try:
                results = self.translator.translate_many([t for _, t in unique])
            except Exception:
                continue

            grouped: Dict[str, List[dict]] = {}
            for (sid, src), dst in zip(unique, results):
                if not dst:
                    continue
                grouped.setdefault(sid, []).append({"src": src, "dst": dst})

            for sid, items in grouped.items():
                session = self.get(sid)
                if session:
                    session.broadcast({"type": "translation", "items": items})

    def translate_text(self, text: str) -> Optional[str]:
        """手动翻译（前端选中文字后调用）。"""
        return self.translator.translate(text)


class _Handler(BaseHTTPRequestHandler):
    server_version = "BuddyBridgeTerm"
    protocol_version = "HTTP/1.1"

    # 由 WebTermServer 注入
    hub: TerminalHub
    token: str = ""
    static_root: Path = WEB_DIR

    # ---------------------------------------------------------------- 工具
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 默认实现会往 stderr 打印每条请求，太吵
        pass

    def _client_ok(self, query: dict) -> bool:
        if not self.token:
            return True
        supplied = (query.get("token", [""])[0] or
                    self.headers.get("X-Bridge-Token", ""))
        return secrets.compare_digest(supplied, self.token)

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"ok": False, "error": message})

    def _check_host(self) -> bool:
        """挡 DNS rebinding：只接受本机名/回环地址。"""
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        allowed = {"127.0.0.1", "localhost", "::1", "[::1]", ""}
        return host in allowed or not self.token

    # ---------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if not self._check_host():
            self._error(403, "Host 不被允许")
            return

        if path == "/ws":
            self._handle_ws(query)
            return

        if path == "/api/sessions":
            if not self._client_ok(query):
                self._error(401, "令牌无效")
                return
            self._json(200, {"ok": True, "sessions": self.hub.list_info()})
            return

        if path == "/api/translate":
            if not self._client_ok(query):
                self._error(401, "令牌无效")
                return
            text = (query.get("text", [""])[0] or "").strip()
            if not text:
                self._error(400, "缺少 text 参数")
                return
            self._json(200, {"ok": True, "result": self.hub.translate_text(text)})
            return

        # 静态资源
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        self._serve_static(rel, query)

    def _serve_static(self, rel: str, query: dict) -> None:
        root = self.static_root.resolve()
        try:
            target = (root / rel).resolve()
        except (OSError, ValueError):
            self._error(400, "路径非法")
            return
        # 防目录穿越
        if root not in target.parents and target != root:
            self._error(403, "禁止访问")
            return
        if not target.is_file():
            self._error(404, "文件不存在")
            return

        ctype, _ = mimetypes.guess_type(str(target))
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._check_host() or not self._client_ok(query):
            self._error(401, "未授权")
            return
        if parsed.path != "/api/sessions":
            self._error(404, "接口不存在")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._error(400, "请求体不是合法 JSON")
            return

        default_argv = ["cmd.exe"] if os.name == "nt" else ["bash"]
        argv = payload.get("argv") or default_argv
        if isinstance(argv, str):
            argv = [argv]
        if not isinstance(argv, list) or not argv:
            self._error(400, "argv 非法")
            return

        try:
            session = self.hub.create(
                [str(a) for a in argv],
                cwd=payload.get("cwd") or None,
                cols=int(payload.get("cols") or 100),
                rows=int(payload.get("rows") or 30),
                title=str(payload.get("title") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            self._error(500, f"启动失败：{exc}")
            return

        self._json(200, {"ok": True, "session": session.pty.info()})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._check_host() or not self._client_ok(query):
            self._error(401, "未授权")
            return
        sid = parsed.path.rsplit("/", 1)[-1]
        if sid in ("", "sessions"):
            self._error(400, "缺少会话 ID")
            return
        ok = self.hub.close(sid)
        self._json(200 if ok else 404, {"ok": ok})

    # ----------------------------------------------------------- WebSocket
    def _handle_ws(self, query: dict) -> None:
        if not self._client_ok(query):
            self._error(401, "令牌无效")
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._error(400, "缺少 WebSocket 握手头")
            return
        sid = (query.get("sid", [""])[0] or "").strip()
        session = self.hub.get(sid)
        if not session:
            self._error(404, "会话不存在")
            return

        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            return

        sock = self.connection
        sock.settimeout(None)
        client = _WsClient(sock)
        with session.lock:
            session.clients.append(client)
        client.send_json(json.dumps({"type": "ready", "session": session.pty.info()},
                                    ensure_ascii=False))

        try:
            while not client.closed:
                frame = _read_frame(sock)
                if frame is None:
                    break
                fin, opcode, payload = frame

                if opcode == 0x8:  # close
                    _send_close(sock)
                    break
                if opcode == 0x9:  # ping
                    client.send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode not in (0x1, 0x2):
                    continue

                try:
                    msg = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                self._dispatch(client, session, msg)
        except (OSError, ConnectionError):
            pass
        finally:
            with session.lock:
                if client in session.clients:
                    session.clients.remove(client)
            client.close()

    def _dispatch(self, client: _WsClient, session: Session, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "input":
            data = msg.get("data") or ""
            if isinstance(data, str) and data:
                session.pty.write(data)
        elif kind == "resize":
            try:
                session.pty.resize(int(msg.get("cols") or 100),
                                   int(msg.get("rows") or 30))
            except (TypeError, ValueError):
                pass
        elif kind == "ping":
            client.send_json(json.dumps({"type": "pong"}))
        elif kind == "translate":
            text = (msg.get("data") or "").strip()
            if text:
                result = self.hub.translate_text(text)
                if result:
                    client.send_json(json.dumps(
                        {"type": "translation", "items": [{"src": text, "dst": result}]},
                        ensure_ascii=False))

    # HTTP/1.1 keep-alive 下，DELETE 等方法的 body 需要读掉
    def _drain_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
        except (ValueError, OSError):
            pass


# ---------------------------------------------------------------- WS 帧工具


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf += chunk
    return buf


def _read_frame(sock: socket.socket):
    try:
        head = _read_exact(sock, 2)
    except (ConnectionError, OSError):
        return None
    b1, b2 = head[0], head[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F

    if length == 126:
        length = struct.unpack("!H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(sock, 8))[0]

    if length > 8 * 1024 * 1024:  # 单帧 8MB 上限，防内存打爆
        raise ConnectionError("帧过大")

    mask = _read_exact(sock, 4) if masked else None
    payload = _read_exact(sock, length) if length else b""
    if mask:
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return fin, opcode, payload


def _send_close(sock: socket.socket) -> None:
    try:
        sock.sendall(b"\x88\x00")
    except OSError:
        pass


# ---------------------------------------------------------------- 服务封装


class _QuietHTTPServer(ThreadingHTTPServer):
    """终端客户端断开连接是常态（关标签页、刷新），不该刷堆栈。"""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        pass


class WebTermServer:
    """把 HTTP 服务 + 会话中心打包成一个可启动对象。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7333,
        translator: Optional[Translator] = None,
        token: str = "",
        static_root: Optional[Path] = None,
        on_log=None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.hub = TerminalHub(translator=translator, on_log=on_log)
        self.on_log = on_log or (lambda *a, **k: None)
        self.static_root = Path(static_root) if static_root else WEB_DIR
        # 非回环监听必须带令牌，否则局域网内谁都能控制你的终端
        if host not in ("127.0.0.1", "localhost", "::1"):
            self.token = token or secrets.token_urlsafe(18)
        else:
            self.token = token or ""
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        suffix = f"?token={self.token}" if self.token else ""
        return f"http://{self.host}:{self.port}/{suffix}"

    def start(self) -> tuple:
        hub = self.hub
        token = self.token
        static_root = self.static_root

        class Handler(_Handler):
            pass

        Handler.hub = hub
        Handler.token = token
        Handler.static_root = static_root

        try:
            httpd = _QuietHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            return False, f"端口 {self.port} 无法监听：{exc}"

        self._httpd = httpd
        self.hub.start()
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="webterm", daemon=True)
        self._thread.start()
        return True, self.url

    def stop(self) -> None:
        self.hub.shutdown()
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    def wait(self) -> None:
        try:
            while self._httpd:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop()
