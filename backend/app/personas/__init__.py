"""角色（Persona）库：加载 personas.json，构造 system prompt，对外暴露公开字段。

对外只暴露安全的公开字段（不含完整 prompt），完整 prompt 仅在服务端使用。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "personas.json")

# 全局追加的输出约束：无论角色怎么写，都必须遵守
_GLOBAL_RULES = """
## Non-negotiable output contract
- Reply with spoken English only. No markdown, no bullet lists, no emoji, no asterisks, no stage directions, no headings.
- Keep it SHORT: normally 1-2 sentences, at most 3. Never more than 55 words per turn.
- Always use contractions (I'm, it's, don't, that's).
- Never say you are an AI, a model, or that this is a language exercise. Stay in character 100%.
- Never correct the learner explicitly. If they make an error, silently reuse the corrected form in your own sentence.
- Do not ask more than one question per turn.
- If the learner writes in Chinese, respond in English and gently keep going in English.
"""


@dataclass(frozen=True)
class VoiceConfig:
    ref_audio: str = ""
    ref_text: str = ""
    speed: float = 1.0
    style: str = ""
    pitch_hint: str = ""


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    title: str
    scene: str
    accent: str
    accent_short: str
    flag: str
    city: str
    difficulty: str
    tags: tuple[str, ...]
    opening_line: str
    system_prompt: str
    suggestion_hint: str
    traits: tuple[str, ...]
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    voice_notes: str = ""

    # ---- 公开视图（给前端） ----
    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "scene": self.scene,
            "accent": self.accent,
            "accent_short": self.accent_short,
            "flag": self.flag,
            "city": self.city,
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "opening_line": self.opening_line,
            "traits": list(self.traits),
            "voice_notes": self.voice_notes,
            "has_ref_audio": bool(self.voice.ref_audio),
        }

    # ---- 构造消息 ----
    def build_system_prompt(self, mode_suffix: str = "", extra: str = "") -> str:
        parts = [self.system_prompt.strip()]
        if mode_suffix:
            parts.append("## Interaction mode\n" + mode_suffix.strip())
        parts.append(_GLOBAL_RULES.strip())
        if extra:
            parts.append(extra.strip())
        return "\n\n".join(parts)


@dataclass(frozen=True)
class ConversationMode:
    id: str
    name: str
    description: str
    prompt_suffix: str = ""


class PersonaLibrary:
    def __init__(self, path: str | None = None, data_dir: str | None = None) -> None:
        self.path = path or _DEFAULT_PATH
        self._personas: dict[str, Persona] = {}
        self._order: list[str] = []
        self._modes: dict[str, ConversationMode] = {}
        self._default_id: str = ""
        #: 内置角色 → 用户覆盖的声线（口音市场里改的）
        self._voice_overrides: dict[str, str] = {}
        #: 用户自定义角色/场景的存储
        self._custom: Any = None
        self._data_dir = data_dir
        self._load()
        self._load_custom()

    # ---- 自定义存储 ----
    def _load_custom(self) -> None:
        from app.personas.custom_store import CustomStore  # noqa: PLC0415

        data_dir = self._data_dir
        if data_dir is None:
            try:
                from app.config import get_settings  # noqa: PLC0415

                data_dir = str(get_settings().data_path)
            except Exception:  # noqa: BLE001
                data_dir = "data"
        self._custom = CustomStore(data_dir)
        self._voice_overrides = self._custom.load_voice_overrides()

    @property
    def custom(self) -> Any:
        return self._custom

    def reload_custom(self) -> None:
        """自定义角色/场景改动后重新载入（不动内置角色）。"""
        self._load_custom()

    # ---- 加载 ----
    def _load(self) -> None:
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"personas 配置不存在: {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        personas = raw.get("personas") or []
        if not personas:
            raise ValueError(f"personas 配置为空: {self.path}")

        for item in personas:
            p = self._parse_persona(item)
            if p.id in self._personas:
                log.warning("duplicate persona id=%s, 后者覆盖前者", p.id)
            self._personas[p.id] = p
            if p.id not in self._order:
                self._order.append(p.id)

        for item in raw.get("modes") or []:
            m = ConversationMode(
                id=str(item["id"]),
                name=str(item.get("name", item["id"])),
                description=str(item.get("description", "")),
                prompt_suffix=str(item.get("prompt_suffix", "")),
            )
            self._modes[m.id] = m

        self._default_id = str(raw.get("default_persona_id") or self._order[0])
        if self._default_id not in self._personas:
            self._default_id = self._order[0]
        log.info(
            "loaded %d personas, %d modes (default=%s) from %s",
            len(self._personas), len(self._modes), self._default_id, self.path,
        )

    @staticmethod
    def _parse_persona(item: dict[str, Any]) -> Persona:
        v = item.get("voice") or {}
        voice = VoiceConfig(
            ref_audio=str(v.get("ref_audio", "") or ""),
            ref_text=str(v.get("ref_text", "") or ""),
            speed=float(v.get("speed", 1.0) or 1.0),
            style=str(v.get("style", "") or ""),
            pitch_hint=str(v.get("pitch_hint", "") or ""),
        )
        required = ("id", "name", "system_prompt")
        for key in required:
            if not item.get(key):
                raise ValueError(f"persona 缺少必填字段 {key}: {item!r}")
        return Persona(
            id=str(item["id"]),
            name=str(item["name"]),
            title=str(item.get("title", "")),
            scene=str(item.get("scene", "")),
            accent=str(item.get("accent", "")),
            accent_short=str(item.get("accent_short", item.get("accent", ""))),
            flag=str(item.get("flag", "🌍")),
            city=str(item.get("city", "")),
            difficulty=str(item.get("difficulty", "medium")),
            tags=tuple(item.get("tags") or ()),
            opening_line=str(item.get("opening_line", "")),
            system_prompt=str(item["system_prompt"]),
            suggestion_hint=str(item.get("suggestion_hint", "")),
            traits=tuple(item.get("traits") or ()),
            voice=voice,
            voice_notes=str(item.get("voice_notes", "")),
        )

    # ---- 查询 ----
    @property
    def default_id(self) -> str:
        return self._default_id

    def get(self, persona_id: str | None) -> Persona:
        """拿角色；未知 id 回退到默认角色（服务端不因此中断对话）。

        顺序：内置 → 用户自定义 → 默认角色。
        """
        if persona_id:
            if persona_id in self._personas:
                return self._personas[persona_id]
            cp = self._find_custom(persona_id)
            if cp is not None:
                try:
                    return self._custom_to_persona(cp)
                except Exception as exc:  # noqa: BLE001
                    log.warning("自定义角色 %s 解析失败: %s", persona_id, exc)
            log.warning("unknown persona_id=%r, fallback to %s", persona_id, self._default_id)
        return self._personas[self._default_id]

    def has(self, persona_id: str) -> bool:
        return persona_id in self._personas or self._find_custom(persona_id) is not None

    def _find_custom(self, persona_id: str | None) -> Any:
        if not persona_id or self._custom is None:
            return None
        try:
            return self._custom.get_persona(persona_id)
        except Exception:  # noqa: BLE001
            return None

    def _custom_to_persona(self, cp: Any) -> Persona:
        """把用户自定义角色转成 Persona（与内置角色同构，下游无需感知差异）。"""
        from app.voice.accent_market import get_accent  # noqa: PLC0415

        acc = get_accent(cp.voice) or {}
        scene_text = ""
        scene = self._custom.get_scene(cp.scene_id) if (self._custom and cp.scene_id) else None
        if scene is not None:
            scene_text = scene.build_prompt_suffix()
        elif cp.scene_id in self._modes:
            scene_text = self._modes[cp.scene_id].description

        diff_map = {1: "easy", 2: "easy", 3: "medium", 4: "hard", 5: "hard"}
        return Persona(
            id=cp.id,
            name=cp.name,
            title=cp.job or "自定义角色",
            scene=scene_text or (scene.name if scene else ""),
            accent=acc.get("label", "自定义口音"),
            accent_short=acc.get("region_zh", ""),
            flag=acc.get("flag", "🌐"),
            city=cp.city,
            difficulty=diff_map.get(int(cp.difficulty or 3), "medium"),
            tags=tuple(t.strip() for t in (cp.traits or "").split(",") if t.strip()),
            opening_line=cp.opening_line,
            system_prompt=cp.build_system_prompt(scene_text),
            suggestion_hint="",
            traits=tuple(t.strip() for t in (cp.traits or "").split(",") if t.strip()),
            voice=VoiceConfig(
                ref_audio="",
                ref_text="",
                speed=1.0,
                style=acc.get("vibe", ""),
            ),
            voice_notes=acc.get("label", ""),
        )

    def all(self) -> list[Persona]:
        """内置角色 + 用户自定义角色。"""
        out = [self._personas[i] for i in self._order]
        if self._custom is not None:
            for cp in self._custom.list_personas():
                try:
                    out.append(self._custom_to_persona(cp))
                except Exception as exc:  # noqa: BLE001
                    log.warning("自定义角色 %s 转换失败: %s", cp.id, exc)
        return out

    def public_list(self) -> list[dict[str, Any]]:
        return [p.public() for p in self.all()]

    # ---- 口音覆盖（口音市场设置）----
    def voice_override(self, persona_id: str) -> str:
        """该角色被覆盖的声线（空 = 用内置默认）。"""
        return self._voice_overrides.get(persona_id, "")

    def set_voice_override(self, persona_id: str, voice: str) -> None:
        if self._custom is None:
            raise RuntimeError("自定义存储不可用")
        self._voice_overrides = self._custom.set_voice_override(persona_id, voice)

    def voice_overrides(self) -> dict[str, str]:
        return dict(self._voice_overrides)

    def modes(self) -> list[ConversationMode]:
        return list(self._modes.values())

    def mode(self, mode_id: str | None) -> ConversationMode | None:
        if not mode_id:
            return None
        return self._modes.get(mode_id)

    def mode_public_list(self) -> list[dict[str, Any]]:
        return [
            {"id": m.id, "name": m.name, "description": m.description}
            for m in self._modes.values()
        ]

    def system_prompt_for(self, persona_id: str | None, mode_id: str | None = None) -> str:
        p = self.get(persona_id)
        m = self.mode(mode_id)
        return p.build_system_prompt(m.prompt_suffix if m else "")


@lru_cache(maxsize=1)
def get_personas() -> PersonaLibrary:
    from app.config import get_settings

    return PersonaLibrary(get_settings().personas_path)


def reload_personas() -> PersonaLibrary:
    get_personas.cache_clear()
    return get_personas()
