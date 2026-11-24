"""本地 HTTP 代理服务器：桥接 DeepSeek Harness 与 WorkBuddy。

相比初版修复/加固的内容：
- 精确区分 DNS / 代理隧道 / TLS 握手 / 超时 / HTTP 错误，不再只丢一句 SSL EOF
- 支持 auto / direct / custom 代理策略，并在系统代理掐断隧道时自动回退直连
- 流式响应用 read1 + chunked 编码，修复 SSE 卡顿与 keep-alive 矛盾
- 明确 Accept-Encoding: identity，修复上游 gzip 响应无法被客户端解析
- 断路器 + 限流 + 并发上限 + 模型列表缓存，避免出错时的请求风暴
- 本地访问令牌、Host 校验、路径白名单、请求体大小限制、日志脱敏
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .config import ConfigManager
from .logger import BridgeLogger
from .net import (
    KIND_DNS,
    KIND_PROXY,
    KIND_REFUSED,
    KIND_TLS,
    KIND_TIMEOUT,
    UpstreamFailure,
    build_opener,
    classify_exception,
    describe_proxy,
    dns_probe,
    parse_base_url,
    redact,
)
from .safety import (
    CircuitBreaker,
    ConcurrencyGuard,
    ModelsCache,
    RateLimiter,
    check_local_access,
    generate_token,
    is_loopback_host,
    validate_upstream_path,
)
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

# 只有"连不上"才算失败并触发熔断；上游正常返回 4xx/5xx 不算
_CONNECTION_FAILURES = {KIND_DNS, KIND_PROXY, KIND_TLS, KIND_TIMEOUT, KIND_REFUSED}

# 转发上游响应头时一律丢弃的头（要么由本代理重新计算，要么会泄露上游信息）
_SKIP_RESPONSE_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "set-cookie",
    "server",
    "keep-alive",
    "proxy-authenticate",
    "www-authenticate",
}


class ProxyHandler(BaseHTTPRequestHandler):
    """处理来自 DeepSeek Harness 的请求并转发到 WorkBuddy。"""

    # 由 ProxyServer 注入
    config: ConfigManager
    logger: BridgeLogger
    rate_limiter: RateLimiter
    circuit_breaker: CircuitBreaker
    concurrency: ConcurrencyGuard
    models_cache: ModelsCache
    runtime: Dict[str, Any]

    server_version = "BuddyBridge/2.0"
    protocol_version = "HTTP/1.1"  # 支持 chunked 流式响应

    # ---- 基础工具 ----
    def log_message(self, fmt: str, *args: Any) -> None:
        self.logger.debug("%s - %s", self.address_string(), fmt % args)

    def _secrets(self) -> list:
        return [self.config.get("workbuddy_api_key", ""), self.config.get("local_access_token", "")]

    def _apply_cors(self) -> None:
        """只给本机来源回显 CORS，避免任意网页/DNS rebinding 读取本地代理。"""
        origin = self.headers.get("Origin", "")
        if not origin:
            return
        try:
            host = (urlparse(origin).hostname or "").lower()
        except ValueError:
            return
        if host in ("localhost", "127.0.0.1", "::1"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Bridge-Token")

    def _send_json(self, status: int, payload: Any, extra: Optional[Dict[str, str]] = None) -> None:
        body = b"" if status == 204 else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._apply_cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_error_json(
        self,
        status: int,
        message: str,
        error_type: str = "proxy_error",
        hint: str = "",
        extra: Optional[Dict[str, str]] = None,
    ) -> None:
        err: Dict[str, Any] = {"type": error_type, "message": message}
        if hint:
            err["hint"] = hint
        self._send_json(status, {"error": err}, extra)

    def _read_body(self) -> Tuple[Optional[bytes], Optional[str]]:
        """读取请求体，带大小上限与 chunked 拒绝。"""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return None, "暂不支持 chunked 编码的请求体，请使用 Content-Length"
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return None, "Content-Length 非法"
        max_bytes = int(self.config.get("max_body_bytes", 4 * 1024 * 1024))
        if length > max_bytes:
            return None, f"请求体过大（{length} > {max_bytes} 字节）"
        if length <= 0:
            return b"", None
        return self.rfile.read(length), None

    # ---- 安全检查 ----
    def _precheck(self) -> bool:
        """Host 校验 + 本地访问令牌校验。失败时已写好响应并返回 False。"""
        listen_host = str(self.config.get("listen_host", "127.0.0.1"))
        token = str(self.config.get("local_access_token", "") or "")

        if is_loopback_host(listen_host):
            raw_host = self.headers.get("Host", "")
            hostname = raw_host.rsplit(":", 1)[0].strip("[]").lower() if raw_host else ""
            if hostname and not is_loopback_host(hostname) and hostname != "localhost":
                self._send_error_json(
                    403,
                    "Host 头不是本机地址，已拒绝（防止 DNS rebinding 借用你的 Key）",
                    "host_not_allowed",
                )
                return False

        if not check_local_access(dict(self.headers), self.client_address[0], token, listen_host):
            self._send_error_json(
                401,
                "缺少或错误的本地访问令牌",
                "local_token_required",
                "请在 DeepSeek Harness 的 API Key 字段填入本代理的访问令牌（见主界面），"
                "或在本地监听 127.0.0.1 时留空令牌。",
            )
            return False
        return True

    # ---- 路由 ----
    def do_OPTIONS(self) -> None:
        self._send_json(204, {})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        # 健康检查不暴露任何凭据，放行便于排障
        if path in ("/", "/health", "/status"):
            self._send_json(200, {
                "status": "ok",
                "service": "buddy-bridge",
                "upstream": str(self.config.get("workbuddy_base_url", "")),
                "proxy": describe_proxy(
                    str(self.config.get("upstream_proxy_mode", "auto")),
                    str(self.config.get("upstream_proxy_url", "")),
                ),
                "circuit": "open" if self.circuit_breaker.tripped else "closed",
            })
            return
        if not self._precheck():
            return
        if path in ("/v1/models", "/models"):
            self._handle_models()
        else:
            self._proxy_request("GET")

    def do_POST(self) -> None:
        if not self._precheck():
            return
        self._proxy_request("POST")

    def do_PUT(self) -> None:
        if not self._precheck():
            return
        self._proxy_request("PUT")

    def do_DELETE(self) -> None:
        if not self._precheck():
            return
        self._proxy_request("DELETE")

    # ---- /v1/models ----
    def _handle_models(self) -> None:
        """优先从 WorkBuddy 拉模型列表，失败则返回内置列表（带明确诊断）。"""
        cached = self.models_cache.get()
        if cached is not None:
            self.logger.debug("/v1/models 命中缓存")
            self._send_json(200, cached)
            return

        api_key = str(self.config.get("workbuddy_api_key", "") or "")
        if not api_key:
            self._send_json(200, build_models_response(DEFAULT_MODELS))
            return

        base = str(self.config.get("workbuddy_base_url", "")).rstrip("/")
        models_url = (base + "/models") if base.endswith("/v1") else (base + "/v1/models")

        # 直连模式下 DNS 不通就没必要再等一次超时
        if self.config.get("dns_preflight", True) and \
                self.config.get("upstream_proxy_mode", "auto") == "direct" and \
                self.runtime.get("dns_ok") is False:
            self.logger.warning(
                "跳过 /v1/models：域名 %s 本地 DNS 解析失败（直连模式），请核对 Base URL",
                self.runtime.get("host", "?"),
            )
            payload = build_models_response(DEFAULT_MODELS)
            self.models_cache.put(payload)
            self._send_json(200, payload)
            return

        # 与 chat 请求共用同一套认证头/隐身头，避免 x-api-key 模式下模型列表拉不到
        models_headers = translate_request_headers(
            {},
            api_key,
            stealth_mode=self.config.get("stealth_mode", True),
            spoof_user_agent=str(self.config.get("spoof_user_agent", "OpenAI-Python/1.54.0")),
            auth_header_mode=str(self.config.get("auth_header_mode", "bearer")),
        )
        req = self._make_request(models_url, None, models_headers, "GET")
        resp, failure = self._open_upstream(req, timeout=10)
        if isinstance(resp, urllib.error.HTTPError):
            self.logger.warning(
                "获取 WorkBuddy 模型列表失败：上游返回 HTTP %d，返回内置列表", resp.code
            )
            resp.close()
            payload = build_models_response(DEFAULT_MODELS)
            self.models_cache.put(payload)
            self._send_json(200, payload)
            return
        if failure is not None or resp is None:
            f = failure or UpstreamFailure(KIND_TLS, "连接上游失败")
            self.logger.warning(
                "获取 WorkBuddy 模型列表失败[%s]，返回内置列表: %s %s",
                f.kind, f.summary, f.hint,
            )
            payload = build_models_response(DEFAULT_MODELS)
            self.models_cache.put(payload)
            self._send_json(200, payload)
            return
        try:
            data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("解析 /v1/models 响应失败: %s", redact(exc, self._secrets()))
            data = build_models_response(DEFAULT_MODELS)
        finally:
            resp.close()
        self.models_cache.put(data)
        self._send_json(200, data)

    # ---- 上游请求 ----
    @staticmethod
    def _make_request(
        url: str,
        body: Optional[bytes],
        headers: Dict[str, str],
        method: str,
    ) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            data=body if method in ("POST", "PUT", "PATCH", "DELETE") else None,
            headers=headers,
            method=method,
        )

    def _build_upstream_url(self, path: str) -> Optional[str]:
        base = str(self.config.get("workbuddy_base_url", "")).rstrip("/")
        if not base:
            return None
        if base.endswith("/v1") and path.startswith("/v1"):
            return base + path[len("/v1"):]
        if path.startswith("/"):
            return base + path
        return base + "/" + path

    def _open_upstream(
        self,
        template: urllib.request.Request,
        timeout: float,
    ) -> Tuple[Optional[Any], Optional[UpstreamFailure]]:
        """发起上游请求；系统代理失败且本地 DNS 正常时自动回退直连一次。"""
        rt = self.runtime
        proxy_mode = str(self.config.get("upstream_proxy_mode", "auto"))
        prefer_direct = bool(rt.get("prefer_direct"))
        opener = rt["direct_opener"] if prefer_direct else rt["opener"]
        via_proxy = (not prefer_direct) and proxy_mode != "direct"

        def _clone(op: Any) -> urllib.request.Request:
            return self._make_request(
                template.full_url,
                template.data,
                dict(template.headers),
                template.get_method(),
            )

        try:
            return opener.open(_clone(opener), timeout=timeout), None
        except urllib.error.HTTPError as exc:
            # 上游明确回了错误码，属于"能连通"，不当作连接失败
            return exc, None
        except Exception as exc:  # noqa: BLE001
            failure = classify_exception(
                exc,
                host=str(rt.get("host", "")),
                via_proxy=via_proxy,
                dns_ok=rt.get("dns_ok"),
            )

        # 自动回退：仅 auto 模式、代理类失败、且本地确实能解析域名
        if (proxy_mode == "auto" and not prefer_direct
                and failure.kind in (KIND_PROXY, KIND_TLS, KIND_DNS)
                and rt.get("dns_ok") is True):
            self.logger.warning("经系统代理连接失败，自动改用直连重试一次（proxy_mode=auto）")
            try:
                resp = rt["direct_opener"].open(_clone(rt["direct_opener"]), timeout=timeout)
                rt["prefer_direct"] = True
                self.logger.info("直连成功，本次运行将优先使用直连")
                return resp, None
            except urllib.error.HTTPError as exc2:
                return exc2, None
            except Exception as exc2:  # noqa: BLE001
                failure = classify_exception(
                    exc2,
                    host=str(rt.get("host", "")),
                    via_proxy=False,
                    dns_ok=rt.get("dns_ok"),
                )

        return None, failure

    def _proxy_request(self, method: str) -> None:
        api_key = str(self.config.get("workbuddy_api_key", "") or "")
        if not api_key:
            self._send_error_json(400, "尚未配置 WorkBuddy API Key", "missing_api_key")
            return

        # 0. Base URL 热更新：用户在界面上改了地址就重新预检，不必重启代理
        base_now = str(self.config.get("workbuddy_base_url", ""))
        if self.runtime.get("observed_base_url") != base_now:
            _, new_host = parse_base_url(base_now)
            ok: Optional[bool] = None
            if new_host and self.config.get("dns_preflight", True):
                ok, _ = dns_probe(new_host)
            self.runtime.update({
                "observed_base_url": base_now,
                "host": new_host,
                "dns_ok": ok,
                "dns_confirmed_dead": False,
                "prefer_direct": False,
            })
            self.models_cache.clear()
            if ok is False:
                self.logger.error(
                    "Base URL 已改为 %s，但该域名本地 DNS 解析失败，请核对地址", base_now
                )
            elif ok is True:
                self.logger.info("Base URL 已更新为 %s，域名解析正常", base_now)

        # 已确认域名不存在：直接给出确定性结论，不再每 30 秒重试一次无意义的连接
        if self.runtime.get("dns_confirmed_dead"):
            self._send_error_json(
                502,
                "上游域名无法解析（NXDOMAIN），代理无法建立连接",
                "dns_nxdomain",
                "已确认根因：Base URL 的域名不存在。切换代理模式、等待断路器冷却都不会改变结果，"
                "请在「连接」页填写正确的 Base URL（可用 python probe_endpoints.py 探测候选端点）。",
            )
            return

        # 1. 断路器：连续连不上时直接挡住，避免客户端 1 秒一次的请求风暴
        if not self.circuit_breaker.allow():
            cooldown = int(self.config.get("circuit_breaker_cooldown", 30))
            self.logger.warning("断路器已打开，冷却期内拒绝请求（%ds）", cooldown)
            self._send_error_json(
                503,
                "上游连续连接失败，已暂时熔断以避免触发风控",
                "circuit_open",
                "请先用「网络诊断」确认 Base URL 与代理设置，冷却结束后会自动放行一次探测请求。",
                {"Retry-After": str(cooldown)},
            )
            return

        # 2. 速率限制
        rate_limit = int(self.config.get("rate_limit_per_minute", 60))
        if rate_limit > 0 and not self.rate_limiter.allow():
            self.logger.warning("请求被速率限制（%d 次/分钟），已拒绝", rate_limit)
            self._send_error_json(
                429, "请求过于频繁，请稍后再试", "rate_limited",
                "可在「网络与防护」中降低速率或增大最小请求间隔。",
                {"Retry-After": "5"},
            )
            return

        # 3. 路径白名单（防代理被当作任意地址转发器）
        raw_path = urlparse(self.path).path
        path = validate_upstream_path(raw_path)
        if path is None:
            self.logger.warning("拒绝非法上游路径: %s", redact(raw_path, self._secrets()))
            self._send_error_json(400, "不允许的请求路径", "path_rejected")
            return

        upstream_url = self._build_upstream_url(path)
        if not upstream_url:
            self._send_error_json(400, "WorkBuddy Base URL 未配置", "missing_base_url")
            return

        raw_body, body_error = self._read_body()
        if body_error:
            self._send_error_json(413, body_error, "body_too_large")
            return
        assert raw_body is not None

        body_json: Optional[Dict[str, Any]] = None
        if raw_body:
            try:
                parsed = json.loads(raw_body.decode("utf-8"))
                if isinstance(parsed, dict):
                    body_json = parsed
            except (json.JSONDecodeError, UnicodeDecodeError):
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
            spoof_user_agent=str(self.config.get("spoof_user_agent", "OpenAI-Python/1.54.0")),
            auth_header_mode=str(self.config.get("auth_header_mode", "bearer")),
        )
        upstream_headers["Content-Type"] = self.headers.get("Content-Type", "application/json")

        model = body_json.get("model", "?") if body_json else "?"
        self.logger.info(
            "%s %s -> %s (model=%s, key=%s%s)",
            method, path, redact(upstream_url, self._secrets()), model,
            mask_api_key(api_key),
            ", " + "; ".join(changes) if changes else "",
        )

        req = self._make_request(upstream_url, forward_body, upstream_headers, method)
        is_stream = bool(body_json and body_json.get("stream"))
        timeout = float(self.config.get("request_timeout", 120))

        acquired = self.concurrency.acquire(timeout=10.0)
        if not acquired:
            self._send_error_json(
                503, "并发请求过多，请稍后再试", "concurrency_limited",
                "可调大「最大并发请求数」，但过高并发容易触发上游风控。",
            )
            return

        resp: Any = None
        try:
            resp, failure = self._open_upstream(req, timeout=timeout)

            # 上游明确返回 4xx/5xx：能连通，不算连接失败，原样转发错误体
            if isinstance(resp, urllib.error.HTTPError):
                self._forward_http_error(resp)
                return

            if failure is not None or resp is None:
                f = failure or UpstreamFailure(KIND_TLS, "连接上游失败")
                # 本地 DNS 已确认失败且连接也失败：域名不存在是确定结论，记录并快速失败
                if self.runtime.get("dns_ok") is False and f.kind in _CONNECTION_FAILURES:
                    self.runtime["dns_confirmed_dead"] = True
                    self.logger.error(
                        "⛔ 确认根因：Base URL 域名 %s 不存在（NXDOMAIN）。"
                        "后续请求将直接拒绝，直到修改 Base URL——切代理、等冷却都无效。",
                        self.runtime.get("host", "?"),
                    )
                if f.kind in _CONNECTION_FAILURES and self.circuit_breaker.record_failure():
                    if self.runtime.get("dns_ok") is False:
                        self.logger.error(
                            "断路器已打开。注意：根因是域名不存在，冷却结束后也不会恢复，"
                            "请先在「连接」页修正 Base URL"
                        )
                    else:
                        self.logger.error("连续失败达到阈值，断路器已打开，暂停请求上游")
                self.logger.error(
                    "连接 WorkBuddy 失败[%s]: %s | %s", f.kind, f.summary, f.hint
                )
                self._send_error_json(502, f.summary, f.kind, f.hint)
                return

            self.circuit_breaker.record_success()
            self._forward_response(resp, is_stream)
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            self.concurrency.release()

    def _forward_response(self, resp: Any, is_stream: bool) -> None:
        """把上游响应写回客户端：流式用 chunked，非流式用 Content-Length。"""
        status = int(getattr(resp, "status", 0) or resp.getcode())
        headers = resp.headers
        content_type = headers.get("Content-Type", "") or ""
        passthrough = [
            (k, v) for k, v in headers.items()
            if k.lower() not in _SKIP_RESPONSE_HEADERS
            and k.lower() != "content-type"
            and not k.lower().startswith("access-control-")
            and "\r" not in str(v) and "\n" not in str(v)
        ]
        # 只有上游确实返回 SSE 才走流式；否则按普通响应转发，避免客户端空等
        streaming = is_stream and status == 200 and "event-stream" in content_type.lower()

        self.send_response(status)
        for k, v in passthrough:
            self.send_header(k, str(v))
        self._apply_cors()
        self.send_header("Cache-Control", "no-store")

        if streaming:
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    # read1 只取当前可用字节，不会等凑满 8192 才返回（SSE 不卡顿的关键）
                    chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.logger.debug("客户端在流式传输中断开连接")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("转发流式响应时出错: %s", redact(exc, self._secrets()))
        else:
            body = resp.read()
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.logger.debug("客户端断开连接")

    def _forward_http_error(self, exc: urllib.error.HTTPError) -> None:
        """上游明确返回错误码：原样转发，但内容脱敏。"""
        err_body = exc.read()
        if exc.code == 429 or exc.code >= 500:
            self.rate_limiter.note_throttle()
            self.logger.warning("上游返回 %d，已临时降低有效速率", exc.code)
        self.logger.error(
            "上游返回 %d: %s", exc.code,
            redact(err_body[:300].decode("utf-8", "replace"), self._secrets()),
        )
        self.send_response(exc.code)
        for k, v in exc.headers.items():
            if k.lower() in _SKIP_RESPONSE_HEADERS or k.lower().startswith("access-control-"):
                continue
            self.send_header(k, str(v).replace("\r", "").replace("\n", ""))
        self.send_header("Content-Length", str(len(err_body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(err_body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class ProxyServer:
    """管理代理服务器的生命周期。"""

    def __init__(self, config: ConfigManager, logger: BridgeLogger) -> None:
        self.config = config
        self.logger = logger
        self.rate_limiter = RateLimiter(
            int(config.get("rate_limit_per_minute", 60)),
            int(config.get("min_request_interval_ms", 300)),
            int(config.get("jitter_ms", 150)),
        )
        self.circuit_breaker = CircuitBreaker(
            int(config.get("circuit_breaker_threshold", 5)),
            float(config.get("circuit_breaker_cooldown", 30)),
        )
        self.concurrency = ConcurrencyGuard(int(config.get("max_concurrent_requests", 4)))
        self.models_cache = ModelsCache(float(config.get("models_cache_ttl", 300)))
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    def start(self) -> Tuple[bool, str]:
        if self._running.is_set():
            return True, "代理已在运行"

        host = str(self.config.get("listen_host", "127.0.0.1"))
        port = int(self.config.get("listen_port", 18789))

        # 暴露到非 loopback 地址却没令牌 = 任何人都能借用你的 Key，强制生成
        if not is_loopback_host(host) and not self.config.get("local_access_token"):
            token = generate_token()
            self.config.set("local_access_token", token)
            try:
                self.config.save()
            except OSError:
                pass
            self.logger.warning(
                "监听地址 %s 非本机回环，已自动生成访问令牌：%s",
                host, token,
            )
            self.logger.warning("请在 DeepSeek Harness 的 API Key 字段填入该令牌（Bearer %s）", token)

        # 同步防护参数
        self.rate_limiter.configure(
            int(self.config.get("rate_limit_per_minute", 60)),
            int(self.config.get("min_request_interval_ms", 300)),
            int(self.config.get("jitter_ms", 150)),
        )
        self.circuit_breaker.configure(
            int(self.config.get("circuit_breaker_threshold", 5)),
            float(self.config.get("circuit_breaker_cooldown", 30)),
        )
        self.circuit_breaker.reset()
        self.concurrency.configure(int(self.config.get("max_concurrent_requests", 4)))
        self.models_cache.configure(float(self.config.get("models_cache_ttl", 300)))

        proxy_mode = str(self.config.get("upstream_proxy_mode", "auto"))
        proxy_url = str(self.config.get("upstream_proxy_url", "") or "")
        verify_tls = bool(self.config.get("verify_tls", True))

        _, upstream_host = parse_base_url(str(self.config.get("workbuddy_base_url", "")))
        dns_ok: Optional[bool] = None
        if self.config.get("dns_preflight", True) and upstream_host:
            dns_ok, detail = dns_probe(upstream_host)
            if not dns_ok:
                self.logger.error("")
                self.logger.error("⛔ 启动预检失败：Base URL 域名 %s 无法解析（%s）", upstream_host, detail)
                self.logger.error("   这不是代理或 SSL 的问题——域名本身不存在，任何代理设置都连不上它。")
                if upstream_host.lower() == "gw.goworkbuddy.cn":
                    self.logger.error(
                        "   gw.goworkbuddy.cn 已由阿里/腾讯/Google 公共 DNS 确认 NXDOMAIN，"
                        "官方文档中也没有这个网关；请改用服务方控制台给出的地址。"
                    )
                self.logger.error("   请到服务提供方控制台核对真实地址，或运行 python probe_endpoints.py 探测候选端点。")
                self.logger.error("")
            else:
                self.logger.info("启动预检：域名 %s 解析成功 -> %s", upstream_host, detail)

        runtime = {
            "opener": build_opener(proxy_mode, proxy_url, verify_tls),
            "direct_opener": build_opener("direct", "", verify_tls),
            "prefer_direct": False,
            "dns_ok": dns_ok,
            "host": upstream_host,
            "observed_base_url": str(self.config.get("workbuddy_base_url", "")),
            "dns_confirmed_dead": False,
        }

        handler_cls = type(
            "BoundProxyHandler",
            (ProxyHandler,),
            {
                "config": self.config,
                "logger": self.logger,
                "rate_limiter": self.rate_limiter,
                "circuit_breaker": self.circuit_breaker,
                "concurrency": self.concurrency,
                "models_cache": self.models_cache,
                "runtime": runtime,
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
        self.logger.info("上游代理策略: %s", describe_proxy(proxy_mode, proxy_url))
        if dns_ok is False:
            # 上面启动预检已输出完整结论，这里只给一句可执行提醒，避免重复刷屏
            self.logger.warning(
                "代理已启动，但 Base URL 域名解析不了：请求会被快速拒绝，改好地址后无需重启即可生效。"
            )
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
