"""Tkinter 图形界面：配置 WorkBuddy 凭据并控制本地代理。"""

import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Optional

from .config import ConfigManager
from .logger import BridgeLogger
from .proxy import ProxyServer


INSTRUCTIONS = """使用方法：
1. 在上方填写 WorkBuddy 的 Base URL 和 API Key（去 goworkbuddy.cn 控制台复制）
2. 按需勾选兼容性选项（默认即可）
3. 点击「启动代理」
4. 打开 DeepSeek Harness → 设置 → 模型 → 添加自定义模型 API
   - Provider ID: workbuddy
   - Base URL: http://127.0.0.1:<端口>/v1
   - API 协议: OpenAI Chat Completions
   - API Key: 随便填（代理会替换成你的 WorkBuddy Key）
   - 模型: 点「获取可用模型」自动探测，或手动填 glm-5.3-flash 等
5. 在 DeepSeek Harness 中选择该模型开始对话即可"""


class BridgeGUI:
    def __init__(self) -> None:
        self.config = ConfigManager()
        self.logger = BridgeLogger()
        self.server = ProxyServer(self.config, self.logger)

        self.root = tk.Tk()
        self.root.title("BuddyBridge — WorkBuddy ⇄ DeepSeek Harness 桥接工具")
        self.root.geometry("760x640")
        self.root.minsize(680, 560)

        self._build_widgets()
        self._load_config_to_form()
        self.logger.subscribe(self._on_log)
        self._refresh_status()

        if self.config.get("auto_start", False):
            self.root.after(300, self.toggle_server)

    # ---- 界面构建 ----
    def _build_widgets(self) -> None:
        pad = {"padx": 10, "pady": 6}

        # --- 顶部风险提示横幅 ---
        warn = tk.Frame(self.root, bg="#fff3cd")
        warn.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(
            warn,
            text="⚠ 账号安全提示：WorkBuddy API Key 仅限会员创建。请使用官方渠道获取的 Key，"
                 "本工具通过隐身模式降低风控风险，但仍需遵守平台规则，请勿高频调用。",
            background="#fff3cd", foreground="#856404", wraplength=720, justify="left",
        ).pack(padx=10, pady=6)

        # --- 顶部：WorkBuddy 配置 ---
        top = ttk.LabelFrame(self.root, text="WorkBuddy 配置", padding=10)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Base URL:").grid(row=0, column=0, sticky="w", pady=3)
        self.var_base_url = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_base_url, width=60).grid(
            row=0, column=1, sticky="we", pady=3, padx=(8, 0)
        )

        ttk.Label(top, text="API Key:").grid(row=1, column=0, sticky="w", pady=3)
        self.var_api_key = tk.StringVar()
        self.ent_api_key = ttk.Entry(top, textvariable=self.var_api_key, width=60, show="•")
        self.ent_api_key.grid(row=1, column=1, sticky="we", pady=3, padx=(8, 0))
        self.var_show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top, text="显示", variable=self.var_show_key,
            command=self._toggle_key_visibility,
        ).grid(row=1, column=2, padx=(8, 0))

        top.columnconfigure(1, weight=1)

        # --- 中部：本地代理与兼容性 ---
        mid = ttk.LabelFrame(self.root, text="本地代理与兼容性", padding=10)
        mid.pack(fill="x", **pad)

        ttk.Label(mid, text="监听端口:").grid(row=0, column=0, sticky="w", pady=3)
        self.var_port = tk.StringVar()
        ttk.Entry(mid, textvariable=self.var_port, width=12).grid(
            row=0, column=1, sticky="w", pady=3, padx=(8, 0)
        )

        self.var_dev_to_sys = tk.BooleanVar()
        ttk.Checkbutton(
            mid, text="developer 角色 → system（多数网关需要）",
            variable=self.var_dev_to_sys,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=2)

        self.var_max_tokens = tk.BooleanVar()
        ttk.Checkbutton(
            mid, text="max_completion_tokens → max_tokens",
            variable=self.var_max_tokens,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=2)

        self.var_strip_thinking = tk.BooleanVar()
        ttk.Checkbutton(
            mid, text="去除 thinking 字段（DeepSeek 特有）",
            variable=self.var_strip_thinking,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=2)

        self.var_reasoning = tk.BooleanVar()
        ttk.Checkbutton(
            mid, text="保留 reasoning_effort 字段",
            variable=self.var_reasoning,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=2)

        self.var_auto_start = tk.BooleanVar()
        ttk.Checkbutton(
            mid, text="启动时自动开启代理",
            variable=self.var_auto_start,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=2)

        # --- 防风控（隐身）---
        stealth = ttk.LabelFrame(self.root, text="防风控（隐身模式）", padding=10)
        stealth.pack(fill="x", **pad)

        self.var_stealth = tk.BooleanVar()
        ttk.Checkbutton(
            stealth, text="隐身模式：伪装 User-Agent、去除 Origin/Referer/X- 头（降低封号风险，推荐开启）",
            variable=self.var_stealth,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=2)

        ttk.Label(stealth, text="伪装 UA:").grid(row=1, column=0, sticky="w", pady=3)
        self.var_spoof_ua = tk.StringVar()
        ttk.Entry(stealth, textvariable=self.var_spoof_ua, width=40).grid(
            row=1, column=1, sticky="w", pady=3, padx=(8, 0)
        )

        ttk.Label(stealth, text="速率限制(次/分):").grid(row=1, column=2, sticky="e", pady=3, padx=(16, 0))
        self.var_rate_limit = tk.StringVar()
        ttk.Entry(stealth, textvariable=self.var_rate_limit, width=8).grid(
            row=1, column=3, sticky="w", pady=3, padx=(8, 0)
        )
        stealth.columnconfigure(1, weight=1)

        # --- 控制按钮 ---
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", **pad)

        self.btn_toggle = ttk.Button(ctrl, text="启动代理", command=self.toggle_server)
        self.btn_toggle.pack(side="left")

        ttk.Button(ctrl, text="保存配置", command=self.save_config).pack(side="left", padx=(8, 0))
        ttk.Button(ctrl, text="测试连接", command=self.test_connection).pack(side="left", padx=(8, 0))

        self.var_status = tk.StringVar(value="状态：未启动")
        self.lbl_status = ttk.Label(ctrl, textvariable=self.var_status, foreground="#666")
        self.lbl_status.pack(side="left", padx=(16, 0))

        # --- 日志区 ---
        log_frame = ttk.LabelFrame(self.root, text="运行日志", padding=8)
        log_frame.pack(fill="both", expand=True, **pad)

        self.txt_log = scrolledtext.ScrolledText(log_frame, height=12, wrap="word")
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.configure(state="disabled")

        # --- 底部使用说明 ---
        instr = ttk.LabelFrame(self.root, text="DeepSeek Harness 配置指引", padding=8)
        instr.pack(fill="x", **pad)
        ttk.Label(instr, text=INSTRUCTIONS, justify="left", foreground="#444").pack(anchor="w")

    # ---- 配置读写 ----
    def _load_config_to_form(self) -> None:
        self.var_base_url.set(self.config.get("workbuddy_base_url", ""))
        self.var_api_key.set(self.config.get("workbuddy_api_key", ""))
        self.var_port.set(str(self.config.get("listen_port", 18789)))
        self.var_dev_to_sys.set(self.config.get("translate_developer_to_system", True))
        self.var_max_tokens.set(self.config.get("translate_max_completion_tokens", True))
        self.var_strip_thinking.set(self.config.get("strip_deepseek_thinking_field", False))
        self.var_reasoning.set(self.config.get("use_reasoning_effort", True))
        self.var_auto_start.set(self.config.get("auto_start", False))
        self.var_stealth.set(self.config.get("stealth_mode", True))
        self.var_spoof_ua.set(self.config.get("spoof_user_agent", "OpenAI-Python/1.54.0"))
        self.var_rate_limit.set(str(self.config.get("rate_limit_per_minute", 60)))

    def _collect_form(self) -> None:
        try:
            port = int(self.var_port.get().strip())
        except ValueError:
            port = 18789
        try:
            rate_limit = int(self.var_rate_limit.get().strip())
        except ValueError:
            rate_limit = 60
        self.config.update({
            "workbuddy_base_url": self.var_base_url.get().strip(),
            "workbuddy_api_key": self.var_api_key.get().strip(),
            "listen_port": port,
            "translate_developer_to_system": self.var_dev_to_sys.get(),
            "translate_max_completion_tokens": self.var_max_tokens.get(),
            "strip_deepseek_thinking_field": self.var_strip_thinking.get(),
            "use_reasoning_effort": self.var_reasoning.get(),
            "auto_start": self.var_auto_start.get(),
            "stealth_mode": self.var_stealth.get(),
            "spoof_user_agent": self.var_spoof_ua.get().strip() or "OpenAI-Python/1.54.0",
            "rate_limit_per_minute": rate_limit,
        })

    def save_config(self) -> None:
        self._collect_form()
        self.config.save()
        self.logger.info("配置已保存到 %s", self.config.path)
        messagebox.showinfo("已保存", f"配置已保存到：\n{self.config.path}")

    # ---- 代理控制 ----
    def toggle_server(self) -> None:
        if self.server.is_running:
            self.server.stop()
            self.btn_toggle.configure(text="启动代理")
        else:
            self._collect_form()
            if not self.config.get("workbuddy_api_key"):
                messagebox.showwarning("缺少 API Key", "请先填写 WorkBuddy API Key")
                return
            if not self.config.get("workbuddy_base_url"):
                messagebox.showwarning("缺少 Base URL", "请先填写 WorkBuddy Base URL")
                return
            ok, msg = self.server.start()
            self.logger.info(msg)
            if ok:
                self.btn_toggle.configure(text="停止代理")
            else:
                messagebox.showerror("启动失败", msg)
        self._refresh_status()

    def test_connection(self) -> None:
        self._collect_form()
        if not self.config.get("workbuddy_api_key"):
            messagebox.showwarning("缺少 API Key", "请先填写 WorkBuddy API Key")
            return
        self.logger.info("正在测试 WorkBuddy 连接...")

        def _do_test() -> None:
            import json
            import urllib.request

            base = self.config.get("workbuddy_base_url", "").rstrip("/")
            url = base + ("/models" if base.endswith("/v1") else "/v1/models")
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", f"Bearer {self.config.get('workbuddy_api_key')}")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("id") for m in data.get("data", [])]
                self.logger.info("连接成功！可用模型: %s", ", ".join(models) if models else "(无)")
                self.root.after(
                    0, lambda: messagebox.showinfo(
                        "连接成功",
                        f"WorkBuddy 连接成功！\n可用模型：\n" + "\n".join(models) if models else "连接成功，但未返回模型列表。",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error("连接失败: %s", exc)
                self.root.after(0, lambda: messagebox.showerror("连接失败", str(exc)))

        threading.Thread(target=_do_test, daemon=True).start()

    # ---- 状态与日志刷新 ----
    def _refresh_status(self) -> None:
        if self.server.is_running:
            port = self.config.get("listen_port", 18789)
            self.var_status.set(f"状态：运行中  →  http://127.0.0.1:{port}/v1")
            self.lbl_status.configure(foreground="#0a7d2e")
        else:
            self.var_status.set("状态：未启动")
            self.lbl_status.configure(foreground="#666")
        self.root.after(1000, self._refresh_status)

    def _on_log(self, line: str) -> None:
        # logger 在工作线程调用，需切回主线程更新 UI
        self.root.after(0, lambda: self._append_log(line))

    def _append_log(self, line: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", line + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _toggle_key_visibility(self) -> None:
        self.ent_api_key.configure(show="" if self.var_show_key.get() else "•")

    # ---- 启动 ----
    def run(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self) -> None:
        if self.server.is_running:
            self.server.stop()
        self._collect_form()
        self.config.save()
        self.root.destroy()


def main() -> None:
    BridgeGUI().run()


if __name__ == "__main__":
    main()
