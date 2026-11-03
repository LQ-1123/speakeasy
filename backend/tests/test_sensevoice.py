"""离线单测：SiliconFlow SenseVoiceSmall 客户端（httpx.MockTransport，全程不联网）。

覆盖：
- 成功路径：URL / Bearer / multipart（model + WAV RIFF 头 + 16k/单声道/2 字节）；
- 错误路径：401、500、超时、网络错误、非 JSON、缺 text / text 非字符串；
- 空文本（合法 200）返回空 Transcript（不抛，避免静音被当成失败）；
- verify()：成功 / 401 / 超时 / 异常折叠，永不抛；提示音时长 0.4~0.6s；
- WAV 工具函数的头部与采样率。
"""

from __future__ import annotations

import io
import wave

import httpx
import numpy as np
import pytest

from app.voice.audio_utils import SR_IN, float32_to_pcm16
from app.voice.sensevoice import (
    DEFAULT_MODEL,
    SenseVoiceError,
    SiliconFlowSTT,
    make_verify_tone_wav,
    pcm16_to_wav_bytes,
)
from app.voice.stt import Transcript

BASE = "https://api.siliconflow.cn/v1"
API_KEY = "sk-test-123456"


def _pcm16(duration_s: float = 0.3, *, freq: float = 220.0, sample_rate: int = SR_IN) -> bytes:
    n = max(1, int(round(duration_s * sample_rate)))
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    return float32_to_pcm16(0.2 * np.sin(2.0 * np.pi * freq * t))


def _client(handler, **overrides) -> SiliconFlowSTT:
    kwargs = {
        "base_url": BASE,
        "api_key": API_KEY,
        "model": DEFAULT_MODEL,
        "timeout_s": 5.0,
        "language": "en",
        "transport": httpx.MockTransport(handler),
    }
    kwargs.update(overrides)
    return SiliconFlowSTT(**kwargs)


def _extract_wav(body: bytes) -> bytes:
    idx = body.find(b"RIFF")
    assert idx >= 0, "multipart 里没有 WAV 数据"
    return body[idx:]


def _open_wav(body: bytes):
    return wave.open(io.BytesIO(_extract_wav(body)), "rb")


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
async def test_transcribe_success_sends_correct_multipart_and_returns_transcript() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  Hello   there!  "})

    client = _client(handler)
    pcm = _pcm16(0.3)
    transcript = await client.transcribe(pcm)

    assert isinstance(transcript, Transcript)
    assert transcript.text == "Hello there!"          # 已过 clean_transcript
    assert transcript.backend == "sensevoice-siliconflow"
    assert transcript.language == "en"
    assert transcript.duration_s == pytest.approx(0.3, abs=0.02)
    assert transcript.latency_ms >= 0
    assert transcript.segments == []

    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/audio/transcriptions"
    assert seen["auth"] == f"Bearer {API_KEY}"
    assert seen["content_type"].startswith("multipart/form-data")

    body: bytes = seen["body"]
    assert b'name="model"' in body
    assert DEFAULT_MODEL.encode() in body
    assert b'name="file"' in body
    assert b"audio.wav" in body
    assert b"audio/wav" in body

    with _open_wav(body) as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == SR_IN
        assert wf.getnframes() / SR_IN == pytest.approx(0.3, abs=0.02)


async def test_base_url_with_trailing_slash_is_normalized() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"text": "ok"})

    client = _client(handler, base_url=BASE + "/")
    await client.transcribe(_pcm16(0.1))
    assert seen["url"] == f"{BASE}/audio/transcriptions"
    assert "//audio" not in seen["url"]


async def test_custom_sample_rate_is_preserved_in_wav() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "ok"})

    client = _client(handler)
    await client.transcribe(_pcm16(0.2, sample_rate=24000), sample_rate=24000)

    with _open_wav(seen["body"]) as wf:
        assert wf.getframerate() == 24000
        assert wf.getnframes() / 24000 == pytest.approx(0.2, abs=0.02)
    assert client.configured is True


async def test_empty_text_is_valid_silence_not_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "   "})

    transcript = await _client(handler).transcribe(_pcm16(0.3))
    assert transcript.text == ""
    assert transcript.backend == "sensevoice-siliconflow"


