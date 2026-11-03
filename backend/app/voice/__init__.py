"""本地语音层：音频工具 / 流式 VAD / STT / TTS / 参考音频库。

导入本包**不会**加载任何模型或重依赖（torch / faster-whisper / silero-vad 都在
函数内部 import），因此在没有 GPU、没有模型权重的机器上依然可以 `import app.voice`。

契约见 ``docs/CONTRACTS.md`` §1–§5。

常用入口::

    from app.voice import StreamingVAD, VADEvent, STTEngine, TTSEngine

    vad = StreamingVAD(settings.vad)
    stt = STTEngine(settings.stt)
    tts = TTSEngine(settings.tts, personas=personas)
"""

from __future__ import annotations

from app.voice.audio_utils import (
    SR_IN,
    SR_OUT,
    AudioDecodeError,
    duration_s,
    float32_to_pcm16,
    pcm16_to_float32,
    read_audio_file,
    resample,
    rms,
    trim_silence,
    write_wav,
)
from app.voice.ref_audio import (
    ReferenceAudioMissing,
    ReferenceLibrary,
    ReferenceVoice,
)
from app.voice.stt import STTEngine, Transcript, clean_transcript
from app.voice.tone_tts import ToneSynthesizer
from app.voice.tts import (
    AudioChunk,
    TTSEngine,
    TTSError,
    TTSSentenceRequest,
    normalize_for_tts,
    split_for_tts,
)
from app.voice.vad import StreamingVAD, VADEvent, VADResult

__all__ = [
    # audio_utils
    "SR_IN",
    "SR_OUT",
    "AudioDecodeError",
    "pcm16_to_float32",
    "float32_to_pcm16",
    "resample",
    "duration_s",
    "rms",
    "trim_silence",
    "write_wav",
    "read_audio_file",
    # vad
    "VADEvent",
    "VADResult",
    "StreamingVAD",
    # stt
    "Transcript",
    "STTEngine",
    "clean_transcript",
    # tts
    "AudioChunk",
    "TTSSentenceRequest",
    "TTSEngine",
    "TTSError",
    "ToneSynthesizer",
    "normalize_for_tts",
    "split_for_tts",
    # ref audio
    "ReferenceVoice",
    "ReferenceLibrary",
    "ReferenceAudioMissing",
]
