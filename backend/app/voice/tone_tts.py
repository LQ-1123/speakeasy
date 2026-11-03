"""无模型占位 TTS：把英文句子合成成"能听出节奏的含糊语音"。

定位：**全局兜底**。当没有 GPU、没有 CosyVoice/F5-TTS 权重、或者克隆后端在某一句
上失败时，``TTSEngine`` 会落到 ``tone`` 后端。目标不是好听，而是

1. 链路完整（前端能拿到 PCM、能播放、能测延迟）；
2. 有可辨识的**音节节奏**与停顿，能听出"这是一句话"；
3. 有可辨识的疑问/陈述语调差异，便于人工检查韵律通路。

实现：纯 numpy，**不依赖 torch**。每个音节是一组谐波正弦（基频 120–260 Hz +
共振峰加权），带起振/衰减包络与轻微颤音；词间 30–60 ms 间隙、句首句尾各 80 ms
静音；整句有音高下倾（declination），问句末音节上扬。

时间尺度：按字符数估算，约 62 ms/字符（55–70 ms 区间内），下限 0.4 s。语速由
``speed`` 缩放（1.1 = 快 10%）。
"""

from __future__ import annotations

import re
import zlib
from typing import Iterator

import numpy as np

from app.voice.audio_utils import SR_OUT

#: 每字符对应的目标发音时长（秒）。落在需求给的 55–70 ms 区间。
SEC_PER_CHAR = 0.062
#: 最短句子时长（秒）
MIN_DURATION_S = 0.4
#: 句首/句尾静音（秒）
EDGE_SILENCE_S = 0.08
#: 词间间隙范围（秒），按词做确定性抖动，让节奏不机械
WORD_GAP_MIN_S = 0.030
WORD_GAP_MAX_S = 0.060

#: 常见英文元音的 (F1, F2) 近似值（Hz），用于给谐波加权，制造"元音感"
_VOWEL_FORMANTS: tuple[tuple[float, float], ...] = (
    (730.0, 1090.0),   # /a/
    (270.0, 2290.0),   # /i/
    (530.0, 1840.0),   # /e/
    (570.0, 840.0),    # /o/
    (300.0, 870.0),    # /u/
    (660.0, 1720.0),   # /ae/
    (490.0, 1350.0),   # schwa
)

_PUNCT_RE = re.compile(r"[^\w'\-]+", re.UNICODE)
_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_QUESTION_TAIL_RE = re.compile(r"[?][\"')\]]*$")
_EXCLAIM_TAIL_RE = re.compile(r"[!][\"')\]]*$")


def _stable_seed(text: str) -> int:
    """确定性种子（内置 ``hash()`` 跨进程不稳定，会破坏可复现性）。"""
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


def _estimate_syllables(word: str) -> int:
    """粗估音节数：按元音簇数，夹到 1..3。"""
    groups = re.findall(r"[aeiouy]+", word.lower())
    n = len(groups)
    if word.lower().endswith(("e", "es", "ed")) and n > 1:
        n -= 1       # 不发音的 e
    return max(1, min(3, n))


