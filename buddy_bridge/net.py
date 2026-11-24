"""网络层：上游连接策略、错误分类与可执行的诊断。

这个模块的存在主要是为了解决一类很坑的问题：当系统代理 / 防火墙在
TLS 握手阶段把 CONNECT 隧道掐断时，Python 只会抛一句
``[SSL: UNEXPECTED_EOF_WHILE_READING]``，完全看不出真正原因是
"域名根本不存在" 还是 "代理隧道失败"。这里把异常还原成人能看懂的结论。
"""

from __future__ import annotations

import re
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse


# ---- 错误分类 ----

KIND_OK = "ok"
KIND_DNS = "dns"            # 域名不存在 / 解析失败
KIND_PROXY = "proxy"        # 代理 CONNECT 隧道失败
KIND_TLS = "tls"            # TLS 握手被中断 / 证书问题
KIND_TIMEOUT = "timeout"    # 连接或读取超时
KIND_REFUSED = "refused"    # 连接被拒绝 / 网络不可达
KIND_HTTP = "http"          # 上游返回了 HTTP 错误码
KIND_OTHER = "other"


class UpstreamFailure(Exception):
    """带可读结论与修复建议的上游错误。"""

    def __init__(self, kind: str, summary: str, detail: str = "", hint: str = "") -> None:
        super().__init__(summary)
        self.kind = kind
        self.summary = summary
        self.detail = detail
        self.hint = hint

    def as_dict(self) -> Dict[str, str]:
        return {
            "type": self.kind,
            "message": self.summary,
            "detail": self.detail,
            "hint": self.hint,
        }

    def __str__(self) -> str:
        parts = [self.summary]
        if self.detail:
            parts.append(f"（{self.detail}）")
        if self.hint:
            parts.append(f" 建议：{self.hint}")
        return "".join(parts)


_TLS_MARKERS = (
    "UNEXPECTED_EOF_WHILE_READING",
    "EOF OCCURRED IN VIOLATION",
    "SSLV3_ALERT",
    "CERTIFICATE_VERIFY_FAILED",
    "WRONG_VERSION_NUMBER",
    "TLSV1_ALERT",
    "HANDSHAKE",
    "UNKNOWN_PROTOCOL",
    "CERT_",
)
_PROXY_MARKERS = (
    "TUNNEL CONNECTION FAILED",
    "PROXY",
    "BAD GATEWAY",
    "502",
    "503",
    "FORBIDDEN",
    "NO ROUTE TO HOST",
)
_DNS_MARKERS = (
    "GETADDRINFO FAILED",
    "NAME OR SERVICE NOT KNOWN",
    "NODENAME NOR SERVNAME",
    "NO SUCH HOST",
    "NON-EXISTENT DOMAIN",
    "11001",
    "11004",
    "EAI_AGAIN",
    "TEMPORARY FAILURE IN NAME RESOLUTION",
)


def _flatten(exc: BaseException) -> Tuple[str, str]:
    reason = getattr(exc, "reason", None)
    text = f"{type(exc).__name__}: {exc}"
    reason_text = ""
    if reason is not None and reason is not exc:
        reason_text = f"{type(reason).__name__}: {reason}"
        text += " | " + reason_text
    return text.upper(), text


