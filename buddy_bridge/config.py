"""配置管理：持久化用户的 WorkBuddy 凭据与代理设置。"""

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    # WorkBuddy 侧
    "workbuddy_base_url": "https://gw.goworkbuddy.cn/v1",
    "workbuddy_api_key": "",
    # 本地代理侧
    "listen_host": "127.0.0.1",
    "listen_port": 18789,
    # 兼容性翻译开关
    "translate_developer_to_system": True,
    "translate_max_completion_tokens": True,
    "strip_deepseek_thinking_field": False,
    "use_reasoning_effort": True,
    # 运行时
    "auto_start": False,
    "log_level": "INFO",
}


class ConfigManager:
    """线程安全的 JSON 配置管理器。"""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        if config_path is None:
            config_path = Path.home() / ".buddy_bridge" / "config.json"
        self._path = Path(config_path)
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(DEFAULT_CONFIG)
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
                        self._data.update(saved)
                except (json.JSONDecodeError, OSError):
                    # 损坏的配置文件：保留默认值，不覆盖
                    pass

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._data.update(data)

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)
