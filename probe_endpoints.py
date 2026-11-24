#!/usr/bin/env python3
"""候选 Base URL 连通性探测（纯标准库，**不发送任何 API Key**）。

用途：当 BuddyBridge 反复报 SSL EOF / 连接失败时，用这个脚本区分
「域名根本不存在」与「域名通但需要正确的 Key / 认证头」。

用法：
    python probe_endpoints.py                       # 探测内置候选列表
    python probe_endpoints.py https://your/v1 ...    # 只探测自定义地址
    python probe_endpoints.py --direct              # 强制直连（忽略系统代理）

说明：
- 所有请求都是不带凭据的 GET，不会产生计费，也不写任何配置文件。
- HTTP 401/403/404 都算「能连通」，只是缺凭据或路径不对；
  真正的问题是 DNS 失败 / TLS 握手失败 / 连接超时。
- 探测结果只是网络层结论，Key 是否有效请在 GUI 填好后点「网络诊断」确认。
"""

from __future__ import annotations

import socket
import sys
import urllib.error
import urllib.request
from typing import List, Tuple
from urllib.parse import urlparse

from buddy_bridge.net import (
    build_opener,
    classify_exception,
    current_proxies,
    dns_probe,
    tls_probe,
)

# (名称, Base URL, 来源 / 适用 Key, 是否可作为 BuddyBridge 的 Base URL)
CANDIDATES: List[Tuple[str, str, str, bool]] = [
    ("旧默认值（对照）", "https://gw.goworkbuddy.cn/v1",
     "BuddyBridge 旧版预置；公共 DNS 验证为 NXDOMAIN，预期全部失败", False),
    ("腾讯云 Coding Plan", "https://api.lkeap.cloud.tencent.com/coding/v3",
     "腾讯云官方文档；套餐专属 Key 格式 sk-sp-xxxx", True),
    ("ModelArk Coding Plan", "https://ark.ap-southeast.bytepluses.com/api/coding/v3",
     "BytePlus WorkBuddy 接入文档（海外区）；支持 deepseek-v4-flash / glm-5.3 等模型", True),
    ("DeepSeek 官方", "https://api.deepseek.com/v1",
     "DeepSeek 开放平台；Key 格式 sk-xxxx", True),
    ("智谱 GLM Coding Plan", "https://open.bigmodel.cn/api/coding/paas/v4",
     "智谱 Coding Plan；Key 为开放平台 API Key", True),
    ("Kimi 开放平台", "https://api.moonshot.cn/v1",
     "月之暗面开放平台；Key 格式 sk-xxxx", True),
    ("MiniMax 开放平台", "https://api.minimaxi.com/v1",
     "MiniMax 开放平台", True),
    ("CodeBuddy 官方入口", "https://copilot.tencent.com",
     "CodeBuddy / WorkBuddy 产品域名；走自有 Chat 协议，官方未公开 OpenAI 兼容端点，不能当 Base URL", False),
]


def probe_one(url: str, opener: urllib.request.OpenerDirector, via_proxy: bool) -> dict:
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = parsed.hostname or ""
    scheme = parsed.scheme or "https"
    port = parsed.port or (443 if scheme == "https" else 80)

    ok_dns, dns_detail = dns_probe(host)
    if scheme == "https":
        ok_tls, tls_detail = tls_probe(host, port)
    else:
        ok_tls, tls_detail = True, "跳过（非 https）"

    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("Accept-Encoding", "identity")
        with opener.open(req, timeout=8) as resp:
            http = f"HTTP {getattr(resp, 'status', 0) or resp.getcode()}（有响应）"
    except urllib.error.HTTPError as exc:
        http = f"HTTP {exc.code}（能连通，缺凭据或路径不同属正常）"
    except Exception as exc:  # noqa: BLE001
        f = classify_exception(exc, host=host, via_proxy=via_proxy, dns_ok=ok_dns)
        http = f"FAIL [{f.kind}] {f.summary}"

    if not ok_dns:
        verdict = "✗ 域名不存在（NXDOMAIN）——任何代理设置都连不上"
    elif http.startswith("HTTP"):
        verdict = "✓ 链路可达（能否作为 Base URL 取决于协议兼容性与 Key，见结尾汇总）"
    elif not ok_tls:
        verdict = "△ 域名能解析但 TLS 握手失败，检查代理 / 端口"
    else:
        verdict = "△ 未取得 HTTP 响应，看上面失败项"

    return {
        "url": url,
        "host": host,
        "dns": ("OK  " if ok_dns else "FAIL") + " " + dns_detail,
        "tls": ("OK  " if ok_tls else "FAIL") + " " + tls_detail,
        "http": http,
        "verdict": verdict,
    }


def main() -> int:
    args = [a for a in sys.argv[1:]]
    direct = "--direct" in args
    args = [a for a in args if a != "--direct"]

    if args:
        items = [(f"自定义 {i + 1}", u, "命令行传入", True) for i, u in enumerate(args)]
    else:
        items = CANDIDATES

    proxies = current_proxies()
    proxy_desc = proxies.get("https") or proxies.get("http") or "(无)"
    mode = "direct" if direct else "auto"
    opener = build_opener(mode)
    via_proxy = not direct

    print("=" * 78)
    print("BuddyBridge 候选 Base URL 连通性探测（不发送任何 API Key）")
    print("=" * 78)
    print(f"代理策略: {'强制直连' if direct else '跟随系统/环境代理'}  |  系统代理: {proxy_desc}")
    print(f"本机 Python: {sys.version.split()[0]}  |  候选数: {len(items)}")
    print()

    results = []
    for name, url, source, usable in items:
        r = probe_one(url, opener, via_proxy)
        r["usable"] = usable
        results.append(r)
        print(f"■ {name}")
        print(f"  URL   : {url}")
        print(f"  说明  : {source}")
        print(f"  DNS   : {r['dns']}")
        print(f"  TLS   : {r['tls']}")
        print(f"  HTTP  : {r['http']}")
        print(f"  结论  : {r['verdict']}")
        print()

    reachable = [r for r in results if r["http"].startswith("HTTP") and r["usable"]]
    not_for_base = [r for r in results if r["http"].startswith("HTTP") and not r["usable"]]
    print("-" * 78)
    if reachable:
        print("能连通的候选地址（可复制进 GUI 的 Base URL 逐个试，Key 要匹配对应平台）：")
        for r in reachable:
            print(f"  {r['url']}")
    else:
        print("没有任何候选地址能连通。请检查本机网络 / 系统代理设置。")

    if not_for_base:
        print()
        print("域名能通但不能作为 Base URL 的地址（不是 OpenAI 兼容端点，填了也不会成功）：")
        for r in not_for_base:
            print(f"  {r['url']}")

    dead = [r for r in results if r["dns"].startswith("FAIL")]
    if dead:
        print()
        print("域名解析失败的候选（这些地址无论怎么改代理都不可能连上）：")
        for r in dead:
            print(f"  {r['url']}")

    print()
    print("注意：腾讯云 Coding Plan 条款明确限制套餐 Key 只能用于指定编程工具，")
    print("      禁止用于自建代理 / 自动化脚本 / 非交互式批量调用；请确认你的使用方式合规。")
    print("      Key 是否有效、认证头选 Bearer 还是 X-Api-Key，请在 GUI 填好后点「网络诊断」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
