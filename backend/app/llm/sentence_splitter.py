"""流式断句器：LLM token 流 → 可朗读的句子。

对外契约见 ``docs/CONTRACTS.md`` 第 7 节。

为什么需要它
------------
首包延迟（``first_audio_ms``）完全由"第一句什么时候能交给 TTS"决定。
等待一个完整句号太慢，所以我们：

1. **只有拿到"句末标点 + 后续空白"才切**——这样流式（逐字符）与一次性喂入的结果
   完全一致（见 ``_find_cut`` 的纯函数设计）。
2. **首句激进**：还没产出任何句子时，逗号/分号/冒号、甚至空格都能作为切点，
   把第一句压到 ~60 字符以内（``first_chunk_max_chars``）。
3. **``max_chars`` 兜底**：任何情况下不超过 ``max_chars``，优先在空格处断开。

不切分的情形
------------
- 英文缩写：``Mr. Mrs. Ms. Dr. Prof. St. vs. e.g. i.e. etc. a.m. p.m. U.S. U.K.
  No. Jr. Sr. approx.`` 以及单字母缩写 ``J.``；
  （``No.`` 例外：只有后面跟数字时才视作 No. 3 这类编号缩写，否则 "I said no." 会正常断句）
- 小数 / 版本号：``3.5``、``v1.2.3``、``10:30``（标点后面紧跟数字则不切）
- 省略号：``...`` 与 ``…`` 作为**一个整体**处理，绝不会切出 ``..`` 碎片

用法::

    sp = StreamingSentenceSplitter(min_chars=2, max_chars=120, first_chunk_max_chars=60)
    for delta in stream:
        for sentence in sp.feed(delta):
            await tts(sentence)
    for sentence in sp.flush():
        await tts(sentence)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - 仅为类型提示
    from app.config import PipelineSettings

#: 强句末标点：一定会尝试断句
_STRONG_TERMINATORS = ".!?…"

#: 弱标点：只在"首句尚未产出且已够长"时断句（压低首包延迟）
_WEAK_BOUNDARIES = ",;:"

#: 紧随标点、应并入本句的收尾字符（引号 / 括号）
_CLOSERS = "\"')]}’”»」』"

#: 判定为缩写的词（小写、去掉内部点号后比较）
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "st", "vs", "eg", "ie", "etc",
        "am", "pm", "us", "uk", "no", "jr", "sr", "approx", "inc", "ltd",
        "fig", "al", "cf", "viz", "resp", "dept", "est",
    }
)

_WHITESPACE = " \t\r\f\v"


class StreamingSentenceSplitter:
    """把流式文本增量切成句子。"""

    def __init__(
        self,
        *,
        min_chars: int = 2,
        max_chars: int = 120,
        first_chunk_max_chars: int = 60,
    ) -> None:
        self.min_chars = max(1, int(min_chars))
        self.max_chars = max(self.min_chars + 1, int(max_chars))
        self.first_chunk_max_chars = max(self.min_chars, int(first_chunk_max_chars))
        #: 首句"遇空格也切"的阈值
        self.aggressive_space_chars = int(self.first_chunk_max_chars * 1.6)
        self._buf: str = ""
        self._n_sentences: int = 0

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @classmethod
    def from_settings(cls, pipe_cfg: "PipelineSettings | None" = None) -> "StreamingSentenceSplitter":
        """按 ``settings.pipeline`` 构造（不硬编码阈值）。"""
        if pipe_cfg is None:
            return cls()
        return cls(
            min_chars=pipe_cfg.sentence_min_chars,
            max_chars=pipe_cfg.sentence_max_chars,
            first_chunk_max_chars=pipe_cfg.first_sentence_max_chars,
        )

    def feed(self, delta: str) -> list[str]:
        """喂入一段增量，返回本次新完成的句子（0..n 个）。"""
        if delta:
            self._buf += delta
        out: list[str] = []
        while True:
            cut = self._find_cut(self._buf)
            if cut is None:
                break
            if cut <= 0:  # pragma: no cover - 防御：绝不原地打转
                break
            sentence = self._buf[:cut].strip()
            self._buf = self._buf[cut:].lstrip(_WHITESPACE)
            if not sentence:
                continue
            self._n_sentences += 1
            out.append(sentence)
            if not self._buf:
                break
        return out

    def flush(self) -> list[str]:
        """流结束：把残留文本作为最后一句返回（可能为空）。"""
        rest = self._buf.strip()
        self._buf = ""
        if not rest:
            return []
        # 纯标点/空白的残留没有朗读价值
        if not any(c.isalnum() for c in rest):
            return []
        if len(rest) < self.min_chars:
            return []
        self._n_sentences += 1
        return [rest]

    def reset(self) -> None:
        """重置状态（新的一轮对话）。"""
        self._buf = ""
        self._n_sentences = 0

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def pending(self) -> str:
        """尚未切出去的缓冲内容。"""
        return self._buf

    @property
    def sentences_emitted(self) -> int:
        """本回合已经产出的句子数。"""
        return self._n_sentences

    @property
    def has_pending(self) -> bool:
        return bool(self._buf.strip())

    # ------------------------------------------------------------------ #
    # 核心：寻找最早可切点
    # ------------------------------------------------------------------ #
    def _find_cut(self, buf: str) -> int | None:
        """返回最早的切点下标（句子 = ``buf[:cut]``），无可切点时返回 ``None``。

        这是一个**纯函数式**判定：只看 ``buf`` 的内容与已产出句数。
        因此"逐字符喂"与"一次性喂"得到完全相同的切分结果。
        """
        n = len(buf)
        if n == 0:
            return None
        # 切点不得晚于 max_chars：扫描到 limit 就停，保证判定只依赖 buf 的前缀，
        # 从而 "逐字符 feed" 与 "一次性 feed" 结果一致。
        limit = self.max_chars
        first = self._n_sentences == 0
        spaces = _WHITESPACE

        i = 0
        while i < n and i <= limit:
            ch = buf[i]

            # ---- 1) 换行：硬断点 -------------------------------------
            if ch == "\n":
                if self._sentence_ok(buf[:i], cut=i):
                    return i
                i += 1
                continue

            # ---- 2) 强句末标点 ---------------------------------------
            if ch in _STRONG_TERMINATORS:
                end = self._scan_terminator_run(buf, i)
                # 把收尾引号/括号并入本句
                while end < n and buf[end] in _CLOSERS:
                    end += 1
                if end >= n:
                    # 后面还没来字符：现在无法区分 "Mr." 与句末，等下一个 delta
                    i += 1
                    continue
                if buf[end] not in spaces:
                    # 后面紧跟数字/字母（3.5、v1.2.3、10:30）：不是断点
                    i += 1
                    continue
                if end > limit:
                    # 超过 max_chars：交给兜底逻辑
                    break
                if self._is_abbreviation(buf, i, end, ch):
                    i += 1
                    continue
                if self._sentence_ok(buf[:end], cut=end):
                    return end
                i += 1
                continue

            # ---- 3) 弱标点（仅首句、且已够长） ------------------------
            if first and ch in _WEAK_BOUNDARIES:
                if i + 1 < n and buf[i + 1] in spaces and i + 1 >= self.first_chunk_max_chars:
                    if i + 1 <= limit and self._sentence_ok(buf[: i + 1], cut=i + 1):
                        return i + 1
                i += 1
                continue

            # ---- 4) 首句够长时遇空格也切 ------------------------------
            if first and ch in spaces and i >= self.aggressive_space_chars:
                if i <= limit and self._sentence_ok(buf[:i], cut=i):
                    return i
                i += 1
                continue

            # ---- 5) max_chars 兜底 ------------------------------------
            if ch in spaces and i >= limit:
                if self._sentence_ok(buf[:i], cut=i):
                    return i
                i += 1
                continue

            i += 1

        # ---- 6) 超长且完全没有可切空白：硬切 --------------------------
        if n > limit:
            space = max(buf.rfind(" ", 0, limit), buf.rfind("\t", 0, limit))
            return space if space > 0 else limit
        return None

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _sentence_ok(self, piece: str, *, cut: int) -> bool:
        """候选句子是否达到 ``min_chars``（避免切出 ``Ok.`` 之外的碎片）。"""
        del cut  # 保留参数以便将来扩展
        if len(piece.strip()) < self.min_chars:
            return False
        # 纯标点/空白的片段没有意义
        return any(c.isalnum() for c in piece)

    @staticmethod
    def _scan_terminator_run(buf: str, i: int) -> int:
        """从下标 i 起扫过连续的句末标点，返回 run 之后的下标。

        ``...`` / ``?!`` / ``!?`` / ``……`` 都当作一个整体。
        """
        n = len(buf)
        j = i
        ch = buf[i]
        if ch == ".":
            while j < n and buf[j] == ".":
                j += 1
        elif ch == "…":
            while j < n and buf[j] == "…":
                j += 1
        else:
            while j < n and buf[j] in _STRONG_TERMINATORS and buf[j] not in "…":
                j += 1
        return j

    def _is_abbreviation(self, buf: str, i: int, end: int, ch: str) -> bool:
        """``buf[i:end]`` 处的句点是否属于缩写/编号。"""
        # 只有单个 '.' 才可能是缩写；'...' 是省略号（整体断句）
        if ch != "." or end - i != 1:
            return False

        # 往回取包含内部点号的词元，例如 "e.g" / "U.S" / "Mr"
        start = i
        while start > 0 and (buf[start - 1].isalpha() or buf[start - 1] == "."):
            start -= 1
        token = buf[start:i]
        if not token:
            return False

        normalized = token.replace(".", "").lower()
        if normalized == "no":
            # "No." 只有后面跟数字才是编号缩写（No. 3），否则 "I said no." 应断句
            k = end
            while k < len(buf) and buf[k] in _WHITESPACE:
                k += 1
            return k < len(buf) and buf[k].isdigit()
        if normalized in _ABBREVIATIONS:
            return True
        # 单字母缩写：J. / A. （首字母大写且只有一个字母）
        return len(token) == 1 and token.isalpha() and token.isupper()


def split_all(text: str, splitter: StreamingSentenceSplitter | None = None) -> list[str]:
    """一次性把整段文本切成句子（测试与 ``/api/echo`` 用）。"""
    sp = splitter or StreamingSentenceSplitter()
    return sp.feed(text) + sp.flush()


def iter_diff_by_char(text: str, splitter: StreamingSentenceSplitter | None = None) -> Iterable[str]:
    """逐字符喂入，仅用于测试/对照。"""
    sp = splitter or StreamingSentenceSplitter()
    for ch in text:
        yield from sp.feed(ch)
    yield from sp.flush()


__all__ = ["StreamingSentenceSplitter", "iter_diff_by_char", "split_all"]
