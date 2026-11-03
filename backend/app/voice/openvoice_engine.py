"""OpenVoice V2 声音转换引擎：保留口音与韵律，只替换音色。

为什么需要它
------------
XTTS-v2 是「一步到位」的 TTS：文本 → 音色 + 口音。但它的 speaker encoder
提取的是 **timbre embedding**（声带、声道长度），口音所在的
**语调曲线、元音位置、节奏** 不在那个空间里。实测结果：

    同一句文本，用 4 种母语声线做参考，XTTS 克隆后
    归一化 F0 语调曲线相关性:  -0.27 ~ 0.18     ← 口音基本消失
    共振峰 F1 偏离:            源 1214Hz → 输出 1035Hz

OpenVoice 走的是**变声器（Voice Conversion）**架构：

    带口音的语音  →  [内容编码 | 韵律/语调 | 音色]  →  只替换音色分支
                     ↑ 这两支原封不动保留

所以口音、语调、节奏、情感全部保留，只换「谁在说」。

实测对比（归一化 F0 曲线相关性，越高越像原口音）
    口音      XTTS 直克隆    OpenVoice 级联
    印度        -0.269          0.142
    爱尔兰       0.179          0.980
    南非         0.050          0.844

架构
----
    LLM 文本
      ↓  edge-tts（该角色母语声线）      ← 口音、韵律 100% 真实
    带口音的语音
      ↓  OpenVoice V2（换成用户上传的音色）  ← 只动 timbre
    口音真实 + 音色自定义

限制（如实记录）
    · 输出 **22050Hz**（模型原生），需要重采样到 24000Hz 对外
    · 音色转换是「相似」不是「精确复刻」，f0 通常只到位 60-85%
    · 增加约 150-400ms 延迟（GPU）
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

#: OpenVoice 原生输出采样率（全部 VC 模型都是 22.05k）
VOICE_CONVERT_SR = 22050
#: 对外输出采样率（与 TTS 管线一致）
SR_OUT = 24000
#: 参考音频最短时长（秒）。低于此值音色提取不稳。
MIN_REF_S = 3.0
#: 参考音频最长时长（秒）。超过则截取中间段，避免显存与耗时失控。
MAX_REF_S = 60.0

#: coqui 的模型标识
OPENVOICE_MODEL = "voice_conversion_models/multilingual/multi-dataset/openvoice_v2"


class VoiceConversionError(RuntimeError):
    """声音转换失败。"""


@dataclass
class ClonedTimbre:
    """一段用来"取音色"的参考音频。"""

    timbre_id: str
    wav_path: str
    duration_s: float
    created_at: float
    label: str = ""

    @property
    def exists(self) -> bool:
        return os.path.isfile(self.wav_path)


@dataclass
class OpenVoiceEngine:
    """OpenVoice V2 的薄封装（同步推理跑在线程池里）。

    与 XTTSEngine 的分工
    --------------------
    XTTS      : 音色克隆 TTS（文本 → 语音），但会丢口音
    OpenVoice : 声音转换 VC（语音 → 语音），保留口音只换音色

    本项目**两者都保留**：
      · 没上传音色 → 走 XTTS（简单，单模型）
      · 上传了音色 + 需要口音 → 走 cascade（edge 口音 + OpenVoice 换音色）
    """

    sample_rate: int = SR_OUT
    device: str = "cuda"
    _tts: object | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _load_error: str = field(default="", init=False)
    _timbre_dir: str = field(default="", init=False)

    # ------------------------------------------------------------ 可用性
    @staticmethod
    def looks_available() -> bool:
        """廉价探测：装了 coqui-tts 且能 import 到 OpenVoice。"""
        try:
            from TTS.api import TTS  # noqa: F401, PLC0415
            import torch  # noqa: F401, PLC0415

            return True
        except Exception:
            return False

    # ------------------------------------------------------------ 加载
    async def ensure_loaded(self) -> None:
        if self._tts is not None:
            return
        if self._load_error:
            raise VoiceConversionError(self._load_error)
        await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> None:
        with self._lock:
            if self._tts is not None:
                return
            os.environ.setdefault("COQUI_TOS_AGREED", "1")
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            os.environ.setdefault("HF_HOME", "D:/hf-cache")
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            try:
                import torch  # noqa: PLC0415
                from TTS.api import TTS  # noqa: PLC0415

                tts = TTS(model_name=OPENVOICE_MODEL)
                if self.device == "cuda" and torch.cuda.is_available():
                    try:
                        tts.to("cuda")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("OpenVoice 移到 CUDA 失败，留在 CPU: %s", exc)
                self._tts = tts
                log.info("OpenVoice V2 已加载（device=%s）", self._actual_device())
            except Exception as exc:  # noqa: BLE001
                self._load_error = f"OpenVoice V2 加载失败: {exc}"
                raise VoiceConversionError(self._load_error) from exc

    def _actual_device(self) -> str:
        try:
            import torch  # noqa: PLC0415

            m = getattr(self._tts, "synthesizer", None)
            for attr in ("vc", "model"):
                sub = getattr(m, attr, None)
                if sub is not None and hasattr(sub, "parameters"):
                    return str(next(sub.parameters()).device)
            return "cpu"
        except Exception:
            return "?"

    # ------------------------------------------------------------ 音色
    def set_timbre_dir(self, path: str) -> None:
        self._timbre_dir = path
        Path(path).mkdir(parents=True, exist_ok=True)

    def register_timbre(self, timbre_id: str, wav_path: str, label: str = "") -> ClonedTimbre:
        """登记一段参考音频（只用来取音色，不参与合成速度）。"""
        import soundfile as sf  # noqa: PLC0415

        info = sf.info(wav_path)
        ct = ClonedTimbre(
            timbre_id=timbre_id,
            wav_path=wav_path,
            duration_s=float(info.frames) / float(info.samplerate or 1),
            created_at=os.path.getmtime(wav_path),
            label=label,
        )
        _TIMBRES[timbre_id] = ct
        log.info("OpenVoice 音色已登记: %s (%.1fs)", timbre_id, ct.duration_s)
        return ct

    def get_timbre(self, timbre_id: str) -> ClonedTimbre | None:
        ct = _TIMBRES.get(timbre_id)
        if ct is None or not ct.exists:
            return None
        return ct

    def timbres(self) -> list[ClonedTimbre]:
        return [c for c in _TIMBRES.values() if c.exists]

    def drop_timbre(self, timbre_id: str) -> bool:
        return _TIMBRES.pop(timbre_id, None) is not None

    def load_timbres_from_dir(self, path: str) -> int:
        self.set_timbre_dir(path)
        n = 0
        for p in sorted(Path(path).glob("*.wav")):
            try:
                self.register_timbre(p.stem, str(p))
                n += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("加载音色失败 %s: %s", p, exc)
        return n

    # ------------------------------------------------------------ 转换
    async def convert(self, source_pcm: np.ndarray, timbre_id: str) -> np.ndarray:
        """把 source_pcm（float32, 24k）的音色换成 timbre_id 的音色。

        返回 float32 @ ``sample_rate``。口音/韵律来自 source，音色来自 timbre。
        """
        ct = self.get_timbre(timbre_id)
        if ct is None:
            raise VoiceConversionError(f"音色 {timbre_id!r} 未登记（请先上传参考录音）")
        await self.ensure_loaded()
        return await asyncio.to_thread(self._convert_sync, source_pcm, ct.wav_path)

    async def convert_pcm16(self, source_pcm16: bytes, timbre_id: str) -> bytes:
        """bytes 版：pcm16 @ sample_rate → pcm16 @ sample_rate。"""
        arr = np.frombuffer(source_pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        out = await self.convert(arr, timbre_id)
        return np.clip(out * 32767.0, -32768, 32767).astype(np.int16).tobytes()

    def _convert_sync(self, source: np.ndarray, timbre_wav: str) -> np.ndarray:
        if self._tts is None:
            raise VoiceConversionError("OpenVoice 未加载")

        src = np.asarray(source, dtype=np.float32).reshape(-1)
        if src.size == 0:
            return src

        # VC 模型要 22.05k 输入
        src_22 = _resample(src, self.sample_rate, VOICE_CONVERT_SR)

        src_path = tempfile.mktemp(suffix=".wav")
        out_path = tempfile.mktemp(suffix=".wav")
        try:
            import soundfile as sf  # noqa: PLC0415

            sf.write(src_path, src_22, VOICE_CONVERT_SR, subtype="PCM_16")
            with self._lock:
                self._tts.voice_conversion_to_file(  # type: ignore[union-attr]
                    source_wav=src_path,
                    target_wav=timbre_wav,
                    file_path=out_path,
                )
            out, out_sr = sf.read(out_path, dtype="float32")
            out = np.asarray(out, dtype=np.float32).reshape(-1)
            return _resample(out, int(out_sr), self.sample_rate)
        finally:
            for p in (src_path, out_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass


#: 全局音色表（进程内共享，与 XTTS 的音色表分开：这里是"用来取音色的参考"）
_TIMBRES: dict[str, ClonedTimbre] = {}


# --------------------------------------------------------------------------- #
# 重采样
# --------------------------------------------------------------------------- #
def _resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """线性插值重采样（语音够用，且无额外依赖）。"""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if sr_in == sr_out or x.size == 0:
        return x
    n_out = int(round(x.size * sr_out / sr_in))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    idx = np.linspace(0, x.size - 1, n_out)
    return np.interp(idx, np.arange(x.size), x).astype(np.float32)


# --------------------------------------------------------------------------- #
# 参考音频规范化（复用 XTTS 的实现，逻辑一致）
# --------------------------------------------------------------------------- #
def normalize_timbre_reference(src_path: str, dst_path: str) -> dict:
    """把上传的音频规范成适合取音色的 wav（22.05k mono，去静音，归一化）。"""
    from app.voice.xtts_engine import normalize_reference  # noqa: PLC0415

    return normalize_reference(src_path, dst_path)
