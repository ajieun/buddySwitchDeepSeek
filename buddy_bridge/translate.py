"""终端输出的英译中：ANSI 剥离 → 行提取 → 词典/模型翻译。

为什么不做"直接把终端里的英文换成中文"
------------------------------------
终端是**字符画布**而不是文本流。全屏 TUI（codebuddy、vim、less）靠光标跳转
重绘整屏，字符位置就是布局本身。把英文替换成中文会让宽度对不上，
整个边框、表格、光标位置全部错位。

所以这里的定位是**旁路翻译**：原始终端一个字都不改，另开一栏显示译文。
用户的视觉动线是"看左边原文，不懂就看右边"，不会破坏任何 TUI。

翻译策略（省钱 + 快）
--------------------
1. **词典优先**：CLI 的高频提示是固定句式（"Do you want to proceed?"），
   内置对照表直接命中，零延迟零成本。
2. **模型兜底**：未命中且配置了模型端点时才发起请求，结果进 LRU 缓存。
3. **批量合并**：短时间内多行一起送，减少请求次数。
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Dict, List, Optional, Sequence

# ---------------------------------------------------------------- ANSI 处理

# CSI / OSC / 单字符转义：覆盖光标移动、颜色、清屏、窗口标题等
_ANSI_RE = re.compile(
    r"""
    \x1b\[[0-?]*[ -/]*[@-~]      # CSI 序列
  | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)   # OSC 序列
  | \x1b[@-Z\\-_]               # 单字符转义
    """,
    re.VERBOSE,
)

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 像命令/路径/URL 的行，翻译只会帮倒忙
_COMMAND_START = re.compile(
    r"^\s*[\$>#]\s*|^\s*(?:npm|npx|pnpm|yarn|git|pip|python|python3|node|deno|"
    r"curl|wget|cd|ls|dir|rm|del|mkdir|cp|mv|cat|type|echo|export|set|"
    r"docker|kubectl|sudo|apt|brew|choco|codebuddy|bb)\b",
    re.IGNORECASE,
)
_PATHY = re.compile(
    r"(?:/[\w.\-]+){2,}|@[\w\-]+/[\w\-]+|--?[a-z][\w-]*(?:=|\s|$)|"
    r"\bhttps?://|\\\\[\w.$]+\\|[A-Za-z]:\\",
)
_URL_RE = re.compile(r"https?://\S+")


def strip_ansi(text: str) -> str:
    """去掉终端控制序列，只留可读字符。"""
    if not text:
        return ""
    return _CTRL_RE.sub("", _ANSI_RE.sub("", text))


def _english_words(text: str) -> int:
    """粗略统计英文单词数（用于判断译文里还剩多少没翻）。"""
    return len(re.findall(r"\b[A-Za-z][A-Za-z'\-]{1,}\b", text))


def is_translatable(line: str) -> bool:
    """判断一行是否值得翻译。

    过滤掉命令、路径、URL、纯符号、代码片段、已含中文的行。
    """
    s = line.strip()
    if len(s) < 12 or len(s) > 400:
        return False

    # 命令行 / 路径 / URL 直接跳过
    if _COMMAND_START.match(s):
        return False

    # URL 占了大半行时，翻它没有任何意义
    url = _URL_RE.search(s)
    if url and len(url.group(0)) / len(s) > 0.45:
        return False

    letters = sum(1 for c in s if c.isascii() and c.isalpha())
    nonspace = sum(1 for c in s if not c.isspace())
    if letters < 8 or nonspace == 0:
        return False
    ratio = letters / nonspace
    if ratio < 0.45:
        return False

    # 路径/参数太多且字母占比不高 → 更像日志或命令
    if _PATHY.search(s) and ratio < 0.68:
        return False

    # 中文已占多数就没必要翻
    cjk = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    if cjk * 2 > len(s):
        return False

    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", s)
    if len(words) < 3:
        return False
    # 至少要有小写词（全大写的多是常量/表头）
    if not any(w.islower() for w in words):
        return False

    # 要有"句子信号"：多个词 + 空格，或句末标点
    if len(words) < 4 and not s.endswith((".", "?", "!", ":", "...")):
        return False
    return True


class LineCollector:
    """把 PTY 的字节流切成"完整的行"。

    处理要点：``\\r`` 是回到行首覆盖（进度条/TUI 常用），所以同一行内
    出现 ``\\r`` 时只保留最后一段。
    """

    def __init__(self, max_buffer: int = 8192) -> None:
        self._buf = ""
        self._max = max_buffer

    def feed(self, chunk: str) -> List[str]:
        if not chunk:
            return []
        self._buf += self._normalize(chunk)
        if len(self._buf) > self._max:
            self._buf = self._buf[-self._max:]

        out: List[str] = []
        while "\n" in self._buf:
            raw, self._buf = self._buf.split("\n", 1)
            line = self._pick_line(raw)
            if line:
                out.append(line)
        return out

    def flush(self) -> List[str]:
        line = self._pick_line(self._buf)
        self._buf = ""
        return [line] if line else []

    @staticmethod
    def _pick_line(raw: str) -> str:
        # \r 覆盖：取最后一个非空片段
        if "\r" in raw:
            parts = [p for p in raw.split("\r") if p.strip()]
            raw = parts[-1] if parts else ""
        return strip_ansi(raw).strip()

    @staticmethod
    def _normalize(chunk: str) -> str:
        # 终端里换行是 \r\n，统一成 \n 处理
        return chunk.replace("\r\n", "\n")


# ---------------------------------------------------------------- 内置词典

# 高频 CLI / agent 提示。按"整句"优先，其次按"片段"。
_PHRASES: Dict[str, str] = {
    "do you want to proceed": "是否继续？",
    "do you want to allow": "是否允许？",
    "do you want to continue": "是否继续？",
    "are you sure": "确定吗？",
    "yes, and don't ask again": "是，且不再询问",
    "yes, allow once": "是，仅本次允许",
    "no, and tell the model what to do differently": "否，并告诉模型换个做法",
    "permission denied": "权限被拒绝",
    "permission required": "需要授权",
    "access denied": "访问被拒绝",
    "command not found": "找不到命令",
    "no such file or directory": "文件或目录不存在",
    "file not found": "文件不存在",
    "connection refused": "连接被拒绝",
    "connection reset": "连接被重置",
    "operation timed out": "操作超时",
    "request timed out": "请求超时",
    "network is unreachable": "网络不可达",
    "too many requests": "请求过于频繁",
    "internal server error": "服务器内部错误",
    "service unavailable": "服务不可用",
    "unauthorized": "未授权",
    "forbidden": "禁止访问",
    "not found": "未找到",
    "invalid api key": "API Key 无效",
    "rate limit exceeded": "已超出速率限制",
    "context window": "上下文窗口",
    "out of memory": "内存不足",
    "syntax error": "语法错误",
    "unexpected token": "意外的符号",
    "traceback (most recent call last)": "调用栈（最近一次调用在最下方）",
    "warning": "警告",
    "error": "错误",
    "failed to": "失败：",
    "unable to": "无法",
    "is not a function": "不是函数",
    "undefined is not": "未定义值不是",
    "cannot read property": "无法读取属性",
    "module not found": "找不到模块",
    "install": "安装",
    "installing": "正在安装",
    "installed": "已安装",
    "loading": "加载中",
    "thinking": "思考中",
    "running": "运行中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "retrying": "正在重试",
    "waiting for": "等待",
    "press any key": "按任意键",
    "press enter": "按回车",
    "press esc": "按 Esc",
    "to continue": "以继续",
    "to cancel": "以取消",
    "to exit": "以退出",
    "allow": "允许",
    "deny": "拒绝",
    "accept": "接受",
    "reject": "拒绝",
    "approve": "批准",
    "skip": "跳过",
    "retry": "重试",
    "abort": "中止",
    "usage": "用法",
    "options": "选项",
    "description": "说明",
    "example": "示例",
    "note": "注意",
    "tip": "提示",
    "summary": "摘要",
    "changed": "已修改",
    "created": "已创建",
    "deleted": "已删除",
    "updated": "已更新",
    "reading": "读取中",
    "writing": "写入中",
    "searching": "搜索中",
    "analyzing": "分析中",
    "generating": "生成中",
    "building": "构建中",
    "compiling": "编译中",
    "testing": "测试中",
    "done": "完成",
    "ready": "就绪",
    "connected": "已连接",
    "disconnected": "已断开",
    "reconnecting": "正在重连",
    "session": "会话",
    "token": "令牌",
    "tokens": "令牌数",
    "model": "模型",
    "agent": "智能体",
    "workflow": "工作流",
    "repository": "仓库",
    "branch": "分支",
    "commit": "提交",
    "merge": "合并",
    "conflict": "冲突",
    # 动词短语（用于模板捕获组二次替换）
    "connect to": "连接到",
    "write to": "写入",
    "read from": "读取",
    "parse": "解析",
    "resolve": "解析",
    "fetch": "获取",
    "execute": "执行",
    "load": "加载",
    "save": "保存",
    "apply": "应用",
    "create": "创建",
    "update": "更新",
    "delete": "删除",
    "remove": "移除",
    "check": "检查",
    "validate": "校验",
    "verify": "验证",
    "generate": "生成",
    "analyze": "分析",
    "search": "搜索",
    "replace": "替换",
    "restart": "重启",
    "initialize": "初始化",
    "download": "下载",
    "upload": "上传",
    # 名词短语（模板里的宾语）
    "this action": "此操作",
    "this change": "此改动",
    "this command": "此命令",
    "this tool": "此工具",
    "this file": "此文件",
    "the server": "服务器",
    "the file": "文件",
    "the directory": "目录",
    "the repository": "仓库",
    "the model": "模型",
    "the session": "会话",
    "the request": "请求",
    "the response": "响应",
    "the command": "命令",
    "the terminal": "终端",
    "the output": "输出",
}

# 句式模板：整句匹配不上时做局部替换，保留可读性
_SENTENCE_RULES: Sequence[tuple] = (
    (re.compile(r"^\s*Would you like to (.+?)\?\s*$", re.I), r"是否要\1？"),
    (re.compile(r"^\s*Do you want to (.+?)\?\s*$", re.I), r"是否要\1？"),
    (re.compile(r"^\s*Are you sure you want to (.+?)\?\s*$", re.I), r"确定要\1吗？"),
    (re.compile(r"^\s*Failed to (.+)$", re.I), r"失败：\1"),
    (re.compile(r"^\s*Unable to (.+)$", re.I), r"无法\1"),
    (re.compile(r"^\s*Error[:\s]+(.+)$", re.I), r"错误：\1"),
    (re.compile(r"^\s*Warning[:\s]+(.+)$", re.I), r"警告：\1"),
    (re.compile(r"^\s*Note[:\s]+(.+)$", re.I), r"注意：\1"),
    (re.compile(r"^\s*Running (.+?)\.\.\.\s*$", re.I), r"正在执行 \1…"),
    (re.compile(r"^\s*Loading (.+?)\.\.\.\s*$", re.I), r"正在加载 \1…"),
    (re.compile(r"^\s*(\d+) files? changed", re.I), r"\1 个文件已修改"),
)


def glossary_lookup(text: str) -> Optional[str]:
    """词典查找。整句优先，其次片段替换。

    命中返回译文；完全不沾边返回 None（交给模型）。
    """
    s = text.strip().rstrip()
    low = s.lower().strip(" .:!?")

    # 1. 整句精确命中
    if low in _PHRASES:
        return _PHRASES[low]

    # 2. 句式模板
    for pattern, repl in _SENTENCE_RULES:
        if pattern.match(s):
            partial = pattern.sub(repl, s, count=1)
            return _apply_fragments(partial)

    # 3. 片段替换（命中数够多才算，否则交给模型）
    replaced, hits = _replace_fragments(s)
    if hits >= 2 or (hits == 1 and len(s) < 90):
        return replaced

    # 4. 单个短语+标点的整行
    if hits == 1:
        return replaced
    return None


def _apply_fragments(text: str) -> str:
    return _replace_fragments(text)[0]


_CJK_CLASS = "\u4e00-\u9fff，。、；：？！（）「」“”"
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK_CLASS}])\s+(?=[{_CJK_CLASS}])")
_SPACE_BEFORE_PUNCT = re.compile(rf"\s+(?=[，。、；：？！）])")


def _tidy(text: str) -> str:
    """清理替换后产生的中文间多余空格。"""
    out = _SPACE_BETWEEN_CJK.sub("", text)
    out = _SPACE_BEFORE_PUNCT.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def _replace_fragments(text: str):
    """按词边界替换片段，返回（结果, 命中次数）。"""
    hits = 0
    out = text
    for src, dst in sorted(_PHRASES.items(), key=lambda kv: -len(kv[0])):
        if " " in src or src.isalpha():
            pattern = re.compile(r"(?<![A-Za-z])" + re.escape(src) + r"(?![A-Za-z])", re.I)
            new, n = pattern.subn(dst, out)
            if n:
                out = new
                hits += n
    return _tidy(out), hits


# ---------------------------------------------------------------- 翻译器


class Translator:
    """英译中。词典优先，未命中才走模型；结果全部缓存。"""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout: int = 20,
        cache_size: int = 800,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or "gpt-4o-mini"
        self.timeout = timeout
        self._cache: "OrderedDict[str, str]" = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.RLock()
        self._stats = {"hits": 0, "glossary": 0, "model": 0, "failed": 0}

    # -------------------------------------------------------------- 能力
    @property
    def model_enabled(self) -> bool:
        return bool(self.base_url)

    @property
    def enabled(self) -> bool:
        return True  # 词典永远可用

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)

    # -------------------------------------------------------------- 缓存
    def _cache_get(self, text: str) -> Optional[str]:
        with self._lock:
            val = self._cache.get(text)
            if val is not None:
                self._cache.move_to_end(text)
            return val

    def _cache_put(self, text: str, result: str) -> None:
        with self._lock:
            self._cache[text] = result
            self._cache.move_to_end(text)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    # -------------------------------------------------------------- 主入口
    def translate(self, text: str) -> Optional[str]:
        """翻译一行。返回 None 表示无需/无法翻译。"""
        src = (text or "").strip()
        if not src:
            return None

        cached = self._cache_get(src)
        if cached is not None:
            with self._lock:
                self._stats["hits"] += 1
            return cached

        hit = glossary_lookup(src)
        if hit is not None and self._accept_glossary(hit):
            self._cache_put(src, hit)
            with self._lock:
                self._stats["glossary"] += 1
            return hit

        if not self.model_enabled:
            # 没有模型可退，词典结果再糙也比纯英文强
            if hit is not None:
                self._cache_put(src, hit)
                return hit
            return None

        result = self._translate_remote([src])
        if result and result[0]:
            self._cache_put(src, result[0])
            with self._lock:
                self._stats["model"] += 1
            return result[0]
        if hit is not None:
            # 模型失败，退回词典结果
            self._cache_put(src, hit)
            return hit
        with self._lock:
            self._stats["failed"] += 1
        return None

    @staticmethod
    def _accept_glossary(result: str) -> bool:
        """词典结果里残留太多英文词就认为没翻干净，交给模型。"""
        return _english_words(result) < 2

    def translate_many(self, lines: Sequence[str]) -> List[Optional[str]]:
        """批量翻译。已缓存的走缓存，剩下的合并成一次请求。"""
        out: List[Optional[str]] = []
        pending: List[int] = []
        pending_texts: List[str] = []

        for i, raw in enumerate(lines):
            src = (raw or "").strip()
            if not src:
                out.append(None)
                continue
            cached = self._cache_get(src)
            if cached is not None:
                out.append(cached)
                with self._lock:
                    self._stats["hits"] += 1
                continue
            hit = glossary_lookup(src)
            if hit is not None and self._accept_glossary(hit):
                self._cache_put(src, hit)
                with self._lock:
                    self._stats["glossary"] += 1
                out.append(hit)
                continue
            if not self.model_enabled:
                out.append(hit)
                continue
            out.append(None)
            if self.model_enabled:
                pending.append(i)
                pending_texts.append(src)

        if pending and pending_texts:
            results = self._translate_remote(pending_texts)
            for slot, res in zip(pending, results):
                if res:
                    self._cache_put(lines[slot].strip(), res)
                    out[slot] = res
                    with self._lock:
                        self._stats["model"] += 1
        return out

    # -------------------------------------------------------------- 远端
    def _translate_remote(self, lines: List[str]) -> List[Optional[str]]:
        """调用 OpenAI 兼容接口批量翻译。失败返回全 None，不抛异常。"""
        if not self.model_enabled or not lines:
            return [None] * len(lines)

        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines))
        system = (
            "你是终端输出翻译器。把用户给的英文（可能是命令行报错、AI 助手回复、"
            "权限询问）译成简洁准确的中文。要求：\n"
            "1. 保持技术术语原样（如 token、commit、branch、API、JSON、stdout），不要硬翻；\n"
            "2. 保留原始换行与编号；\n"
            "3. 只输出译文，每行对应原文一行，格式为「序号. 译文」；\n"
            "4. 不要解释、不要加注、不要输出多余内容。"
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": numbered},
            ],
            "temperature": 0.2,
            "stream": False,
        }

        url = self.base_url
        if not url.endswith("/chat/completions"):
            url = url + "/chat/completions"

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, OSError):
            return [None] * len(lines)

        content = ""
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return [None] * len(lines)

        return _parse_numbered(content, len(lines))


_NUMBERED_RE = re.compile(r"^\s*(\d+)\s*[\.\)、]\s*(.+?)\s*$")


def _parse_numbered(text: str, expect: int) -> List[Optional[str]]:
    """把「1. 译文」格式解析回列表。缺失项填 None。"""
    out: List[Optional[str]] = [None] * expect
    for line in text.splitlines():
        m = _NUMBERED_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < expect:
            out[idx] = m.group(2).strip()
    # 全部没解析出来但只有一行时，直接把整段当译文
    if expect == 1 and out[0] is None:
        cleaned = text.strip()
        if cleaned:
            out[0] = cleaned
    return out
