"""SpeakEasy 2.0 · 声线原型底池 + 好友语音合成。

契约见 ``docs/V2_ARCHITECTURE.md`` §4.2：:

    load_archetypes(path=None) -> list[dict]     # 读 voice_archetypes.json
    archetype_by_id(aid: str) -> dict | None
    class FriendVoiceSynth:
        async synth_to_wav(text, voice_id, *, rate="+0%", pitch="+0Hz",
                           out_dir) -> tuple[str, int]   # (绝对 wav 路径, duration_ms)

设计
----
- **edge 优先**：用 :class:`EdgeTTSEngine.synthesize_voice`（已支持 ``rate`` / ``pitch``
  关键字）合成 24k PCM；任何异常（未联网 / 声线下线 / 解码失败）都降级到
  :class:`ToneSynthesizer`，保证接口永不 500。
- **进程内缓存**：按 ``(text, voice_id, rate, pitch)`` 缓存已落盘的 wav 与时长，
  命中时直接返回同一文件（不再请求 edge），也避免重复写盘。
- **落盘**：``uuid`` 文件名 + :func:`app.voice.audio_utils.write_wav`（16-bit mono wav）。
- **离线单测**：fake 掉 ``EdgeTTSEngine.synthesize_voice`` 即可，无需网络。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections import OrderedDict
from functools import lru_cache
from typing import Any

import numpy as np

from app.config import get_settings
from app.logging_utils import get_logger
from app.voice.audio_utils import SR_OUT, write_wav
from app.voice.edge_tts_engine import EdgeTTSEngine
from app.voice.tone_tts import ToneSynthesizer

log = get_logger(__name__)

#: 声线原型底池的默认路径（``backend/app/personas/voice_archetypes.json``）。
ARCHETYPES_PATH: str = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "personas", "voice_archetypes.json")
)

#: edge-tts 的韵律参数格式：``"+10%"`` / ``"-2%"``、``"+2Hz"`` / ``"-3Hz"``。
_RATE_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*%\s*$")
_PITCH_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*hz\s*$", re.IGNORECASE)

_WAV_PREFIX = "tts_"


# --------------------------------------------------------------------------- #
# 声线原型加载
# --------------------------------------------------------------------------- #
def resolve_archetypes_path(path: str | None = None) -> str:
    """把用户传入的路径（可为 None）解析成绝对路径。"""
    if not path:
        return ARCHETYPES_PATH
    return os.path.abspath(os.path.expanduser(os.fspath(path)))


@lru_cache(maxsize=8)
def _load_archetypes_cached(path: str) -> tuple[dict[str, Any], ...]:
    """按路径缓存原始 JSON（只读；对外返回浅拷贝）。"""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    if isinstance(raw, dict):
        items = raw.get("archetypes") or raw.get("voices") or []
    elif isinstance(raw, list):
        items = raw
    else:  # pragma: no cover - 数据文件损坏
        raise ValueError(f"voice_archetypes 格式非法（期望 object/array）: {path}")

    archetypes = tuple(dict(item) for item in items if isinstance(item, dict))
    if not archetypes:  # pragma: no cover - 数据文件为空
        raise ValueError(f"voice_archetypes 为空: {path}")
    return archetypes


def load_archetypes(path: str | None = None) -> list[dict[str, Any]]:
    """读取声线原型底池（带缓存），返回 ``list[dict]``（元素为浅拷贝，可安全修改）。"""
    resolved = resolve_archetypes_path(path)
    return [dict(a) for a in _load_archetypes_cached(resolved)]


def archetype_by_id(aid: str) -> dict[str, Any] | None:
    """按 ``archetype_id`` 查原型；不存在返回 ``None``。"""
    if not aid:
        return None
    target = str(aid).strip()
    for archetype in load_archetypes():
        if str(archetype.get("archetype_id") or "") == target:
            return archetype
    return None


# --------------------------------------------------------------------------- #
# 韵律参数小工具
# --------------------------------------------------------------------------- #
def rate_to_speed(rate: str | None) -> float:
    """``"+10%"`` → ``1.1``（tone 兜底用的语速倍率），非法值返回 1.0。"""
    m = _RATE_RE.match(str(rate or "").strip())
    if not m:
        return 1.0
    try:
        value = 1.0 + float(m.group(1)) / 100.0
    except (TypeError, ValueError):  # pragma: no cover - 正则已保证
        return 1.0
    return float(min(2.0, max(0.5, value)))


def pitch_to_hz(pitch: str | None) -> float:
    """``"+4Hz"`` → ``4.0``；非法值返回 0.0。"""
    m = _PITCH_RE.match(str(pitch or "").strip())
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except (TypeError, ValueError):  # pragma: no cover - 正则已保证
        return 0.0


def default_audio_dir() -> str:
    """默认落盘目录：``<data_path>/audio``（契约 §4.2 / §5 音频地址）。"""
    return os.path.join(get_settings().data_path, "audio")


# --------------------------------------------------------------------------- #
# 合成
# --------------------------------------------------------------------------- #
class FriendVoiceSynth:
    """每条好友语音的合成器：edge 主 + tone 兜底 + 进程内缓存。

    Args:
        sample_rate: 输出采样率，默认 24000（契约固定）。
        engine: 可注入的 :class:`EdgeTTSEngine`（测试方便）；默认惰性创建。
        tone: 可注入的 :class:`ToneSynthesizer`；默认惰性创建。
        cache_max_entries: 内存里最多缓存的 (text, voice, rate, pitch) 条数。
    """

    backend_name = "edge"

    def __init__(
        self,
        sample_rate: int = SR_OUT,
        *,
        engine: EdgeTTSEngine | None = None,
        tone: ToneSynthesizer | None = None,
        cache_max_entries: int = 512,
    ) -> None:
        sample_rate = int(sample_rate or SR_OUT)
        if sample_rate <= 0:
            raise ValueError(f"非法采样率: {sample_rate}")
        self.sample_rate = sample_rate
        self._engine = engine
        self._tone = tone
        self._cache_max_entries = max(8, int(cache_max_entries))
        self._cache: "OrderedDict[tuple[str, str, str, str], tuple[str, int]]" = OrderedDict()

    # ---- 惰性依赖 ----
    @property
    def engine(self) -> EdgeTTSEngine:
        if self._engine is None:
            self._engine = EdgeTTSEngine(sample_rate=self.sample_rate)
        return self._engine

    @property
    def tone(self) -> ToneSynthesizer:
        if self._tone is None:
            self._tone = ToneSynthesizer(self.sample_rate)
        return self._tone

    # ---- 缓存 ----
    def _cache_key(self, text: str, voice_id: str, rate: str, pitch: str) -> tuple[str, str, str, str]:
        strict = os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes")
        return (text, voice_id, rate, pitch + (":strict" if strict else ""))

    def _cache_get(self, key: tuple[str, str, str, str]) -> tuple[str, int] | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        path, duration_ms = hit
        if not os.path.isfile(path):
            # 文件被清理/移动：缓存失效，重新合成
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return path, duration_ms

    def _cache_put(self, key: tuple[str, str, str, str], path: str, duration_ms: int) -> None:
        self._cache[key] = (path, duration_ms)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_max_entries:
            self._cache.popitem(last=False)

    def clear_cache(self) -> None:
        """清空进程内缓存（不会删除已落盘 wav）。"""
        self._cache.clear()

    def cache_stats(self) -> dict[str, int]:
        return {"entries": len(self._cache), "max_entries": self._cache_max_entries}

    # ---- 主入口 ----
    async def synth_to_wav(
        self,
        text: str,
        voice_id: str,
        *,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        out_dir: str | None = None,
    ) -> tuple[str, int]:
        """合成一条好友语音并落盘为 24k mono wav。

        Args:
            text: 逐字稿（英文口语，1-2 句）。
            voice_id: edge-tts 声线名（如 ``en-GB-RyanNeural``）。
            rate: 语速，edge 格式 ``"+0%"``。
            pitch: 音高，edge 格式 ``"+0Hz"``。
            out_dir: 输出目录；缺省 ``<data_path>/audio``。目录不存在会自动创建。

        Returns:
            ``(绝对 wav 路径, duration_ms)``；缓存命中时返回同一路径、不再合成。

        Raises:
            ValueError: ``text`` / ``voice_id`` 为空（属调用方编程错误）。
        """
        clean_text = (text or "").strip()
        voice = (voice_id or "").strip()
        if not clean_text:
            raise ValueError("text 不能为空")
        if not voice:
            raise ValueError("voice_id 不能为空")

        rate = str(rate or "+0%").strip() or "+0%"
        pitch = str(pitch or "+0Hz").strip() or "+0Hz"
        target_dir = os.path.abspath(out_dir or default_audio_dir())

        key = self._cache_key(clean_text, voice, rate, pitch + "|" + target_dir)
        cached = self._cache_get(key)
        if cached is not None:
            log.debug("FriendVoiceSynth 缓存命中: %r voice=%s", clean_text[:30], voice)
            return cached

        pcm = await self._synthesize_pcm(clean_text, voice, rate=rate, pitch=pitch)

        os.makedirs(target_dir, exist_ok=True)
        wav_path = os.path.join(target_dir, f"{_WAV_PREFIX}{uuid.uuid4().hex}.wav")
        # 写盘是同步 IO，放到线程里避免阻塞事件循环
        await asyncio.to_thread(write_wav, wav_path, pcm, self.sample_rate)

        duration_ms = max(1, int(round(float(pcm.size) * 1000.0 / self.sample_rate)))
        self._cache_put(key, wav_path, duration_ms)
        log.info(
            "合成好友语音：voice=%s rate=%s pitch=%s duration=%dms file=%s",
            voice, rate, pitch, duration_ms, os.path.basename(wav_path),
        )
        return wav_path, duration_ms

    # ---- 内核：edge 优先 / tone 兜底 ----
    async def _synthesize_pcm(
        self, text: str, voice_id: str, *, rate: str, pitch: str
    ) -> np.ndarray:
        try:
            pcm = await self.engine.synthesize_voice(text, voice_id, rate=rate, pitch=pitch)
            arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
            if arr.size > 0:
                return arr
            if os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes"):
                raise RuntimeError("empty-speech")
            log.warning("Edge 合成返回空音频（text=%r voice=%s），降级 tone", text[:40], voice_id)
        except asyncio.CancelledError:
            raise  # 取消必须透传（barge-in / 关机）
        except Exception as exc:  # noqa: BLE001 - TTS 失败必须降级
            if os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes"):
                raise
            log.warning(
                "Edge 合成失败（text=%r voice=%s: %s: %s），降级 tone",
                text[:40], voice_id, type(exc).__name__, exc,
            )
        return self._tone_pcm(text, rate=rate, pitch=pitch)

    def _tone_pcm(self, text: str, *, rate: str, pitch: str) -> np.ndarray:
        """tone 占位合成（绝不让链路断）：rate→语速，pitch→轻微基频偏移。"""
        synth = self._tone
        if synth is None:
            hz = pitch_to_hz(pitch)
            synth = ToneSynthesizer(self.sample_rate, base_f0=float(max(80.0, 168.0 + hz * 2.0)))
            self._tone = synth
        arr = synth.synthesize(text, speed=rate_to_speed(rate))
        return np.asarray(arr, dtype=np.float32).reshape(-1)


__all__ = [
    "ARCHETYPES_PATH",
    "FriendVoiceSynth",
    "archetype_by_id",
    "default_audio_dir",
    "load_archetypes",
    "pitch_to_hz",
    "rate_to_speed",
    "resolve_archetypes_path",
]
