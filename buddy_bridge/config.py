"""配置管理：持久化用户的 WorkBuddy 凭据与代理设置。"""

import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse


# 旧版预置、且经公共 DNS（阿里/腾讯/Google）确认 NXDOMAIN 的地址。
# 加载到这些值会直接丢弃，避免用户以为"已经配置好了"却一直连不上。
_DEAD_BASE_URL_HOSTS = {"gw.goworkbuddy.cn"}


def is_dead_base_url(url: Any) -> bool:
    """判断 Base URL 是否指向已知失效的域名。"""
    text = str(url or "").strip()
    if not text:
        return False
    if "://" not in text:
        text = "https://" + text
    try:
        host = (urlparse(text).hostname or "").lower()
    except ValueError:
        return False
    return host in _DEAD_BASE_URL_HOSTS


DEFAULT_CONFIG: Dict[str, Any] = {
    # WorkBuddy 侧
    # 故意留空：让用户从服务方控制台填入真实地址。
    # 旧版本预置的 https://gw.goworkbuddy.cn/v1 经公共 DNS 验证为 NXDOMAIN（不存在），不能再作为默认值。
    "workbuddy_base_url": "",
    "workbuddy_api_key": "",
    "auth_header_mode": "bearer",       # bearer | x-api-key
    # 本地代理侧
    "listen_host": "127.0.0.1",
    "listen_port": 18789,
    # 网络
    "upstream_proxy_mode": "auto",      # auto | direct | custom
    "upstream_proxy_url": "",
    "verify_tls": True,
    "request_timeout": 120,
    "dns_preflight": True,
    # 兼容性翻译开关
    "translate_developer_to_system": True,
    "translate_max_completion_tokens": True,
    "strip_deepseek_thinking_field": False,
    "use_reasoning_effort": True,
    # 防风控：让流量像正常使用，而不是异常脉冲
    "stealth_mode": True,
    "spoof_user_agent": "OpenAI-Python/1.54.0",
    "rate_limit_per_minute": 60,
    "min_request_interval_ms": 300,
    "jitter_ms": 150,
    "max_concurrent_requests": 4,
    "circuit_breaker_threshold": 5,
    "circuit_breaker_cooldown": 30,
    "models_cache_ttl": 300,
    "max_body_bytes": 4 * 1024 * 1024,
    # 本地访问控制（防止代理被局域网他人白嫖）
    "local_access_token": "",
    # 运行时
    "auto_start": False,
    "log_level": "INFO",
}


def _harden(path: Path) -> None:
    """尽量把配置文件权限收紧为仅当前用户可读写。"""
    try:
        if os.name == "posix":
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        else:
            # Windows: 移除继承权限，只给当前用户完全控制。
            # CREATE_NO_WINDOW 避免 GUI 程序弹出黑色控制台。
            user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
            if not user:
                return
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F", "/Q"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                check=False,
                timeout=5,
            )
    except Exception:  # noqa: BLE001
        # 权限收紧失败不应影响配置保存
        pass


class ConfigManager:
    """线程安全的 JSON 配置管理器。"""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        if config_path is None:
            config_path = Path.home() / ".buddy_bridge" / "config.json"
        self._path = Path(config_path)
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(DEFAULT_CONFIG)
        # 加载时是否丢弃了旧版预置的失效地址（GUI 用来提示用户）
        self.dropped_dead_base_url = ""
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        with self._lock:
            if self._path.exists():
                try:
                    with self._path.open("r", encoding="utf-8") as f:
                        saved = json.load(f)
                    if isinstance(saved, dict):
                        # 只接受已知键，避免旧配置/手工编辑注入未知字段
                        self._data.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
                except (json.JSONDecodeError, OSError):
                    # 损坏的配置文件：保留默认值，不覆盖
                    pass
            # 旧版预置的失效域名：清空并记录，提示用户重新填写
            current = self._data.get("workbuddy_base_url", "")
            if is_dead_base_url(current):
                self.dropped_dead_base_url = str(current)
                self._data["workbuddy_base_url"] = ""

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            _harden(tmp)
            os.replace(tmp, self._path)
            _harden(self._path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._data.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)
