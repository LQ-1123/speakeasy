"""音频存储与解码（v2 异步语音条）。

- 用户上传（webm/ogg/wav/mp3/mp4）→ 用 ``audio_utils.read_audio_file`` 解码成
  16 kHz 单声道 float32 → 既返回 PCM16 给 STT，也落一份 16k wav 供历史回放。
- 好友 TTS 音频：由 :class:`app.v2.voices.FriendVoiceSynth` 直接写进 ``data/audio``，
  这里只负责路径解析与静态服务。
"""

from __future__ import annotations

import os
import uuid

from app.logging_utils import get_logger
from app.voice.audio_utils import (
    SR_IN,
    SR_OUT,
    array_duration_s,
    float32_to_pcm16,
    read_audio_file,
    write_wav,
)

log = get_logger(__name__)

_UPLOAD_SUFFIXES = {".webm", ".ogg", ".oga", ".wav", ".mp3", ".mp4", ".m4a", ".aac", ".flac"}


class AudioStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.audio_dir = os.path.join(data_dir, "audio")
        os.makedirs(self.audio_dir, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    def path_for(self, filename: str) -> str:
        """``filename`` 必须只是文件名（防目录穿越）。"""
        name = os.path.basename(filename or "")
        if not name or name in (".", ".."):
            raise ValueError("非法音频文件名")
        return os.path.join(self.audio_dir, name)

    @staticmethod
    def url_for(filename: str | None) -> str | None:
        if not filename:
            return None
        return f"/api/v2/audio/{os.path.basename(filename)}"

    # ------------------------------------------------------------------ #
    # 用户上传
    # ------------------------------------------------------------------ #
    def decode_upload(self, data: bytes, filename: str) -> dict:
        """解码上传音频。

        Returns:
            ``{"pcm16": bytes(16k mono), "duration_ms": int, "audio_filename": str,
              "sample_rate": 16000}``
        Raises:
            ValueError: 空文件/无法解码。
        """
        if not data:
            raise ValueError("上传音频为空")
        suffix = os.path.splitext(filename or "")[1].lower()
        if suffix not in _UPLOAD_SUFFIXES:
            suffix = ".webm"
        tmp = os.path.join(self.audio_dir, f".upload_{uuid.uuid4().hex}{suffix}")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            try:
                audio = read_audio_file(tmp, target_sr=SR_IN)
            except Exception as exc:  # noqa: BLE001 - 统一成 422 可识别的 ValueError
                raise ValueError(f"无法解码音频: {exc}") from exc
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if audio.size == 0:
            raise ValueError("音频解码为空")

        duration_s = array_duration_s(audio, SR_IN)
        audio_filename = f"u_{uuid.uuid4().hex}.wav"
        write_wav(self.path_for(audio_filename), audio, SR_IN)
        return {
            "pcm16": float32_to_pcm16(audio),
            "duration_ms": int(duration_s * 1000),
            "audio_filename": audio_filename,
            "sample_rate": SR_IN,
        }

    def save_pcm_wav(self, pcm16: bytes, prefix: str = "u") -> str:
        """把 PCM16 16k 存成 wav，返回文件名。"""
        from app.voice.audio_utils import pcm16_to_float32  # noqa: PLC0415

        name = f"{prefix}_{uuid.uuid4().hex}.wav"
        write_wav(self.path_for(name), pcm16_to_float32(pcm16), SR_IN)
        return name

    def save_tone_wav(self, audio) -> str:
        """保存 24k float32 音频（tone 兜底时用）。"""
        name = f"t_{uuid.uuid4().hex}.wav"
        write_wav(self.path_for(name), audio, SR_OUT)
        return name