def _classify_raw(
    exc: BaseException,
    *,
    host: str = "",
    via_proxy: bool = False,
) -> UpstreamFailure:
    """按异常本身做原始分类（不看 DNS 预检结果）。"""
    if isinstance(exc, urllib.error.HTTPError):
        return UpstreamFailure(
            KIND_HTTP,
            f"上游返回 HTTP {exc.code}",
            detail=str(exc.reason),
            hint="若为 401/403 请检查 API Key 与认证头模式；若为 429 请降低速率限制；"
                 "若为 5xx 多为上游临时故障，稍后会自动恢复。",
        )

    upper, text = _flatten(exc)
    reason = getattr(exc, "reason", exc)

    # 1. DNS：域名不存在是最容易被代理掩盖的错误
    if isinstance(reason, socket.gaierror) or any(m in upper for m in _DNS_MARKERS):
        return UpstreamFailure(
            KIND_DNS,
            f"域名解析失败{'：' + host if host else ''}（DNS 无法解析该主机）",
            detail=text,
            hint="请核对 WorkBuddy Base URL 的域名是否拼写正确、是否仍是官方有效地址。"
                 "注意：开了系统代理时，代理会在 TLS 握手前断开连接，把 DNS 错误伪装成 SSL EOF。",
        )

    # 2. 代理隧道
    if any(m in upper for m in _PROXY_MARKERS):
        return UpstreamFailure(
            KIND_PROXY,
            "系统/上游代理的 CONNECT 隧道建立失败",
            detail=text,
            hint="在「网络与防护」里把上游代理改为「直连」试一次；若必须走代理，"
                 "请确认代理软件已开启且允许访问该域名（或填写自定义代理地址）。",
        )

    # 3. TLS 握手（含用户日志里的 UNEXPECTED_EOF_WHILE_READING）
    if (isinstance(reason, ssl.SSLError)
            or isinstance(reason, ssl.CertificateError)
            or any(m in upper for m in _TLS_MARKERS)):
        proxy_note = "当前请求经过系统代理，隧道被提前关闭通常就是它造成的。" if via_proxy else ""
        return UpstreamFailure(
            KIND_TLS,
            "TLS 握手被对端提前关闭（SSL EOF）",
            detail=text,
            hint=proxy_note
                 + "建议依次：① 用「网络诊断」查看 DNS 与握手结果；② 切换上游代理为「直连」；"
                   "③ 确认 Base URL 域名有效；④ 不要关闭证书校验。",
        )

    # 4. 超时
    if isinstance(reason, (TimeoutError, socket.timeout)) or "TIMED OUT" in upper or "TIMEOUT" in upper:
        return UpstreamFailure(
            KIND_TIMEOUT,
            "连接或读取上游超时",
            detail=text,
            hint="检查网络与代理连通性；断路器会自动阻止持续重试造成的请求风暴。",
        )

    # 5. 连接拒绝 / 不可达
    if isinstance(reason, (ConnectionRefusedError, ConnectionResetError, OSError)) or \
            any(m in upper for m in ("CONNECTION REFUSED", "UNREACHABLE", "CONNECTION RESET", "10061")):
        return UpstreamFailure(
            KIND_REFUSED,
            "无法建立到上游的连接",
            detail=text,
            hint="确认端口/协议正确，且本机网络或代理允许访问该地址。",
        )

    return UpstreamFailure(KIND_OTHER, "连接上游失败", detail=text, hint="请用「网络诊断」查看详细结果。")


def classify_exception(
    exc: BaseException,
    *,
    host: str = "",
    via_proxy: bool = False,
    dns_ok: Optional[bool] = None,
) -> UpstreamFailure:
    """把异常翻译成可执行的结论；带 DNS 预检结果时优先给出确定性根因。

    系统代理会在 CONNECT 阶段因远端 NXDOMAIN 直接断开，Python 只能看到
    ``SSL: UNEXPECTED_EOF_WHILE_READING``。若本地 DNS 已确认解析失败，
    就把它还原成 DNS 结论——此时"切直连 / 调证书 / 换代理"都是无效建议。
    """
    failure = _classify_raw(exc, host=host, via_proxy=via_proxy)
    if dns_ok is not False or failure.kind not in (KIND_TLS, KIND_PROXY, KIND_DNS):
        return failure
    return UpstreamFailure(
        KIND_DNS,
        f"域名不存在或无法解析：{host or '上游主机'}（本地 DNS 预检已确认失败）",
        detail=failure.detail,
        hint="已确认根因不是代理或 SSL 配置：域名解析不了，切换「直连 / 自定义代理」都不会成功，"
             "也不会自行恢复。请到服务提供方控制台重新核对 Base URL（改完需重启代理）。"
             "可用「网络诊断」复核，或运行 python probe_endpoints.py 探测候选官方端点。",
    )


