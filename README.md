# BuddyBridge — WorkBuddy ⇄ DeepSeek Harness 桥接工具

一个轻量级本地代理，让你用购买了 WorkBuddy 积分的 API Key，在 DeepSeek Harness 客户端里直接使用 WorkBuddy 提供的模型（glm-5.3、deepseek-v4-pro、kimi-k3.1-pro 等）。

**不依赖 Electron**，纯 Python 标准库实现，打包后单文件约 28MB。

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
pip install pyinstaller
python build.py
```

产物在 `dist/` 目录。

## 在 DeepSeek Harness 中配置

1. 打开 DeepSeek Harness → **设置 → 模型**
2. 点击 **添加模型提供商** → 切换到 **自定义模型 API**
3. 填写：
   - **Provider ID**：`workbuddy`
   - **Base URL**：`http://127.0.0.1:18789/v1`（端口与 BuddyBridge 中一致）
   - **API 协议**：`OpenAI Chat Completions`
   - **API Key**：随便填一个（代理会自动替换成你的 WorkBuddy Key）
   - **模型**：点击「获取可用模型」自动探测，或手动填 `glm-5.3-flash` 等
4. 保存，选择该模型开始对话

## 兼容性选项说明

BuddyBridge 会自动把 DeepSeek Harness 的请求翻译成 WorkBuddy 能接受的格式：

- **developer → system**：OpenAI 新角色 `developer` 多数网关不认识，转成 `system`（默认开启）
- **max_completion_tokens → max_tokens**：OpenAI 新字段转传统字段（默认开启）
- **去除 thinking 字段**：DeepSeek 特有字段，网关报错时开启
- **保留 reasoning_effort**：保留推理强度参数

如果连接正常但请求被拒，可尝试调整这些开关。

## 文件结构

```
buddy_bridge/
  ├── config.py      # 配置持久化
  ├── logger.py      # 日志系统
  ├── translator.py  # 请求/响应翻译
  ├── proxy.py       # HTTP 代理服务器
  └── gui.py         # Tkinter 图形界面
run.py               # 启动入口
build.py             # PyInstaller 打包脚本
```
