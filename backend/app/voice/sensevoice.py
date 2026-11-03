"""SiliconFlow SenseVoiceSmall 云端 STT 客户端（OpenAI 兼容音频转写接口）。

契约见 ``docs/V2_STT.md`` §1 / §4：

    POST {base_url}/audio/transcriptions
    Authorization: Bearer <API_KEY>
    multipart: file=<audio.wav> (16k mono PCM16 包成 WAV), model=FunAudioLLM/SenseVoiceSmall
    200 → {"text": "..."}

设计
----
- **零重依赖**：只用 ``httpx`` + 标准库 ``wave`` + numpy（提示音），不加载本地模型。
- **可离线测试**：``transport`` 参数直接塞给 ``httpx.AsyncClient``（测试用
  ``httpx.MockTransport``），不改变生产路径。
- **失败可读**：HTTP 4xx/5xx、超时、网络错误、非 JSON、缺少 ``text`` 一律抛
  :class:`SenseVoiceError`；``STTEngine`` 捕获后自动回退本地 faster-whisper → mock。
- **verify 不抛**：设置页「测试连接」只拿到 ``{"ok","message","latency_ms","text"}``。
"""

from __future__ import annotations

import io
import time
import wave
from typing import Any

import httpx
import numpy as np

from app.logging_utils import get_logger
from app.voice.audio_utils import SR_IN, float32_to_pcm16

log = get_logger(__name__)

#: 契约默认值（与 ``app/voice/stt_config.py`` 的 DEFAULT_* 保持一致）
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"
#: 转写接口路径（OpenAI 兼容）
TRANSCRIPTIONS_PATH = "/audio/transcriptions"
#: 探测录音：0.5s 440Hz 提示音
VERIFY_TONE_S = 0.5
VERIFY_TONE_HZ = 440.0
#: 错误响应片段最大长度
_ERROR_SNIPPET_CHARS = 240