class ToneSynthesizer:
    """把文本渲染成 24 kHz float32 波形 / PCM16。

    Args:
        sample_rate: 输出采样率（默认 24000，与契约一致）。
        base_f0: 基础基频（Hz）。默认 168（中性偏低，男女都能听）。
        vibrato_hz: 颤音频率。
        vibrato_depth: 颤音深度（相对基频的比例，0.018 ≈ 1.8%）。
    """

    def __init__(
        self,
        sample_rate: int = SR_OUT,
        base_f0: float = 168.0,
        vibrato_hz: float = 5.5,
        vibrato_depth: float = 0.018,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError(f"非法采样率: {sample_rate}")
        self.sr = int(sample_rate)
        self.base_f0 = float(base_f0)
        self.vibrato_hz = float(vibrato_hz)
        self.vibrato_depth = float(vibrato_depth)

    # ------------------------------------------------------------------ #
    # 时长估算
    # ------------------------------------------------------------------ #
    def estimate_duration_s(self, text: str, speed: float = 1.0) -> float:
        """按字符数估算合成时长（秒），含首尾静音。"""
        clean = _PUNCT_RE.sub(" ", text or "").strip()
        n = len(clean) or len((text or "").strip())
        speech_s = max(MIN_DURATION_S, n * SEC_PER_CHAR)
        speed = _clamp_speed(speed)
        return (speech_s + 2 * EDGE_SILENCE_S) / speed

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def synthesize(self, text: str, speed: float = 1.0) -> np.ndarray:
        """合成整句 → float32 @ ``sample_rate``（峰值归一化到 0.55）。"""
        parts = list(self._iter_segments(text, speed))
        if not parts:
            return np.zeros(0, dtype=np.float32)
        out = np.concatenate(parts).astype(np.float32)
        return _normalize(out, 0.55)

    def synthesize_pcm16(self, text: str, speed: float = 1.0) -> bytes:
        """合成整句 → PCM16 小端 mono bytes。"""
        from app.voice.audio_utils import float32_to_pcm16  # 局部 import 避免循环

        return float32_to_pcm16(self.synthesize(text, speed))

    def iter_synthesize(
        self,
        text: str,
        speed: float = 1.0,
        chunk_ms: int = 200,
    ) -> Iterator[np.ndarray]:
        """流式合成：逐块 yield float32 片段（每块 ≈ ``chunk_ms``）。

        首块在生成到第一个 chunk 长度时立刻产出，不必等整句渲染完。
        """
        chunk_samples = max(1, int(self.sr * max(1, int(chunk_ms)) / 1000))
        buf = np.zeros(0, dtype=np.float32)
        for seg in self._iter_segments(text, speed):
            buf = np.concatenate((buf, seg)) if buf.size else seg
            while buf.size >= chunk_samples:
                piece, buf = buf[:chunk_samples], buf[chunk_samples:]
                yield _apply_gain(piece)
        if buf.size:
            yield _apply_gain(buf)

    # ------------------------------------------------------------------ #
    # 内部分段
    # ------------------------------------------------------------------ #
    def _iter_segments(self, text: str, speed: float) -> Iterator[np.ndarray]:
        """按词产出音频片段（含词前间隙）。"""
        raw = (text or "").strip()
        if not raw:
            return
        speed = _clamp_speed(speed)
        words = _WORD_RE.findall(raw)
        if not words:
            # 纯标点/数字以外的符号：给一段提示性的短音，避免"静默但成功"
            words = ["hmm"]

        is_question = bool(_QUESTION_TAIL_RE.search(raw))
        is_exclaim = bool(_EXCLAIM_TAIL_RE.search(raw))

        rng = np.random.default_rng(_stable_seed(raw))
        lead = int(round(self.sr * EDGE_SILENCE_S))
        trail = int(round(self.sr * EDGE_SILENCE_S))

        char_total = sum(max(1, len(w)) for w in words)
        speech_budget = max(MIN_DURATION_S, char_total * SEC_PER_CHAR) / speed
        # 预留词间间隙
        gaps = [self._word_gap(w) for w in words]
        gap_total = sum(gaps[:-1]) if len(gaps) > 1 else 0.0
        word_time = max(MIN_DURATION_S * 0.2, speech_budget - gap_total)

        yield np.zeros(lead, dtype=np.float32)

        cursor = 0.0                     # 0..1 句子进度，用于音高下倾
        phase = rng.uniform(0.0, 2.0 * np.pi)
        n_words = len(words)

        for i, word in enumerate(words):
            share = max(1, len(word)) / char_total
            slot_s = max(0.10 / speed, word_time * share)

            # ---- 音高：全句下倾 + 末音节扬/降 ----
            prog = i / max(1, n_words - 1) if n_words > 1 else 0.0
            decl = 1.06 - 0.12 * prog            # 1.06 → 0.94
            if i == n_words - 1:
                if is_question:
                    decl *= 1.20                 # 问句：末词上扬
                elif is_exclaim:
                    decl *= 1.06
                else:
                    decl *= 0.90                 # 陈述：末词下沉
            f0 = self.base_f0 * decl

            n_syl = 1 if len(word) <= 4 else min(2, _estimate_syllables(word))
            seg, phase = self._render_word(
                word=word, f0=f0, slot_s=slot_s, n_syl=n_syl,
                rng=rng, phase=phase,
            )
            yield seg
            cursor = prog

            if i < n_words - 1:
                yield np.zeros(int(round(self.sr * gaps[i] / speed)), dtype=np.float32)

        yield np.zeros(trail, dtype=np.float32)
        _ = cursor  # 显式说明：进度只用于音高，不参与时长

    @staticmethod
    def _word_gap(word: str) -> float:
        """由词的 crc32 决定的确定性间隙（30–60 ms），非标点词略短。"""
        span = WORD_GAP_MAX_S - WORD_GAP_MIN_S
        frac = (zlib.crc32(word.lower().encode("utf-8")) % 1000) / 999.0
        gap = WORD_GAP_MIN_S + span * frac
        if len(word) <= 2:                   # 虚词说得快，间隙短一点
            gap *= 0.8
        return float(gap)

    # ------------------------------------------------------------------ #
    # 单词语音渲染
    # ------------------------------------------------------------------ #
    def _render_word(
        self,
        *,
        word: str,
        f0: float,
        slot_s: float,
        n_syl: int,
        rng: np.random.Generator,
        phase: float,
    ) -> tuple[np.ndarray, float]:
        """渲染一个词（1–2 个音节）→ (波形, 新相位)。"""
        sr = self.sr
        word_seed = _stable_seed(word.lower())
        syl_slot = slot_s / n_syl
        pieces: list[np.ndarray] = []

        for k in range(n_syl):
            # 音节时长：占 slot 的 78%，其余留作词内微停顿
            dur_s = max(0.055, syl_slot * 0.78)
            n = max(16, int(round(dur_s * sr)))

            # 音节内轻微音高滑动（第 2 音节略高，更像词重音）
            syl_lift = 1.0 + (0.05 if k == 1 else 0.0)
            f_start = f0 * syl_lift * (1.0 + rng.uniform(-0.03, 0.03))
            f_end = f_start * (1.0 + rng.uniform(-0.05, 0.03))

            t = np.arange(n, dtype=np.float64) / sr
            f_inst = np.linspace(f_start, f_end, n)
            f_inst *= 1.0 + self.vibrato_depth * np.sin(
                2.0 * np.pi * self.vibrato_hz * t + phase + 0.7 * k
            )
            # 相位积分（避免频率突变造成的爆音）
            phase_rad = 2.0 * np.pi * np.cumsum(f_inst) / sr + phase

            vowel = _VOWEL_FORMANTS[(word_seed + 7 * k) % len(_VOWEL_FORMANTS)]
            sig = self._harmonic_bank(phase_rad, f0=f_start, formants=vowel)

            # ---- 包络：快起 + 缓落 + 轻微音节内起伏 ----
            env = np.ones(n, dtype=np.float32)
            att = min(n, max(8, int(0.022 * sr)))
            rel = min(n - att if n - att > 8 else max(8, n // 4), max(8, int(0.045 * sr)))
            env[:att] = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, att))
            env[n - rel:] = 0.5 + 0.5 * np.cos(np.linspace(0.0, np.pi, rel))
            # 轻微 "呼吸感" 起伏
            env *= (0.88 + 0.12 * np.sin(2.0 * np.pi * 3.1 * t + phase)).astype(np.float32)
            syl = (sig * env).astype(np.float32)

            # ---- 词首/音节首辅音感：8 ms 柔和的噪声瞬态（让节奏可听） ----
            if k == 0 and word[0].lower() not in "aeiou":
                nz = max(4, int(0.008 * sr))
                burst = rng.standard_normal(nz).astype(np.float32)
                # 一阶低通，去掉刺耳的极高频
                for idx in range(1, nz):
                    burst[idx] = 0.72 * burst[idx - 1] + 0.28 * burst[idx]
                fade = np.linspace(1.0, 0.0, nz, dtype=np.float32) ** 2
                burst = burst * fade * 0.055
                syl[:nz] += burst

            peak = float(np.max(np.abs(syl))) if syl.size else 0.0
            if peak > 1e-9:
                syl *= 0.42 / peak
            pieces.append(syl)

            if k < n_syl - 1:
                gap = int(round(0.018 * sr))
                pieces.append(np.zeros(gap, dtype=np.float32))

            phase = float(phase_rad[-1]) if phase_rad.size else phase

        if not pieces:
            return np.zeros(0, dtype=np.float32), phase
        return np.concatenate(pieces).astype(np.float32), phase

    def _harmonic_bank(
        self,
        phase_rad: np.ndarray,
        *,
        f0: float,
        formants: tuple[float, float],
    ) -> np.ndarray:
        """基频的谐波叠加，按共振峰加权（制造元音音色）。"""
        nyq = self.sr * 0.5
        f1, f2 = formants
        out = np.zeros(phase_rad.size, dtype=np.float32)
        max_k = int(min(14, max(1, (0.45 * self.sr) / max(40.0, f0))))
        for h in range(1, max_k + 1):
            fh = f0 * h
            if fh >= nyq * 0.92:
                break
            amp = 1.0 / (h ** 1.15)
            amp += 1.10 * float(np.exp(-((fh - f1) / 260.0) ** 2))
            amp += 0.60 * float(np.exp(-((fh - f2) / 420.0) ** 2))
            # 高次谐波继续衰减，避免"电子琴"感
            amp *= 1.0 / (1.0 + 0.06 * h)
            out += (amp * np.sin(h * phase_rad)).astype(np.float32)
        return out


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _clamp_speed(speed: float) -> float:
    try:
        s = float(speed)
    except (TypeError, ValueError):
        return 1.0
    if not np.isfinite(s):
        return 1.0
    return float(min(2.0, max(0.5, s)))


def _normalize(x: np.ndarray, target: float, known_peak: float | None = None) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr
    peak = known_peak if known_peak and known_peak > 1e-9 else float(np.max(np.abs(arr)))
    if peak > 1e-9 and peak > 0.0:
        arr = arr * np.float32(min(8.0, float(target) / peak))
    np.clip(arr, -1.0, 1.0, out=arr)
    return arr


#: 单个音节渲染后峰值被归一到 0.42；流式分块时统一乘这个固定增益。
#: **固定**增益很关键——若按每块自己的峰值归一化，块与块之间音量会一跳一跳。
_STREAM_GAIN = 0.55 / 0.42


def _apply_gain(x: np.ndarray) -> np.ndarray:
    """对已按音节归一化的波形施加固定增益 + clip（跨块音量一致）。"""
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr
    arr = arr * np.float32(_STREAM_GAIN)
    np.clip(arr, -1.0, 1.0, out=arr)
    return arr


def synthesize_text(text: str, sample_rate: int = SR_OUT, speed: float = 1.0) -> bytes:
    """便捷函数：文本 → PCM16@24k（供测试与兜底路径直接调用）。"""
    return ToneSynthesizer(sample_rate).synthesize_pcm16(text, speed)


__all__ = [
    "SEC_PER_CHAR",
    "MIN_DURATION_S",
    "EDGE_SILENCE_S",
    "ToneSynthesizer",
    "synthesize_text",
]
