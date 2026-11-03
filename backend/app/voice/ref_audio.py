"""参考音频库：zero-shot 克隆所需的 ``persona_id → (wav, ref_text, speed)`` 映射。

契约见 ``docs/CONTRACTS.md`` §5。

设计要点
--------
- **缺失即可用**：没有参考音频时 ``get()`` 返回 ``None``，``TTSEngine`` 会退回
  tone 后端合成该句（不会整轮失败）。``ensure()`` 才是"必须有"的语义。
- **manifest 与角色配置解耦**：manifest 描述"文件 + 逐字转写 + 来源/许可"；
  ``personas.json`` 里的 ``voice.ref_audio`` 只是文件名提示。两者对不上时以
  manifest 为准（manifest 是 TTS 层的权威清单）。
- ``validate()`` 是给 ``scripts/smoke_test.py`` 用的体检报告：**返回问题列表**，
  不抛异常、不阻塞启动。

manifest 结构（``assets/ref_audio/manifest.json``）::

    {
      "version": 1,
      "sample_rate_hint": 24000,
      "min_duration_s": 4.0,
      "max_duration_s": 12.0,
      "voices": {
        "oliver": {
          "file": "oliver_rp.wav",
          "ref_text": "Right then, that's a flat white for you...",
          "duration_s": 7.4,
          "speed": 1.0,
          "description": "warm, clipped, slightly wry",
          "source": {"license": "CC0-1.0", "url": "...", "author": "..."}
        }
      }
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

#: 参考音频合理的时长区间（秒）。太短克隆不稳，太长首包延迟高。
MIN_DURATION_S = 4.0
MAX_DURATION_S = 12.0
#: 允许的采样率（CosyVoice2 / F5-TTS 走 16k；F5-TTS 直接吃 24k 也行）
ALLOWED_SAMPLE_RATES = (16000, 24000)


class ReferenceAudioMissing(RuntimeError):
    """``ensure()`` 时参考音频缺失或不可用。"""


@dataclass(frozen=True)
class ReferenceVoice:
    """一个角色的克隆参考。"""

    persona_id: str
    ref_wav: str
    """绝对路径，16k 或 24k wav，5–10 s。"""
    ref_text: str
    """参考音频的逐字转写（zero-shot 必需）。"""
    speed: float = 1.0
    description: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.ref_wav) and os.path.isfile(self.ref_wav)

    @property
    def usable(self) -> bool:
        """文件存在**且**转写非空——零样本克隆的最小可用条件。"""
        return self.exists and bool(self.ref_text.strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "ref_wav": self.ref_wav,
            "ref_text": self.ref_text,
            "speed": self.speed,
            "description": self.description,
            "exists": self.exists,
        }


def _as_float(value: Any, default: float = 1.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):   # NaN / inf
        return default
    return f


class ReferenceLibrary:
    """加载并查询 ``assets/ref_audio/manifest.json``。

    Args:
        ref_dir: 参考音频目录（``settings.ref_audio_path``）。
        manifest_path: manifest 路径；``None`` → ``<ref_dir>/manifest.json``。

    构造**不抛异常**（manifest 缺失只记 warning），保证服务总能启动。
    """

    def __init__(self, ref_dir: str, manifest_path: str | None = None) -> None:
        self.ref_dir = os.path.abspath(ref_dir) if ref_dir else os.path.abspath(".")
        self.manifest_path = (
            os.path.abspath(manifest_path)
            if manifest_path
            else os.path.join(self.ref_dir, "manifest.json")
        )
        self._voices: dict[str, ReferenceVoice] = {}
        self._order: list[str] = []
        self._meta: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not os.path.isfile(self.manifest_path):
            log.warning(
                "参考音频 manifest 不存在: %s（克隆后端将退回 tone 兜底）", self.manifest_path
            )
            return
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("读取参考音频 manifest 失败 %s: %s", self.manifest_path, exc)
            return
        if not isinstance(raw, dict):
            log.warning("参考音频 manifest 顶层不是对象: %s", type(raw).__name__)
            return

        self._meta = {
            "version": raw.get("version", 1),
            "sample_rate_hint": raw.get("sample_rate_hint", 24000),
            "min_duration_s": _as_float(raw.get("min_duration_s", MIN_DURATION_S), MIN_DURATION_S),
            "max_duration_s": _as_float(raw.get("max_duration_s", MAX_DURATION_S), MAX_DURATION_S),
        }

        # 兼容两种写法：{"voices": {id: {...}}} 与顶层直接 {id: {...}}
        entries = raw.get("voices")
        if not isinstance(entries, dict):
            entries = {
                k: v for k, v in raw.items()
                if isinstance(v, dict) and ("file" in v or "path" in v)
            }

        for pid, item in entries.items():
            if not isinstance(item, dict):
                log.warning("manifest 中 %s 的配置不是对象，跳过", pid)
                continue
            rel = str(item.get("file") or item.get("path") or "").strip()
            if not rel:
                log.warning("manifest 中 %s 缺少 file 字段，跳过", pid)
                continue
            wav = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(self.ref_dir, rel))
            voice = ReferenceVoice(
                persona_id=str(pid),
                ref_wav=wav,
                ref_text=str(item.get("ref_text") or "").strip(),
                speed=_as_float(item.get("speed", 1.0), 1.0),
                description=str(item.get("description") or ""),
            )
            self._voices[voice.persona_id] = voice
            self._order.append(voice.persona_id)

        log.info(
            "参考音频库: %d 个条目（%d 个文件可用）from %s",
            len(self._voices),
            sum(1 for v in self._voices.values() if v.exists),
            self.manifest_path,
        )

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get(self, persona_id: str) -> ReferenceVoice | None:
        """拿角色的参考音频；未知 id 返回 ``None``（不抛）。"""
        if not persona_id:
            return None
        return self._voices.get(str(persona_id))

    def ensure(self, persona_id: str) -> ReferenceVoice:
        """拿"可用的"参考音频；缺失/不完整抛 :class:`ReferenceAudioMissing`。"""
        voice = self.get(persona_id)
        if voice is None:
            raise ReferenceAudioMissing(
                f"参考音频未配置: persona_id={persona_id!r}（manifest={self.manifest_path}）"
            )
        if not voice.exists:
            raise ReferenceAudioMissing(f"参考音频文件不存在: {voice.ref_wav}")
        if not voice.ref_text.strip():
            raise ReferenceAudioMissing(f"参考音频缺少 ref_text: {voice.ref_wav}")
        return voice

    def all(self) -> list[ReferenceVoice]:
        return [self._voices[i] for i in self._order]

    def ids(self) -> list[str]:
        return list(self._order)

    def available_ids(self) -> list[str]:
        """仅返回文件真实存在的 persona_id。"""
        return [v.persona_id for v in self.all() if v.usable]

    def __len__(self) -> int:
        return len(self._voices)

    def __contains__(self, persona_id: object) -> bool:
        return isinstance(persona_id, str) and persona_id in self._voices

    # ------------------------------------------------------------------ #
    # 体检
    # ------------------------------------------------------------------ #
    def validate(self) -> list[str]:
        """返回问题列表（空列表 = 全部合格）。

        检查项：文件缺失 / 时长越界 / ref_text 为空 / 采样率非 16k 或 24k /
        全静音。**不解码整段**（wav 只读头），所以可以放心在启动时调用。
        """
        problems: list[str] = []
        if not os.path.isfile(self.manifest_path):
            problems.append(f"manifest 不存在: {self.manifest_path}")
            return problems
        if not self._voices:
            problems.append(f"manifest 中没有任何 voice 条目: {self.manifest_path}")
            return problems

        min_d = float(self._meta.get("min_duration_s", MIN_DURATION_S))
        max_d = float(self._meta.get("max_duration_s", MAX_DURATION_S))

        for voice in self.all():
            tag = f"[{voice.persona_id}]"
            if not voice.ref_wav:
                problems.append(f"{tag} 未指定 file")
                continue
            if not os.path.isfile(voice.ref_wav):
                problems.append(f"{tag} 文件不存在: {voice.ref_wav}")
                continue

            if not voice.ref_text.strip():
                problems.append(f"{tag} ref_text 为空（zero-shot 必需）")

            try:
                from app.voice.audio_utils import probe_audio_file  # noqa: PLC0415

                sr, dur = probe_audio_file(voice.ref_wav)
            except Exception as exc:  # noqa: BLE001 - 体检不应抛
                problems.append(f"{tag} 无法读取音频 {os.path.basename(voice.ref_wav)}: {exc}")
                continue

            if int(sr) not in ALLOWED_SAMPLE_RATES:
                problems.append(
                    f"{tag} 采样率 {sr} Hz 不受支持（需 16000 或 24000）: "
                    f"{os.path.basename(voice.ref_wav)}"
                )
            if dur < min_d - 1e-6:
                problems.append(f"{tag} 时长 {dur:.2f}s 过短（需 ≥ {min_d:g}s）")
            elif dur > max_d + 1e-6:
                problems.append(f"{tag} 时长 {dur:.2f}s 过长（需 ≤ {max_d:g}s）")

            try:
                from app.voice.audio_utils import peak, read_audio_file  # noqa: PLC0415

                x = read_audio_file(voice.ref_wav, target_sr=int(sr))
                if x.size and peak(x) < 0.005:
                    problems.append(f"{tag} 音频近乎全静音（peak={peak(x):.4f}）")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{tag} 解码失败: {exc}")

        return problems

    def summary(self) -> dict[str, Any]:
        """给 ``/api/health`` 的简明状态。"""
        total = len(self._voices)
        ready = sum(1 for v in self.all() if v.usable)
        return {
            "manifest": self.manifest_path,
            "total": total,
            "ready": ready,
            "missing": max(0, total - ready),
            "persona_ids": self.ids(),
        }


__all__ = [
    "MIN_DURATION_S",
    "MAX_DURATION_S",
    "ALLOWED_SAMPLE_RATES",
    "ReferenceAudioMissing",
    "ReferenceVoice",
    "ReferenceLibrary",
]