class SenseVoiceError(RuntimeError):
    """SiliconFlow 转写失败（网络/HTTP/解析）。

    ``status_code`` 为 HTTP 状态码（网络层失败时为 ``None``），便于上层做细分处理。
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# 工具：PCM16 → WAV / 提示音 / 错误信息
# --------------------------------------------------------------------------- #
def pcm16_to_wav_bytes(pcm16: bytes, sample_rate: int = SR_IN) -> bytes:
    """把 16-bit 小端 mono PCM 包成标准 WAV 字节串（``BytesIO``，不落盘）。"""
    sr = int(sample_rate or SR_IN)
    if sr <= 0:
        raise ValueError(f"非法采样率: {sample_rate}")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(pcm16 or b""))
    return buf.getvalue()


def make_verify_tone_wav(
    duration_s: float = VERIFY_TONE_S,
    *,
    sample_rate: int = SR_IN,
    freq: float = VERIFY_TONE_HZ,
) -> bytes:
    """生成 verify 用的短提示音 WAV（默认 0.5s / 440Hz；不依赖扬声器）。"""
    sr = int(sample_rate or SR_IN)
    n = max(1, int(round(sr * max(0.05, float(duration_s)))))
    t = np.arange(n, dtype=np.float32) / float(sr)
    tone = (0.2 * np.sin(2.0 * np.pi * float(freq) * t)).astype(np.float32)
    # 两端 5ms 淡入淡出，避免爆音（对云端转写无影响，但更像"提示音"）
    fade = max(1, int(0.005 * sr))
    if n > 2 * fade:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        tone[:fade] *= ramp
        tone[-fade:] *= ramp[::-1]
    return pcm16_to_wav_bytes(float32_to_pcm16(tone), sr)


def _body_snippet(response: httpx.Response, limit: int = _ERROR_SNIPPET_CHARS) -> str:
    """错误响应正文的可读片段（压成一行、截断）。"""
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - 响应体读取失败不影响错误信息
        return ""
    text = " ".join(str(text or "").split())
    return text[:limit]


def _http_error_message(response: httpx.Response) -> str:
    """把 HTTP 错误映射成可读中文（含状态码与响应片段）。"""
    status = int(response.status_code)
    snippet = _body_snippet(response)
    if status in (401, 403):
        base = f"API Key 无效或无权限（HTTP {status}）"
    elif status == 429:
        base = f"请求过于频繁 / 额度不足（HTTP {status}）"
    elif status >= 500:
        base = f"硅基流动服务端错误（HTTP {status}）"
    elif status == 404:
        base = f"转写接口不存在（HTTP {status}），请检查 base_url"
    else:
        base = f"转写请求失败（HTTP {status}）"
    return f"{base}：{snippet}" if snippet else base


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class SiliconFlowSTT:
    """硅基流动 SenseVoiceSmall 的薄客户端（异步、无状态）。"""

    backend_name = "sensevoice-siliconflow"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 30.0,
        language: str = "en",
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = str(base_url or "").strip()
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        try:
            timeout = float(timeout_s)
        except (TypeError, ValueError):
            timeout = 30.0
        self.timeout_s = max(5.0, min(120.0, timeout))
        self.language = str(language or "en").strip() or "en"
        #: 仅测试注入；生产为 None（走真实网络）
        self._transport = transport

    # ---- 状态 ----
    @property
    def configured(self) -> bool:
        """base_url / api_key / model 三者齐全才算可用。"""
        return bool(self.base_url and self.api_key and self.model)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}{TRANSCRIPTIONS_PATH}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"SiliconFlowSTT(base_url={self.base_url!r}, model={self.model!r}, "
            f"key={'set' if self.api_key else 'empty'})"
        )

    # ---- 内核请求 ----
    async def _post_audio(self, wav_bytes: bytes) -> tuple[str, float]:
        """发一次 multipart 转写请求，返回 ``(原始 text, latency_ms)``。

        Raises:
            SenseVoiceError: 网络/超时/HTTP/JSON 解析失败。
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data = {"model": self.model}

        start = time.perf_counter()
        try:
            async with self._client() as client:
                response = await client.post(
                    self.endpoint, headers=headers, files=files, data=data
                )
        except httpx.TimeoutException as exc:
            raise SenseVoiceError(
                f"连接超时（{self.timeout_s:.0f}s）：{type(exc).__name__}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SenseVoiceError(
                f"网络错误（{self.endpoint}）：{type(exc).__name__}: {exc}"
            ) from exc
        latency_ms = (time.perf_counter() - start) * 1000.0

        if response.status_code >= 400:
            raise SenseVoiceError(
                _http_error_message(response), status_code=response.status_code
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SenseVoiceError(
                f"响应不是合法 JSON（HTTP {response.status_code}）："
                f"{_body_snippet(response)}",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise SenseVoiceError(
                f"响应格式非法（期望 JSON object）：{_body_snippet(response)}",
                status_code=response.status_code,
            )
        text = payload.get("text")
        if not isinstance(text, str):
            raise SenseVoiceError(
                f"响应缺少 text 字段：{_body_snippet(response)}",
                status_code=response.status_code,
            )
        return text, latency_ms

    # ---- 公开接口 ----
    async def transcribe(self, pcm16: bytes, sample_rate: int = SR_IN):
        """把 16k mono PCM16 转成 :class:`app.voice.stt.Transcript`。

        Raises:
            SenseVoiceError: 未配置 / 空音频 / 网络 / HTTP / 解析失败。
        """
        from app.voice.stt import Transcript, clean_transcript  # noqa: PLC0415 - 避免循环 import

        if not self.configured:
            raise SenseVoiceError("SiliconFlow STT 未配置（base_url / api_key / model 不能为空）")
        if not pcm16:
            raise SenseVoiceError("音频为空，无法调用云端转写")

        sr = int(sample_rate or SR_IN)
        if sr <= 0:
            sr = SR_IN
        duration_s = (len(pcm16) / 2.0) / float(sr)
        wav_bytes = pcm16_to_wav_bytes(pcm16, sr)

        raw_text, latency_ms = await self._post_audio(wav_bytes)
        text = clean_transcript(raw_text)
        log.debug(
            "SenseVoice transcribe %.0fms dur=%.2fs text=%r",
            latency_ms, duration_s, (text or raw_text)[:80],
        )
        return Transcript(
            text=text,
            language=self.language,
            duration_s=duration_s,
            latency_ms=round(latency_ms, 1),
            backend=self.backend_name,
            segments=[],
        )

    async def verify(self) -> dict[str, Any]:
        """「测试连接」：发一次 0.5s 提示音转写；**永不抛异常**。

        Returns:
            ``{"ok": bool, "message": str, "latency_ms": float, "text": str}``
        """
        if not self.configured:
            return {
                "ok": False,
                "message": "未配置 SiliconFlow STT：base_url / api_key / model 不能为空",
                "latency_ms": 0.0,
                "text": "",
            }
        start = time.perf_counter()
        try:
            wav_bytes = make_verify_tone_wav()
            raw_text, latency_ms = await self._post_audio(wav_bytes)
        except SenseVoiceError as exc:
            elapsed = (time.perf_counter() - start) * 1000.0
            log.info("SiliconFlow STT verify 失败: %s", exc)
            return {
                "ok": False,
                "message": str(exc),
                "latency_ms": round(elapsed, 1),
                "text": "",
            }
        except Exception as exc:  # noqa: BLE001 - 测试连接绝不抛
            elapsed = (time.perf_counter() - start) * 1000.0
            log.info("SiliconFlow STT verify 异常: %s: %s", type(exc).__name__, exc)
            return {
                "ok": False,
                "message": f"连接失败：{type(exc).__name__}: {exc}",
                "latency_ms": round(elapsed, 1),
                "text": "",
            }

        from app.voice.stt import clean_transcript  # noqa: PLC0415 - 避免循环 import

        text = clean_transcript(raw_text)
        short_model = (self.model.rsplit("/", 1)[-1] or "SenseVoice").strip()
        log.info("SiliconFlow STT verify 成功 %.0fms", latency_ms)
        return {
            "ok": True,
            "message": f"连接成功，{short_model} 正常响应",
            "latency_ms": round(latency_ms, 1),
            "text": text,
        }


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "SenseVoiceError",
    "SiliconFlowSTT",
    "TRANSCRIPTIONS_PATH",
    "make_verify_tone_wav",
    "pcm16_to_wav_bytes",
]
