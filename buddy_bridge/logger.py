"""轻量日志：同时输出到控制台和 GUI 回调。"""

import logging
import threading
from collections import deque
from datetime import datetime
from typing import Callable, Deque, List, Optional


class BridgeLogger:
    """环形缓冲日志器，支持 GUI 订阅新日志。"""

    def __init__(self, max_entries: int = 500) -> None:
        self._buffer: Deque[str] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._subscribers: List[Callable[[str], None]] = []
        self._level = logging.INFO
        self._setup_std_logger()

    def _setup_std_logger(self) -> None:
        self._logger = logging.getLogger("buddy_bridge")
        self._logger.setLevel(logging.DEBUG)
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            )
            self._logger.addHandler(handler)

    def set_level(self, level: int) -> None:
        self._level = level
        self._logger.setLevel(level)

    def subscribe(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def _emit(self, level: int, msg: str, *args: Any) -> None:
        if level < self._level:
            return
        if args:
            try:
                msg = msg % args
            except (TypeError, ValueError):
                msg = msg + " " + " ".join(str(a) for a in args)
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {logging.getLevelName(level)} {msg}"
        with self._lock:
            self._buffer.append(line)
            subscribers = list(self._subscribers)
        self._logger.log(level, msg)
        for cb in subscribers:
            try:
                cb(line)
            except Exception:
                pass

    def debug(self, msg: str, *args: Any) -> None:
        self._emit(logging.DEBUG, msg, *args)

    def info(self, msg: str, *args: Any) -> None:
        self._emit(logging.INFO, msg, *args)

    def warning(self, msg: str, *args: Any) -> None:
        self._emit(logging.WARNING, msg, *args)

    def error(self, msg: str, *args: Any) -> None:
        self._emit(logging.ERROR, msg, *args)

    def get_history(self) -> List[str]:
        with self._lock:
            return list(self._buffer)
