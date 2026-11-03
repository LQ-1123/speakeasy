"""XTTS-v2 声音克隆后端：用 6–30 秒参考音频克隆任何音色。

为什么是 XTTS-v2
----------------
- **零样本克隆**：只要一段参考音频 + 对应文字，无需训练、无需微调。
- **中文/英文等多语言**：17 种语言，英语质量好。
- **6 秒起可用**：官方建议 ≥6s；30s–1min 的录音效果更好（我们支持自动切片）。
- 相比 F5-TTS：依赖干净得多（无 wandb/gradio/boto3），模型 ~1.9GB。
- 相比 CosyVoice2：不需要 pynini（Windows 上编译困难）。

本机实测（RTX 5080）
--------------------
- 加载：8.2s，显存 1.74GB
- 合成：718–1319ms（3.5s 音频）
- 三个不同参考音色 → ZCR 0.053/0.083/0.079，可明确区分

Windows 上的一个坑
------------------
torch 2.9+ 的 ``torchaudio.load`` 改走 torchcodec，而 torchcodec 的预编译 DLL 与
torch 2.11+cu128 ABI 不匹配，导致读参考音频直接崩。
解决：用 :mod:`app.voice.xtts_patch` 把 ``load_audio`` 换成 soundfile 实现。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.logging_utils import get_logger

log = get_logger(__name__)

#: XTTS-v2 原生输出采样率
XTTS_SR = 24000
#: 官方建议的最小/最大参考时长（秒）
MIN_REF_S = 3.0
MAX_REF_S = 120.0
#: 传给模型的参考音频上限（太长会拖慢首包；60s 足够抓住音色）
MODEL_REF_MAX_S = 30.0


class XTTSError(RuntimeError):
    pass


@dataclass
class ClonedVoice:
    """一个已上传的克隆音色。"""

    voice_id: str
    """用户/角色标识（如 persona_id 或 'custom_xxx'）"""
    wav_path: str
    """规范化后的 22.05k mono wav"""
    ref_text: str = ""
    duration_s: float = 0.0
    created_at: float = 0.0

    @property
    def exists(self) -> bool:
        return bool(self.wav_path) and os.path.isfile(self.wav_path)


def normalize_reference(
    src_path: str,
    dst_path: str,
    *,
    target_sr: int = 22050,
    max_s: float = MODEL_REF_MAX_S,
) -> dict[str, Any]:
    """把用户上传的任意音频规范化成 XTTS 需要的 wav。

    - 转 mono / 22.05kHz / wav
    - 超过 ``max_s`` 时截取**中间段**（开头常有"喂喂"试音，结尾常有静音）
    - 去掉首尾静音
    - 音量归一化

    返回 ``{"duration_s", "sample_rate", "trimmed"}``。
    """
    import soundfile as sf

    data, sr = sf.read(src_path, dtype="float32", always_2d=True)
    if data.size == 0:
        raise XTTSError("音频为空")
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = np.asarray(mono, dtype=np.float32)

    dur = mono.size / float(sr)
    if dur < MIN_REF_S:
        raise XTTSError(
            f"音频太短（{dur:.1f}s）。XTTS 克隆至少需要 {MIN_REF_S:.0f} 秒，"
            f"建议 10–30 秒。"
        )
    if dur > MAX_REF_S:
        raise XTTSError(f"音频太长（{dur:.0f}s）。请上传 30 秒到 1 分钟的片段。")

    # 重采样到 22.05k
    if sr != target_sr:
        ratio = target_sr / float(sr)
        n_out = max(1, int(round(mono.size * ratio)))
        idx = np.linspace(0.0, mono.size - 1, n_out, dtype=np.float64)
        mono = np.interp(idx, np.arange(mono.size, dtype=np.float64), mono).astype(np.float32)
        sr = target_sr

    # 去首尾静音（基于能量阈值）
    mono = _trim_silence(mono, sr)

    # 太长则取中间段
    trimmed = False
    max_n = int(max_s * sr)
    if mono.size > max_n:
        start = max(0, (mono.size - max_n) // 2)
        mono = mono[start : start + max_n]
        trimmed = True

    # 峰值归一化到 -3dB，避免削波
    peak = float(np.abs(mono).max())
    if peak > 1e-6:
        mono = mono * (0.7 / peak)
    mono = np.clip(mono, -1.0, 1.0)

    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
    sf.write(dst_path, mono, sr, subtype="PCM_16")

    return {
        "duration_s": round(mono.size / float(sr), 2),
        "sample_rate": sr,
        "trimmed": trimmed,
    }


def _trim_silence(mono: np.ndarray, sr: int, *, keep_margin_s: float = 0.15) -> np.ndarray:
    """按帧能量去掉首尾静音，保留一点余量。"""
    if mono.size == 0:
        return mono
    frame = max(1, int(sr * 0.02))  # 20ms
    n_frames = mono.size // frame
    if n_frames < 2:
        return mono
    frames = mono[: n_frames * frame].reshape(n_frames, frame)
    energy = np.sqrt(np.mean(frames**2, axis=1))
    peak = float(energy.max())
    if peak <= 1e-6:
        return mono
    thr = max(peak * 0.06, 1e-4)
    voiced = np.where(energy > thr)[0]
    if voiced.size == 0:
        return mono
    margin = int(keep_margin_s / 0.02)
    start = max(0, int(voiced[0]) - margin) * frame
    end = min(n_frames, int(voiced[-1]) + 1 + margin) * frame
    return mono[start:end]


class XTTSEngine:
    """XTTS-v2 封装：加载一次，反复用不同参考音频合成。"""

    backend_name = "xtts"

    def __init__(self, *, sample_rate: int = XTTS_SR, device: str = "cuda") -> None:
        self.sample_rate = sample_rate
        self.device = device
        self._tts: Any = None
        self._load_lock = asyncio.Lock()
        self._infer_sem = asyncio.Semaphore(1)  # 单卡串行
        self._voices: dict[str, ClonedVoice] = {}

    # ---------------- 探测 ----------------
    @staticmethod
    def looks_available() -> bool:
        try:
            import TTS  # noqa: F401,PLC0415

            return True
        except Exception:
            return False

    # ---------------- 加载 ----------------
    async def ensure_loaded(self) -> None:
        if self._tts is not None:
            return
        async with self._load_lock:
            if self._tts is not None:
                return
            self._tts = await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> Any:
        # 必须先打补丁（绕开 torchcodec）
        from app.voice.xtts_patch import apply as apply_patch  # noqa: PLC0415

        apply_patch()

        os.environ.setdefault("COQUI_TOS_AGREED", "1")
        try:
            from TTS.api import TTS  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise XTTSError(f"coqui-tts 未安装或不可用: {exc}") from exc

        device = self._pick_device()
        model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
        log.info("XTTS-v2 已加载 (device=%s)", device)
        return model

    def _pick_device(self) -> str:
        try:
            import torch  # noqa: PLC0415

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:  # noqa: BLE001
            return "cpu"

    # ---------------- 音色登记 ----------------
    def register_voice(self, voice_id: str, wav_path: str, *, ref_text: str = "") -> ClonedVoice:
        import soundfile as sf  # noqa: PLC0415

        try:
            info = sf.info(wav_path)
            dur = float(info.duration)
        except Exception:  # noqa: BLE001
            dur = 0.0
        import time

        voice = ClonedVoice(
            voice_id=voice_id,
            wav_path=wav_path,
            ref_text=ref_text,
            duration_s=dur,
            created_at=time.time(),
        )
        self._voices[voice_id] = voice
        return voice

    def get_voice(self, voice_id: str) -> ClonedVoice | None:
        v = self._voices.get(voice_id)
        return v if v and v.exists else None

    def voices(self) -> list[ClonedVoice]:
        return [v for v in self._voices.values() if v.exists]

    def drop_voice(self, voice_id: str) -> bool:
        v = self._voices.pop(voice_id, None)
        if v is None:
            return False
        try:
            if os.path.isfile(v.wav_path):
                os.unlink(v.wav_path)
        except OSError:
            pass
        return True

    def load_voices_from_dir(self, ref_dir: str) -> int:
        """启动时把已存在的 ``<id>.wav`` 收进内存（跨重启保留音色）。"""
        if not os.path.isdir(ref_dir):
            return 0
        n = 0
        for name in os.listdir(ref_dir):
            if not name.lower().endswith(".wav"):
                continue
            vid = os.path.splitext(name)[0]
            path = os.path.join(ref_dir, name)
            try:
                self.register_voice(vid, path)
                n += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("载入音色 %s 失败: %s", name, exc)
        if n:
            log.info("XTTS 从 %s 载入 %d 个已保存音色", ref_dir, n)
        return n

    # ---------------- 合成 ----------------
    async def synthesize(self, text: str, voice_id: str) -> np.ndarray:
        """用指定音色合成，返回 float32 mono @24k。"""
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        await self.ensure_loaded()
        voice = self.get_voice(voice_id)
        if voice is None:
            raise XTTSError(f"音色 {voice_id!r} 未上传（请先上传 30s–1min 录音）")

        async with self._infer_sem:
            return await asyncio.to_thread(self._synth_sync, text, voice.wav_path)

    def _synth_sync(self, text: str, ref_wav: str) -> np.ndarray:
        if self._tts is None:
            raise XTTSError("XTTS 未加载")
        wav = self._tts.tts(
            text=text,
            speaker_wav=ref_wav,
            language="en",
            split_sentences=False,
        )
        arr = np.asarray(wav, dtype=np.float32).reshape(-1)
        return np.clip(arr, -1.0, 1.0)

    async def synthesize_pcm16(self, text: str, voice_id: str) -> bytes:
        arr = await self.synthesize(text, voice_id)
        pcm = np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)
        return pcm.tobytes()


__all__ = [
    "MAX_REF_S",
    "MIN_REF_S",
    "MODEL_REF_MAX_S",
    "ClonedVoice",
    "XTTS_SR",
    "XTTSEngine",
    "XTTSError",
    "normalize_reference",
]
