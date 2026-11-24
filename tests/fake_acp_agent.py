"""一个最小的假 ACP agent，只用于测试协议客户端，不依赖 CodeBuddy CLI。

协议要点：JSON-RPC 2.0，stdio，每行一条 JSON。
用法：python fake_acp_agent.py --acp
"""

import json
import sys

SESSION_ID = "sess_fake_001"


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def notify_update(session_id, update):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": update}})


def read_msg():
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


def main():
    while True:
        msg = read_msg()
        if msg is None:
            break
        if not msg:
            continue

        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "fake-agent", "version": "0.0.1"},
                    "agentCapabilities": {"loadSession": False},
                    "authMethods": [],
                },
            })
            continue

        if method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": SESSION_ID}})
            continue

        if method == "session/prompt":
            sid = params.get("sessionId") or SESSION_ID
            text = ""
            for block in params.get("prompt") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text") or ""

            # 权限往返测试：prompt 里带 PERM 就向客户端发一次授权请求
            if "PERM" in text:
                send({
                    "jsonrpc": "2.0",
                    "id": 900,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": sid,
                        "toolCall": {"toolCallId": "tc_1", "title": "rm -rf build/"},
                        "options": [
                            {"optionId": "allow", "name": "允许", "kind": "allow_once"},
                            {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
                        ],
                    },
                })
                reply = read_msg() or {}
                outcome = (reply.get("result") or {}).get("outcome") or {}
                chosen = outcome.get("optionId") or outcome.get("outcome") or "none"
                for piece in (f"决定={chosen}；", "已按该决定处理。"):
                    notify_update(sid, {
                        "sessionUpdate": "agent_message_chunk",
                        "messageId": "m1",
                        "content": {"type": "text", "text": piece},
                    })
                send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
                continue

            for piece in ("你好，", "这是", "假 agent 的回复。"):
                notify_update(sid, {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": "m1",
                    "content": {"type": "text", "text": piece},
                })
            notify_update(sid, {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc_0",
                "title": "读取文件",
                "status": "completed",
            })
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
            continue

        if method == "session/cancel":
            continue

        if rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"未知方法 {method}"}})


if __name__ == "__main__":
    main()
