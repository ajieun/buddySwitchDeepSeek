"""本地 HTTP 代理服务器：桥接 DeepSeek Harness 与 WorkBuddy。"""

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .config import ConfigManager
from .logger import BridgeLogger
from .translator import (
    build_models_response,
    mask_api_key,
    translate_request_body,
    translate_request_headers,
)


# WorkBuddy 常见可用模型（用于 /v1/models 兜底列表）
DEFAULT_MODELS = [
    "glm-5.3-flash",
    "glm-5.3",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "kimi-k3.1-pro",
]


class RateLimiter:
    """滑动窗口速率限制器，防止异常流量触发 WorkBuddy 风控。"""

    def __init__(self, max_per_minute: int = 60) -> None:
        self._max = max(1, max_per_minute)
        self._timestamps: deque = deque()
        self._lock = threading.Lock()

    def set_limit(self, max_per_minute: int) -> None:
        with self._lock:
            self._max = max(1, max_per_minute)

    def allow(self) -> bool:
        now = time.time()
        with self._lock:
            # 清理 60 秒前的时间戳
            cutoff = now - 60
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True


class ProxyHandler(BaseHTTPRequestHandler):
    """处理来自 DeepSeek Harness 的请求并转发到 WorkBuddy。"""

    # 由 ProxyServer 注入
    config: ConfigManager
    logger: BridgeLogger
    rate_limiter: RateLimiter
    upstream_base: str  # 例如 https://gw.goworkbuddy.cn

    server_version = "BuddyBridge/1.0"

    # ---- 工具方法 ----
    def log_message(self, fmt: str, *args: Any) -> None:
        # 把 BaseHTTPRequestHandler 的默认日志交给我们的 logger
        self.logger.debug("%s - %s", self.address_string(), fmt % args)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str, error_type: str = "proxy_error") -> None:
        self._send_json(status, {"error": {"type": error_type, "message": message}})

    # ---- 路由分发 ----
    def do_OPTIONS(self) -> None:
        self._send_json(204, {})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/v1/models", "/models"):
            self._handle_models()
        elif path in ("/", "/health", "/status"):
            self._send_json(200, {"status": "ok", "service": "buddy-bridge"})
        else:
            # 其他 GET 一律转发
            self._proxy_request("GET")

    def do_POST(self) -> None:
        self._proxy_request("POST")

    def do_PUT(self) -> None:
        self._proxy_request("PUT")

    def do_DELETE(self) -> None:
        self._proxy_request("DELETE")

    # ---- 具体处理 ----
    def _handle_models(self) -> None:
        """优先从 WorkBuddy 拉模型列表，失败则返回内置列表。"""
        api_key = self.config.get("workbuddy_api_key", "")
        if not api_key:
            self._send_json(200, build_models_response(DEFAULT_MODELS))
            return
        try:
            upstream_url = self._build_upstream_url("/v1/models")
            req = urllib.request.Request(upstream_url, method="GET")
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("Accept", "application/json")
            if self.config.get("stealth_mode", True):
                req.add_header("User-Agent", self.config.get("spoof_user_agent", "OpenAI-Python/1.54.0"))
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._send_json(200, data)
        except Exception as exc:
            self.logger.warning("获取 WorkBuddy 模型列表失败，返回内置列表: %s", exc)
            self._send_json(200, build_models_response(DEFAULT_MODELS))

    def _proxy_request(self, method: str) -> None:
        api_key = self.config.get("workbuddy_api_key", "")
        if not api_key:
            self._send_error_json(400, "尚未配置 WorkBuddy API Key", "missing_api_key")
            return

        # 速率限制：防止异常流量触发 WorkBuddy 风控
        rate_limit = int(self.config.get("rate_limit_per_minute", 60))
        if rate_limit > 0 and not self.rate_limiter.allow():
            self.logger.warning("请求被速率限制（%d 次/分钟），已拒绝", rate_limit)
            self._send_error_json(429, "请求过于频繁，请稍后再试", "rate_limited")
            return

        path = urlparse(self.path).path
        upstream_url = self._build_upstream_url(path)
        if not upstream_url:
            self._send_error_json(400, "WorkBuddy Base URL 未配置", "missing_base_url")
            return

        raw_body = self._read_body()
        body_json: Optional[Dict[str, Any]] = None
        if raw_body:
            try:
                body_json = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                body_json = None

        changes: list = []
        if body_json is not None:
            body_json, changes = translate_request_body(
                body_json,
                developer_to_system=self.config.get("translate_developer_to_system", True),
                max_compat_tokens=self.config.get("translate_max_completion_tokens", True),
                strip_thinking=self.config.get("strip_deepseek_thinking_field", False),
                use_reasoning_effort=self.config.get("use_reasoning_effort", True),
            )
            forward_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
        else:
            forward_body = raw_body

        upstream_headers = translate_request_headers(
            dict(self.headers),
            api_key,
            stealth_mode=self.config.get("stealth_mode", True),
            spoof_user_agent=self.config.get("spoof_user_agent", "OpenAI-Python/1.54.0"),
        )
        upstream_headers["Content-Type"] = self.headers.get("Content-Type", "application/json")

        model = body_json.get("model", "?") if body_json else "?"
        self.logger.info(
            "%s %s -> %s (model=%s, key=%s%s)",
            method,
            path,
            upstream_url,
            model,
            mask_api_key(api_key),
            ", " + "; ".join(changes) if changes else "",
        )

        req = urllib.request.Request(
            upstream_url,
            data=forward_body if method in ("POST", "PUT", "PATCH") else None,
            headers=upstream_headers,
            method=method,
        )

        is_stream = bool(body_json and body_json.get("stream"))

        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as exc:
            err_body = exc.read()
            self.logger.error("上游 %s 返回 %d: %s", path, exc.code, err_body[:300])
            self.send_response(exc.code)
            for k, v in exc.headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
            return
        except urllib.error.URLError as exc:
            self.logger.error("连接 WorkBuddy 失败: %s", exc.reason)
            self._send_error_json(502, f"无法连接 WorkBuddy: {exc.reason}", "upstream_unreachable")
            return
        except Exception as exc:  # noqa: BLE001
            self.logger.error("代理请求异常: %s", exc)
            self._send_error_json(500, f"代理内部错误: {exc}", "internal_error")
            return

        # 把上游响应头转发回去
        self.send_response(resp.status)
        content_type = None
        for k, v in resp.headers.items():
            lk = k.lower()
            if lk in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                continue
            if lk == "content-type":
                content_type = v
                continue
            self.send_header(k, v)
        self.send_header("Access-Control-Allow-Origin", "*")

        if is_stream:
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # 流式逐块转发
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.logger.debug("客户端断开连接")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("转发流式响应时出错: %s", exc)
            finally:
                resp.close()
        else:
            # 非流式：缓冲完整响应并设置 Content-Length，避免客户端挂起
            body = resp.read()
            resp.close()
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.logger.debug("客户端断开连接")

    def _build_upstream_url(self, path: str) -> Optional[str]:
        base = self.config.get("workbuddy_base_url", "").rstrip("/")
        if not base:
            return None
        # 如果用户填的 base 带了 /v1，path 也带 /v1，需要去重
        if base.endswith("/v1") and path.startswith("/v1"):
            return base + path[len("/v1"):]
        if path.startswith("/"):
            return base + path
        return base + "/" + path


