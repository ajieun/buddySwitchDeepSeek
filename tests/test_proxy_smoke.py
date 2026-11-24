"""BuddyBridge 回归测试：用本地 mock 上游验证代理行为，不依赖外网。

运行：python -m unittest discover -s tests -v
"""

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buddy_bridge.config import ConfigManager  # noqa: E402
from buddy_bridge.logger import BridgeLogger  # noqa: E402
from buddy_bridge.proxy import ProxyServer  # noqa: E402


class MockUpstream:
    """记录收到的请求，并按路径返回 JSON / SSE。"""

    def __init__(self) -> None:
        self.calls = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _record(self, body):
                with outer.lock:
                    outer.calls.append({
                        "path": self.path,
                        "method": self.command,
                        "headers": dict(self.headers),
                        "body": body,
                    })

            def handle(self):
                # 客户端读够就断开时，避免测试输出被 ConnectionAborted 噪音刷屏
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

            def do_GET(self):
                self._record(b"")
                if self.path.endswith("/models"):
                    payload = json.dumps({
                        "object": "list",
                        "data": [{"id": "mock-model", "object": "model"}],
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length) if length else b""
                self._record(body)
                data = json.loads(body or b"{}")
                if data.get("model") == "boom":
                    err = json.dumps({"error": {"message": "bad key"}}).encode()
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return
                if data.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for i in range(3):
                        chunk = f'data: {{"delta":{i}}}\n\n'.encode()
                        self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                        time.sleep(0.02)
                    done = b"data: [DONE]\n\n"
                    self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                else:
                    payload = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # 流式测试里客户端读够即断开，属预期行为，不打印 traceback
                pass

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ProxyTestBase(unittest.TestCase):
    upstream: MockUpstream

    @classmethod
    def setUpClass(cls):
        cls.upstream = MockUpstream()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.stop()

    def make_config(self, **overrides):
        tmp = Path(tempfile.mkdtemp()) / "config.json"
        cfg = ConfigManager(tmp)
        base = {
            "workbuddy_base_url": f"http://127.0.0.1:{self.upstream.port}/v1",
            "workbuddy_api_key": "ck_test_key_123456",
            "listen_host": "127.0.0.1",
            "listen_port": free_port(),
            "upstream_proxy_mode": "direct",
            "min_request_interval_ms": 0,
            "jitter_ms": 0,
            "rate_limit_per_minute": 600,
        }
        base.update(overrides)
        cfg.update(base)
        return cfg

    def start(self, cfg):
        logger = BridgeLogger()
        logger.set_level(50)  # CRITICAL，静默测试日志
        server = ProxyServer(cfg, logger)
        ok, msg = server.start()
        self.assertTrue(ok, msg)
        self.addCleanup(server.stop)
        return server, cfg

    def request(self, cfg, method, path, body=None, headers=None, timeout=15):
        conn = http.client.HTTPConnection("127.0.0.1", int(cfg.get("listen_port")), timeout=timeout)
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        payload = json.dumps(body).encode() if isinstance(body, dict) else body
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        out = (resp.status, dict(resp.getheaders()), data)
        conn.close()
        return out


class TestForwarding(ProxyTestBase):
    def test_chat_translation_and_forwarding(self):
        cfg = self.make_config()
        self.start(cfg)
        body = {
            "model": "glm-5.3",
            "messages": [{"role": "developer", "content": "hi"}],
            "max_completion_tokens": 16,
            "stream": False,
        }
        status, headers, data = self.request(cfg, "POST", "/v1/chat/completions", body)
        self.assertEqual(status, 200)
        self.assertIn(b'"content": "ok"', data)

        sent = self.upstream.calls[-1]
        fwd = json.loads(sent["body"])
        self.assertEqual(fwd["messages"][0]["role"], "system")
        self.assertEqual(fwd["max_tokens"], 16)
        self.assertNotIn("max_completion_tokens", fwd)
        # 认证头被替换成真实 Key
        self.assertEqual(sent["headers"].get("Authorization"), "Bearer ck_test_key_123456")
        # 不压缩，避免客户端拿到无法解析的 gzip
        self.assertEqual(sent["headers"].get("Accept-Encoding"), "identity")

    def test_streaming_chunked(self):
        cfg = self.make_config()
        self.start(cfg)
        status, headers, data = self.request(
            cfg, "POST", "/v1/chat/completions",
            {"model": "glm-5.3", "messages": [], "stream": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Transfer-Encoding", "").lower(), "chunked")
        self.assertIn(b"data: [DONE]", data)
        self.assertIn(b'"delta":0', data)
        self.assertIn(b'"delta":2', data)

    def test_upstream_http_error_passthrough(self):
        cfg = self.make_config()
        self.start(cfg)
        status, _, data = self.request(
            cfg, "POST", "/v1/chat/completions", {"model": "boom", "messages": []}
        )
        self.assertEqual(status, 401)
        self.assertIn(b"bad key", data)

    def test_models_and_cache(self):
        cfg = self.make_config()
        self.start(cfg)
        before = self._models_calls()
        status, _, data = self.request(cfg, "GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertIn(b"mock-model", data)
        after_first = self._models_calls()
        self.request(cfg, "GET", "/v1/models")
        after_second = self._models_calls()
        self.assertEqual(after_first - before, 1)
        self.assertEqual(after_second, after_first, "第二次应命中缓存")

    def _models_calls(self):
        return sum(1 for c in self.upstream.calls if c["path"].endswith("/models"))


class TestSecurity(ProxyTestBase):
    def test_path_whitelist(self):
        cfg = self.make_config()
        self.start(cfg)
        for path in ("/v1/../../etc/passwd", "/internal/secret", "/v1/../admin"):
            status, _, _ = self.request(cfg, "POST", path, {"model": "x"})
            self.assertEqual(status, 400, path)

    def test_local_token_required(self):
        cfg = self.make_config(local_access_token="tok_abc123")
        self.start(cfg)
        status, _, _ = self.request(cfg, "POST", "/v1/chat/completions", {"model": "x"})
        self.assertEqual(status, 401)
        status, _, _ = self.request(
            cfg, "POST", "/v1/chat/completions", {"model": "glm-5.3", "messages": []},
            headers={"Authorization": "Bearer tok_abc123"},
        )
        self.assertEqual(status, 200)

    def test_host_header_rejected(self):
        cfg = self.make_config()
        self.start(cfg)
        conn = http.client.HTTPConnection("127.0.0.1", int(cfg.get("listen_port")), timeout=10)
        conn.putrequest("POST", "/v1/chat/completions", skip_host=True)
        conn.putheader("Host", "evil.example.com")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "2")
        conn.endheaders(b"{}")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 403)
        resp.read()
        conn.close()


class TestDnsDiagnosis(ProxyTestBase):
    """系统代理会把 NXDOMAIN 伪装成 SSL EOF，这里验证根因能正确还原。"""

    def test_tls_failure_upgraded_to_dns(self):
        import ssl
        import urllib.error

        from buddy_bridge.net import KIND_DNS, KIND_TLS, classify_exception

        exc = urllib.error.URLError(
            ssl.SSLEOFError("EOF occurred in violation of protocol (_ssl.c:1147)")
        )
        dead = classify_exception(
            exc, host="gw.goworkbuddy.cn", via_proxy=True, dns_ok=False
        )
        self.assertEqual(dead.kind, KIND_DNS)
        self.assertIn("不会成功", dead.hint, "DNS 已死时不应再建议切直连/查证书")
        self.assertNotIn("切换上游代理为「直连」", dead.hint)

        alive = classify_exception(
            exc, host="gw.goworkbuddy.cn", via_proxy=True, dns_ok=True
        )
        self.assertEqual(alive.kind, KIND_TLS, "DNS 正常时仍应报 TLS 握手问题")

        unknown = classify_exception(exc, host="gw.goworkbuddy.cn", via_proxy=True)
        self.assertEqual(unknown.kind, KIND_TLS, "未做预检时保持原始分类")

    def test_nxdomain_fast_fail_and_base_url_hot_reload(self):
        # .invalid 是保留域，本地 DNS 必然解析失败（不依赖外网）
        cfg = self.make_config(
            workbuddy_base_url="http://definitely-not-exists.invalid/v1",
            upstream_proxy_mode="direct",
        )
        server, _ = self.start(cfg)

        first = self.request(cfg, "POST", "/v1/chat/completions", {"model": "x"})
        self.assertEqual(first[0], 502)
        self.assertIn(b"dns", first[2])
        self.assertTrue(
            server._server.RequestHandlerClass.runtime.get("dns_confirmed_dead"),
            "确认域名不存在后应记录确定性结论",
        )

        second = self.request(cfg, "POST", "/v1/chat/completions", {"model": "x"})
        self.assertEqual(second[0], 502)
        self.assertIn(b"dns_nxdomain", second[2], "后续请求应直接快速失败，不再打上游")

        # 用户在界面上改成有效地址后应自动重新预检，无需重启代理
        cfg.set("workbuddy_base_url", f"http://127.0.0.1:{self.upstream.port}/v1")
        third = self.request(
            cfg, "POST", "/v1/chat/completions", {"model": "glm-5.3", "messages": []}
        )
        self.assertEqual(third[0], 200, "Base URL 热更新后应恢复正常转发")

    def test_dead_base_url_dropped_on_load(self):
        from buddy_bridge.config import ConfigManager, is_dead_base_url

        self.assertTrue(is_dead_base_url("https://gw.goworkbuddy.cn/v1"))
        self.assertTrue(is_dead_base_url("gw.goworkbuddy.cn/v1"))
        self.assertFalse(is_dead_base_url("https://api.deepseek.com/v1"))
        self.assertFalse(is_dead_base_url(""))

        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(
            json.dumps({
                "workbuddy_base_url": "https://gw.goworkbuddy.cn/v1",
                "workbuddy_api_key": "ck_keep_me_123456",
                "listen_port": 19001,
            }),
            encoding="utf-8",
        )
        cfg = ConfigManager(path)
        self.assertEqual(cfg.get("workbuddy_base_url"), "", "失效域名应被清空")
        self.assertEqual(cfg.dropped_dead_base_url, "https://gw.goworkbuddy.cn/v1")
        self.assertEqual(cfg.get("workbuddy_api_key"), "ck_keep_me_123456", "其他配置不应被改动")
        self.assertEqual(cfg.get("listen_port"), 19001)

    def test_network_report_flags_nxdomain(self):
        from buddy_bridge.net import network_report

        report = network_report(
            "https://definitely-not-exists.invalid/v1", proxy_mode="direct"
        )
        self.assertIn("NXDOMAIN", report)
        self.assertIn("Base URL", report)


class TestGuardrails(ProxyTestBase):
    def test_rate_limit(self):
        cfg = self.make_config(rate_limit_per_minute=1)
        self.start(cfg)
        first = self.request(cfg, "POST", "/v1/chat/completions", {"model": "glm-5.3", "messages": []})
        second = self.request(cfg, "POST", "/v1/chat/completions", {"model": "glm-5.3", "messages": []})
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 429)

    def test_circuit_breaker(self):
        # 指向一个拒绝连接的端口，模拟上游长期连不上
        dead = free_port()
        cfg = self.make_config(
            workbuddy_base_url=f"http://127.0.0.1:{dead}/v1",
            circuit_breaker_threshold=3,
            circuit_breaker_cooldown=60,
        )
        self.start(cfg)
        statuses = []
        for _ in range(5):
            status, _, _ = self.request(cfg, "POST", "/v1/chat/completions", {"model": "x"})
            statuses.append(status)
        self.assertEqual(statuses[:3], [502, 502, 502])
        self.assertEqual(statuses[3], 503, "达到阈值后应熔断")
        self.assertEqual(statuses[4], 503)

    def test_health_endpoint(self):
        cfg = self.make_config()
        self.start(cfg)
        status, _, data = self.request(cfg, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertIn(b'"status": "ok"', data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
