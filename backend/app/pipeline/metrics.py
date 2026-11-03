"""回合延迟指标：单回合分解 + 最近 N 回合的内存环形缓冲。

用于 ``GET /api/metrics/last`` 与 ``assistant_done.metrics``（见 ``docs/PROTOCOL.md`` 2.8）。

线程安全说明
------------
WS 每条连接跑在（可能不同的）事件循环里，但 :func:`record` 只做几次 list 操作，
用 ``collections.deque(maxlen=...)`` + ``threading.Lock`` 保护即可，
不依赖 asyncio 原语（避免跨 loop 的 ``Lock`` 绑定问题）。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Deque

from app.config import get_settings
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 浮点字段保留的小数位
_PRECISION = 1


def _r(value: float | int | None) -> float:
    """四舍五入到 1 位小数（``None`` → 0.0）。"""
    if value is None:
        return 0.0
    try:
        return round(float(value), _PRECISION)
    except (TypeError, ValueError):  # pragma: no cover - 防御
        return 0.0


@dataclass
class TurnMetrics:
    """单回合延迟分解（单位：毫秒；``audio_bytes`` 为字节数）。

    - ``stt_ms``：STT 耗时（文字输入时恒为 0）
    - ``llm_ttft_ms``：从 ``run_turn`` 开始到 LLM 首个 token 的时间
    - ``first_audio_ms``：从 ``run_turn`` 开始到**第一条 audio 帧**发出的时间（核心 SLO）
    - ``total_ms``：整回合总耗时（含最后一句音频发完）
    - ``tts_first_sentence_ms``：第一句从交给 TTS 到第一个 chunk 就绪的耗时
    - ``sentences``：本回合合成/发送的句数
    - ``audio_bytes``：本回合发出的 PCM 字节数
    """

    turn_id: int = 0
    stt_ms: float = 0.0
    llm_ttft_ms: float = 0.0
    first_audio_ms: float = 0.0
    total_ms: float = 0.0
    tts_first_sentence_ms: float = 0.0
    sentences: int = 0
    audio_bytes: int = 0
    #: 附加信息（不参与 to_dict 的固定字段，便于调试）
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """协议友好视图：浮点保留 1 位小数，字段顺序固定。"""
        return {
            "stt_ms": _r(self.stt_ms),
            "llm_ttft_ms": _r(self.llm_ttft_ms),
            "first_audio_ms": _r(self.first_audio_ms),
            "total_ms": _r(self.total_ms),
            "tts_first_sentence_ms": _r(self.tts_first_sentence_ms),
            "sentences": int(self.sentences),
            "audio_bytes": int(self.audio_bytes),
        }

    def to_full_dict(self) -> dict[str, Any]:
        """含 ``turn_id`` 的完整视图（``/api/metrics/last`` 用）。"""
        out = {"turn_id": int(self.turn_id)}
        out.update(self.to_dict())
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    def as_flat(self) -> dict[str, Any]:
        """原样 dataclass 视图（调试/日志用）。"""
        return asdict(self)


# --------------------------------------------------------------------------- #
# 模块级环形缓冲
# --------------------------------------------------------------------------- #
_LOCK = threading.Lock()
_BUFFER: Deque[dict[str, Any]] = deque(maxlen=max(1, get_settings().metrics.keep_last))
_TOTAL = 0


def _resize_locked(keep: int) -> None:
    global _BUFFER
    keep = max(1, int(keep))
    if _BUFFER.maxlen != keep:
        _BUFFER = deque(_BUFFER, maxlen=keep)


def record(metrics: TurnMetrics, *, keep_last: int | None = None) -> dict[str, Any]:
    """记录一回合指标，返回写入的 dict 视图。"""
    global _TOTAL
    cfg = get_settings()
    payload = metrics.to_full_dict()
    with _LOCK:
        if keep_last is not None:
            _resize_locked(keep_last)
        else:
            _resize_locked(cfg.metrics.keep_last)
        if cfg.metrics.enabled:
            _BUFFER.append(payload)
        _TOTAL += 1
    log.debug("turn metrics: %s", payload)
    return payload


def last(n: int = 1) -> dict[str, Any]:
    """最近一回合（无数据返回 ``{}``）。``n>1`` 时返回最后 n 回合的列表视图？——不，
    契约要求 ``/api/metrics/last`` 返回单个对象，故 ``n`` 仅用于未来扩展，
    这里始终返回最近一条。
    """
    del n
    with _LOCK:
        return dict(_BUFFER[-1]) if _BUFFER else {}


def history(limit: int | None = None) -> list[dict[str, Any]]:
    """最近的指标列表（旧 → 新）。"""
    with _LOCK:
        items = list(_BUFFER)
    if limit is not None and limit >= 0:
        items = items[-limit:] if limit else []
    return [dict(i) for i in items]


def average(field_name: str = "first_audio_ms") -> float:
    """最近窗口内某字段的均值（无数据返回 0.0），用于本地性能看板。"""
    with _LOCK:
        values = [float(i.get(field_name, 0.0) or 0.0) for i in _BUFFER]
    return round(sum(values) / len(values), _PRECISION) if values else 0.0


def count() -> int:
    """进程启动以来记录过的总回合数。"""
    with _LOCK:
        return _TOTAL


def clear() -> None:
    """清空缓冲（测试用）。"""
    global _TOTAL
    with _LOCK:
        _BUFFER.clear()
        _TOTAL = 0


__all__ = [
    "TurnMetrics",
    "average",
    "clear",
    "count",
    "history",
    "last",
    "record",
]
