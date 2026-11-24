"""安全与稳健性组件。

这里的"防风控"思路是让流量看起来像正常的人工使用：
控制速率、限制并发、失败时熔断退避、缓存重复请求、复用连接，
避免客户端在出错时以 1 秒间隔疯狂重试形成明显的异常流量。
同时提供本地访问控制，防止代理把你的真实 Key 暴露给局域网。
"""

from __future__ import annotations

import os
import posixpath
import random
import threading
import time
from collections import deque
from typing import Any, Dict, Optional
from urllib.parse import unquote


class RateLimiter:
    """滑动窗口限速 + 最小请求间隔 + 抖动 + 429 自适应降速。"""

    def __init__(
        self,
        max_per_minute: int = 60,
        min_interval_ms: int = 0,
        jitter_ms: int = 0,
    ) -> None:
        self._max = max(1, int(max_per_minute))
        self._min_interval = max(0.0, min_interval_ms / 1000.0)
        self._jitter = max(0.0, jitter_ms / 1000.0)
        self._timestamps: deque = deque()
        self._last_at = 0.0
        self._throttle_until = 0.0
        self._penalty = 1.0
        self._lock = threading.Lock()

    def configure(self, max_per_minute: int, min_interval_ms: int = 0, jitter_ms: int = 0) -> None:
        with self._lock:
            self._max = max(1, int(max_per_minute))
            self._min_interval = max(0.0, min_interval_ms / 1000.0)
            self._jitter = max(0.0, jitter_ms / 1000.0)

    def note_throttle(self, cooldown_seconds: float = 20.0) -> None:
        """上游返回 429 / 5xx 时调用：临时压低有效速率。"""
        with self._lock:
            self._throttle_until = max(self._throttle_until, time.time() + cooldown_seconds)
            self._penalty = max(0.3, self._penalty * 0.6)

    def _effective_max(self, now: float) -> int:
        if now < self._throttle_until:
            return max(1, int(self._max * self._penalty))
        self._penalty = min(1.0, self._penalty + 0.05)  # 逐步恢复
        return self._max

    def allow(self, *, wait: bool = True) -> bool:
        now = time.time()
        with self._lock:
            cutoff = now - 60
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._effective_max(now):
                return False

            # 最小间隔：把突发请求摊平，避免整点式脉冲
            elapsed = now - self._last_at
            if self._last_at and elapsed < self._min_interval:
                delay = self._min_interval - elapsed
                if self._jitter:
                    delay += random.uniform(0, self._jitter)
                if wait:
                    # 在锁外睡眠，避免阻塞其它线程的允许判断
                    pass
                else:
                    return False
            else:
                delay = 0.0
            self._timestamps.append(now)
            self._last_at = now
        if delay > 0:
            time.sleep(min(delay, 5.0))
        return True


class CircuitBreaker:
    """连续连接类失败达到阈值后熔断，冷却期内直接拒绝，避免请求风暴。

    半开状态只放一个探测请求，成功则闭合，失败则重新计时冷却。
    """

    def __init__(self, threshold: int = 5, cooldown_seconds: float = 30.0) -> None:
        self._threshold = max(1, int(threshold))
        self._cooldown = max(1.0, float(cooldown_seconds))
        self._failures = 0
        self._opened_at = 0.0
        self._half_open = False
        self._lock = threading.Lock()

    def configure(self, threshold: int, cooldown_seconds: float) -> None:
        with self._lock:
            self._threshold = max(1, int(threshold))
            self._cooldown = max(1.0, float(cooldown_seconds))

    def allow(self) -> bool:
        with self._lock:
            if not self._half_open and self._opened_at:
                if time.time() - self._opened_at < self._cooldown:
                    return False
                self._half_open = True  # 放一个探测请求
                return True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0
            self._half_open = False

    def record_failure(self) -> bool:
        """返回 True 表示本次触发了熔断。"""
        with self._lock:
            if self._half_open:
                self._half_open = False
                self._opened_at = time.time()
                return True
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = time.time()
                self._half_open = False
                return True
            return False

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0
            self._half_open = False

    @property
    def tripped(self) -> bool:
        with self._lock:
            return bool(self._opened_at) and not self._half_open


