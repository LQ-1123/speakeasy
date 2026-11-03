"""TTS 运行时配置（edge / fish），落盘 data/tts_config.json。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

CONFIG_FILENAME = "tts_config.json"
DEFAULT_FISH_BASE = "https://api.fish.audio"
DEFAULT_FISH_MODEL = "s2.1-pro-free"


def _mask(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:5]}...{key[-4:]}"


@dataclass
class TTSConfig:
    """provider=edge 用 edge-tts（免费）；provider=fish 用 Fish Audio。"""

    provider: str = "edge"
    fish_api_key: str = ""
    fish_base_url: str = DEFAULT_FISH_BASE
    fish_model: str = DEFAULT_FISH_MODEL
    #: Fish 预设音色 / 自有 voice model 的 reference_id（可留空走默认音色）
    fish_reference_id: str = "e3cd384158934cc9a01029cd7d278634"
    fish_format: str = "mp3"
    extra: dict[str, Any] = field(default_factory=dict)

    # ---- 状态 ----
    @property
    def enabled(self) -> bool:
        return self.provider == "fish"

    @property
    def configured(self) -> bool:
        return self.provider != "fish" or bool(self.fish_api_key.strip())

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "configured": self.configured,
            "fish_api_key": _mask(self.fish_api_key),
            "fish_base_url": self.fish_base_url,
            "fish_model": self.fish_model,
            "fish_reference_id": self.fish_reference_id,
            "fish_format": self.fish_format,
        }

    def to_file_dict(self) -> dict[str, Any]:
        return asdict(self)


class TTSConfigStore:
    """读写 tts_config.json，原子替换。"""

    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(data_dir, CONFIG_FILENAME)
        self.data_dir = data_dir
        self._cfg: TTSConfig | None = None

    def load(self) -> TTSConfig:
        if self._cfg is not None:
            return self._cfg
        cfg = TTSConfig()
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f) or {}
            for k, v in raw.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        if cfg.provider not in ("edge", "fish"):
            cfg.provider = "edge"
        self._cfg = cfg
        return cfg

    def save(self, **patch: Any) -> TTSConfig:
        cfg = self.load()
        for k, v in patch.items():
            if v is None or not hasattr(cfg, k):
                continue
            if k == "provider" and v not in ("edge", "fish"):
                continue
            setattr(cfg, k, v)
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg.to_file_dict(), f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        return cfg

    def reset(self) -> TTSConfig:
        self._cfg = TTSConfig()
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        return self._cfg
