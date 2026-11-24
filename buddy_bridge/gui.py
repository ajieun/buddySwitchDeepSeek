"""Tkinter 图形界面：配置 WorkBuddy 凭据并控制本地代理。"""

import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from .config import ConfigManager
from .logger import BridgeLogger
from .net import network_report
from .proxy import ProxyServer
from .safety import generate_token


AUTH_MODES = [
    ("Bearer (Authorization)", "bearer"),
    ("X-Api-Key (官方 API Key 认证)", "x-api-key"),
]
PROXY_MODES = [
    ("跟随系统代理 (auto)", "auto"),
    ("直连，忽略系统代理 (direct)", "direct"),
    ("自定义代理 (custom)", "custom"),
]

INSTRUCTIONS = """DeepSeek Harness → 设置 → 模型 → 添加自定义模型 API：
Provider ID 填 workbuddy，Base URL 填 http://127.0.0.1:<端口>/v1，
API 协议选 OpenAI Chat Completions，API Key 留空/随便填（设了本地令牌则填令牌），
模型点「获取可用模型」探测或手填 glm-5.3-flash。"""


class BridgeGUI:
    def __init__(self) -> None:
        self.config = ConfigManager()
        self.logger = BridgeLogger()
        self.server = ProxyServer(self.config, self.logger)

        self.root = tk.Tk()
        self.root.title("BuddyBridge — WorkBuddy ⇄ DeepSeek Harness 桥接工具")
        self.root.geometry("860x820")
        self.root.minsize(760, 700)

        self._build_widgets()
        self._load_config_to_form()
        self.logger.subscribe(self._on_log)
        self._refresh_status()

        dropped = getattr(self.config, "dropped_dead_base_url", "")
        if dropped:
            self.logger.warning(
                "已丢弃配置文件里失效的 Base URL：%s（域名不存在，NXDOMAIN）。请重新填写真实地址。",
                dropped,
            )
            self.root.after(400, lambda: messagebox.showwarning(
                "Base URL 已失效",
                f"配置文件里的 Base URL 已自动清空：\n{dropped}\n\n"
                "该域名经公共 DNS 验证为 NXDOMAIN（不存在），填它必然报 SSL EOF，"
                "切换代理模式也无效。\n\n"
                "请到你的 Key 获取页面核对真实地址，或运行\n"
                "python probe_endpoints.py\n探测候选端点后重新填写。",
            ))

        if self.config.get("auto_start", False):
            self.root.after(300, self.toggle_server)

    # ---- 界面构建 ----
    def _build_widgets(self) -> None:
        # --- 顶部风险提示横幅 ---
        warn = tk.Frame(self.root, bg="#fff3cd")
        warn.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(
            warn,
            text="⚠ 请只使用官方渠道获取的 API Key。代理默认仅监听 127.0.0.1，并对上游限速/熔断。"
                 "若 Base URL 域名无效，系统代理会把 DNS 失败伪装成 SSL EOF，请先点「网络诊断」。",
            background="#fff3cd", foreground="#856404", wraplength=790, justify="left",
        ).pack(padx=10, pady=6)

        # --- 分页容器：把长表单拆开，避免窗口超过屏幕高度 ---
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(6, 0))
        self._build_tab_connection(nb)
        self._build_tab_network(nb)
        self._build_tab_guard(nb)

        # --- 控制按钮 ---
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", padx=10, pady=6)
        self.btn_toggle = ttk.Button(ctrl, text="启动代理", command=self.toggle_server)
        self.btn_toggle.pack(side="left")
        ttk.Button(ctrl, text="保存配置", command=self.save_config).pack(side="left", padx=(8, 0))
        ttk.Button(ctrl, text="网络诊断", command=self.run_diagnosis).pack(side="left", padx=(8, 0))

        self.var_status = tk.StringVar(value="状态：未启动")
        self.lbl_status = ttk.Label(ctrl, textvariable=self.var_status, foreground="#666")
        self.lbl_status.pack(side="left", padx=(16, 0))

        # --- 日志区 ---
        log_frame = ttk.LabelFrame(self.root, text="运行日志", padding=6)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self.txt_log = scrolledtext.ScrolledText(
            log_frame, height=8, wrap="word", font=("Consolas", 9)
        )
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.configure(state="disabled")

        # --- 底部指引（精简，详细排障见 README） ---
        ttk.Label(
            self.root, text=INSTRUCTIONS, justify="left", foreground="#555", wraplength=790,
        ).pack(fill="x", padx=12, pady=(0, 8))

    def _build_tab_connection(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="连接")

        top = ttk.LabelFrame(tab, text="WorkBuddy 配置", padding=10)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Label(top, text="Base URL:").grid(row=0, column=0, sticky="w", pady=3)
        self.var_base_url = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_base_url, width=58).grid(
            row=0, column=1, columnspan=2, sticky="we", pady=3, padx=(8, 0)
        )

        ttk.Label(top, text="API Key:").grid(row=1, column=0, sticky="w", pady=3)
        self.var_api_key = tk.StringVar()
        self.ent_api_key = ttk.Entry(top, textvariable=self.var_api_key, width=58, show="•")
        self.ent_api_key.grid(row=1, column=1, sticky="we", pady=3, padx=(8, 0))
        self.var_show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top, text="显示", variable=self.var_show_key, command=self._toggle_key_visibility,
        ).grid(row=1, column=2, padx=(8, 0))

        ttk.Label(top, text="认证头:").grid(row=2, column=0, sticky="w", pady=3)
        self.cmb_auth = ttk.Combobox(
            top, values=[x[0] for x in AUTH_MODES], state="readonly", width=32
        )
        self.cmb_auth.grid(row=2, column=1, sticky="w", pady=3, padx=(8, 0))
        ttk.Label(top, text="官方 Key→X-Api-Key，兼容网关→Bearer", foreground="#666").grid(
            row=2, column=2, sticky="w", padx=(8, 0)
        )
        top.columnconfigure(1, weight=1)

        ttk.Label(
            tab,
            text="Base URL 必须是服务方控制台给出的真实地址；旧版预置的 "
                 "gw.goworkbuddy.cn 是不存在的域名（NXDOMAIN），填它必然报 SSL EOF。"
                 "不确定时运行 python probe_endpoints.py 探测候选端点。",
            foreground="#a33", wraplength=780, justify="left",
        ).pack(fill="x", padx=12, pady=(0, 6))

        mid = ttk.LabelFrame(tab, text="本地代理与兼容性", padding=10)
        mid.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Label(mid, text="监听地址:").grid(row=0, column=0, sticky="w", pady=2)
        self.var_host = tk.StringVar()
        ttk.Entry(mid, textvariable=self.var_host, width=16).grid(
            row=0, column=1, sticky="w", pady=2, padx=(8, 0)
        )
        ttk.Label(mid, text="端口:").grid(row=0, column=2, sticky="e", pady=2, padx=(16, 0))
        self.var_port = tk.StringVar()
        ttk.Entry(mid, textvariable=self.var_port, width=8).grid(
            row=0, column=3, sticky="w", pady=2, padx=(8, 0)
        )
        ttk.Label(
            mid, text="保持 127.0.0.1 最安全；改 0.0.0.0 会自动生成令牌", foreground="#666",
        ).grid(row=0, column=4, columnspan=2, sticky="w", padx=(12, 0))

        options = [
            ("var_dev_to_sys", "developer 角色 → system"),
            ("var_max_tokens", "max_completion_tokens → max_tokens"),
            ("var_strip_thinking", "去除 thinking 字段"),
            ("var_reasoning", "保留 reasoning_effort"),
            ("var_auto_start", "启动时自动开启代理"),
        ]
        for i, (attr, text) in enumerate(options):
            var = tk.BooleanVar()
            setattr(self, attr, var)
            r, c = divmod(i, 2)
            ttk.Checkbutton(mid, text=text, variable=var).grid(
                row=1 + r, column=c * 3, columnspan=3, sticky="w", pady=1
            )

    def _build_tab_network(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="网络与安全")

        box = ttk.LabelFrame(tab, text="上游连接", padding=10)
        box.pack(fill="x", padx=8, pady=8)

        ttk.Label(box, text="上游代理:").grid(row=0, column=0, sticky="w", pady=3)
        self.cmb_proxy = ttk.Combobox(
            box, values=[x[0] for x in PROXY_MODES], state="readonly", width=28
        )
        self.cmb_proxy.grid(row=0, column=1, sticky="w", pady=3, padx=(8, 0))
        ttk.Label(box, text="自定义代理:").grid(row=0, column=2, sticky="e", pady=3, padx=(16, 0))
        self.var_proxy_url = tk.StringVar()
        ttk.Entry(box, textvariable=self.var_proxy_url, width=26).grid(
            row=0, column=3, sticky="w", pady=3, padx=(8, 0)
        )
        ttk.Label(
            box,
            text="遇到 SSL: UNEXPECTED_EOF_WHILE_READING 时，先把上游代理切到「直连」再诊断。",
            foreground="#666", wraplength=740, justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        self.var_verify_tls = tk.BooleanVar()
        ttk.Checkbutton(
            box, text="校验 TLS 证书（不建议关闭）", variable=self.var_verify_tls,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
        self.var_dns_preflight = tk.BooleanVar()
        ttk.Checkbutton(
            box, text="启动时做 DNS 预检（提前发现无效域名）", variable=self.var_dns_preflight,
        ).grid(row=2, column=2, columnspan=2, sticky="w", pady=2)

        sec = ttk.LabelFrame(tab, text="本地访问控制", padding=10)
        sec.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Label(sec, text="本地访问令牌:").grid(row=0, column=0, sticky="w", pady=3)
        self.var_local_token = tk.StringVar()
        ttk.Entry(sec, textvariable=self.var_local_token, width=52).grid(
            row=0, column=1, sticky="w", pady=3, padx=(8, 0)
        )
        ttk.Button(sec, text="生成", width=8, command=self._generate_local_token).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )
        ttk.Label(
            sec,
            text="留空 = 仅本机可用。设置后客户端 API Key 必须填该令牌，防止局域网他人借用你的 Key。",
            foreground="#666", wraplength=740, justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        sec.columnconfigure(1, weight=1)

    def _build_tab_guard(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="防风控")

        box = ttk.LabelFrame(tab, text="隐身与流量整形", padding=10)
        box.pack(fill="x", padx=8, pady=8)

        self.var_stealth = tk.BooleanVar()
        ttk.Checkbutton(
            box, text="隐身模式：伪装 User-Agent、去除 Origin/Referer/X- 头",
            variable=self.var_stealth,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=2)

        ttk.Label(box, text="伪装 UA:").grid(row=1, column=0, sticky="w", pady=2)
        self.var_spoof_ua = tk.StringVar()
        ttk.Entry(box, textvariable=self.var_spoof_ua, width=36).grid(
            row=1, column=1, columnspan=2, sticky="w", pady=2, padx=(8, 0)
        )
        ttk.Label(box, text="速率(次/分):").grid(row=1, column=3, sticky="e", pady=2, padx=(12, 0))
        self.var_rate_limit = tk.StringVar()
        ttk.Entry(box, textvariable=self.var_rate_limit, width=7).grid(
            row=1, column=4, sticky="w", pady=2, padx=(8, 0)
        )

        rows = [
            ("最小间隔(ms):", "var_min_interval"),
            ("抖动(ms):", "var_jitter"),
            ("最大并发:", "var_concurrency"),
        ]
        for i, (label, attr) in enumerate(rows):
            ttk.Label(box, text=label).grid(row=2, column=i * 2, sticky="e", pady=2)
            var = tk.StringVar()
            setattr(self, attr, var)
            ttk.Entry(box, textvariable=var, width=7).grid(
                row=2, column=i * 2 + 1, sticky="w", pady=2, padx=(6, 0)
            )

        rows2 = [
            ("熔断阈值(次):", "var_cb_threshold"),
            ("冷却(秒):", "var_cb_cooldown"),
            ("模型缓存(秒):", "var_cache_ttl"),
        ]
        for i, (label, attr) in enumerate(rows2):
            ttk.Label(box, text=label).grid(row=3, column=i * 2, sticky="e", pady=2)
            var = tk.StringVar()
            setattr(self, attr, var)
            ttk.Entry(box, textvariable=var, width=7).grid(
                row=3, column=i * 2 + 1, sticky="w", pady=2, padx=(6, 0)
            )

        ttk.Label(
            tab,
            text="说明：以上设置让请求节奏更接近正常使用 —— 限速 + 抖动摊平突发流量，"
                 "连续失败时熔断，避免客户端每秒重试形成请求风暴；模型列表缓存减少无谓探测。\n"
                 "这些措施只降低异常流量特征，不承诺「不被封」，请仍遵守平台条款、避免高频批量调用。",
            foreground="#666", wraplength=760, justify="left",
        ).pack(fill="x", padx=16, pady=(0, 8))

    # ---- 配置读写 ----
    @staticmethod
    def _lookup(pairs, value):
        for label, key in pairs:
            if key == value:
                return label
        return pairs[0][0]

    @staticmethod
    def _reverse(pairs, label, default):
        for text, key in pairs:
            if text == label:
                return key
        return default

    def _int(self, var: tk.StringVar, default: int) -> int:
        try:
            return int(var.get().strip())
        except (ValueError, AttributeError):
            return default

    def _load_config_to_form(self) -> None:
        self.var_base_url.set(self.config.get("workbuddy_base_url", ""))
        self.var_api_key.set(self.config.get("workbuddy_api_key", ""))
        self.var_host.set(self.config.get("listen_host", "127.0.0.1"))
        self.var_port.set(str(self.config.get("listen_port", 18789)))
        self.var_dev_to_sys.set(self.config.get("translate_developer_to_system", True))
        self.var_max_tokens.set(self.config.get("translate_max_completion_tokens", True))
        self.var_strip_thinking.set(self.config.get("strip_deepseek_thinking_field", False))
        self.var_reasoning.set(self.config.get("use_reasoning_effort", True))
        self.var_auto_start.set(self.config.get("auto_start", False))
        self.var_stealth.set(self.config.get("stealth_mode", True))
        self.var_spoof_ua.set(self.config.get("spoof_user_agent", "OpenAI-Python/1.54.0"))
        self.var_rate_limit.set(str(self.config.get("rate_limit_per_minute", 60)))
        self.cmb_auth.set(self._lookup(AUTH_MODES, self.config.get("auth_header_mode", "bearer")))
        self.cmb_proxy.set(self._lookup(PROXY_MODES, self.config.get("upstream_proxy_mode", "auto")))
        self.var_proxy_url.set(self.config.get("upstream_proxy_url", ""))
        self.var_local_token.set(self.config.get("local_access_token", ""))
        self.var_verify_tls.set(self.config.get("verify_tls", True))
        self.var_dns_preflight.set(self.config.get("dns_preflight", True))
        self.var_min_interval.set(str(self.config.get("min_request_interval_ms", 300)))
        self.var_jitter.set(str(self.config.get("jitter_ms", 150)))
        self.var_concurrency.set(str(self.config.get("max_concurrent_requests", 4)))
        self.var_cb_threshold.set(str(self.config.get("circuit_breaker_threshold", 5)))
        self.var_cb_cooldown.set(str(self.config.get("circuit_breaker_cooldown", 30)))
        self.var_cache_ttl.set(str(self.config.get("models_cache_ttl", 300)))

    def _collect_form(self) -> None:
        self.config.update({
            "workbuddy_base_url": self.var_base_url.get().strip(),
            "workbuddy_api_key": self.var_api_key.get().strip(),
            "listen_host": self.var_host.get().strip() or "127.0.0.1",
            "listen_port": self._int(self.var_port, 18789),
            "auth_header_mode": self._reverse(AUTH_MODES, self.cmb_auth.get(), "bearer"),
            "upstream_proxy_mode": self._reverse(PROXY_MODES, self.cmb_proxy.get(), "auto"),
            "upstream_proxy_url": self.var_proxy_url.get().strip(),
            "verify_tls": bool(self.var_verify_tls.get()),
            "dns_preflight": bool(self.var_dns_preflight.get()),
            "local_access_token": self.var_local_token.get().strip(),
            "translate_developer_to_system": self.var_dev_to_sys.get(),
            "translate_max_completion_tokens": self.var_max_tokens.get(),
            "strip_deepseek_thinking_field": self.var_strip_thinking.get(),
            "use_reasoning_effort": self.var_reasoning.get(),
            "auto_start": self.var_auto_start.get(),
            "stealth_mode": self.var_stealth.get(),
            "spoof_user_agent": self.var_spoof_ua.get().strip() or "OpenAI-Python/1.54.0",
            "rate_limit_per_minute": self._int(self.var_rate_limit, 60),
            "min_request_interval_ms": self._int(self.var_min_interval, 300),
            "jitter_ms": self._int(self.var_jitter, 150),
            "max_concurrent_requests": max(1, self._int(self.var_concurrency, 4)),
            "circuit_breaker_threshold": max(1, self._int(self.var_cb_threshold, 5)),
            "circuit_breaker_cooldown": max(1, self._int(self.var_cb_cooldown, 30)),
            "models_cache_ttl": max(0, self._int(self.var_cache_ttl, 300)),
        })

    def save_config(self) -> None:
        self._collect_form()
        self.config.save()
        self.logger.info("配置已保存到 %s（已尽量收紧文件权限）", self.config.path)
        messagebox.showinfo("已保存", f"配置已保存到：\n{self.config.path}")

    def _generate_local_token(self) -> None:
        token = generate_token()
        self.var_local_token.set(token)
        self.logger.info("已生成本地访问令牌：%s（请在客户端 API Key 字段填入）", token)

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
                token = self.config.get("local_access_token", "")
                if token:
                    self.var_local_token.set(token)
            else:
                messagebox.showerror("启动失败", msg)
        self._refresh_status()

    # ---- 网络诊断 ----
    def run_diagnosis(self) -> None:
        self._collect_form()
        self.logger.info("正在运行网络诊断（不会使用 API Key 发起计费请求）...")

        def _do() -> None:
            report = network_report(
                base_url=str(self.config.get("workbuddy_base_url", "")),
                api_key=str(self.config.get("workbuddy_api_key", "")),
                proxy_mode=str(self.config.get("upstream_proxy_mode", "auto")),
                proxy_url=str(self.config.get("upstream_proxy_url", "")),
                verify_tls=bool(self.config.get("verify_tls", True)),
                request_timeout=8.0,
            )
            self.root.after(0, lambda: self._show_report(report))

        threading.Thread(target=_do, daemon=True).start()

    def _show_report(self, report: str) -> None:
        for line in report.splitlines():
            if line.strip():
                self._append_log("[诊断] " + line)
        win = tk.Toplevel(self.root)
        win.title("网络诊断结果")
        win.geometry("760x440")
        win.transient(self.root)
        txt = scrolledtext.ScrolledText(win, wrap="word", font=("Consolas", 10))
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("1.0", report)
        txt.configure(state="disabled")

        def _copy() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(report)
            self.logger.info("诊断报告已复制到剪贴板")

        ttk.Button(win, text="复制报告", command=_copy).pack(pady=(0, 8))

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