class ProxyServer:
    """管理代理服务器的生命周期。"""

    def __init__(self, config: ConfigManager, logger: BridgeLogger) -> None:
        self.config = config
        self.logger = logger
        self.rate_limiter = RateLimiter(int(config.get("rate_limit_per_minute", 60)))
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    def start(self) -> Tuple[bool, str]:
        if self._running.is_set():
            return True, "代理已在运行"

        host = self.config.get("listen_host", "127.0.0.1")
        port = int(self.config.get("listen_port", 18789))

        # 同步速率限制配置
        self.rate_limiter.set_limit(int(self.config.get("rate_limit_per_minute", 60)))

        # 注入 handler 依赖
        handler_cls = type(
            "BoundProxyHandler",
            (ProxyHandler,),
            {
                "config": self.config,
                "logger": self.logger,
                "rate_limiter": self.rate_limiter,
                "upstream_base": self.config.get("workbuddy_base_url", ""),
            },
        )

        try:
            self._server = ThreadingHTTPServer((host, port), handler_cls)
        except OSError as exc:
            return False, f"端口 {port} 被占用: {exc}"

        self._running.set()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.logger.info("代理已启动: http://%s:%d", host, port)
        return True, f"代理已启动: http://{host}:{port}"

    def _serve(self) -> None:
        try:
            assert self._server is not None
            self._server.serve_forever()
        except Exception as exc:  # noqa: BLE001
            self.logger.error("代理服务器异常退出: %s", exc)
        finally:
            self._running.clear()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.logger.info("代理已停止")
