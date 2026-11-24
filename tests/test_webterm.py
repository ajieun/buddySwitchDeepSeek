"""Web 终端集成测试：HTTP 托管、会话创建、WebSocket 双向、翻译推送、安全检查。

Windows 下需要 pywinpty 才能建真伪终端；缺失时整体跳过。
"""
import base64
import json
import os
import socket
import struct
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buddy_bridge.translate import Translator
from buddy_bridge.webterm import WebTermServer

try:
    if os.name == "nt":
        import winpty  # noqa: F401

    HAS_PTY = True
except ImportError:
    HAS_PTY = False


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_json(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, json.loads(r.read() or b"{}")


class WsClient:
    """极简 WebSocket 客户端（仅测试用，文本帧）。"""

    def __init__(self, port, path):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=12)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("握手失败")
            buf += chunk
        head = buf.split(b"\r\n\r\n")[0].decode("latin1")
        self.status = int(head.split("\r\n")[0].split()[1])

    def send(self, obj):
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = bytearray([0x81, 0x80 | n])
        elif n < 65536:
            header = bytearray([0x81, 0x80 | 126]) + struct.pack("!H", n)
        else:
            header = bytearray([0x81, 0x80 | 127]) + struct.pack("!Q", n)
        header += mask
        self.sock.sendall(bytes(header) +
                          bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise ConnectionError
            buf += c
        return buf

    def recv(self):
        head = self._exact(2)
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._exact(8))[0]
        if head[1] & 0x80:
            self._exact(4)
        payload = self._exact(length) if length else b""
        return opcode, payload

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


@unittest.skipUnless(HAS_PTY, "需要 pywinpty 才能建真伪终端")
class TestWebTerm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.server = WebTermServer(port=cls.port, translator=Translator())
        ok, _ = cls.server.start()
        assert ok, "服务启动失败"
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.child = str(Path(__file__).resolve().parent / "_echo_child.py")

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    # ---------------------------------------------------------------- HTTP
    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=8) as r:
            body = r.read()
        self.assertEqual(r.status, 200)
        self.assertIn(b"xterm", body)

    def test_path_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/../bb.py", timeout=8)
        self.assertIn(ctx.exception.code, (400, 403, 404))
        ctx.exception.close()

    def test_sessions_list(self):
        code, data = http_json(self.base + "/api/sessions")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["sessions"], list)

    # ----------------------------------------------------------------- WS
    def _spawn_echo(self):
        argv = [sys.executable, self.child]
        code, data = http_json(self.base + "/api/sessions", "POST",
                               {"argv": argv, "cols": 100, "rows": 30})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        return data["session"]["id"]

    def test_ws_rejects_unknown_session(self):
        client = WsClient(self.port, "/ws?sid=does-not-exist")
        self.assertEqual(client.status, 404)
        client.close()

    def test_echo_and_injection_and_translation(self):
        sid = self._spawn_echo()
        client = WsClient(self.port, f"/ws?sid={sid}")
        self.assertEqual(client.status, 101)

        output = ""
        translations = []
        injected = False
        deadline = time.time() + 15
        client.sock.settimeout(1.0)

        while time.time() < deadline:
            try:
                opcode, payload = client.recv()
            except (socket.timeout, ConnectionError):
                if "GOT:[" in output:
                    break
                continue
            if opcode != 1:
                continue
            msg = json.loads(payload.decode("utf-8"))
            if msg["type"] == "output":
                output += msg["data"]
                if not injected and "proceed" in output:
                    client.send({"type": "input", "data": "HELLO_UI\r"})
                    injected = True
            elif msg["type"] == "translation":
                translations.extend(msg["items"])
            elif msg["type"] == "exit":
                # 再等一点，翻译可能还在路上
                client.sock.settimeout(1.0)
                try:
                    while True:
                        op2, p2 = client.recv()
                        if op2 == 1:
                            m2 = json.loads(p2.decode("utf-8"))
                            if m2["type"] == "translation":
                                translations.extend(m2["items"])
                except (socket.timeout, ConnectionError):
                    pass
                break

        client.close()
        self.assertIn("Do you want to proceed?", output)
        self.assertIn("GOT:[HELLO_UI]", output, "注入的输入没有被子进程读到")
        self.assertTrue(translations, "没有收到任何译文")
        self.assertTrue(any("是否继续" in t["dst"] for t in translations),
                        f"译文内容不对：{translations}")

    def test_resize_accepted(self):
        sid = self._spawn_echo()
        client = WsClient(self.port, f"/ws?sid={sid}")
        client.send({"type": "resize", "cols": 120, "rows": 40})
        client.send({"type": "ping"})
        # 不崩溃即为通过
        time.sleep(0.5)
        client.close()

    def test_close_session(self):
        sid = self._spawn_echo()
        code, data = http_json(f"{self.base}/api/sessions/{sid}", "DELETE")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])


if __name__ == "__main__":
    unittest.main()