# ---- 脱敏 ----

# (正则, 是否保留第 1 组作为前缀)。保留前缀用于 "Bearer xxx" / "apiKey: xxx" 这类结构
_SECRET_PATTERNS = (
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.=<>/+]{8,}", re.IGNORECASE), True),
    (re.compile(r"\bck_[A-Za-z0-9_\-]{6,}"), False),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), False),
    (re.compile(r"((?:api[_-]?key|apikey|authorization|x-api-key)[\"']?\s*[:=]\s*[\"']?)[^\s,\"}]{8,}", re.IGNORECASE), True),
)


def redact(text: Any, secrets: Optional[list] = None) -> str:
    """抹掉文本里可能泄露的凭据，用于日志与错误响应。"""
    s = str(text)
    for secret in secrets or []:
        if secret and len(str(secret)) >= 6:
            s = s.replace(str(secret), "***REDACTED***")
    for pat, keep_prefix in _SECRET_PATTERNS:
        s = pat.sub(
            lambda m, kp=keep_prefix: (m.group(1) if kp else "") + "***REDACTED***",
            s,
        )
    return s


# ---- SSL / 代理 ----

def build_ssl_context(verify_tls: bool = True) -> ssl.SSLContext:
    """构造显式 TLS 上下文：强制 TLS1.2+、带 SNI、加载系统根证书。"""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if not verify_tls:
        # 仅在用户明确知情时用于排障；默认绝不能关
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def current_proxies() -> Dict[str, str]:
    """返回 urllib 实际会使用的代理（含 Windows 注册表系统代理）。"""
    try:
        return dict(urllib.request.getproxies())
    except Exception:  # noqa: BLE001
        return {}


def describe_proxy(proxy_mode: str, proxy_url: str = "") -> str:
    if proxy_mode == "direct":
        return "直连（忽略系统代理）"
    if proxy_mode == "custom":
        return f"自定义代理 {proxy_url or '(未填写)'}"
    proxies = current_proxies()
    https = proxies.get("https") or proxies.get("http") or "(无)"
    return f"跟随系统/环境代理 -> {https}"


def build_opener(
    proxy_mode: str = "auto",
    proxy_url: str = "",
    verify_tls: bool = True,
) -> urllib.request.OpenerDirector:
    """按策略构造 opener。

    auto    : 使用 getproxies()（环境变量 + Windows 注册表系统代理）
    direct  : ProxyHandler({}) 强制忽略一切代理
    custom  : 使用用户指定的代理地址
    """
    handlers = []
    if proxy_mode == "direct":
        handlers.append(urllib.request.ProxyHandler({}))
    elif proxy_mode == "custom" and proxy_url:
        p = proxy_url if "://" in proxy_url else "http://" + proxy_url
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    else:
        handlers.append(urllib.request.ProxyHandler())
    handlers.append(urllib.request.HTTPSHandler(context=build_ssl_context(verify_tls)))
    return urllib.request.build_opener(*handlers)


# ---- 预检与诊断 ----

def parse_base_url(base_url: str) -> Tuple[str, str]:
    """返回 (scheme://host[:port], host)。"""
    parsed = urlparse(base_url if "://" in base_url else "https://" + base_url)
    host = parsed.hostname or ""
    return f"{parsed.scheme}://{parsed.netloc}", host


def dns_probe(host: str, timeout: float = 3.0) -> Tuple[bool, str]:
    """本地 DNS 预检。返回 (是否解析成功, IP 或错误描述)。"""
    if not host:
        return False, "Base URL 中未解析出主机名"
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        return True, ", ".join(ips[:3])
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        socket.setdefaulttimeout(old)


