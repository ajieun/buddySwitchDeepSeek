"""请求/响应翻译：把 DeepSeek Harness 的请求适配到 WorkBuddy。

DeepSeek Harness 通过 pi-ai 适配器发出 OpenAI 兼容请求，但会带上
DeepSeek 特有的字段（developer 角色、max_completion_tokens、thinking 等），
部分网关不认识这些字段。这里按用户开关做兼容转换。
"""

import copy
import json
from typing import Any, Dict, Optional, Tuple


def translate_request_body(
    body: Dict[str, Any],
    *,
    developer_to_system: bool = True,
    max_compat_tokens: bool = True,
    strip_thinking: bool = False,
    use_reasoning_effort: bool = True,
) -> Tuple[Dict[str, Any], list]:
    """翻译请求体，返回 (新请求体, 变更记录列表)。"""
    changes: list = []
    data = copy.deepcopy(body)

    # 1. 角色映射：developer -> system
    if developer_to_system and isinstance(data.get("messages"), list):
        for msg in data["messages"]:
            if isinstance(msg, dict) and msg.get("role") == "developer":
                msg["role"] = "system"
                changes.append("role developer -> system")

    # 2. max_completion_tokens -> max_tokens
    if max_compat_tokens and "max_completion_tokens" in data:
        if "max_tokens" not in data:
            data["max_tokens"] = data.pop("max_completion_tokens")
            changes.append("max_completion_tokens -> max_tokens")
        else:
            data.pop("max_completion_tokens", None)
            changes.append("removed max_completion_tokens (kept max_tokens)")

    # 3. thinking 字段处理
    if strip_thinking and "thinking" in data:
        data.pop("thinking", None)
        changes.append("removed thinking field")

    # 4. reasoning_effort：保留或去除
    if not use_reasoning_effort and "reasoning_effort" in data:
        data.pop("reasoning_effort", None)
        changes.append("removed reasoning_effort")

    return data, changes


# 这些头会暴露客户端来源（DeepSeek Harness / 浏览器），隐身模式下一律去除
_STRIP_HEADERS = {
    "user-agent",
    "origin",
    "referer",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "upgrade-insecure-requests",
    "priority",
    "x-bridge-token",
}

# 这些头由代理自身管理，不转发
_MANAGED_HEADERS = {
    "authorization",
    "x-api-key",
    "host",
    "content-length",
    "connection",
    "accept-encoding",
    "transfer-encoding",
    "proxy-authorization",
    "proxy-connection",
    "cookie",
}


def _clean_value(value: Any) -> Optional[str]:
    """拒绝含 CRLF / 控制字符的头值，防止请求头注入。"""
    if value is None:
        return None
    s = str(value)
    if any(c in s for c in ("\r", "\n", "\x00")) or any(ord(c) < 32 for c in s):
        return None
    try:
        s.encode("latin-1")  # http.client 只接受 latin-1 头值
    except UnicodeEncodeError:
        return None
    return s[:4096]


def translate_request_headers(
    headers: Dict[str, str],
    workbuddy_api_key: str,
    *,
    stealth_mode: bool = True,
    spoof_user_agent: str = "OpenAI-Python/1.54.0",
    auth_header_mode: str = "bearer",
) -> Dict[str, str]:
    """构造发往 WorkBuddy 的请求头，隐身模式下去除客户端指纹。

    auth_header_mode:
      bearer   -> Authorization: Bearer <key>（多数 OpenAI 兼容网关）
      x-api-key-> X-Api-Key: <key>（CodeBuddy/WorkBuddy 官方 API Key 认证）
    """
    out: Dict[str, str] = {}
    for k, v in headers.items():
        lk = str(k).lower()
        if lk in _MANAGED_HEADERS:
            continue
        if stealth_mode and lk in _STRIP_HEADERS:
            continue
        # 隐身模式下去掉所有 X- 开头的自定义头（DeepSeek Harness 可能注入）
        if stealth_mode and lk.startswith("x-"):
            continue
        clean = _clean_value(v)
        if clean is None or not str(k).isascii() or any(ord(c) < 33 or ord(c) > 126 for c in str(k)):
            continue
        out[str(k)] = clean

    if auth_header_mode == "x-api-key":
        out["X-Api-Key"] = workbuddy_api_key
    else:
        out["Authorization"] = f"Bearer {workbuddy_api_key}"
    out["Accept"] = "application/json, text/event-stream"
    # 明确要求上游不压缩：否则我们过滤掉 Content-Encoding 后客户端会拿到无法解析的 gzip 数据
    out["Accept-Encoding"] = "identity"
    if stealth_mode:
        out["User-Agent"] = spoof_user_agent or "OpenAI-Python/1.54.0"
    return out


def mask_api_key(key: str) -> str:
    """对 API Key 做脱敏，仅保留首尾字符用于日志。"""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return key[:4] + "***" + key[-4:]


def build_models_response(models: list) -> Dict[str, Any]:
    """构造 /v1/models 响应。"""
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": 0,
                "owned_by": "workbuddy",
            }
            for m in models
        ],
    }


def parse_sse_chunk(line: str) -> Optional[Dict[str, Any]]:
    """解析单行 SSE 数据，返回 JSON 或 None。"""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
