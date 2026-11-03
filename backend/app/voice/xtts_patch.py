"""XTTS-v2 的 torchcodec 绕行层。

问题
----
torch 2.9+ 起 ``torchaudio.load`` 内部改用 torchcodec，而 torchcodec 的预编译
DLL 与我们这套 torch 2.11+cu128 的 ABI 不匹配：

    OSError: Could not load this library: ...\\libtorchcodec_core6.dll

于是 XTTS 在读参考音频时直接崩掉。但**模型本身能正常加载并推理**（实测 1.74GB
显存、17 种语言），只有这一个 IO 环节坏掉。

解决方案
--------
把 ``TTS.tts.models.xtts.load_audio`` 换成 soundfile 实现。语义完全等价：

    (audio: Tensor[1, T] float32 in [-1,1], sample_rate)

这样绕开 torchcodec / torchaudio 的整套 decode 链路，且 soundfile 是纯 wheel
（libsndfile 静态链接），不依赖系统 FFmpeg。

同时也要处理 ``torchaudio.load`` 的其他调用点（如 VC 模型），但 XTTS 路径只需要
上面这一个。
"""

from __future__ import annotations

import numpy as np

from app.logging_utils import get_logger

log = get_logger(__name__)

_patched = False


def _load_audio_soundfile(audiopath, sampling_rate):
    """替代 ``TTS.tts.models.xtts.load_audio``。

    用 soundfile 读 → numpy float32 mono → torch tensor (1, T)，
    必要时用线性插值重采样到目标采样率。
    """
    import soundfile as sf
    import torch

    data, sr = sf.read(str(audiopath), dtype="float32", always_2d=True)
    # always_2d → (T, channels)；取平均转 mono
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = np.ascontiguousarray(mono, dtype=np.float32)

    if sr != sampling_rate:
        ratio = float(sampling_rate) / float(sr)
        n_out = max(1, int(round(mono.size * ratio)))
        # 线性插值（足够好，且不引入额外依赖）
        src_idx = np.linspace(0.0, mono.size - 1, n_out, dtype=np.float64)
        mono = np.interp(src_idx, np.arange(mono.size, dtype=np.float64), mono).astype(np.float32)

    audio = torch.from_numpy(mono).unsqueeze(0)
    audio.clip_(-1, 1)
    return audio


def apply() -> bool:
    """幂等地打补丁。返回是否成功。"""
    global _patched
    if _patched:
        return True
    try:
        import TTS.tts.models.xtts as xtts_mod  # noqa: PLC0415

        xtts_mod.load_audio = _load_audio_soundfile  # type: ignore[assignment]
        _patched = True
        log.info("XTTS load_audio 已替换为 soundfile 实现（绕开 torchcodec）")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("无法打 XTTS load_audio 补丁: %s", exc)
        return False
