# BuddyBridge

这个仓库现在有两条线，先看你需要哪条：

| 你想做的事 | 用哪个 | 说明 |
|---|---|---|
| **在浏览器里操作 codebuddy / 任意终端，不用记命令，英文自动翻中文** | **`bb`** | 新主线，见 [bb：浏览器遥控终端](#bb浏览器遥控终端) |
| 用 `sk-` 开头的第三方 Key，把本代理当 OpenAI 兼容端点给别的客户端用 | `run.py` | 旧的 HTTP 代理，见下文 |

---

# bb：浏览器遥控终端

终端还是终端，界面只是遥控器。`codebuddy` 在真正的伪终端里跑，
浏览器打开的是同一份画面的镜像，多出来的只是"可以点的按钮"和"右侧的中文译文"。

```bash
pip install pywinpty          # Windows 必须；Linux/macOS 用系统自带 pty，不需要
python bb.py codebuddy        # 或 python bb.py 直接开 shell
```

打开后你会看到：左边是 codebuddy 的真实 TUI，右边是英文提示的中文翻译，
底部是 `Esc / Tab / Enter / ↑↓ / Ctrl+C` 按钮和一个输入框——点按钮等于敲键，输入框回车等于敲一行。

**为什么必须用 `bb` 启动，而不是接管已经开着的窗口？**
Windows 的 ConPTY 句柄只属于创建它的进程，没有公开 API 让第三个进程插进去。
tmux 能 attach 是因为它从终端创建那一刻就持有 PTY。所以"从创建时持有"是唯一路径。

完整设计、实测数据与边界说明见 [`docs/DESIGN-v3.md`](docs/DESIGN-v3.md)。

**翻译说明**：内置词典覆盖 CLI 高频提示（权限询问、常见报错），零延迟零成本；
想翻长句可以再配一个 OpenAI 兼容端点：

```bash
set BB_TRANSLATE_URL=https://api.deepseek.com/v1
set BB_TRANSLATE_KEY=sk-xxxx
set BB_TRANSLATE_MODEL=deepseek-chat
```

**为什么译文在右边，而不是直接替换终端里的英文？**
终端是字符画布，TUI 靠光标跳转重绘。把 `Do you want to proceed?` 换成 `是否继续？`，
宽度从 24 列变 5 列，后面的边框、表格、光标全部错位。所以原文一个字不动，译文旁路显示。

---

# BuddyBridge HTTP 代理（旧版主线）

一个轻量级本地代理，让你用购买了 WorkBuddy 积分的 API Key，在 DeepSeek Harness 客户端里直接使用 WorkBuddy 提供的模型（glm-5.3、deepseek-v4-pro、kimi-k3.1-pro 等）。

**不依赖 Electron**，纯 Python 标准库实现（Python 3.8+），打包后单文件约 12MB。

> **重要：旧版预置的 `gw.goworkbuddy.cn` 是一个不存在的域名。**
> 2026-11-23 在阿里 223.5.5.5、腾讯 119.29.29.29、Google 8.8.8.8 及本机 DNS 上
> 均返回 NXDOMAIN。日志里的 `SSL: UNEXPECTED_EOF_WHILE_READING` 并不是 TLS 配置坏了，
> 而是**系统代理在 CONNECT 隧道阶段把连不上的域名直接掐断**，Python 只能看到握手被提前关闭。
> 新版已把默认 Base URL 改成空，并在预检判定域名不存在时直接给出确定性结论、停止重试。
> 先运行 `python probe_endpoints.py` 找出真实可用的地址，或点主界面的「网络诊断」复核。
> **代码无法连上一个不存在的服务**——这一步必须由你到 Key 的获取页面核对。

## 快速开始

### 方式一：直接运行打包好的可执行文件

1. 下载对应平台的 `BuddyBridge` 可执行文件（`dist/` 目录）
2. 双击运行
3. 填写 WorkBuddy 的 Base URL 和 API Key
4. 点击「启动代理」
5. 在 DeepSeek Harness 中配置自定义模型（见下方）

### 方式二：用 Python 直接运行（需 Python 3.8+）

- Windows：双击 `启动 BuddyBridge.bat`
- macOS：双击 `启动 BuddyBridge.command`（首次需在终端 `chmod +x "启动 BuddyBridge.command"`）
- Linux：`python3 run.py`

### 方式三：自己打包为单文件

```bash
python build.py
```

产物在 `dist/BuddyBridge.exe`。

`build.py` 会自动检查并安装缺失的 PyInstaller，但有两个前提：

- **必须用带 tkinter 的完整 Python**。Windows 上就是官网安装的
  `C:\Users\<你>\AppData\Local\Programs\Python\Python3xx\python.exe`；
  WorkBuddy 自带的精简解释器没有 tkinter，打出来的 exe 会一闪就退。
  缺 tkinter 时脚本会直接报错退出，不会白打一个坏包。
- 想锁定解释器时用完整路径调用：
  `"C:\Users\<你>\AppData\Local\Programs\Python\Python315\python.exe" build.py`

常用参数：`--onedir`（目录模式，启动更快）、`--no-install`（缺 PyInstaller 时只提示不自动装）。

## 在 DeepSeek Harness 中配置

1. 打开 DeepSeek Harness → **设置 → 模型**
2. 点击 **添加模型提供商** → 切换到 **自定义模型 API**
3. 填写：
   - **Provider ID**：`workbuddy`
   - **Base URL**：`http://127.0.0.1:18789/v1`（端口与 BuddyBridge 中一致）
   - **API 协议**：`OpenAI Chat Completions`
   - **API Key**：未设「本地访问令牌」时随便填（代理会替换成你的 WorkBuddy Key）；设了令牌就填该令牌
   - **模型**：点击「获取可用模型」自动探测，或手动填 `glm-5.3-flash` 等
4. 保存，选择该模型开始对话

## 兼容性选项说明

BuddyBridge 会自动把 DeepSeek Harness 的请求翻译成 WorkBuddy 能接受的格式：

- **developer → system**：OpenAI 新角色 `developer` 多数网关不认识，转成 `system`（默认开启）
- **max_completion_tokens → max_tokens**：OpenAI 新字段转传统字段（默认开启）
- **去除 thinking 字段**：DeepSeek 特有字段，网关报错时开启
- **保留 reasoning_effort**：保留推理强度参数

如果连接正常但请求被拒，可尝试调整这些开关。

## 连不上时先做什么：网络诊断

点「网络诊断」，它会按顺序检查并给出一份可复制的报告：

1. Base URL 解析出的主机与端口
2. 当前生效的上游代理策略（`auto` 会带上 Windows 注册表的系统代理）
3. 本地 DNS 能否解析该域名
4. 绕过代理直连做一次 TLS 握手
5. 按当前代理策略实际 GET 一次 `/v1/models`（**不带 API Key**，不会计费）
6. 结论与下一步建议

### `SSL: UNEXPECTED_EOF_WHILE_READING` 的成因对照

| 诊断结果 | 真实原因 | 处理 |
|---|---|---|
| `[4] 本地 DNS 解析 FAIL` | 域名不存在（NXDOMAIN），Base URL 写错了 | **改 Base URL**。此时切代理、等冷却、查证书全都无效，代理会给出 `dns_nxdomain` 结论并停止重试 |
| `[6] FAIL -> [proxy]` | 系统代理 CONNECT 隧道失败 | 上游代理切「直连」，或修好代理软件 |
| `[5] 直连 TLS OK`、`[6] FAIL` | 代理掐断了隧道 | 切「直连」 |
| `[6] HTTP 401/403` | 能连通，认证有问题 | 换「认证头」为 `X-Api-Key` 或核对 Key |
| `[6] HTTP 429` | 调得太频繁 | 降低速率、增大最小间隔 |

### 代理策略

- **auto**（默认）：跟随环境变量与 Windows 系统代理设置。适合本来就靠代理上网的环境。
- **direct**：`ProxyHandler({})` 强制忽略一切代理。**仅在域名能正常解析时**才值得用它排代理问题。
- **custom**：手动指定 `http://127.0.0.1:port`。

> 注意：如果启动预检已判定域名不存在，日志会明确写「任何代理设置都连不上」，
> 此时不要再去切换 `direct` / `custom`——本地 DNS 解析不了，直连同样解析不了。

`auto` 模式下，如果系统代理把隧道掐断、而本地 DNS 又能正常解析，代理会自动改用直连重试一次，
成功后本次运行都走直连。

## Base URL 应该填什么

旧版把 `https://gw.goworkbuddy.cn/v1` 预置成默认值，这是**错误**的：该域名经阿里、腾讯、
Google 公共 DNS 验证均为 NXDOMAIN，WorkBuddy / CodeBuddy 官方文档里也没有这个网关。
新版把默认值改成空，必须自己填真实地址。

不确定填什么时，先跑一次无凭据探测（**不发送任何 Key**，不会计费）：

```bash
python probe_endpoints.py
# 或只测某个地址：python probe_endpoints.py https://你的地址/v1
# 强制直连：      python probe_endpoints.py --direct
```

它会逐个报 DNS / TLS / HTTP 结果，并把能连通的候选地址列出来。常见官方端点：

| 服务 | Base URL | Key 格式 |
|---|---|---|
| 腾讯云 Coding Plan | `https://api.lkeap.cloud.tencent.com/coding/v3` | `sk-sp-xxxx` |
| ModelArk Coding Plan（WorkBuddy 接入文档） | `https://ark.ap-southeast.bytepluses.com/api/coding/v3` | 套餐页 API Key |
| DeepSeek 官方 | `https://api.deepseek.com/v1` | `sk-xxxx` |
| 智谱 GLM Coding Plan | `https://open.bigmodel.cn/api/coding/paas/v4` | 开放平台 Key |
| Kimi / MiniMax | `https://api.moonshot.cn/v1`、`https://api.minimaxi.com/v1` | `sk-xxxx` |

**关于 `ck_` 开头的 Key**：官方 IAM 文档说明 CodeBuddy / WorkBuddy 的产品 Key 用
`X-Api-Key` 请求头认证，从 `copilot.tencent.com/profile/` 获取；产品网关走自有 Chat 协议，
**官方没有公开供第三方客户端直接调用的 OpenAI 兼容 `/v1/chat/completions` 端点**。
`copilot.tencent.com` 能 ping 通不等于能当 Base URL 用。如果你的 `ck_` Key 没有配套的
Coding Plan 网关地址，那么这个代理在当前架构下无法用这个 Key 工作——这是前提条件问题，
不是代码能修的。请到 Key 的获取页面核对它对应的服务地址与协议。

**条款风险（重要）**：腾讯云 Coding Plan 条款明确写着套餐 Key **仅限在指定编程工具中使用，
禁止用于自动化脚本、自定义应用后端、非交互式批量调用**，违规可能导致订阅暂停或 Key 被封禁。
把套餐 Key 接进自建代理给其他客户端用，本身就落在受限范围内。限速、熔断、UA 伪装都规避不了
这一条——所以本项目不承诺「不被封」，请先确认自己的使用方式合规。

## 安全与防风控说明

这些设置的目标是**让流量表现得像正常使用、出错时不形成异常风暴**，而不是伪造身份去骗过检测。
请仍遵守平台条款，不要高频、并发地跑批量任务。

**凭据安全**

- 代理默认只监听 `127.0.0.1`；改成 `0.0.0.0` 等非回环地址时会**自动生成访问令牌**，
  否则局域网里任何人都能用你的 Key。
- 校验 `Host` 头，只接受本机地址，防止 DNS rebinding 从网页借用代理。
- 上游路径白名单（仅 `/v1/*` 等），拒绝 `//evil.com`、`../` 等，避免被当作任意转发器。
- 日志与错误响应统一脱敏 `Bearer` / `ck_` / `sk-` 形态的密钥。
- 配置文件保存时尽量收紧权限（POSIX `chmod 600`，Windows `icacls` 仅当前用户）。
- 请求体大小上限（默认 4MB），防止内存被打爆。

**流量整形**

- 滑动窗口限速 + 最小请求间隔 + 随机抖动，避免整点脉冲式请求。
- 连续连接失败达到阈值后**熔断**（默认 5 次 / 冷却 30 秒），
  挡住客户端「每秒重试一次」的请求风暴——这恰恰是最容易被风控盯上的行为。
- 同时在途请求数上限（默认 4）。
- `/v1/models` 结果缓存（默认 5 分钟），避免反复探测模型列表。
- 上游返回 429/5xx 时自动临时降速，冷却后逐步恢复。
- 明确发送 `Accept-Encoding: identity`，避免上游 gzip 响应导致客户端解析失败。

## 文件结构

```
bb.py                # ★ 浏览器遥控终端入口（新主线）
buddy_bridge/
  ├── pty_host.py    # ★ PTY 抽象：Windows ConPTY / POSIX pty
  ├── webterm.py     # ★ HTTP + WebSocket + 会话中心
  ├── translate.py   # ★ ANSI 剥离 / 行提取 / 词典与模型翻译
  ├── config.py      # 配置持久化（含权限收紧）
  ├── logger.py      # 日志系统
  ├── net.py         # 网络层：错误分类、代理策略、网络诊断、脱敏
  ├── safety.py      # 限流/熔断/并发/缓存/路径校验/访问令牌
  ├── translator.py  # 请求/响应翻译
  ├── proxy.py       # HTTP 代理服务器（旧版主线）
  └── gui.py         # Tkinter 图形界面（旧版主线）
web/index.html       # ★ 前端：xterm.js + 译文栏 + 按键面板
bb.spec              # ★ bb 的 PyInstaller 配置（含 web/ 目录）
run.py               # 旧版 GUI 代理入口
build.py             # PyInstaller 打包脚本
probe_endpoints.py   # 候选 Base URL 无凭据连通性探测（排障用）
docs/DESIGN-v3.md    # bb 的设计说明、实测数据与边界
tests/               # 回归测试：python -m unittest discover -s tests
```