# --------------------------------------------------------------------------- #
# 错误路径
# --------------------------------------------------------------------------- #
async def test_transcribe_401_raises_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid api key"})

    with pytest.raises(SenseVoiceError) as ei:
        await _client(handler).transcribe(_pcm16())
    message = str(ei.value)
    assert "401" in message
    assert "API Key 无效或无权限" in message
    assert ei.value.status_code == 401


async def test_transcribe_500_raises_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    with pytest.raises(SenseVoiceError) as ei:
        await _client(handler).transcribe(_pcm16())
    message = str(ei.value)
    assert "500" in message
    assert "服务端错误" in message
    assert "upstream exploded" in message
    assert ei.value.status_code == 500


async def test_transcribe_timeout_raises_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(SenseVoiceError) as ei:
        await _client(handler).transcribe(_pcm16())
    message = str(ei.value)
    assert "超时" in message
    assert "timed out" in message


async def test_transcribe_network_error_raises_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(SenseVoiceError) as ei:
        await _client(handler).transcribe(_pcm16())
    assert "网络错误" in str(ei.value)


async def test_transcribe_non_json_response_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(SenseVoiceError) as ei:
        await _client(handler).transcribe(_pcm16())
    assert "JSON" in str(ei.value)


async def test_transcribe_missing_text_field_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "no text here"})

    with pytest.raises(SenseVoiceError) as ei:
        await _client(handler).transcribe(_pcm16())
    assert "text" in str(ei.value)


async def test_transcribe_non_string_text_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": 123})

    with pytest.raises(SenseVoiceError):
        await _client(handler).transcribe(_pcm16())


async def test_unconfigured_client_raises_before_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("未配置时不应发网络请求")

    client = _client(handler, api_key="")
    assert client.configured is False
    with pytest.raises(SenseVoiceError) as ei:
        await client.transcribe(_pcm16())
    assert "未配置" in str(ei.value)


async def test_empty_audio_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("空音频不应发网络请求")

    with pytest.raises(SenseVoiceError):
        await _client(handler).transcribe(b"")


# --------------------------------------------------------------------------- #
# verify()
# --------------------------------------------------------------------------- #
async def test_verify_success_sends_short_tone_wav() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"text": " hi "})

    result = await _client(handler).verify()

    assert result["ok"] is True
    assert "连接成功" in result["message"]
    assert result["latency_ms"] >= 0
    assert result["text"] == "hi"
    assert seen["auth"] == f"Bearer {API_KEY}"

    with _open_wav(seen["body"]) as wf:
        assert wf.getframerate() == SR_IN
        assert wf.getnchannels() == 1
        duration_s = wf.getnframes() / wf.getframerate()
    assert 0.4 <= duration_s <= 0.6, f"提示音时长异常: {duration_s}"


async def test_verify_401_folds_into_ok_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad key"})

    result = await _client(handler).verify()
    assert result["ok"] is False
    assert "API Key 无效或无权限" in result["message"]
    assert result["latency_ms"] >= 0
    assert result["text"] == ""


async def test_verify_timeout_folds_into_ok_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    result = await _client(handler).verify()
    assert result["ok"] is False
    assert "超时" in result["message"]
    assert isinstance(result["latency_ms"], float)


async def test_verify_unexpected_exception_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("boom")

    result = await _client(handler).verify()
    assert result["ok"] is False
    assert "boom" in result["message"]


async def test_verify_unconfigured_returns_ok_false_without_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("未配置时不应发网络请求")

    result = await _client(handler, base_url="", api_key="", model="").verify()
    assert result["ok"] is False
    assert result["message"]
    assert result["latency_ms"] == 0.0
    assert result["text"] == ""


# --------------------------------------------------------------------------- #
# WAV 工具
# --------------------------------------------------------------------------- #
def test_pcm16_to_wav_bytes_header_and_rate() -> None:
    pcm = _pcm16(0.25, sample_rate=16000)
    data = pcm16_to_wav_bytes(pcm, 16000)
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    with wave.open(io.BytesIO(data), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() == len(pcm) // 2


def test_make_verify_tone_wav_duration() -> None:
    data = make_verify_tone_wav()
    with wave.open(io.BytesIO(data), "rb") as wf:
        assert wf.getframerate() == SR_IN
        assert wf.getnchannels() == 1
        duration_s = wf.getnframes() / wf.getframerate()
    assert 0.4 <= duration_s <= 0.6
