"""音频工具测试：PCM 往返、重采样、WAV 读写、静音裁剪。"""

from __future__ import annotations

import math

import numpy as np
import pytest


def test_pcm16_roundtrip():
    from app.voice.audio_utils import float32_to_pcm16, pcm16_to_float32

    x = np.array([0.0, 0.5, -0.5, 0.999, -0.999], dtype=np.float32)
    pcm = float32_to_pcm16(x)
    assert isinstance(pcm, bytes)
    assert len(pcm) == len(x) * 2  # 16-bit

    y = pcm16_to_float32(pcm)
    assert y.dtype == np.float32
    assert np.allclose(x, y, atol=1e-3)


def test_pcm16_clips_out_of_range():
    from app.voice.audio_utils import float32_to_pcm16, pcm16_to_float32

    x = np.array([2.0, -2.0, 5.0, -5.0], dtype=np.float32)
    y = pcm16_to_float32(float32_to_pcm16(x))
    assert np.all(np.abs(y) <= 1.0)
    assert y[0] > 0.99 and y[1] < -0.99


def test_pcm16_is_little_endian():
    from app.voice.audio_utils import float32_to_pcm16

    # 0x0100 = 256 → LE 字节序为 00 01
    pcm = float32_to_pcm16(np.array([256 / 32768], dtype=np.float32))
    assert pcm[0] == 0x00
    assert pcm[1] == 0x01


@pytest.mark.parametrize("sr_in,sr_out", [(16000, 24000), (24000, 16000), (16000, 16000), (48000, 16000)])
def test_resample_preserves_tone(sr_in, sr_out):
    from app.voice.audio_utils import resample

    freq = 440.0
    dur = 0.25
    t = np.arange(int(sr_in * dur)) / sr_in
    x = np.sin(2 * math.pi * freq * t).astype(np.float32)

    y = resample(x, sr_in, sr_out)
    assert y.dtype == np.float32
    expected_len = int(round(len(x) * sr_out / sr_in))
    assert abs(len(y) - expected_len) <= max(8, expected_len // 100)

    # 主频不变：过零率近似 2*freq
    zc = np.sum(np.abs(np.diff(np.signbit(y)).astype(np.int8)))
    est_freq = zc / 2.0 / (len(y) / sr_out)
    assert abs(est_freq - freq) < 40, f"est={est_freq:.1f} expected={freq}"


def test_resample_same_rate_is_identity():
    from app.voice.audio_utils import resample

    x = np.random.default_rng(0).standard_normal(1000).astype(np.float32) * 0.1
    y = resample(x, 16000, 16000)
    assert len(y) == len(x)
    assert np.allclose(x, y)


def test_write_and_read_wav(tmp_path):
    from app.voice.audio_utils import read_audio_file, write_wav

    sr = 16000
    t = np.arange(sr // 2) / sr
    x = (0.4 * np.sin(2 * math.pi * 300 * t)).astype(np.float32)

    path = tmp_path / "test.wav"
    write_wav(str(path), x, sr)
    assert path.exists()
    assert path.stat().st_size > 44  # RIFF header

    y = read_audio_file(str(path), target_sr=sr)
    assert len(y) == pytest.approx(len(x), abs=8)
    assert np.max(np.abs(y)) == pytest.approx(np.max(np.abs(x)), abs=0.02)


def test_read_audio_file_resamples(tmp_path):
    from app.voice.audio_utils import read_audio_file, write_wav

    sr = 24000
    t = np.arange(sr) / sr
    x = (0.3 * np.sin(2 * math.pi * 200 * t)).astype(np.float32)
    path = tmp_path / "24k.wav"
    write_wav(str(path), x, sr)

    y = read_audio_file(str(path), target_sr=16000)
    expected = int(len(x) * 16000 / 24000)
    assert abs(len(y) - expected) <= 16


def test_read_missing_file_raises():
    from app.voice.audio_utils import AudioDecodeError, read_audio_file

    with pytest.raises((AudioDecodeError, FileNotFoundError)):
        read_audio_file("definitely_not_here.wav")


def test_trim_silence_removes_padding():
    from app.voice.audio_utils import trim_silence

    sr = 16000
    silence = np.zeros(sr // 2, dtype=np.float32)
    t = np.arange(sr // 4) / sr
    tone = (0.5 * np.sin(2 * math.pi * 300 * t)).astype(np.float32)
    x = np.concatenate([silence, tone, silence])

    y = trim_silence(x, sr, threshold=0.05, pad_ms=50)
    # 应该显著短于原始
    assert len(y) < len(x) * 0.6
    assert len(y) >= len(tone)


def test_rms_and_duration():
    from app.voice.audio_utils import duration_s, float32_to_pcm16, rms

    x = np.full(16000, 0.5, dtype=np.float32)
    assert rms(x) == pytest.approx(0.5, abs=1e-6)

    pcm = float32_to_pcm16(x)
    assert duration_s(pcm, 16000) == pytest.approx(1.0, abs=1e-3)
