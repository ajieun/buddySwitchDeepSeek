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


def translate_request_headers(
    headers: Dict[str, str],
    workbuddy_api_key: str,
) -> Dict[str, str]:
    """构造发往 WorkBuddy 的请求头。"""
    out: Dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        # 去掉客户端传来的鉴权与连接相关头，由代理重新设置
        if lk in (
            "authorization",
            "host",
            "content-length",
            "connection",
            "accept-encoding",
        ):
            continue
        out[k] = v
    out["Authorization"] = f"Bearer {workbuddy_api_key}"
    out["Accept"] = "application/json, text/event-stream"
    return out


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
