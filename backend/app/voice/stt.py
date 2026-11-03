"""流式 STT：faster-whisper 优先，缺失/加载失败时降级 mock。

契约见 ``docs/CONTRACTS.md`` §3。

关键约束
--------
- **构造不加载权重**：``STTEngine(...)`` 只记录配置，``prewarm()`` / 首次
  ``transcribe()`` 才懒加载（契约 §0）。
- **重依赖函数内 import**：``faster_whisper`` 在 ``_ensure_model()`` 里才 import，
  因此在没装它的机器上本模块仍可 import、服务仍可启动。
- **不阻塞事件循环**：``model.transcribe`` 全部走 ``asyncio.to_thread``。
- **降级链**：``cfg.device`` 加载失败 → 若 ``cpu_fallback`` 换 ``cpu`` + ``int8``
  → 再失败且 ``backend=="auto"`` → 降级 mock（warning，**不抛**）。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from app.config import STTSettings
from app.logging_utils import Stopwatch, get_logger
from app.voice.audio_utils import (
    SR_IN,
    array_duration_s,
    float32_to_pcm16,
    pcm16_to_float32,
    resample,
)
from app.voice.sensevoice import (
    DEFAULT_BASE_URL as SENSEVOICE_DEFAULT_BASE_URL,
    DEFAULT_MODEL as SENSEVOICE_DEFAULT_MODEL,
    SiliconFlowSTT,
)

log = get_logger(__name__)

#: whisper 在静音/噪声上常见的幻觉输出（整句完全匹配才丢弃）
_HALLUCINATION_EXACT = {
    "thank you.",
    "thank you",
    "thanks for watching!",
    "thanks for watching.",
    "thanks for watching",
    "you",
    "bye.",
    "bye",
    ".",
    "..",
    "...",
    "...",
    "okay.",
    "okay",
    "so",
    "hmm",
}
#: 形如 [Music] / (upbeat music) / <|nospeech|> / 【音乐】 的整段噪声标记
_NOISE_TAG_RE = re.compile(
    r"^\s*[\[\(【<]\s*(?:music|musique|applause|silence|noise|blank_?audio|inaudible|"
    r"beep|laughter|foreign|speech|sound|音乐|掌声|静音|噪声)[^\]\)】>]*[\]\)】>]\s*$",
    re.IGNORECASE,
)
#: whisper 的特殊 token（如 ``<|startoftranscript|>``）
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]*\|>")
#: 只由标点/空白组成的"文本"
_EMPTY_TEXT_RE = re.compile(r"^[\s\.\,\!\?…\-—–、。，！？:;'\"·]*$")
#: 连续重复标点收敛（``Hello!!`` → ``Hello!``）
_REPEAT_PUNCT_RE = re.compile(r"([\.\!\?…])\1{1,}")


@dataclass
class Transcript:
    """一次转写的结果。"""

    text: str
    language: str = "en"
    duration_s: float = 0.0
    latency_ms: float = 0.0
    backend: str = "mock"
    segments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "duration_s": round(self.duration_s, 3),
            "latency_ms": round(self.latency_ms, 1),
            "backend": self.backend,
        }


# --------------------------------------------------------------------------- #
# 后处理（纯函数，可单测）
# --------------------------------------------------------------------------- #
def clean_transcript(text: str) -> str:
    """清洗 whisper 输出：去特殊 token、去幻听标记、收敛标点、strip。

    **不过度加工**：不改大小写、不补句号、不纠正语法——上层（LLM/前端）负责。
    """
    if not text:
        return ""
    s = str(text)

    # whisper 特殊 token
    s = _SPECIAL_TOKEN_RE.sub(" ", s)

    # 去掉行首/行尾的 [Music] / (applause) 这类整段标记
    for _ in range(4):
        stripped = s.strip()
        if not stripped:
            s = ""
            break
        m = _NOISE_TAG_RE.match(stripped)
        if not m:
            break
        s = stripped[m.end():]

    s = " ".join(s.split())
    s = _REPEAT_PUNCT_RE.sub(r"\1", s)
    s = s.strip()

    if not s or _EMPTY_TEXT_RE.match(s):
        return ""

    # 整句等于已知幻觉 → 丢弃
    if s.lower() in _HALLUCINATION_EXACT:
        return ""
    # 幻觉常见形态：句首 "Thank you." 后紧跟空（"Thank you." + 无内容）
    low = s.lower()
    for prefix in ("thank you.", "thanks for watching."):
        if low == prefix or (low.startswith(prefix) and len(low) <= len(prefix) + 2):
            return ""

    # 去掉首尾多余的引号与短横
    s = s.strip(" \t\r\n\"'“”‘’")
    while s and s[0] in "-–—":
        s = s[1:].lstrip()
    while s and s[-1] in "-–—":
        s = s[:-1].rstrip()

    if not s or _EMPTY_TEXT_RE.match(s):
        return ""
    return s


def _clean_hotwords(raw: str) -> str:
    """把 ``cfg.hotwords`` 规范化：逗号分隔、去重、保序。"""
    if not raw:
        return ""
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[,\n;]+", str(raw)):
        w = part.strip()
        if not w:
            continue
        key = w.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return ", ".join(out)


# --------------------------------------------------------------------------- #
# 云端 STT 配置（SiliconFlow SenseVoiceSmall）—— 运行时热切换
# --------------------------------------------------------------------------- #
#: ``apply_cloud_config`` 认识的字段（对象或 dict 均可）
_CLOUD_CONFIG_FIELDS = (
    "provider",
    "enabled",
    "base_url",
    "api_key",
    "model",
    "language",
    "timeout_s",
)

#: 云端 STT 在 health/config 里的 provider 取值
_CLOUD_PROVIDERS = ("auto", "local", "siliconflow")


def _now_iso() -> str:
    """UTC ISO8601（与 DB 时间约定一致）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp_timeout(value: Any, default: float = 30.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(5.0, min(120.0, v))


def _coerce_cloud_config(cfg: Any) -> dict[str, Any]:
    """把 ``apply_cloud_config`` 的入参规范成 dict。

    支持三种形态：
    - ``dict``（``STTRuntimeConfig.to_engine_dict()``）；
    - pydantic 模型（有 ``model_dump()``）；
    - 普通对象/``SimpleNamespace``（按属性读取 ``_CLOUD_CONFIG_FIELDS``）。
    """
    if cfg is None:
        return {}
    if isinstance(cfg, dict):
        return dict(cfg)

    for method_name in ("to_engine_dict", "model_dump", "dict"):
        method = getattr(cfg, method_name, None)
        if callable(method):
            try:
                data = method()
                if isinstance(data, dict):
                    return dict(data)
            except Exception:  # noqa: BLE001 - 继续退化为属性读取
                pass

    data: dict[str, Any] = {}
    for key in _CLOUD_CONFIG_FIELDS:
        if hasattr(cfg, key):
            try:
                data[key] = getattr(cfg, key)
            except Exception:  # noqa: BLE001 - property 抛错则忽略该字段
                continue
    return data


class STTEngine:
    """语音转文字引擎。

    Args:
        cfg: ``app.config.STTSettings``；``None`` 用默认值。

    ``backend_name`` 是**属性**且**不触发模型加载**：在 ``prewarm()`` 之前，
    它返回配置的后端名（``auto`` 会显示为 ``auto``→实际解析在 prewarm 后更新）。
    """

    def __init__(self, cfg: STTSettings | None = None) -> None:
        self.cfg = cfg or STTSettings()
        self._requested = str(self.cfg.backend or "auto")
        self._backend = self._requested            # 实际后端，prewarm 后可能变化
        self._model: Any = None
        self._load_error: str | None = None
        self._load_lock = asyncio.Lock()
        self._resolved_device = str(self.cfg.device)
        self._resolved_compute = str(self.cfg.compute_type)
        self._closed = False
        self._calls = 0

        # ---- 云端 STT（SiliconFlow SenseVoiceSmall）状态 ----
        # 默认关闭：由 `apply_cloud_config()`（engines 装配 / 设置页保存时）热开启，
        # 因此 1.0 的纯本地行为完全不变。
        self._cloud: SiliconFlowSTT | None = None
        self._cloud_enabled = False
        self._cloud_provider = "auto"
        self._cloud_config: dict[str, Any] = {
            "base_url": SENSEVOICE_DEFAULT_BASE_URL,
            "model": SENSEVOICE_DEFAULT_MODEL,
            "language": str(self.cfg.language or "en"),
            "timeout_s": 30.0,
        }
        self._cloud_last_error: str | None = None
        self._cloud_last_latency_ms: float | None = None
        self._cloud_requests = 0
        self._cloud_last_success_at: str | None = None

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def _cloud_is_active(self) -> bool:
        """云端是否真正生效（启用 + 客户端已配置）。"""
        return bool(self._cloud_enabled and self._cloud is not None and self._cloud.configured)

    @property
    def backend_name(self) -> str:
        """当前后端名（``faster-whisper`` / ``mock`` / 云端生效时 ``sensevoice``）。

        **不加载模型**：云端生效（enabled + configured）时返回 ``sensevoice``；
        否则保持 1.0 语义（``auto`` 在 prewarm 前仍显示 ``auto``）。
        """
        if self._cloud_is_active():
            return "sensevoice"
        return self._backend

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    @property
    def device(self) -> str:
        return self._resolved_device

    @property
    def compute_type(self) -> str:
        return self._resolved_compute

    def health(self) -> dict[str, Any]:
        return {
            "backend": self._backend,
            "requested": self._requested,
            "model_size": self.cfg.model_size,
            "device": self._resolved_device,
            "compute_type": self._resolved_compute,
            "loaded": self._model is not None,
            "error": self._load_error,
            "calls": self._calls,
        }

    # ------------------------------------------------------------------ #
    # 云端 STT（热切换 / health）
    # ------------------------------------------------------------------ #
    def apply_cloud_config(self, cfg: Any) -> None:
        """应用云端 STT 配置（对象或 dict），**立即生效、无需重启**。

        字段：``provider`` / ``enabled`` / ``base_url`` / ``api_key`` / ``model`` /
        ``language`` / ``timeout_s``。

        启用语义（``docs/V2_STT.md`` §2 / 任务契约）：
        - ``provider == "local"`` → 关闭云端（恢复纯本地）；
        - ``provider == "siliconflow"`` → 启用（key 为空时 enabled=True 但 configured=False，
          不会真正发请求，health 里可见）；
        - 其它（``auto``）→ 有 api_key 才启用。

        重建 :class:`SiliconFlowSTT`；同时清空 ``last_error`` / ``last_latency_ms``
        （新配置的首次请求重新计时），``requests`` / ``last_success_at`` 保留累计值。
        """
        data = _coerce_cloud_config(cfg)

        provider = str(data.get("provider") or "auto").strip().lower()
        if provider not in _CLOUD_PROVIDERS:
            provider = "auto"
        base_url = str(data.get("base_url") or SENSEVOICE_DEFAULT_BASE_URL).strip()
        api_key = str(data.get("api_key") or "").strip()
        model = str(data.get("model") or SENSEVOICE_DEFAULT_MODEL).strip()
        language = str(data.get("language") or self.cfg.language or "en").strip() or "en"
        timeout_s = _clamp_timeout(data.get("timeout_s"), 30.0)

        if provider == "local":
            enabled = False
        elif provider == "siliconflow":
            enabled = True
        else:  # auto：有 key 才启用
            enabled = bool(api_key)

        self._cloud_provider = provider
        self._cloud_config = {
            "base_url": base_url,
            "model": model,
            "language": language,
            "timeout_s": timeout_s,
        }
        if enabled:
            self._cloud = SiliconFlowSTT(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_s=timeout_s,
                language=language,
            )
            log.info(
                "STT 云端配置已应用: provider=%s base_url=%s model=%s key=%s configured=%s",
                provider, base_url, model, "set" if api_key else "empty",
                self._cloud.configured,
            )
        else:
            self._cloud = None
            log.info("STT 云端已关闭（provider=%s），使用本地 faster-whisper → mock", provider)

        self._cloud_enabled = bool(enabled)
        self._cloud_last_error = None
        self._cloud_last_latency_ms = None

    def cloud_health(self) -> dict[str, Any]:
        """云端 STT 健康状态（``/api/v2/settings`` 直接读取）。"""
        model = ""
        if self._cloud is not None:
            model = str(self._cloud.model or "")
        if not model:
            model = str(self._cloud_config.get("model") or SENSEVOICE_DEFAULT_MODEL)
        latency = self._cloud_last_latency_ms
        return {
            "enabled": bool(self._cloud_enabled),
            "provider": self._cloud_provider,
            "configured": bool(self._cloud is not None and self._cloud.configured),
            "backend": self.backend_name,
            "model": model,
            "last_error": self._cloud_last_error,
            "last_latency_ms": round(float(latency), 1) if latency is not None else None,
            "requests": int(self._cloud_requests),
            "last_success_at": self._cloud_last_success_at,
        }

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def prewarm(self) -> None:
        """懒加载模型（线程池里做）。失败时按契约降级，**不抛**。"""
        if self._closed:
            log.debug("STT prewarm() 在已关闭的引擎上调用，忽略")
            return
        if self._backend == "mock":
            log.info("STT backend=mock，无需加载模型")
            return
        try:
            await self._ensure_model()
        except Exception as exc:  # pragma: no cover - _ensure_model 内部已兜底
            log.warning("STT 预热失败: %s", exc)

    async def _ensure_model(self) -> Any:
        """确保 faster-whisper 模型可用；不可用则返回 ``None``（表示走 mock）。"""
        if self._model is not None:
            return self._model
        if self._backend == "mock":
            return None

        async with self._load_lock:
            if self._model is not None:
                return self._model

            try:
                from faster_whisper import WhisperModel  # noqa: PLC0415 - 重依赖
            except Exception as exc:
                self._load_error = f"faster_whisper import 失败: {type(exc).__name__}: {exc}"
                self._degrade_to_mock(self._load_error)
                return None

            attempts: list[tuple[str, str]] = [(self.cfg.device, self.cfg.compute_type)]
            if self.cfg.cpu_fallback and str(self.cfg.device).lower() != "cpu":
                attempts.append(("cpu", "int8"))

            for device, compute in attempts:
                try:
                    log.info(
                        "STT 加载 faster-whisper model=%s device=%s compute_type=%s ...",
                        self.cfg.model_size, device, compute,
                    )
                    sw = Stopwatch()
                    model = await asyncio.to_thread(
                        WhisperModel,
                        self.cfg.model_size,
                        device=device,
                        compute_type=compute,
                        num_workers=max(1, int(self.cfg.num_workers)),
                        download_root=self.cfg.download_root or None,
                        local_files_only=bool(self.cfg.local_files_only),
                    )
                    self._model = model
                    self._resolved_device = str(device)
                    self._resolved_compute = str(compute)
                    self._backend = "faster-whisper"
                    self._load_error = None
                    log.info(
                        "STT ready: model=%s device=%s compute_type=%s (load %.0fms)",
                        self.cfg.model_size, device, compute, sw.elapsed_ms(),
                    )
                    return model
                except Exception as exc:
                    self._load_error = (
                        f"{type(exc).__name__} @ {device}/{compute}: {exc}"
                    )
                    log.warning(
                        "STT 加载失败 (device=%s compute_type=%s): %s: %s",
                        device, compute, type(exc).__name__, exc,
                    )

            self._degrade_to_mock(self._load_error or "未知加载失败")
            return None

    def _degrade_to_mock(self, reason: str) -> None:
        if self._requested in ("auto", "mock"):
            if self._backend != "mock":
                log.warning("STT 降级为 mock 后端（%s）", reason)
            self._backend = "mock"
            self._model = None
            return
        # 显式要求 faster-whisper：仍然降级（绝不因为 STT 挂掉让整条链路不可用）
        log.error(
            "STT 显式要求 backend=%s 但不可用（%s），仍降级为 mock 以保证链路可用",
            self._requested, reason,
        )
        self._backend = "mock"
        self._model = None

    async def aclose(self) -> None:
        """释放模型（int8 ctranslate2 模型在 GC 时释放显存）。"""
        self._closed = True
        model, self._model = self._model, None
        if model is not None:
            try:
                await asyncio.to_thread(self._release_model, model)
            except Exception as exc:  # pragma: no cover
                log.debug("STT 模型释放失败（忽略）: %s", exc)
        log.debug("STT engine closed (backend=%s)", self._backend)

    @staticmethod
    def _release_model(model: Any) -> None:
        for attr in ("model", "ctranslate2_model"):
            sub = getattr(model, attr, None)
            if sub is not None and hasattr(sub, "unload_model"):
                try:
                    sub.unload_model()
                except Exception:  # pragma: no cover
                    pass

    # ------------------------------------------------------------------ #
    # 转写
    # ------------------------------------------------------------------ #
    async def transcribe(self, pcm16: bytes, sample_rate: int = SR_IN) -> Transcript:
        """把 PCM16 语音转成文本。

        Args:
            pcm16: 16-bit 小端 mono PCM 字节串。
            sample_rate: 输入采样率；非 16k 时内部重采样（上层已保证 16k）。

        Returns:
            :class:`Transcript`（``text`` 已清洗；空文本表示"没听到有效语音"）。
        """
        sw = Stopwatch()
        self._calls += 1
        sr = int(sample_rate or SR_IN)
        if sr <= 0:
            sr = SR_IN

        audio = pcm16_to_float32(pcm16)
        if audio.size == 0:
            log.debug("STT 收到空音频，返回空转写")
            return Transcript(
                text="", language=self.cfg.language, duration_s=0.0,
                latency_ms=sw.elapsed_ms(), backend=self._backend, segments=[],
            )
        if sr != SR_IN:
            audio = resample(audio, sr, SR_IN)
            sr = SR_IN
        duration = array_duration_s(audio, sr)

        # 云端优先：成功直接返回（backend="sensevoice-siliconflow"）；
        # 失败只记 health，继续本地 faster-whisper → mock，绝不抛给上层。
        cloud_transcript = await self._try_cloud(audio, duration)
        if cloud_transcript is not None:
            return cloud_transcript

        if self._backend == "mock":
            return await self._transcribe_mock(sw, duration)

        model = await self._ensure_model()
        if model is None:
            return await self._transcribe_mock(sw, duration)

        try:
            text, segments, language = await asyncio.to_thread(
                self._transcribe_sync, model, audio
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("STT 推理失败（%s: %s），本次返回空转写", type(exc).__name__, exc)
            return Transcript(
                text="", language=self.cfg.language, duration_s=duration,
                latency_ms=sw.elapsed_ms(), backend=self._backend, segments=[],
            )

        latency = sw.elapsed_ms()
        cleaned = clean_transcript(text)
        if text and not cleaned:
            log.debug("STT 输出被清洗为空（疑似幻听）: %r", text[:80])
        log.debug(
            "STT %.0fms dur=%.2fs text=%r (backend=%s)",
            latency, duration, cleaned[:80], self._backend,
        )
        return Transcript(
            text=cleaned,
            language=language or self.cfg.language,
            duration_s=duration,
            latency_ms=latency,
            backend=self._backend,
            segments=segments,
        )

    async def _try_cloud(self, audio: np.ndarray, duration: float) -> Transcript | None:
        """云端转写（成功返回 Transcript；失败返回 None 让上层走本地链路）。

        绝不抛异常（``CancelledError`` 例外，必须透传）。失败时记录
        ``cloud_health()`` 的 ``last_error`` / ``requests``。
        """
        cloud = self._cloud
        if not (self._cloud_enabled and cloud is not None and cloud.configured):
            return None

        try:
            pcm16 = float32_to_pcm16(audio)
            transcript = await cloud.transcribe(pcm16, SR_IN)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 云端失败必须回退本地，不能抛
            self._cloud_requests += 1
            self._cloud_last_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "云端 STT 失败（自动回退本地 faster-whisper/mock）: %s",
                self._cloud_last_error,
            )
            return None

        self._cloud_requests += 1
        self._cloud_last_error = None
        self._cloud_last_latency_ms = float(getattr(transcript, "latency_ms", 0.0) or 0.0)
        self._cloud_last_success_at = _now_iso()
        log.debug(
            "云端 STT %.0fms dur=%.2fs text=%r",
            self._cloud_last_latency_ms, duration, transcript.text[:80],
        )
        return transcript

    def _transcribe_sync(
        self, model: Any, audio: np.ndarray
    ) -> tuple[str, list[dict[str, Any]], str]:
        """真正调用 faster-whisper（在线程池里跑）。"""
        kwargs: dict[str, Any] = {
            "language": self.cfg.language,
            "beam_size": max(1, int(self.cfg.beam_size)),
            "best_of": max(1, int(self.cfg.best_of)),
            "vad_filter": False,
            "condition_on_previous_text": False,
            "without_timestamps": bool(self.cfg.without_timestamps),
            "initial_prompt": self.cfg.initial_prompt or None,
        }
        hot = _clean_hotwords(self.cfg.hotwords)
        if hot:
            kwargs["hotwords"] = hot

        segments_iter, info = model.transcribe(audio, **kwargs)

        texts: list[str] = []
        summary: list[dict[str, Any]] = []
        for seg in segments_iter:
            piece = getattr(seg, "text", "") or ""
            texts.append(piece)
            if len(summary) < 64:
                summary.append(
                    {
                        "start": round(float(getattr(seg, "start", 0.0) or 0.0), 3),
                        "end": round(float(getattr(seg, "end", 0.0) or 0.0), 3),
                        "text": str(piece).strip(),
                        "avg_logprob": round(
                            float(getattr(seg, "avg_logprob", 0.0) or 0.0), 3
                        ),
                        "no_speech_prob": round(
                            float(getattr(seg, "no_speech_prob", 0.0) or 0.0), 3
                        ),
                    }
                )
        language = str(getattr(info, "language", self.cfg.language) or self.cfg.language)
        return " ".join(texts).strip(), summary, language

    async def _transcribe_mock(self, sw: Stopwatch, duration: float) -> Transcript:
        """mock 后端：固定延迟 + 固定文本（用于无模型端到端联调）。"""
        delay = max(0, int(self.cfg.mock_latency_ms)) / 1000.0
        if delay > 0:
            await asyncio.sleep(delay)
        text = clean_transcript(str(self.cfg.mock_text)) or str(self.cfg.mock_text)
        return Transcript(
            text=text,
            language=self.cfg.language,
            duration_s=duration,
            latency_ms=sw.elapsed_ms(),
            backend="mock",
            segments=[{"start": 0.0, "end": round(duration, 3), "text": text}],
        )


__all__ = ["Transcript", "STTEngine", "clean_transcript"]
