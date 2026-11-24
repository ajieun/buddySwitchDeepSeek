"""ACP 客户端回归测试：用本地假 agent 验证协议流，不依赖 CodeBuddy CLI 或网络。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from buddy_bridge.acp_client import (  # noqa: E402
    AcpClient,
    AcpError,
    describe_update,
    extract_text,
    resolve_argv,
)

FAKE_AGENT = str(Path(__file__).with_name("fake_acp_agent.py"))


class TestAcpHelpers(unittest.TestCase):
    def test_extract_text_only_for_message_chunks(self):
        self.assertEqual(
            extract_text({
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hi"},
            }),
            "hi",
        )
        self.assertEqual(extract_text({"sessionUpdate": "tool_call", "content": {"text": "x"}}), "")
        self.assertEqual(extract_text({"sessionUpdate": "agent_message_chunk"}), "")

    def test_describe_update(self):
        self.assertIn("tool_call", describe_update({"sessionUpdate": "tool_call", "title": "读文件"}))
        self.assertIn("plan", describe_update({"sessionUpdate": "plan", "entries": [1, 2]}))

    def test_resolve_argv_returns_non_empty(self):
        self.assertTrue(resolve_argv(sys.executable))


class TestAcpClientAgainstFakeAgent(unittest.TestCase):
    def setUp(self):
        self.client = AcpClient(
            command=sys.executable,
            extra_args=[FAKE_AGENT, "--acp"],
            permission_policy="reject",
            request_timeout=20,
        )
        self.client.start()
        self.addCleanup(self.client.stop)

    def test_initialize_reports_agent_info(self):
        res = self.client.initialize()
        self.assertEqual(res.get("protocolVersion"), 1)
        self.assertEqual(self.client.agent_info.get("name"), "fake-agent")

    def test_prompt_streams_chunks_in_order(self):
        self.client.initialize()
        sid = self.client.new_session()
        self.assertEqual(sid, "sess_fake_001")

        chunks = []
        seen_kinds = []

        def on_update(update):
            seen_kinds.append(update.get("sessionUpdate"))
            text = extract_text(update)
            if text:
                chunks.append(text)

        stop = self.client.prompt(sid, [{"type": "text", "text": "说点什么"}], on_update=on_update)

        self.assertEqual(stop, "end_turn")
        self.assertEqual("".join(chunks), "你好，这是假 agent 的回复。")
        self.assertIn("agent_message_chunk", seen_kinds)
        self.assertIn("tool_call", seen_kinds)

    def test_permission_is_rejected_by_default(self):
        self.client.initialize()
        sid = self.client.new_session()

        chunks = []

        def on_update(update):
            text = extract_text(update)
            if text:
                chunks.append(text)

        stop = self.client.prompt(sid, [{"type": "text", "text": "PERM 删目录"}], on_update=on_update)

        self.assertEqual(stop, "end_turn")
        joined = "".join(chunks)
        # 客户端选中的是 kind 以 reject 开头的选项
        self.assertIn("决定=reject", joined)

    def test_permission_can_be_allowed(self):
        client = AcpClient(
            command=sys.executable,
            extra_args=[FAKE_AGENT, "--acp"],
            permission_policy="allow",
            request_timeout=20,
        )
        client.start()
        self.addCleanup(client.stop)
        client.initialize()
        sid = client.new_session()

        chunks = []
        client.prompt(
            sid,
            [{"type": "text", "text": "PERM 放行"}],
            on_update=lambda u: chunks.append(extract_text(u)),
        )
        self.assertIn("决定=allow", "".join(chunks))

    def test_missing_command_raises_readable_error(self):
        client = AcpClient(command="definitely-not-a-real-binary-xyz", extra_args=[], request_timeout=5)
        with self.assertRaises(AcpError) as ctx:
            client.start()
        self.assertIn("找不到命令", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