def tls_probe(host: str, port: int = 443, timeout: float = 5.0, verify_tls: bool = True) -> Tuple[bool, str]:
    """直连做一次 TLS 握手探测（不经代理）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            ctx = build_ssl_context(verify_tls)
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                return True, f"{tls.version()} / {tls.cipher()[0]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def network_report(
    base_url: str,
    api_key: str = "",
    proxy_mode: str = "auto",
    proxy_url: str = "",
    verify_tls: bool = True,
    request_timeout: float = 8.0,
) -> str:
    """生成一份可复制的网络诊断报告，用于「网络诊断」按钮。"""
    lines = []
    try:
        origin, host = parse_base_url(base_url)
        port = urlparse(origin).port or (443 if origin.startswith("https") else 80)
    except Exception as exc:  # noqa: BLE001
        return f"Base URL 格式有误：{exc}"

    lines.append(f"[1] Base URL      : {base_url or '(空)'}")
    lines.append(f"[2] 主机 / 端口   : {host}:{port}")
    lines.append(f"[3] 上游代理策略  : {describe_proxy(proxy_mode, proxy_url)}")

    ok_dns, dns_detail = dns_probe(host)
    lines.append(f"[4] 本地 DNS 解析 : {'OK  -> ' + dns_detail if ok_dns else 'FAIL -> ' + dns_detail}")

    if origin.startswith("https"):
        ok_tls, tls_detail = tls_probe(host, port, verify_tls=verify_tls)
        lines.append(f"[5] 直连 TLS 握手 : {'OK  -> ' + tls_detail if ok_tls else 'FAIL -> ' + tls_detail}")
    else:
        ok_tls = False
        lines.append("[5] 直连 TLS 握手 : 跳过（非 https）")

    # 按当前策略实际发一次 /v1/models（不带 Key，避免产生计费/风控噪音）
    models_url = base_url.rstrip("/")
    if models_url.endswith("/v1"):
        models_url += "/models"
    elif not models_url.endswith("/v1/models"):
        models_url += "/v1/models"
    try:
        opener = build_opener(proxy_mode, proxy_url, verify_tls)
        req = urllib.request.Request(models_url, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("Accept-Encoding", "identity")
        with opener.open(req, timeout=request_timeout) as resp:
            body = resp.read(200)
        lines.append(f"[6] GET /v1/models: OK  -> HTTP {resp.status} {body[:80]!r}")
        probe_ok = True
    except urllib.error.HTTPError as exc:
        lines.append(f"[6] GET /v1/models: HTTP {exc.code}（能连通，通常需要有效 API Key）")
        probe_ok = True
    except Exception as exc:  # noqa: BLE001
        f = classify_exception(exc, host=host, via_proxy=proxy_mode != "direct")
        lines.append(f"[6] GET /v1/models: FAIL -> [{f.kind}] {f.summary}")
        probe_ok = False

    lines.append("")
    if probe_ok:
        lines.append("结论：网络链路可达。若仍报错，问题多在 API Key / 认证头 / 请求体兼容性。")
    elif not ok_dns:
        lines.append("结论：域名不存在（NXDOMAIN）。这不是代理或 SSL 配置问题，")
        lines.append("      代码无法连上一个不存在的服务。请到官方控制台重新核对 Base URL。")
        lines.append("      症状对照：开了系统代理时，DNS 失败会被伪装成 SSL: UNEXPECTED_EOF_WHILE_READING。")
    elif not ok_tls and proxy_mode != "direct":
        lines.append("结论：直连握手也失败，且当前走系统代理。建议把上游代理切到「直连」再测一次，")
        lines.append("      以区分是代理隧道问题还是网关侧 TLS 限制。")
    else:
        lines.append("结论：链路不可达。请按上面 [4][5][6] 的失败项逐项排查代理与网关地址。")

    if api_key:
        lines.append(f"      （本次诊断未使用 API Key：{redact(api_key)}）")
    return "\n".join(lines)