class ConcurrencyGuard:
    """限制同时在途的上游请求数。"""

    def __init__(self, limit: int = 4) -> None:
        self._limit = max(1, int(limit))
        self._sem = threading.BoundedSemaphore(self._limit)

    def configure(self, limit: int) -> None:
        new = max(1, int(limit))
        if new == self._limit:
            return
        self._sem = threading.BoundedSemaphore(new)
        self._limit = new

    def acquire(self, timeout: float = 10.0) -> bool:
        return self._sem.acquire(timeout=timeout)

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            pass


class ModelsCache:
    """/v1/models 结果缓存，避免客户端反复探测模型列表。"""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._payload: Optional[Any] = None
        self._expire_at = 0.0
        self._lock = threading.Lock()

    def configure(self, ttl_seconds: float) -> None:
        with self._lock:
            self._ttl = max(0.0, float(ttl_seconds))
            self._payload = None

    def get(self) -> Optional[Any]:
        with self._lock:
            if self._payload is not None and time.time() < self._expire_at:
                return self._payload
            return None

    def put(self, payload: Any) -> None:
        with self._lock:
            self._payload = payload
            self._expire_at = time.time() + self._ttl

    def clear(self) -> None:
        with self._lock:
            self._payload = None
            self._expire_at = 0.0


# ---- 上游路径校验（防 SSRF / 开放代理滥用） ----

_ALLOWED_PREFIXES = ("/v1/", "/models", "/health", "/status")


def validate_upstream_path(path: str) -> Optional[str]:
    """校验客户端请求的路径，返回规范化路径或 None（拒绝）。

    只放行 /v1/* 与少量本地路由，拒绝协议相对 URL、路径穿越、
    控制字符与 @ 等可能把代理变成任意地址转发器的输入。
    """
    if not path or not isinstance(path, str):
        return None
    decoded = unquote(path)
    low = decoded.lower()
    if any(c in decoded for c in ("\r", "\n", "\x00")) or any(ord(c) < 32 for c in decoded):
        return None
    if low.startswith("//") or "@" in decoded or "\\" in decoded:
        return None
    if "://" in decoded or decoded.startswith("http:") or decoded.startswith("https:"):
        return None
    normalized = posixpath.normpath(decoded)
    if normalized.startswith("..") or "/../" in normalized or normalized == "..":
        return None
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized in ("/v1", "/v1/", "/"):
        return "/v1/models" if normalized == "/" else normalized
    if not normalized.startswith(_ALLOWED_PREFIXES):
        return None
    # 保留 query 无关部分（本函数只处理 path）
    return normalized


def is_loopback_host(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1")


def check_local_access(
    headers: Dict[str, str],
    client_ip: str,
    token: str,
    listen_host: str,
) -> bool:
    """本地访问控制。

    - 监听在 loopback 且未设置令牌：沿用旧行为，直接放行。
    - 一旦监听地址不是 loopback，或配置了令牌，就必须提供匹配的令牌，
      否则局域网内任何人都能借用这个代理消耗你的 Key。
    """
    required = bool(token) or not is_loopback_host(listen_host)
    if not required:
        return True
    if not token:
        return False  # 暴露到局域网却没设令牌，一律拒绝
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    supplied = ""
    auth = lowered.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    supplied = supplied or lowered.get("x-bridge-token", "").strip()
    return bool(supplied) and _const_eq(supplied, token)


def _const_eq(a: str, b: str) -> bool:
    """常量时间比较，避免令牌被逐字节猜测。"""
    try:
        import hmac
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return a == b


def generate_token(nbytes: int = 16) -> str:
    return os.urandom(nbytes).hex()
