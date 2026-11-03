"""VAD 测试：事件序列、preroll 不吃字、短噪声过滤、超长截断、flush。

全部使用 energy 后端（无 torch 依赖）。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.config import VADSettings
from app.voice.vad import StreamingVAD, VADEvent


def make_vad(**overrides) -> StreamingVAD:
    """构造 energy 后端 VAD；overrides 覆盖默认值（不会重复传参）。"""
    base = dict(
        backend="energy",
        threshold=0.5,
        min_silence_ms=300,
        min_speech_ms=100,
        speech_pad_ms=150,
        max_utterance_s=5.0,
        energy_threshold=0.02,
        energy_abs_floor=0.0,
    )
    base.update(overrides)
    return StreamingVAD(VADSettings(**base))


def silence(ms: int, sr: int = 16000, level: float = 0.0005) -> bytes:
    n = int(sr * ms / 1000)
    x = (np.random.default_rng(1).standard_normal(n).astype(np.float32) * level)
    return np.clip(x * 32767, -32768, 32767).astype(np.int16).tobytes()


def tone(ms: int, sr: int = 16000, amp: float = 0.35, freq: float = 300.0) -> bytes:
    n = int(sr * ms / 1000)
    t = np.arange(n) / sr
    x = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.clip(x * 32767, -32768, 32767).astype(np.int16).tobytes()


def feed_all(vad: StreamingVAD, pcm: bytes, frame_ms: int = 20) -> list:
    """按 20ms 帧喂入，返回所有非空结果。"""
    sr = 16000
    step = int(sr * frame_ms / 1000) * 2  # bytes
    out = []
    for i in range(0, len(pcm), step):
        r = vad.push(pcm[i : i + step])
        if r and r.event:
            out.append(r)
    return out


def test_energy_backend_selected():
    v = make_vad()
    # backend_name 在首次 push 之前是 "pending"（懒探测），探测后应是具体名字
    feed_all(v, silence(100))
    assert v.backend_name in ("energy", "silero", "pending"), v.backend_name
    # 显式要求 energy 时，探测后必须就是 energy（本机无 silero）
    assert v.backend_name == "energy", f"期望 energy，实际 {v.backend_name}"


def test_starts_idle():
    v = make_vad()
    assert not v.is_speaking


def test_speech_start_and_end_events():
    v = make_vad()
    events = feed_all(v, silence(300) + tone(600) + silence(500))
    kinds = [e.event for e in events]
    assert VADEvent.SPEECH_START in kinds, f"应检测到语音开始，实际={kinds}"
    assert VADEvent.SPEECH_END in kinds, f"应检测到语音结束，实际={kinds}"
    # 顺序正确
    assert kinds.index(VADEvent.SPEECH_START) < kinds.index(VADEvent.SPEECH_END)


def test_utterance_produced_on_speech_end():
    v = make_vad()
    events = feed_all(v, silence(300) + tone(600) + silence(600))
    ends = [e for e in events if e.event is VADEvent.SPEECH_END]
    assert len(ends) == 1
    assert ends[0].utterance is not None
    assert len(ends[0].utterance) > 0
    # 16-bit → 字节数应为偶数
    assert len(ends[0].utterance) % 2 == 0


def test_preroll_does_not_eat_leading_audio():
    """preroll 必须把语音开始前 pad_ms 的音频带上，否则会吃字。"""
    v = make_vad(speech_pad_ms=150)
    speech_ms = 600
    events = feed_all(v, silence(400) + tone(speech_ms) + silence(600))
    ends = [e for e in events if e.event is VADEvent.SPEECH_END]
    assert ends, "未产生语音结束事件"

    utt_bytes = len(ends[0].utterance)
    utt_ms = utt_bytes / 2 / 16000 * 1000

    # 至少要覆盖语音本身 + 一点尾音；过多则说明 preroll 没生效
    assert utt_ms >= speech_ms, f"utterance {utt_ms:.0f}ms 短于语音 {speech_ms}ms，食物前段"
    # 应包含 preroll（150ms）的量级，但不该把前面 400ms 全带上
    assert utt_ms <= speech_ms + 900, f"utterance {utt_ms:.0f}ms 过长（preroll 过大）"


def test_short_noise_is_filtered():
    """短于 min_speech_ms 的噪声不应产出 utterance。"""
    v = make_vad(min_speech_ms=200)
    events = feed_all(v, silence(300) + tone(40) + silence(500))
    ends = [e for e in events if e.event is VADEvent.SPEECH_END]
    assert not ends or all(e.utterance is None for e in ends), (
        "短促噪声不应产生 utterance"
    )


def test_pure_silence_produces_nothing():
    v = make_vad()
    events = feed_all(v, silence(2000))
    assert events == []


def test_max_utterance_forces_cut():
    """超长语音必须被强制截断，不能无限累积。"""
    v = make_vad(max_utterance_s=1.0)
    events = feed_all(v, silence(200) + tone(2500) + silence(600))
    ends = [e for e in events if e.event is VADEvent.SPEECH_END and e.utterance]
    assert ends, "超长语音应被强制截断"
    longest_ms = max(len(e.utterance) for e in ends) / 2 / 16000 * 1000
    assert longest_ms <= 1400, f"单段 {longest_ms:.0f}ms 超过 max_utterance_s=1.0 的合理上界"


def test_flush_produces_utterance_while_speaking():
    v = make_vad()
    # 说一段但**还没**喂足够静音（VAD 仍认为在说话）
    feed_all(v, silence(200) + tone(500))
    assert v.is_speaking, "500ms 语音后应处于说话状态"

    r = v.flush()
    assert r is not None
    assert r.event is VADEvent.SPEECH_END
    assert r.utterance is not None
    assert len(r.utterance) > 0
    # flush 后回到空闲
    assert not v.is_speaking


def test_flush_when_idle_is_harmless():
    v = make_vad()
    feed_all(v, silence(300))
    r = v.flush()
    # 空闲时 flush 不应崩，也不应产出语音
    assert r is None or r.utterance is None


def test_reset_clears_state():
    v = make_vad()
    feed_all(v, silence(200) + tone(500))
    assert v.is_speaking, "应在说话状态"
    v.reset()
    assert not v.is_speaking
    r = v.flush()
    assert r is None or r.utterance is None


def test_two_utterances_in_one_stream():
    v = make_vad()
    pcm = silence(300) + tone(500) + silence(600) + tone(500) + silence(600)
    events = feed_all(v, pcm)
    starts = [e for e in events if e.event is VADEvent.SPEECH_START]
    ends = [e for e in events if e.event is VADEvent.SPEECH_END and e.utterance]
    assert len(starts) >= 2, f"应检测到 2 次语音开始，实际 {len(starts)}"
    assert len(ends) >= 2, f"应产出 2 段语音，实际 {len(ends)}"


def test_speech_ms_reported():
    v = make_vad()
    events = feed_all(v, silence(300) + tone(600) + silence(600))
    ends = [e for e in events if e.event is VADEvent.SPEECH_END]
    assert ends
    assert ends[0].speech_ms > 0


def test_arbitrary_frame_sizes():
    """帧长任意（不是 20ms 的整数倍）也不能崩。"""
    v = make_vad()
    pcm = silence(300) + tone(600) + silence(600)
    for i in range(0, len(pcm), 517):  # 故意用奇数长度
        v.push(pcm[i : i + 517])
    # 不抛异常即通过


def test_empty_frame_is_safe():
    v = make_vad()
    r = v.push(b"")
    assert r is None or r.event is None or True  # 只要不抛异常
