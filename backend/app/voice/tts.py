"""TTS 引擎：CosyVoice2 / F5-TTS zero-shot 克隆，失败一律降级到 ``tone`` 占位后端。

契约见 ``docs/CONTRACTS.md`` §4。

分层
----
* ``normalize_for_tts`` / ``split_for_tts`` —— **纯函数**，可单测，负责把 LLM 输出的
  文本变成"适合朗读的英文"。
* ``ToneSynthesizer``（``app.voice.tone_tts``）—— 无模型兜底，保证任何机器上链路都通。
* ``TTSEngine`` —— 后端探测/加载/串行化/分块/重试/降级。

后端探测顺序（``backend="auto"``）：``cosyvoice2`` → ``f5tts`` → ``tone``。
探测只看"依赖是否 importable + 模型目录是否存在"，**不加载权重**（契约 §0），
所以 ``engines.snapshot()`` 里读 ``backend_name`` 是廉价操作。

产出约定
--------
- 采样率统一 **24000 Hz**，mono，PCM16 小端。
- 每句按 ``pipeline.audio_chunk_ms``（默认 200 ms）切片，``is_first``/``is_last``
  恰好各一个。
- 同一时刻只允许一个推理在跑：实例级 ``asyncio.Semaphore(1)``。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import numpy as np

from app.config import TTSSettings
from app.logging_utils import Stopwatch, get_logger
from app.voice.audio_utils import (
    SR_OUT,
    float32_to_pcm16,
    ms_to_samples,
    read_audio_file,
    resample,
)
from app.voice.tone_tts import ToneSynthesizer

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# 错误类型
# --------------------------------------------------------------------------- #
class TTSError(RuntimeError):
    """TTS 合成/加载失败。"""


class _BackendUnavailable(TTSError):
    """后端依赖或权重缺失（可降级，不视为致命）。"""


# --------------------------------------------------------------------------- #
# 数据结构（契约 §4）
# --------------------------------------------------------------------------- #
@dataclass
class AudioChunk:
    """一句语音的一个切片。"""

    pcm16: bytes
    sample_rate: int
    index: int
    is_first: bool
    is_last: bool

    @property
    def n_samples(self) -> int:
        return len(self.pcm16) // 2

    @property
    def duration_ms(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return (self.n_samples / self.sample_rate) * 1000.0


@dataclass
class TTSSentenceRequest:
    """一句待合成的文本。"""

    text: str
    persona_id: str
    sentence_index: int = 0
    is_first_sentence: bool = False


# --------------------------------------------------------------------------- #
# 文本清洗（纯函数）
# --------------------------------------------------------------------------- #
#: markdown 粗体 ``**x**`` → 保留 x；斜体/旁白 ``*x*`` → 整段删除
_BOLD_RE = re.compile(r"\*\*([^*]+?)\*\*")
_ITALIC_STAGE_RE = re.compile(r"\*([^*]+?)\*")
_UNDERLINE_BOLD_RE = re.compile(r"__([^_]+?)__")
_MD_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]*", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^[ \t]*[-*+•][ \t]+", re.MULTILINE)
_MD_QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
#: 方括号/圆括号里的旁白（含全角）
_BRACKET_RE = re.compile(r"[\[【][^\]】]*[\]】]")
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
#: emoji / 装饰性符号 / 变体选择符 / 肤色修饰 / 组合键帽
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoticons, symbols, pictographs, supplemental
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U00002B00-\U00002BFF"   # arrows/stars
    "\U00002190-\U000021FF"   # arrows
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0001F3FB-\U0001F3FF"   # skin tone modifiers
    "\U0000200D"              # ZWJ
    "\U000020E3"              # combining keycap
    "\U00003030\U0000303D\U0000FE19"  # 〜〰 等波浪装饰
    "]+",
    flags=re.UNICODE,
)
_CURLY_QUOTES_RE = re.compile(r"[\u201c\u201d\u201e\u2033]")
_SINGLE_CURLY_QUOTES_RE = re.compile(r"[\u2018\u2019\u2032]")
#: 连续空白
_WS_RE = re.compile(r"\s+")
#: 空白 + 标点 → 只留标点
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?%])")
#: 重复标点
_REPEAT_PUNCT_RE = re.compile(r"([,.;:!?])\1+")
#: 省略号 → 逗号停顿
_ELLIPSIS_RE = re.compile(r"(?:\.{3,}|…+)")
#: 括号/引号留下的空壳
_EMPTY_PAIRS_RE = re.compile(r"\(\s*\)|\[\s*\]|“\s*”|\"\s*\"")
#: 只由标点空白组成
_ONLY_PUNCT_RE = re.compile(r"^[\s\W_]*$", re.UNICODE)

#: 常见缩写 → 朗读形式（长句优先，避免 ``e.g.`` 被 ``e.`` 之类误伤）
#: 第一项是"按整串替换"（适合 ``&amp;`` 这类非字母串），其余按词边界替换。
_ABBREV_PLAIN: tuple[tuple[str, str], ...] = (
    ("&amp;", "and"),
    ("&nbsp;", " "),
    ("&lt;", " less than "),
    ("&gt;", " greater than "),
)
_ABBREV_MAP: tuple[tuple[str, str], ...] = (
    ("e.g.", "for example"),
    ("eg.", "for example"),
    ("i.e.", "that is"),
    ("ie.", "that is"),
    ("etc.", "et cetera"),
    ("vs.", "versus"),
    ("approx.", "approximately"),
    ("no.", "number"),
    ("&", "and"),
    ("+", " plus "),
    ("=", " equals "),
    ("@", " at "),
    ("#", ""),
)

_DIGITS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
)


def _number_to_words(n: int) -> str:
    """0–999999 的英文读法；越界或负数原样返回数字字符串。"""
    if n < 0 or n > 999_999:
        return str(n)
    if n < 20:
        return _DIGITS[n]
    if n < 100:
        tens, rest = divmod(n, 10)
        return _TENS[tens] + (f"-{_DIGITS[rest]}" if rest else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        out = f"{_DIGITS[hundreds]} hundred"
        return out + (f" {_number_to_words(rest)}" if rest else "")
    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        out = f"{_number_to_words(thousands)} thousand"
        return out + (f" {_number_to_words(rest)}" if rest else "")
    return str(n)


_DOLLAR_RE = re.compile(r"\$\s*(\d{1,6})(?:\.(\d{1,2}))?")
_PERCENT_RE = re.compile(r"(\d{1,6}(?:\.\d{1,2})?)\s*%")
_TEMP_RE = re.compile(r"(\d{1,4})\s*°\s*([CF])\b")
#: 前面不能是数字或小数点，否则 ``0.5 h`` 里的 ``5 h`` 会被误替换
_NUM_UNIT_RE = re.compile(r"(?<![\d.])(\d{1,6})\s*(km|kg|cm|mm|GB|MB|KB|ms|m|s|h)\b")


def _expand_numbers(text: str) -> str:
    """金额/百分比/温度/常见单位 → 英文读法（只处理小数值，避免过度加工）。"""

    def _dollar(m: re.Match[str]) -> str:
        whole = int(m.group(1))
        cents = m.group(2)
        unit = "dollar" if whole == 1 else "dollars"
        out = f"{_number_to_words(whole)} {unit}"
        if cents and int(cents) > 0:
            c = int(cents.ljust(2, "0"))
            out += f" and {_number_to_words(c)} cents"
        return out

    def _percent(m: re.Match[str]) -> str:
        raw = m.group(1)
        if "." in raw:
            return f"{raw.replace('.', ' point ')} percent"
        return f"{_number_to_words(int(raw))} percent"

    def _temp(m: re.Match[str]) -> str:
        unit = "degrees Celsius" if m.group(2) == "C" else "degrees Fahrenheit"
        return f"{_number_to_words(int(m.group(1)))} {unit}"

    def _unit(m: re.Match[str]) -> str:
        unit_map = {
            "km": "kilometres", "kg": "kilograms", "cm": "centimetres",
            "mm": "millimetres", "m": "metres", "s": "seconds", "h": "hours",
            "GB": "gigabytes", "MB": "megabytes", "KB": "kilobytes", "ms": "milliseconds",
        }
        return f"{_number_to_words(int(m.group(1)))} {unit_map.get(m.group(2), m.group(2))}"

    text = _TEMP_RE.sub(_temp, text)
    text = _DOLLAR_RE.sub(_dollar, text)
    text = _PERCENT_RE.sub(_percent, text)
    text = _NUM_UNIT_RE.sub(_unit, text)
    return text


def normalize_for_tts(text: str) -> str:
    """把 LLM/用户文本清洗成适合朗读的英文单句（**纯函数**）。

    处理：``*动作*``/``**加粗**``/``#`` 标题/``>`` 引用/列表符/代码围栏、
    emoji、``[...]``/``(...)`` 旁白、多余引号；展开 ``e.g.``/``&``/``%``/``$5``
    这类常见缩写与符号；收敛标点与空白。

    **不做**：改大小写、纠正语法、补句号（那是上层的活）。
    """
    if not text:
        return ""
    s = str(text)

    # 1) 结构标记
    s = _CODE_FENCE_RE.sub(" ", s)
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _MD_HEADER_RE.sub("", s)
    s = _MD_QUOTE_RE.sub("", s)
    s = _MD_BULLET_RE.sub(" ", s)

    # 2) 强调 / 旁白
    s = _BOLD_RE.sub(r"\1", s)          # **really** → really
    s = _UNDERLINE_BOLD_RE.sub(r"\1", s)
    s = _ITALIC_STAGE_RE.sub(" ", s)    # *sighs* → 删除

    # 3) 括号旁白 / 表情
    s = _BRACKET_RE.sub(" ", s)
    s = _PAREN_RE.sub(" ", s)
    s = _EMOJI_RE.sub(" ", s)

    # 4) 引号：弯引号统一去掉（朗读时不需要），保留撇号
    s = _EMOJI_RE.sub(" ", s)
    s = _CURLY_QUOTES_RE.sub("", s)
    # 只把成对的弯单引号转直撇号（don’t → don't）
    s = _SINGLE_CURLY_QUOTES_RE.sub("'", s)
    s = _EMPTY_PAIRS_RE.sub(" ", s)

    # 5) 缩写 / 符号
    for src, dst in _ABBREV_PLAIN:
        if src in s:
            s = s.replace(src, dst)
    for src, dst in _ABBREV_MAP:
        if src not in s:
            continue
        if src[-1].isalpha() or src[-1] == ".":
            # 带单词边界的缩写（e.g. / etc. / vs.）
            s = re.sub(rf"(?<!\w){re.escape(src)}(?!\w)", dst, s, flags=re.IGNORECASE)
        else:
            s = s.replace(src, dst)
    # "No. 5" → "number five"（只在后面跟数字时展开，避免把"I said no."改坏）
    s = re.sub(r"\bNo\.\s*(?=\d)", "number ", s, flags=re.IGNORECASE)
    s = _expand_numbers(s)

    # 6) 标点 / 空白收敛
    s = _ELLIPSIS_RE.sub(", ", s)
    s = _REPEAT_PUNCT_RE.sub(r"\1", s)
    s = _WS_RE.sub(" ", s)
    s = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", s)
    s = s.strip(" \t\r\n,;:-–—")

    if not s or _ONLY_PUNCT_RE.match(s):
        return ""
    return s


#: 不能在其后断句的缩写（小写比较）
_NO_SPLIT_AFTER = frozenset(
    {
        "mr.", "mrs.", "ms.", "miss.", "dr.", "prof.", "st.", "jr.", "sr.", "vs.",
        "e.g.", "i.e.", "etc.", "no.", "fig.", "approx.", "u.s.", "u.k.", "a.m.",
        "p.m.", "inc.", "ltd.", "co.", "dept.", "eg.", "ie.", "cf.", "al.", "est.",
    }
)
#: 断句优先级的标点（先冒号/分号，再逗号，最后空格）
_STRONG_BREAKS = frozenset(":;")
_WEAK_BREAKS = frozenset(",")


def _is_protected_break(text: str, pos: int) -> bool:
    """``text[pos]`` 之后是否紧跟一个"不能断"的缩写。"""
    head = text[: pos + 1]
    token = head.rsplit(" ", 1)[-1].strip().lower()
    if not token:
        return False
    return token in _NO_SPLIT_AFTER


def _find_break(text: str, start: int, limit: int) -> int:
    """在 ``[start, limit)`` 里找一个尽量靠后的断点，返回切分位置（不含）。

    优先级：``: ;`` > ``,`` > 空格；同一优先级取最靠后的位置。
    找不到返回 ``-1``。
    """
    window = text[start:limit]
    if not window:
        return -1
    for charset in (_STRONG_BREAKS, _WEAK_BREAKS):
        best = -1
        for i, ch in enumerate(window):
            if ch in charset:
                abs_pos = start + i
                if _is_protected_break(text, abs_pos):
                    continue
                best = abs_pos + 1
        if best > start:
            return best
    # 空格兜底
    best = window.rfind(" ")
    if best > 0:
        abs_pos = start + best
        if not _is_protected_break(text, abs_pos):
            return abs_pos + 1
        # 缩写后的空格不能断 → 往前再找
        prev = text.rfind(" ", start, abs_pos)
        if prev > start:
            return prev + 1
    return -1


def split_for_tts(text: str, max_chars: int = 220) -> list[str]:
    """把过长文本二次切分成可朗读片段（**纯函数**）。

    Args:
        text: 已 ``normalize_for_tts`` 的文本（未清洗也能用）。
        max_chars: 单片段上限（默认 220，与 ``TTSSettings.max_chars_per_sentence`` 一致）。

    Returns:
        非空片段列表；``text`` 为空时返回 ``[]``；不超长时返回 ``[text]``。
        切分点优先 ``, ; : ``，其次空格，且**不会切在缩写之后**。
    """
    s = (text or "").strip()
    if not s:
        return []
    limit = max(20, int(max_chars))
    if len(s) <= limit:
        return [s]

    parts: list[str] = []
    pos = 0
    n = len(s)
    guard = 0
    while pos < n and guard < n + 16:
        guard += 1
        remaining = n - pos
        if remaining <= limit:
            tail = s[pos:].strip()
            if tail:
                parts.append(tail)
            break
        cut = _find_break(s, pos, pos + limit)
        if cut <= pos:
            # 没有可用断点：硬切（绝不无限循环）
            cut = min(n, pos + limit)
        piece = s[pos:cut].strip(" \t,;:")
        if piece:
            parts.append(piece)
        pos = cut
        while pos < n and s[pos] in " \t,;:":
            pos += 1

    # 二次保护：仍超长的片段按空格硬切
    out: list[str] = []
    for p in parts:
        if len(p) <= limit * 2:
            out.append(p)
            continue
        for i in range(0, len(p), limit):
            chunk = p[i: i + limit].strip()
            if chunk:
                out.append(chunk)
    return out


# --------------------------------------------------------------------------- #
# torch/numpy 边界
# --------------------------------------------------------------------------- #
def _to_numpy_audio(obj: Any) -> tuple[np.ndarray, int | None]:
    """把 ``torch.Tensor`` / list / ndarray 转成 (float32 mono, sample_rate?)。"""
    sr: int | None = None
    if isinstance(obj, dict):
        sr = int(obj.get("sample_rate") or obj.get("sr") or 0) or None
        obj = obj.get("tts_speech", obj.get("wav", obj.get("audio")))
    if obj is None:
        return np.zeros(0, dtype=np.float32), sr
    if hasattr(obj, "detach"):                     # torch.Tensor
        try:
            arr = obj.detach().to("cpu").float().numpy()
        except Exception:  # pragma: no cover
            arr = np.asarray(obj.detach().cpu().tolist(), dtype=np.float32)
    else:
        arr = np.asarray(obj)
    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim == 2:
        # (1, N) 或 (N, 1) → (N,)
        arr = arr[0] if arr.shape[0] == 1 else arr.mean(axis=1)
    return arr.reshape(-1).copy(), sr


def _torch_available() -> bool:
    try:
        import importlib.util  # noqa: PLC0415

        return importlib.util.find_spec("torch") is not None
    except Exception:  # pragma: no cover
        return False


def _find_spec(name: str) -> bool:
    try:
        import importlib.util  # noqa: PLC0415

        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


# --------------------------------------------------------------------------- #
# 异步迭代 ↔ 线程桥
# --------------------------------------------------------------------------- #
_SENTINEL = object()

#: 省略号 / 破折号：XTTS 会把它们读成很长的停顿
#: （实测 "Right then…" 变成 3.5s 音频，中间 0.49s + 0.37s 两段静音）。
#: 换成逗号后停顿约 0.2s，自然得多。
_XTTS_LONG_PAUSE_RE = re.compile(r"(?:\.{2,}|…+|—+|–+|--+)")
#: 连续标点（"?!", "!!"）收敛为单个，避免 XTTS 重复拉长。
_XTTS_REPEAT_PUNCT_RE = re.compile(r"([,.!?;:])\1+")


def _expected_speech_s(text: str) -> float:
    """粗略估算一句话"应该"多长（秒）。

    真人英语语速约 150 词/分钟 ≈ 2.5 词/秒，平均词长 ~5 字符，
    所以约 **12.5 字符/秒**。再加上标点停顿。
    主要用于识别 XTTS 偶发的"拖长"合成（实测同一句可在 1.84s~6.12s 跳动）。
    """
    t = (text or "").strip()
    if not t:
        return 0.0
    words = len(t.split())
    base = words / 2.5 if words else len(t) / 12.5
    # 标点带来的停顿
    pauses = t.count(",") * 0.15 + t.count(";") * 0.15 + t.count(":") * 0.15
    return max(0.4, base + pauses)


def _normalize_for_xtts(text: str) -> str:
    """XTTS 专用的朗读前处理（**只用于 XTTS，不影响显示文本**）。

    与 Edge-TTS 不同，XTTS 对省略号/破折号会给出极长的停顿，逐句拼接时
    听起来像"欲言又止"。这里把它们换成逗号，并收敛重复标点。
    """
    t = (text or "").strip()
    if not t:
        return ""
    t = _XTTS_LONG_PAUSE_RE.sub(", ", t)
    t = _XTTS_REPEAT_PUNCT_RE.sub(r"\1", t)
    # 去掉标点前的空格（"Well , actually" → "Well, actually"）
    t = re.sub(r"\s+([,;:.!?])", r"\1", t)
    # 清理 ", ," / 行首逗号 / 多余空格
    t = re.sub(r"\s*,\s*(?:,\s*)+", ", ", t)
    t = re.sub(r"^[\s,]+", "", t)
    t = re.sub(r"\s{2,}", " ", t)
    # 结尾残留的逗号/空格去掉（否则会额外拖一个停顿）
    t = t.rstrip(" ,;:")
    return t.strip()

#: 为角色自动生成 XTTS 参考音频时用的文案。
#: 用**该角色自己的 Edge 声线**合成 → 保留其口音特征 → 作为 XTTS 克隆参考。
#: 每段约 20-25 秒（XTTS 最佳区间），内容自然、含多种音素。
_XTTS_BOOTSTRAP_TEXT: dict[str, str] = {
    "oliver": (
        "Right then, I've been working at this little coffee shop for about three years now, "
        "and I really do love it. The morning rush is my favourite part of the day, "
        "all the regulars coming in, saying hello, asking for their usual. "
        "We roast our own beans just round the corner, so everything you taste here is fresh. "
        "If you're not sure what you fancy, just say the word and I'll sort you out."
    ),
    "raj": (
        "So basically, my name is Raj and I work as a software engineer in Bangalore. "
        "I've been doing this for about six years now, mostly backend systems and APIs. "
        "Actually, one thing I really enjoy is debugging tricky production issues, "
        "because you learn so much from them. Outside work I like playing cricket "
        "and trying out new restaurants with my friends."
    ),
    "marco": (
        "Allora, I'm Marco and I'm a designer. I work in Rome, in a small studio "
        "near the centre, with just four other people. We do mostly branding work "
        "for restaurants and small businesses. The colour and the shape, they must "
        "work together, no? For me, design is about feeling, not just looking. "
        "Forza, let's get started."
    ),
    "emily": (
        "Hi, I'm Emily and I'm a recruiter here in New York. I talk to people all day long, "
        "every single day, so I've heard pretty much every kind of introduction there is. "
        "What I really care about is whether someone can tell me a clear story about their work. "
        "Skills matter, sure, but the way you explain things matters just as much."
    ),
    "priya": (
        "Hi, I'm Priya and I manage product at a fintech company in Singapore. "
        "Can, can, we move fast here, but we try to be careful also. "
        "My team handles payments and onboarding, so there's a lot of detail work. "
        "Actually, most of my day is talking to people, aligning everyone, "
        "making sure we're building the right thing and not just building fast."
    ),
    "_default": (
        "Hello, I'm glad to meet you. I've been learning and working in this field "
        "for several years now, and I still enjoy it very much. "
        "In my free time I like reading, walking around the city, and cooking for friends. "
        "I'm looking forward to our conversation today."
    ),
}

#: 按句末标点决定的自然停顿（毫秒）。
#: 参考真人对话：从句后 ~50-100ms，句号后 ~150-200ms，省略号/破折号更长。
#: 注意：句间总停顿 = 上句尾静音(60ms) + 这里 + 下句首静音(20ms)，
#: 所以取这组值后实际听感约为 80ms(逗号) / 170ms(句号) / 250ms(省略号)。
PAUSE_MS_BY_PUNCT: dict[str, int] = {
    ",": 55,
    ";": 60,
    ":": 60,
    ".": 95,
    "!": 95,
    "?": 95,
    "…": 170,
    "—": 150,
    "–": 120,
    '"': 95,
    "'": 95,
    ")": 95,
    "」": 95,
    "』": 95,
}
#: 句末没有可识别标点时（被长度硬切）的停顿。
PAUSE_MS_NO_PUNCT = 70
#: 最后一句不需要句间停顿（避免说完还静一下）。
PAUSE_MS_LAST = 30


def trailing_pause_ms(text: str, *, is_last: bool = False) -> int:
    """按句末标点给出应补的静音毫秒数。

    Edge-TTS 每句自带 ~0.85s 尾静音（已在 ``trim_silence`` 里裁掉），
    裁完后必须按语义补回一个**自然**的停顿，否则会变成连读。
    """
    t = (text or "").strip()
    if not t:
        return 0
    if is_last:
        return PAUSE_MS_LAST

    # 从末尾往前找第一个非空白、非引号包裹的标点
    for ch in reversed(t):
        if ch.isspace():
            continue
        ms = PAUSE_MS_BY_PUNCT.get(ch)
        if ms is not None:
            return ms
        # 命中其他字符（字母/数字）→ 说明没有句末标点（被硬切）
        return PAUSE_MS_NO_PUNCT
    return PAUSE_MS_NO_PUNCT

#: Edge-TTS 启动预热的常用短语。
#: 这些都是模型高概率会说的"句首/固定搭配"，命中缓存后首音直接省掉 ~800-1100ms。
#: **只预热第一个角色**（通常是默认角色）—— 5 个角色×12 短语=60 次请求要 80 秒，
#: 会把启动拖慢且和首轮对话抢带宽。其余角色按需自然缓存。
_EDGE_WARMUP_PHRASES: tuple[str, ...] = (
    "Right then.",
    "Cheers.",
    "No worries.",
    "Actually.",
    "Allora.",
    "Okay so.",
    "Got it.",
    "Hello.",
)

async def _iter_from_thread(factory: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
    """在后台线程里跑一个**同步生成器**，把元素搬回事件循环。

    这样 ``torch`` 的同步推理不会阻塞事件循环，同时保留流式产出。消费方取消时
    通过 ``threading.Event`` 通知线程尽快停止。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    stop = threading.Event()

    def _put(item: Any) -> None:
        if loop.is_closed():  # pragma: no cover
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:  # pragma: no cover - 事件循环已关闭
            stop.set()

    def worker() -> None:
        try:
            gen = factory(*args, **kwargs)
            try:
                for item in gen:
                    if stop.is_set():
                        break
                    _put(item)
            finally:
                close = getattr(gen, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # pragma: no cover
                        pass
        except BaseException as exc:  # noqa: BLE001 - 转交给消费方
            _put((exc,))
        finally:
            _put(_SENTINEL)

    thread = threading.Thread(target=worker, name="tts-worker", daemon=True)
    thread.start()
    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            if isinstance(item, tuple) and len(item) == 1 and isinstance(item[0], BaseException):
                raise item[0]
            yield item
    finally:
        stop.set()


async def _to_array_stream(items: AsyncIterator[Any]) -> AsyncIterator[np.ndarray]:
    """把后端产出的异构对象（Tensor/dict/tuple/ndarray）统一成 float32 ndarray 流。"""
    async for item in items:
        if isinstance(item, tuple) and len(item) >= 1:
            item = item[0]
        arr, sr = _to_numpy_audio(item)
        if arr.size == 0:
            continue
        if sr and int(sr) != SR_OUT:
            arr = resample(arr, int(sr), SR_OUT)
        yield arr


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class TTSEngine:
    """语音合成引擎（CosyVoice2 / F5-TTS / tone 三后端）。

    Args:
        cfg: ``app.config.TTSSettings``；``None`` 用默认值（测试友好）。
        personas: 角色库，用于取 ``voice.ref_audio`` / ``speed``；``None`` 时
            回退到 :func:`app.personas.get_personas`（若不可用则忽略）。
        ref_library: 参考音频库（**依赖注入用**，测试可传临时目录）。
            ``None`` 时从 ``settings.ref_audio_path`` 构造。

    构造**不加载权重**，也不 import torch。
    """

    BACKENDS: tuple[str, ...] = ("edge", "cosyvoice2", "f5tts", "tone")

    def __init__(
        self,
        cfg: TTSSettings | None = None,
        *,
        personas: Any = None,
        ref_library: Any = None,
    ) -> None:
        self.cfg = cfg or TTSSettings()
        self._personas = personas
        self._ref_library = ref_library

        self._requested = str(self.cfg.backend or "auto")
        self._backend: str = self._detect_backend()   # 廉价探测，不加载权重
        self._backend_ready = False
        self._plan_logged = False

        self._cosy: Any = None
        self._f5: Any = None
        self._edge: Any = None
        self._xtts: Any = None
        self._openvoice: Any = None
        #: persona_id → voice_id 的克隆绑定；由 API 层注入
        self._clone_assignments: dict[str, str] = {}
        self._load_error: str | None = None
        self._load_lock = asyncio.Lock()
        self._infer_lock = asyncio.Lock()             # 额外的一道进程内互斥
        self._sem = asyncio.Semaphore(1)              # 实例级串行化（契约：单卡串行）
        self._tone = ToneSynthesizer(sample_rate=SR_OUT)
        self._ref_warned: set[str] = set()
        self._closed = False
        self._calls = 0
        self._synthesized_s = 0.0

        self._chunk_ms = self._read_chunk_ms()
        self._max_chars = max(40, int(self.cfg.max_chars_per_sentence or 220))

    # ------------------------------------------------------------------ #
    # 基础属性
    # ------------------------------------------------------------------ #
    @property
    def backend_name(self) -> str:
        """当前后端名（``cosyvoice2`` / ``f5tts`` / ``tone``）。**不触发加载**。"""
        return self._backend

    @property
    def sample_rate(self) -> int:
        return SR_OUT

    @property
    def model_loaded(self) -> bool:
        return self._cosy is not None or self._f5 is not None

    @property
    def chunk_ms(self) -> int:
        return self._chunk_ms

    def health(self) -> dict[str, Any]:
        return {
            "backend": self._backend,
            "requested": self._requested,
            "sample_rate": SR_OUT,
            "chunk_ms": self._chunk_ms,
            "loaded": self.model_loaded,
            "error": self._load_error,
            "calls": self._calls,
            "synthesized_s": round(self._synthesized_s, 2),
        }

    def _read_chunk_ms(self) -> int:
        """从 ``pipeline.audio_chunk_ms`` 读分块长度；读不到就用 200。"""
        try:
            from app.config import get_settings  # noqa: PLC0415

            return max(20, int(get_settings().pipeline.audio_chunk_ms))
        except Exception:  # noqa: BLE001 - 测试环境无 settings 时用默认
            return 200

    # ------------------------------------------------------------------ #
    # 参考音频 / 角色
    # ------------------------------------------------------------------ #
    def _get_ref_library(self) -> Any:
        if self._ref_library is not None:
            return self._ref_library
        try:
            from app.voice.ref_audio import ReferenceLibrary  # noqa: PLC0415

            path = self._default_ref_dir()
            self._ref_library = ReferenceLibrary(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("参考音频库初始化失败（将使用 tone 兜底）: %s", exc)
            return None
        return self._ref_library

    @staticmethod
    def _default_ref_dir() -> str:
        try:
            from app.config import get_settings  # noqa: PLC0415

            return str(get_settings().ref_audio_path)
        except Exception:  # noqa: BLE001
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)
            ))))
            return os.path.join(root, "assets", "ref_audio")

    def _get_personas(self) -> Any:
        if self._personas is not None:
            return self._personas
        try:
            from app.personas import get_personas  # noqa: PLC0415

            self._personas = get_personas()
        except Exception as exc:  # noqa: BLE001
            log.debug("角色库不可用（不影响 tone 后端）: %s", exc)
            return None
        return self._personas

    def _persona_speed(self, persona_id: str) -> float:
        """角色语速：personas.json 的 ``voice.speed`` 优先，否则 ``cfg.speed``。"""
        base = float(self.cfg.speed or 1.0)
        personas = self._get_personas()
        if personas is None:
            return base
        try:
            persona = personas.get(persona_id)
            speed = float(getattr(persona.voice, "speed", 1.0) or 1.0)
        except Exception:  # noqa: BLE001
            return base
        return float(min(2.0, max(0.5, base * speed)))

    def list_persona_voices(self) -> list[str]:
        """可用于 zero-shot 克隆的角色 id（参考音频文件真实存在且转写非空）。"""
        lib = self._get_ref_library()
        if lib is None:
            return []
        try:
            return list(lib.available_ids())
        except Exception as exc:  # noqa: BLE001
            log.debug("列出可用参考音频失败: %s", exc)
            return []

    # ------------------------------------------------------------------ #
    # 后端探测 / 加载
    # ------------------------------------------------------------------ #
    def _resolve_path(self, path: str) -> str:
        """把相对路径按项目根解析（``third_party/CosyVoice`` 这类配置）。"""
        if not path:
            return ""
        if os.path.isabs(path):
            return os.path.normpath(path)
        try:
            from app.config import PROJECT_ROOT  # noqa: PLC0415

            return os.path.normpath(os.path.join(PROJECT_ROOT, path))
        except Exception:  # noqa: BLE001
            here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)
            ))))
            return os.path.normpath(os.path.join(here, path))

    def _detect_backend(self) -> str:
        """廉价探测（不做任何权重加载）：按 auto 顺序选第一个看起来可用的后端。

        ``auto`` 顺序：**cosyvoice2 → f5tts → xtts → edge → tone**

        **为什么 xtts 排在 edge 之前**（实测教训）
        ----------------------------------------
        Edge-TTS 虽然开箱即用，但它是**网络服务**，从本机（上海）实测：
            未命中短句  1900–6000ms（会被限流，波动极大）
            中途预热 300 次后，延迟一度劣化到 6000ms 且持续数分钟
        而 XTTS-v2 本地推理（RTX 5080）：
            短句 248ms / 中句 819ms / RTF 0.21–0.25
            **完全稳定，不受网络和限流影响**

        对话场景对延迟极其敏感，所以本地优先。Edge-TTS 保留为兜底：
        当 XTTS 没有参考音频（用户还没录）时，仍能发出真人声音。
        """
        requested = self._requested
        if requested in ("tone", "mock"):
            if requested == "mock":
                log.info("TTS backend=mock → 使用 tone 占位合成器（接口一致）")
            return "tone"
        if requested in ("cosyvoice2", "f5tts", "edge", "xtts"):
            return requested
        # auto：本地优先（稳定低延迟），网络服务兜底
        if self._cosyvoice_looks_available():
            return "cosyvoice2"
        if self._f5_looks_available():
            return "f5tts"
        if self._xtts_looks_available():
            return "xtts"
        if self._edge_looks_available():
            return "edge"
        return "tone"

    def _xtts_looks_available(self) -> bool:
        """XTTS 可用 = 装了 coqui-tts + 有 torch。"""
        try:
            from app.voice.xtts_engine import XTTSEngine  # noqa: PLC0415

            return XTTSEngine.looks_available() and _torch_available()
        except Exception:
            return False

    def _edge_looks_available(self) -> bool:
        """edge-tts 只需 import 成功（网络在首次调用时验证）。"""
        try:
            from app.voice.edge_tts_engine import EdgeTTSEngine  # noqa: PLC0415

            return EdgeTTSEngine.looks_available()
        except Exception:
            return False


    def _cosyvoice_looks_available(self) -> bool:
        repo = self._resolve_path(self.cfg.cosyvoice_repo_dir)
        model_dir = self._resolve_path(self.cfg.cosyvoice_model_dir)
        has_code = _find_spec("cosyvoice") or os.path.isdir(os.path.join(repo, "cosyvoice"))
        if not has_code:
            return False
        # 仓库在但模型目录不在 → 无法克隆
        if repo and not os.path.isdir(repo):
            return False
        if model_dir and not os.path.isdir(model_dir):
            return False
        return _torch_available()

    def _f5_looks_available(self) -> bool:
        repo = self._resolve_path(self.cfg.f5_repo_dir)
        has_code = _find_spec("f5_tts") or (repo and os.path.isdir(os.path.join(repo, "f5_tts")))
        if not has_code:
            return False
        return _torch_available()

    async def _ensure_backend(self) -> str:
        """确保后端已就绪：第一个成功后锁定；失败继续往下探；最终 tone。"""
        if self._backend_ready:
            return self._backend
        async with self._load_lock:
            if self._backend_ready:
                return self._backend
            order = self._candidate_order()
            for name in order:
                if name == "tone":
                    self._backend = "tone"
                    self._backend_ready = True
                    log.info("TTS backend = tone（占位合成，链路可跑）")
                    return self._backend
                try:
                    if name == "edge":
                        # Edge-TTS：真人声线，需要联网（首次做一次极小合成验证）
                        from app.voice.edge_tts_engine import EdgeTTSEngine  # noqa: PLC0415

                        edge = EdgeTTSEngine(sample_rate=SR_OUT)
                        await edge.ensure_ready()
                        self._edge = edge
                    elif name == "xtts":
                        # XTTS-v2 本地克隆：加载到 GPU（~10s，1.8GB 显存）
                        from app.voice.xtts_engine import XTTSEngine  # noqa: PLC0415

                        xtts = XTTSEngine(sample_rate=SR_OUT)
                        await xtts.ensure_loaded()
                        self._xtts = xtts
                    elif name == "cosyvoice2":
                        self._cosy = await asyncio.to_thread(self._load_cosyvoice2)
                    elif name == "f5tts":
                        self._f5 = await asyncio.to_thread(self._load_f5tts)
                    else:  # pragma: no cover - 枚举保护
                        continue
                    self._backend = name
                    self._backend_ready = True
                    self._load_error = None
                    log.info("TTS backend = %s（%s）", name, "真人神经声线" if name == "edge" else "zero-shot 克隆")
                    return self._backend
                except Exception as exc:  # noqa: BLE001 - 逐个降级
                    self._load_error = f"{name}: {type(exc).__name__}: {exc}"
                    log.warning("TTS 后端 %s 不可用：%s", name, self._load_error)
            self._backend = "tone"
            self._backend_ready = True
            return self._backend

    def _candidate_order(self) -> list[str]:
        if self._requested in ("tone", "mock"):
            return ["tone"]
        if self._requested == "edge":
            return ["edge", "tone"]
        if self._requested == "xtts":
            return ["xtts", "edge", "tone"]
        if self._requested == "cosyvoice2":
            return ["cosyvoice2", "xtts", "edge", "tone"]
        if self._requested == "f5tts":
            return ["f5tts", "xtts", "edge", "tone"]
        # auto：本地优先（稳定低延迟），网络兜底
        return ["cosyvoice2", "f5tts", "xtts", "edge", "tone"]

    def _plan(self) -> str:
        return " → ".join(self._candidate_order())

    # ---- CosyVoice2 ----
    def _load_cosyvoice2(self) -> Any:
        """加载 CosyVoice2。**在不同 commit 的 API 差异上做签名探测**。"""
        repo = self._resolve_path(self.cfg.cosyvoice_repo_dir)
        model_dir = self._resolve_path(self.cfg.cosyvoice_model_dir)
        if repo and repo not in sys.path:
            sys.path.insert(0, repo)
        try:
            import torch  # noqa: F401,PLC0415 - 确认 torch 可用
        except Exception as exc:
            raise _BackendUnavailable(f"torch 不可用: {exc}") from exc
        try:
            from cosyvoice.cli.cosyvoice import CosyVoice2  # noqa: PLC0415
        except Exception as exc:
            raise _BackendUnavailable(f"import CosyVoice2 失败: {exc}") from exc
        if model_dir and not os.path.isdir(model_dir):
            raise _BackendUnavailable(f"模型目录不存在: {model_dir}")

        # 按构造函数签名过滤参数：不同版本有 load_vllm / load_trt / fp16 等差异
        wanted = {
            "load_jit": bool(self.cfg.load_jit),
            "load_trt": bool(self.cfg.load_trt),
            "load_vllm": bool(self.cfg.load_vllm),
            "fp16": bool(self.cfg.fp16),
        }
        kwargs = dict(wanted)
        try:
            params = inspect.signature(CosyVoice2.__init__).parameters
            kwargs = {k: v for k, v in kwargs.items() if k in params}
        except (TypeError, ValueError):  # pragma: no cover
            kwargs = {}
        sw = Stopwatch()
        log.info("加载 CosyVoice2: model_dir=%s kwargs=%s", model_dir, kwargs)
        model = CosyVoice2(model_dir, **kwargs)
        log.info("CosyVoice2 加载完成（%.0fms）", sw.elapsed_ms())
        return model

    # ---- F5-TTS ----
    def _load_f5tts(self) -> Any:
        repo = self._resolve_path(self.cfg.f5_repo_dir)
        if repo and os.path.isdir(repo) and repo not in sys.path:
            sys.path.insert(0, repo)
        try:
            import torch  # noqa: F401,PLC0415
        except Exception as exc:
            raise _BackendUnavailable(f"torch 不可用: {exc}") from exc
        try:
            from f5_tts.api import F5TTS  # noqa: PLC0415
        except Exception as exc:
            raise _BackendUnavailable(f"import F5TTS 失败: {exc}") from exc

        device = self._pick_device()
        base_kwargs: dict[str, Any] = {"model": self.cfg.f5_model, "device": device}
        ckpt = self.cfg.f5_ckpt_file.strip()
        if ckpt:
            base_kwargs["ckpt_file"] = self._resolve_path(ckpt)
        try:
            params = inspect.signature(F5TTS.__init__).parameters
            base_kwargs = {k: v for k, v in base_kwargs.items() if k in params}
        except (TypeError, ValueError):  # pragma: no cover
            pass
        sw = Stopwatch()
        log.info("加载 F5-TTS: %s", base_kwargs)
        model = F5TTS(**base_kwargs)
        log.info("F5-TTS 加载完成（%.0fms）", sw.elapsed_ms())
        return model

    def _pick_device(self) -> str:
        want = str(self.cfg.device or "cuda")
        if want.startswith("cuda"):
            try:
                import torch  # noqa: PLC0415

                if not torch.cuda.is_available():
                    log.info("CUDA 不可用，TTS 使用 CPU（会很慢，仅验证链路）")
                    return "cpu"
            except Exception:  # noqa: BLE001
                return "cpu"
        return want

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def prewarm(self, persona_ids: list[str] | None = None) -> None:
        """加载模型 + 预热参考音频 + 可选 warmup 合成。失败不抛。"""
        if self._closed:
            return
        sw = Stopwatch()
        backend = await self._ensure_backend()
        if not self._plan_logged:
            log.info("TTS 计划顺序 %s → 实际 %s", self._plan(), backend)
            self._plan_logged = True

        # 参考音频体检（只记日志，不阻塞）
        lib = self._get_ref_library()
        if lib is not None:
            try:
                ready = lib.available_ids()
            except Exception:  # noqa: BLE001
                ready = []
            asked = list(persona_ids or [])
            missing = [p for p in asked if p not in ready]
            if missing:
                log.warning(
                    "参考音频缺失 %d/%d 个角色: %s（这些角色将用 tone 兜底）",
                    len(missing), len(asked), ", ".join(missing[:8]),
                )
            else:
                log.info("参考音频就绪: %d 个角色", len(ready))

        if backend == "tone" or not self.cfg.prewarm:
            self._backend_ready = True
            log.info("TTS prewarm 完成（backend=%s, %.0fms）", backend, sw.elapsed_ms())
            return

        # ---- XTTS：为所有角色准备参考音频（纯本地，之后无网络依赖）----
        if backend == "xtts":
            if self._xtts is None:
                from app.voice.xtts_engine import XTTSEngine  # noqa: PLC0415

                self._xtts = XTTSEngine(sample_rate=SR_OUT)
            try:
                await self._xtts.ensure_loaded()
            except Exception as exc:  # noqa: BLE001
                log.warning("XTTS 加载失败（忽略）: %s", exc)
                return

            ok = 0
            for pid in (persona_ids or [])[:3]:   # 先准备前 3 个常用角色
                if self._xtts.get_voice(pid) is not None:
                    ok += 1
                    continue
                try:
                    await self._xtts_bootstrap_voice(pid)
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("XTTS 参考音频准备失败（%s）: %s", pid, exc)
            # 立刻做一次 warmup 合成，把 CUDA kernel 编译挪到前面
            try:
                first = (persona_ids or [""])[0]
                if first and self._xtts.get_voice(first) is not None:
                    await self._xtts.synthesize("Right then, cheers.", first)
            except Exception as exc:  # noqa: BLE001
                log.debug("XTTS warmup 合成失败: %s", exc)
            log.info(
                "TTS prewarm 完成（backend=xtts, 参考音频 %d/%d, %.0fms）",
                ok, min(3, len(persona_ids or [])), sw.elapsed_ms(),
            )
            return

        # ---- Edge-TTS：预热常用短语（缓存命中可省 ~800-1100ms）----
        # 只预热**第一个角色**：5 角色 × 8 短语 = 40 次请求要 ~55s，
        # 会把启动拖慢且与首轮对话抢微软服务带宽（实测会把首音从 1.3s 拉到 4.5s）。
        if backend == "edge":
            if self._edge is None:
                from app.voice.edge_tts_engine import EdgeTTSEngine  # noqa: PLC0415

                self._edge = EdgeTTSEngine(sample_rate=SR_OUT)
                await self._edge.ensure_ready()
            phrases = _EDGE_WARMUP_PHRASES
            pids = list(persona_ids or [])[:1]   # ← 关键：只第一个角色
            try:
                n = await self._edge.warmup(phrases, pids)
                log.info(
                    "TTS prewarm 完成（backend=edge, 预热 %d 条 × %d 角色, %.0fms）",
                    len(phrases), len(pids), sw.elapsed_ms(),
                )
                _ = n
            except Exception as exc:  # noqa: BLE001
                log.warning("Edge-TTS 预热失败（忽略）: %s", exc)
            return

        # 真实后端：做一次最短的 warmup 合成，把 CUDA kernel 编译/显存分配挪到前面
        target = persona_ids[0] if persona_ids else ""
        for pid in (target, *(persona_ids or [])):
            if pid and self._voice_usable(pid):
                try:
                    async for _ in self._iter_pcm_chunks("Hello.", pid, 1.0):
                        pass
                    log.info("TTS warmup 完成（persona=%s, %.0fms）", pid, sw.elapsed_ms())
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning("TTS warmup 失败（忽略）: %s", exc)
                    return
        log.info("TTS prewarm 完成（backend=%s，无可用参考音频，跳过 warmup）", backend)

    async def aclose(self) -> None:
        """释放模型引用（torch 模型的显存在 GC 时归还）。"""
        self._closed = True
        self._cosy = None
        self._f5 = None
        log.debug("TTS engine closed (backend=%s)", self._backend)

    # ------------------------------------------------------------------ #
    # 合成主入口
    # ------------------------------------------------------------------ #
    async def synthesize(self, req: TTSSentenceRequest) -> AsyncIterator[AudioChunk]:
        """把一句文本合成成按 ``audio_chunk_ms`` 切好的 :class:`AudioChunk` 流。

        - ``is_first`` / ``is_last`` 在本句内各恰好一次。
        - 内部串行化：``asyncio.Semaphore(1)``，保证同一时刻只有一个推理。
        - 任何后端失败都会降级到 ``tone``，**绝不整轮失败**。
        """
        if self._closed:
            raise TTSError("TTSEngine 已关闭")
        text = normalize_for_tts(req.text)
        if not text:
            log.debug("TTS 清洗后为空，跳过（sentence_index=%s）", req.sentence_index)
            return

        self._calls += 1
        speed = self._persona_speed(req.persona_id)
        pieces = split_for_tts(text, self._max_chars)
        if not pieces:
            return
        if len(pieces) > 1:
            log.debug(
                "TTS 句子 %d 过长（%d 字符），二次切分为 %d 段",
                req.sentence_index, len(text), len(pieces),
            )

        sw = Stopwatch()
        index = 0
        first_emitted = False
        last_emitted = False
        total_bytes = 0

        async with self._sem:
            for pi, piece in enumerate(pieces):
                async for pcm, part_last in self._iter_pcm_chunks(piece, req.persona_id, speed):
                    if not pcm and not part_last:
                        continue
                    is_last = bool(part_last and pi == len(pieces) - 1)
                    chunk = AudioChunk(
                        pcm16=pcm,
                        sample_rate=SR_OUT,
                        index=index,
                        is_first=not first_emitted,
                        is_last=is_last,
                    )
                    index += 1
                    first_emitted = True
                    total_bytes += len(pcm)
                    if is_last:
                        last_emitted = True
                    yield chunk

        self._synthesized_s += total_bytes / 2.0 / SR_OUT
        if not first_emitted:
            log.warning("TTS 未产出任何音频（sentence_index=%d, backend=%s）",
                        req.sentence_index, self._backend)
        else:
            log.debug(
                "TTS 完成 sentence=%d backend=%s chunks=%d audio=%.2fs %.0fms",
                req.sentence_index, self._backend, index,
                total_bytes / 2.0 / SR_OUT, sw.elapsed_ms(),
            )
        _ = last_emitted

    # ------------------------------------------------------------------ #
    # 分块 + 后端调度
    # ------------------------------------------------------------------ #
    def _chunk_samples(self) -> int:
        return max(1, ms_to_samples(self._chunk_ms, SR_OUT))

    async def _iter_pcm_chunks(
        self, text: str, persona_id: str, speed: float
    ) -> AsyncIterator[tuple[bytes, bool]]:
        """产出一句的 PCM16 切片 ``(bytes, is_last)``（不改 sampling rate）。"""
        # ---- 0) 克隆音色优先：用户上传了自己的录音并绑到该角色 ----
        #
        # 这里有两条路，选哪条取决于**是否可用 cascade（口音保留）**：
        #
        #   cascade（首选）: edge 该角色母语声线 → OpenVoice 换成用户音色
        #                    口音 100% 真实 + 音色是用户的
        #   xtts   （兜底）: 直接用 XTTS 克隆
        #                    音色对，但口音会被洗掉（实测语调相关性 -0.27~0.18）
        #
        # cascade 是这两轮排查后的正确架构，所以只要 OpenVoice 可用就优先它。
        clone_id = self._clone_assignments.get(persona_id or "")
        if clone_id:
            # --- 0a) cascade：口音保留 ---
            if self._cascade_ready(clone_id):
                try:
                    async for item in self._cascade_chunks(text, persona_id, clone_id, speed):
                        yield item
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 失败则降到 xtts
                    log.warning(
                        "级联合成失败（timbre=%s, persona=%s）: %s: %s，降级到 XTTS",
                        clone_id, persona_id, type(exc).__name__, exc,
                    )

            # --- 0b) XTTS 直接克隆（兜底）---
            if self._xtts is not None:
                voice = None
                try:
                    voice = self._xtts.get_voice(clone_id)
                except Exception:  # noqa: BLE001
                    voice = None
                if voice is not None:
                    try:
                        async for item in self._xtts_chunks(text, clone_id):
                            yield item
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - 克隆失败则降级
                        log.warning(
                            "克隆音色合成失败（voice=%s, persona=%s）: %s: %s，降级到默认后端",
                            clone_id, persona_id, type(exc).__name__, exc,
                        )

        backend = await self._ensure_backend()

        # ---- Edge-TTS：真人声线（真口音）----
        # 不需要参考音频：声线本身就带口音，直接按角色映射到对应 locale。
        if backend == "edge":
            try:
                async for item in self._edge_chunks(text, persona_id, speed):
                    yield item
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络抖动等 → 降级
                log.warning(
                    "Edge-TTS 合成失败（persona=%s）: %s: %s",
                    persona_id, type(exc).__name__, exc,
                )
                self._load_error = f"edge: {type(exc).__name__}: {exc}"
                if self.cfg.tone_fallback_enabled:
                    async for item in self._tone_chunks(text, speed):
                        yield item
                    return
                raise TTSError(f"Edge-TTS 失败: {exc}") from exc

        if backend == "tone":
            async for item in self._tone_chunks(text, speed):
                yield item
            return

        # ---- XTTS 本地克隆（auto 的默认后端）----
        if backend == "xtts":
            try:
                async for item in self._xtts_chunks(text, persona_id):
                    yield item
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 本地失败则退回 edge
                log.warning(
                    "XTTS 合成失败（persona=%s）: %s: %s；退回 edge-tts",
                    persona_id, type(exc).__name__, exc,
                )
                self._load_error = f"xtts: {type(exc).__name__}: {exc}"
                if self._edge_looks_available():
                    async for item in self._edge_chunks(text, persona_id, speed):
                        yield item
                    return
                if self.cfg.tone_fallback_enabled:
                    async for item in self._tone_chunks(text, speed):
                        yield item
                    return
                raise TTSError(f"XTTS 失败且无兜底: {exc}") from exc

        # 参考音频不可用 → 直接 tone（不能因为缺素材整轮失败）
        if not self._voice_usable(persona_id):
            if self.cfg.tone_fallback_enabled:
                self._warn_ref_once(persona_id)
                async for item in self._tone_chunks(text, speed):
                    yield item
                return
            raise TTSError(f"参考音频不可用且 tone 兜底已关闭: persona_id={persona_id!r}")

        produced = False
        for attempt in (1, 2):
            try:
                async for item in self._real_chunks(text, persona_id, speed):
                    produced = True
                    yield item
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 重试 / 降级
                log.warning(
                    "TTS 合成失败（backend=%s, persona=%s, 第%d次）: %s: %s",
                    backend, persona_id, attempt, type(exc).__name__, exc,
                )
                self._load_error = f"{type(exc).__name__}: {exc}"
                if produced:
                    # 已经往上游吐过音频：不再重试（否则重复播放），干净收尾
                    log.warning("已产出部分音频后失败，结束该句（不重复合成）")
                    return

        if self.cfg.tone_fallback_enabled:
            log.warning("TTS 两次失败 → 用 tone 后端兜底该句：%r", text[:60])
            async for item in self._tone_chunks(text, speed):
                yield item
        else:
            raise TTSError("TTS 合成失败且 tone 兜底已关闭")

    def _voice_usable(self, persona_id: str) -> bool:
        lib = self._get_ref_library()
        if lib is None:
            return False
        try:
            voice = lib.get(persona_id)
        except Exception:  # noqa: BLE001
            return False
        return bool(voice is not None and getattr(voice, "usable", False))

    def _warn_ref_once(self, persona_id: str) -> None:
        if persona_id in self._ref_warned:
            return
        self._ref_warned.add(persona_id)
        lib = self._get_ref_library()
        voice = lib.get(persona_id) if lib is not None else None
        if voice is None:
            log.warning(
                "persona=%r 没有参考音频配置 → 用 tone 兜底（后续同角色不再提示）",
                persona_id,
            )
        else:
            log.warning(
                "persona=%r 的参考音频不可用（%s）→ 用 tone 兜底（后续同角色不再提示）",
                persona_id, getattr(voice, "ref_wav", "?"),
            )

    # ---- tone 后端 ----
    # ---- 级联：edge 真口音 → OpenVoice 换音色 ----
    def _cascade_ready(self, timbre_id: str) -> bool:
        """cascade 可用 = OpenVoice 已加载 + 该 timbre_id 已登记。"""
        ov = self._openvoice
        if ov is None:
            return False
        try:
            return ov.get_timbre(timbre_id) is not None
        except Exception:
            return False

    async def _cascade_chunks(
        self, text: str, persona_id: str, timbre_id: str, speed: float
    ) -> AsyncIterator[tuple[bytes, bool]]:
        """**保留口音的克隆**：edge 母语声线 → OpenVoice 换成用户音色。

        这是本项目解决"口音是假的"的核心路径。

        为什么这样能保留口音
        --------------------
        TTS（edge）用的是**该国母语者的真人录音声线**，所以产出的语音天然带
        真实口音（印度英语的音节计时、爱尔兰英语的升调尾等）。
        OpenVoice 是变声器：把语音拆成 [内容 | 韵律/语调 | 音色]，
        **只替换音色这一支**，韵律和语调原封不动。

        实测（归一化 F0 语调曲线相关性，1.0 = 完全一致）
            口音      XTTS 直克隆   本方法
            印度        -0.269        0.142
            爱尔兰       0.179        0.980
            南非         0.050        0.844

        代价
        ----
        · 延迟：edge 网络请求 + VC 推理，比纯 XTTS 慢
        · 输出经 22.05k→24k 重采样，极高频略有损失
        """
        import numpy as _np  # noqa: PLC0415

        if self._openvoice is None:
            raise TTSError("OpenVoice 引擎未注入")
        if self._edge is None:
            from app.voice.edge_tts_engine import EdgeTTSEngine  # noqa: PLC0415

            self._edge = EdgeTTSEngine(sample_rate=SR_OUT)
            await self._edge.ensure_ready()

        # ① 用该角色母语声线合成（自带真实口音）
        src_arr = await self._edge.synthesize(text, persona_id)
        if src_arr is None or len(src_arr) == 0:
            raise TTSError("edge 合成返回空音频")

        # ② 换音色（保留口音）
        converted = await self._openvoice.convert(src_arr, timbre_id)
        if converted is None or converted.size == 0:
            raise TTSError("OpenVoice 转换返回空音频")

        # ③ 裁静音 + 补停顿（与其它后端一致）
        from app.voice.edge_tts_engine import trim_silence  # noqa: PLC0415

        converted = trim_silence(converted, SR_OUT, head_s=0.02, tail_s=0.05)
        if converted.size == 0:
            return

        pause_ms = trailing_pause_ms(text)
        sil = int(SR_OUT * pause_ms / 1000) if pause_ms > 0 else 0
        if sil > 0:
            converted = _np.concatenate(
                [converted, _np.zeros(sil, dtype=_np.float32)]
            )

        total = converted.size
        pcm_all = _np.clip(converted * 32767.0, -32768, 32767).astype(_np.int16).tobytes()

        chunk_n = self._chunk_samples()
        pos = 0
        while pos < total:
            end = min(total, pos + chunk_n)
            yield pcm_all[pos * 2 : end * 2], end >= total
            pos = end

    async def _xtts_chunks(
        self, text: str, voice_id: str
    ) -> AsyncIterator[tuple[bytes, bool]]:
        """克隆音色合成 → 裁剪 → 切片。

        XTTS 的输出和 Edge 一样会带明显尾静音（实测 **0.61–0.66s**），
        必须裁掉，否则逐句拼接后句间停顿约 0.78s，听感不自然。
        """
        import numpy as _np  # noqa: PLC0415

        from app.voice.edge_tts_engine import trim_silence  # noqa: PLC0415

        pcm16 = await self._xtts_ensure_voice_and_synth(text, voice_id)
        if not pcm16:
            return

        # 裁掉首尾静音（与 edge 路径用同一套参数）
        arr = _np.frombuffer(pcm16, dtype=_np.int16).astype(_np.float32) / 32768.0
        arr = trim_silence(arr, SR_OUT, head_s=0.02, tail_s=0.04)
        if arr.size == 0:
            return

        chunk_n = self._chunk_samples()
        total = arr.size

        # 句末自然停顿（按标点）
        pause_ms = trailing_pause_ms(text)
        sil = int(SR_OUT * pause_ms / 1000) if pause_ms > 0 else 0
        if sil > 0:
            arr = _np.concatenate([arr, _np.zeros(sil, dtype=_np.float32)])
            total = arr.size

        pcm_all = _np.clip(arr * 32767.0, -32768, 32767).astype(_np.int16).tobytes()
        step = chunk_n
        pos = 0
        while pos < total:
            end = min(total, pos + step)
            yield pcm_all[pos * 2 : end * 2], end >= total
            pos = end

    async def _xtts_ensure_voice_and_synth(self, text: str, voice_id: str) -> bytes:
        """确保该 persona 有可用参考音频（没有就用 edge 现场生成一次），然后合成。"""
        xtts = self._xtts
        if xtts is None:
            raise TTSError("XTTS 引擎未注入")

        # 1) 已有参考音频 → 直接用
        if xtts.get_voice(voice_id) is None:
            await self._xtts_bootstrap_voice(voice_id)

        # 2) XTTS 对省略号/破折号会产生过长的停顿（实测 "Right then…" 被读成
        #    3.5s，中间两段静音 0.49s + 0.37s）。合成前换成逗号，让停顿自然。
        speakable = _normalize_for_xtts(text)
        if not speakable:
            speakable = text

        # 3) XTTS 没有显式时长控制，同一句话不同次合成可能明显拉长。
        #    实测：
        #      "one flat white coming up."  1.57~1.97s（稳定）
        #      "Right then…"                1.50~4.08s（超短句容易"加戏"）
        #    对超短句用更严格的上界与更多次重试。
        expected = _expected_speech_s(speakable)
        is_short = len(speakable.split()) <= 8
        # 上界收紧到 1.35× 预期 + 0.6s：XTTS 正常输出通常落在预期 0.85~1.25 倍，
        # 超过 1.35 倍基本就是"加戏"（拖长、多插停顿）。宁可多重试几次取最短。
        upper = expected * 1.35 + 0.6
        attempts = 4 if is_short else 3

        best: bytes | None = None
        best_dur = float("inf")
        for _attempt in range(attempts):
            pcm = await xtts.synthesize_pcm16(speakable, voice_id)
            if not pcm:
                continue
            dur = len(pcm) / 2.0 / SR_OUT
            if dur < best_dur:
                best, best_dur = pcm, dur
            if dur <= upper:
                break
            log.debug(
                "XTTS 合成异常偏长（%.2fs > %.2fs 上界），重试：%r",
                dur, upper, speakable[:40],
            )
        return best or b""

    async def _xtts_bootstrap_voice(self, persona_id: str) -> None:
        """为角色自动准备一段参考音频。

        原理：用 Edge-TTS（一次网络请求）合成一段 20 秒左右的**该角色口音**的
        语音，存成 wav 作为 XTTS 的克隆参考。之后该角色的所有合成都是**纯本地**，
        不再受 Edge 网络延迟/限流影响。

        每台机器每个角色只跑一次（文件存在即复用）。
        """
        import time

        xtts = self._xtts
        if xtts is None:
            raise TTSError("XTTS 引擎未注入")

        ref_dir = os.path.join(str(getattr(self.cfg, "data_dir", "") or "data"), "voice_refs")
        ref_dir = os.path.join("data", "voice_refs")
        os.makedirs(ref_dir, exist_ok=True)
        wav_path = os.path.join(ref_dir, f"{persona_id}.wav")

        if os.path.isfile(wav_path):
            xtts.register_voice(persona_id, wav_path)
            return

        # 选一段贴合该角色口音的文案
        text = _XTTS_BOOTSTRAP_TEXT.get(
            persona_id, _XTTS_BOOTSTRAP_TEXT["_default"]
        )

        log.info("XTTS 正在为角色 %s 准备参考音频（一次性，需要 edge-tts）…", persona_id)
        t0 = time.perf_counter()

        try:
            if self._edge is None:
                from app.voice.edge_tts_engine import EdgeTTSEngine  # noqa: PLC0415

                self._edge = EdgeTTSEngine(sample_rate=SR_OUT)
                await self._edge.ensure_ready()
            pcm_f32 = await self._edge.synthesize(text, persona_id)
        except Exception as exc:  # noqa: BLE001
            raise TTSError(
                f"无法为 {persona_id} 生成参考音频（edge-tts 不可用）: {exc}"
            ) from exc

        # 存成 22.05k wav（XTTS 推荐格式）
        from app.voice.xtts_engine import XTTS_SR, normalize_reference  # noqa: PLC0415

        tmp = wav_path + ".tmp.wav"
        try:
            import numpy as _np
            import soundfile as sf  # noqa: PLC0415

            arr = _np.asarray(pcm_f32, dtype=_np.float32).reshape(-1)
            sf.write(tmp, arr, SR_OUT, subtype="PCM_16")
            info = await asyncio.to_thread(normalize_reference, tmp, wav_path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        _ = XTTS_SR
        xtts.register_voice(persona_id, wav_path)
        log.info(
            "XTTS 参考音频就绪：%s (%.1fs, %.0fms)",
            persona_id, info["duration_s"], (time.perf_counter() - t0) * 1000,
        )

    # ---- 克隆音色绑定（由 API 层调用）----
    def bind_clone_engine(self, xtts: Any) -> None:
        """注入 XTTS 引擎实例。"""
        self._xtts = xtts

    # ---- OpenVoice（级联换音色）注入 ----
    def bind_openvoice(self, engine: Any) -> None:
        """注入 OpenVoice 引擎。有了它，克隆就能走「保留口音」的级联路径。"""
        self._openvoice = engine
        if engine is not None:
            log.info("OpenVoice 已注入 TTS 引擎：克隆将走「保留口音」级联路径")
        else:
            log.info("OpenVoice 已移除：克隆回退到 XTTS（口音会丢）")

    @property
    def openvoice(self) -> Any:
        return self._openvoice

    def clonable_backends(self) -> dict[str, bool]:
        """当前克隆能力快照（供 /api/health 和前端展示）。"""
        return {
            "cascade_accent_preserving": self._openvoice is not None,
            "xtts_direct": self._xtts is not None,
        }

    def set_clone_assignments(self, assignments: dict[str, str]) -> None:
        """更新 persona_id → voice_id 绑定。"""
        self._clone_assignments = dict(assignments or {})
        if self._clone_assignments:
            log.info("TTS 克隆绑定已更新: %s", self._clone_assignments)

    def clone_assignments(self) -> dict[str, str]:
        return dict(self._clone_assignments)

    async def _edge_chunks(
        self, text: str, persona_id: str, speed: float
    ) -> AsyncIterator[tuple[bytes, bool]]:
        """Edge-TTS：真人声线合成 → 按 ``audio_chunk_ms`` 切片。

        句间停顿时长
        ------------
        Edge-TTS 原生的句尾静音太长（实测 ~0.85s），逐句拼接后会变成"每句停一秒"。
        这里已在上游裁掉首尾静音，再**按句末标点补一个自然的停顿**：

            ,  ;  :            →  90ms   （从句，很短的换气）
            .  !  ?            →  180ms  （正常句间）
            …  ——              →  260ms  （思考、拖长）
            没有标点（被硬切）  →  120ms

        这样听感接近真人：一句话说完有短促换气，而不是机械地停一秒。
        """
        import numpy as np  # noqa: PLC0415

        from app.voice.edge_tts_engine import resolve_voice  # noqa: PLC0415

        if self._edge is None:
            from app.voice.edge_tts_engine import EdgeTTSEngine  # noqa: PLC0415

            self._edge = EdgeTTSEngine(sample_rate=SR_OUT)
            await self._edge.ensure_ready()

        voice = resolve_voice(persona_id)
        _ = voice
        chunk_n = self._chunk_samples()

        # 流式：拿到一段 PCM 就切块发出去，不等整句
        pending = np.zeros(0, dtype=np.int16)
        first_sent = False
        async for pcm_f32 in self._edge.synth_stream_frames(
            text, persona_id, min_frame_s=0.35
        ):
            arr = np.clip(
                np.asarray(pcm_f32, dtype=np.float32) * 32767.0, -32768, 32767
            ).astype(np.int16)
            if arr.size == 0:
                continue
            pending = np.concatenate([pending, arr]) if pending.size else arr
            while pending.size >= chunk_n:
                piece = pending[:chunk_n]
                pending = pending[chunk_n:]
                first_sent = True
                yield piece.tobytes(), False

        # 句末补一个自然停顿（按标点决定长度）
        pause_ms = trailing_pause_ms(text)
        if pause_ms > 0:
            n_sil = int(SR_OUT * pause_ms / 1000)
            if n_sil > 0:
                pending = np.concatenate(
                    [pending, np.zeros(n_sil, dtype=np.int16)]
                )

        # 收尾：剩余样本一次性给出（带 is_last）
        if pending.size > 0:
            first_sent = True
            yield pending.tobytes(), True
        elif first_sent:
            # 最后一块已经发过但标了 False → 补一个空 last，让上层知道结束
            yield b"", True

    async def _tone_chunks(self, text: str, speed: float) -> AsyncIterator[tuple[bytes, bool]]:
        """tone 占位合成 → 分块。

        纯 CPU，放到线程里不阻塞事件循环；用 ``iter_synthesize`` 逐词产出，
        首个 200 ms 块不必等整句渲染完（压低首包延迟）。
        """
        gen = _iter_from_thread(self._tone.iter_synthesize, text, speed, self._chunk_ms)
        async for item in self._chunk_stream(_to_array_stream(gen)):
            yield item

    async def _chunk_stream(
        self, arrays: AsyncIterator[np.ndarray]
    ) -> AsyncIterator[tuple[bytes, bool]]:
        """把不定长的 float32 片段流拼成定长 PCM16 块。

        最后一个块带 ``is_last=True``；块之间不补零、不丢弃样本（speech 连续）。
        为了"首块尽快"，只要凑满一个 ``audio_chunk_ms`` 就立刻 yield，不等整句。
        """
        chunk_n = self._chunk_samples()
        buf = np.zeros(0, dtype=np.float32)
        pending: bytes | None = None
        async for arr in arrays:
            piece = np.asarray(arr, dtype=np.float32).reshape(-1)
            if piece.size == 0:
                continue
            buf = np.concatenate((buf, piece)) if buf.size else piece
            while buf.size >= chunk_n:
                cur = float32_to_pcm16(buf[:chunk_n])
                buf = buf[chunk_n:]
                if pending is not None:
                    yield pending, False
                pending = cur
        if buf.size:
            cur = float32_to_pcm16(buf)
            if pending is not None:
                yield pending, False
            pending = cur
        if pending is not None:
            yield pending, True

    # ---- 真实后端 ----
    async def _real_chunks(
        self, text: str, persona_id: str, speed: float
    ) -> AsyncIterator[tuple[bytes, bool]]:
        """调用真实克隆后端（在线程里跑），产出定长 PCM16 块。"""
        arrays = _iter_from_thread(self._infer_sync, text, persona_id, speed)
        async for item in self._chunk_stream(_to_array_stream(arrays)):
            yield item

    def _infer_sync(self, text: str, persona_id: str, speed: float) -> Any:
        """（同步，跑在线程里）按后端产出 float32 片段。"""
        if self._backend == "cosyvoice2":
            yield from self._infer_cosyvoice(text, persona_id, speed)
            return
        if self._backend == "f5tts":
            yield from self._infer_f5tts(text, persona_id, speed)
            return
        raise _BackendUnavailable(f"未知后端: {self._backend}")

    def _ref_voice(self, persona_id: str) -> Any:
        lib = self._get_ref_library()
        if lib is None:
            raise TTSError("参考音频库不可用")
        return lib.ensure(persona_id)

    def _infer_cosyvoice(self, text: str, persona_id: str, speed: float) -> Any:
        """CosyVoice2 zero-shot（流式）→ yield float32 @24k。"""
        model = self._cosy
        if model is None:
            raise _BackendUnavailable("CosyVoice2 未加载")
        voice = self._ref_voice(persona_id)

        import torch  # noqa: PLC0415

        ref_audio = read_audio_file(voice.ref_wav, target_sr=16000)
        if ref_audio.size == 0:
            raise TTSError(f"参考音频为空: {voice.ref_wav}")
        prompt_speech = torch.from_numpy(ref_audio.astype(np.float32)).unsqueeze(0)

        fn = getattr(model, "inference_zero_shot", None)
        if not callable(fn):
            raise _BackendUnavailable("CosyVoice2 缺少 inference_zero_shot（版本不兼容）")

        # 按签名过滤参数：不同 commit 的 speed / stream / zero_shot_spk_id 支持不同
        args: dict[str, Any] = {
            "tts_text": text,
            "prompt_text": voice.ref_text,
            "prompt_speech_16k": prompt_speech,
            "stream": True,
            "speed": float(speed),
        }
        try:
            params = inspect.signature(fn).parameters
            positional_ok = {
                name for name, p in params.items()
                if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            }
            args = {k: v for k, v in args.items() if k in positional_ok or len(positional_ok) == 0}
        except (TypeError, ValueError):  # pragma: no cover
            pass

        try:
            iterator = fn(**args)
        except TypeError as exc:
            # 老版本签名：inference_zero_shot(tts_text, prompt_text, prompt_speech_16k, stream)
            log.debug("CosyVoice2 关键字调用失败（%s），退回位置参数", exc)
            iterator = fn(text, voice.ref_text, prompt_speech, True)

        for item in iterator:
            arr, sr = _to_numpy_audio(item)
            if sr and int(sr) != SR_OUT:
                arr = resample(arr, int(sr), SR_OUT)
            if arr.size:
                yield arr

    def _infer_f5tts(self, text: str, persona_id: str, speed: float) -> Any:
        """F5-TTS（非流式）：整句合成后 yield 一个数组，由上层切片。"""
        model = self._f5
        if model is None:
            raise _BackendUnavailable("F5-TTS 未加载")
        voice = self._ref_voice(persona_id)
        if not os.path.isfile(voice.ref_wav):
            raise TTSError(f"参考音频文件不存在: {voice.ref_wav}")

        kwargs: dict[str, Any] = {
            "ref_file": voice.ref_wav,
            "ref_text": voice.ref_text,
            "gen_text": text,
            "nfe_step": max(1, int(self.cfg.nfe_step)),
            "speed": float(speed),
        }
        fn = model.infer
        try:
            params = inspect.signature(fn).parameters
            kwargs = {k: v for k, v in kwargs.items() if k in params}
        except (TypeError, ValueError):  # pragma: no cover
            pass
        out = fn(**kwargs)

        if isinstance(out, tuple):
            wav = out[0]
            sr = int(out[1]) if len(out) > 1 and isinstance(out[1], (int, float)) else SR_OUT
        else:
            wav, sr = out, SR_OUT
        arr, sr2 = _to_numpy_audio(wav)
        sr = sr2 or sr or SR_OUT
        if arr.size == 0:
            raise TTSError("F5-TTS 返回空音频")
        if int(sr) != SR_OUT:
            arr = resample(arr, int(sr), SR_OUT)
        yield arr


def _aiter_to_sync(ait: AsyncIterator[Any]) -> Any:  # pragma: no cover - 保留兼容
    """已废弃：真实后端的分块在 :meth:`TTSEngine._real_chunks` 内完成。"""
    raise AssertionError("不应被调用：真实后端分块在 _real_chunks 内完成")


__all__ = [
    "TTSError",
    "AudioChunk",
    "TTSSentenceRequest",
    "TTSEngine",
    "normalize_for_tts",
    "split_for_tts",
]
