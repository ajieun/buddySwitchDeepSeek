# 项目长期笔记 — BuddyBridge

## 当前主线（v3，分支 `feat/cli-workbench`）：`bb` 浏览器遥控终端

用户真实需求（多轮澄清后确定）：**终端是唯一执行场所，界面只是遥控器**。
`codebuddy` 在真 PTY 里跑，TUI/颜色/光标都在；浏览器是同一画面的镜像 + 可点按钮 + 右栏中文译文。

- 入口：`python bb.py codebuddy` / `python bb.py`（默认 shell）
- 核心模块：`pty_host.py`（PTY 抽象）/ `webterm.py`（HTTP+WS）/ `translate.py`（旁路翻译）/ `web/index.html`
- **硬约束**：Windows ConPTY 无"接管已有终端"的 API，只能从创建时持有 → 必须 `bb` 前缀启动
- **翻译必须旁路**：终端是字符画布，替换英文会毁掉 TUI 布局 → 原文不动，右栏显示译文
- 词典优先（零成本），未命中才发模型请求；模型端点用 `BB_TRANSLATE_URL/KEY/MODEL`
- 设计说明：`docs/DESIGN-v3.md`

### 两个必须记住的 PTY 坑（已修，勿回退）
1. **先起读泵，再启子进程**。反了 ConPTY 会丢弃未读缓冲，短命命令输出整段丢失。
2. `UpdateProcThreadAttribute` 的 `lpValue` 必须是**指向 HPCON 的指针**；传句柄值本身会让绑定静默失效，
   子进程输出漏到父进程。

### 其他既定事实
- `codebuddy` 不在 PATH（npm bin shim 缺失），`resolve_command` 回退到
  `~/.workbuddy/binaries/node/versions/*/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy`
- 带 `HTTP_PROXY` 跑 CLI 会**静默卡死**（`setProxy` TypeError）→ `pty_host.clean_env` 强制注入
  `NO_PROXY=127.0.0.1,localhost,::1`
- Windows 需 `pip install pywinpty`；POSIX 用标准库 pty 零依赖
- 官方 `codebuddy --serve` 自带完整 Web UI（129 路径/156 操作）→ 不做替代品，只做遥控器

## v2（旧主线，代码保留）：HTTP 代理
纯标准库本地代理，把 API Key 桥接给 OpenAI 兼容客户端。
`config.py` / `net.py` / `safety.py` / `translator.py` / `proxy.py` / `gui.py`。
前提已不成立（`ck_` Key 对应的网关讲自有协议，官方无 OpenAI 兼容端点），保留给自带 `sk-` Key 的场景。

## 关键约定
- **代理策略**：`upstream_proxy_mode` = auto | direct | custom；出错必须走 `net.classify_exception` 给可读结论
- **防风控边界**：只做流量整形与凭据安全，不强化身份伪装；不承诺"不被封"
- **默认安全**：只监听 127.0.0.1；非回环自动要令牌；Host 校验；路径白名单；日志脱敏

## 已知坑
- `gw.goworkbuddy.cn` 是 NXDOMAIN；系统代理会把 DNS 失败伪装成
  `SSL: UNEXPECTED_EOF_WHILE_READING` —— 先查 DNS，别怀疑 TLS
- 内置 Python（3.13）无 tkinter 也无 pywinpty；GUI 与 bb 调试用系统 Python 3.15
- 改源码后 `dist/` 的 exe 不会自动更新

## 测试
`python -m unittest discover -s tests`（48 项；bb 相关需 Python 3.15 + pywinpty）
