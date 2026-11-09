"""用户自定义角色与场景的持久化。

设计
----
内置角色在 ``personas.json`` 里（只读）。用户自定义的存 ``data/custom.json``，
两者合并后对外提供。这样：

* 升级不覆盖用户数据
* 内置角色可以随时改进
* 用户能删/改自己创建的，但动不了内置的

数据结构
--------
CustomPersona
    结构化表单字段（名字/城市/职业/性格/口音/开场白）→ 自动生成 system_prompt
CustomScene
    场景模板 + 用户细节 → 生成 scene 描述与 mode 提示
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: 只允许安全字符作为自定义 id
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,41}$")
#: 自定义 id 前缀，避免与内置角色冲突
CUSTOM_PREFIX = "u_"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class CustomPersona:
    """用户创建的角色（结构化表单）。"""

    id: str
    name: str
    city: str = ""
    job: str = ""
    #: Edge 降级声线（口音系统已下线；主音色由 Fish 音色库统一分配）
    voice: str = "en-GB-RyanNeural"
    #: 性别：male / female（只用于展示与默认声线筛选）
    gender: str = "male"
    #: 性格关键词，逗号分隔
    traits: str = ""
    #: 说话风格
    style: str = ""
    #: 开场白
    opening_line: str = ""
    #: 难度 1-5
    difficulty: int = 3
    #: 场景 id（引用 CustomScene 或内置 mode）
    scene_id: str = ""
    #: 用户额外想强调的设定
    extra: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_public(self) -> dict[str, Any]:
        # 阶段四：口音系统已下线。voice 字段只作为 Edge 降级声线保留，
        # 不再从口音市场反查 label/flag（AI 角色主音色由 Fish 音色库统一分配）。
        return {
            "id": self.id,
            "name": self.name,
            "city": self.city,
            "job": self.job,
            "voice": self.voice,
            "gender": self.gender,
            "traits": self.traits,
            "style": self.style,
            "opening_line": self.opening_line,
            "difficulty": self.difficulty,
            "scene_id": self.scene_id,
            "extra": self.extra,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "custom": True,
            # 兼容旧前端字段：口音已下线，恒为空
            "accent_label": "",
            "flag": "",
            "accent_kind": "",
            "voice_mode": "fish",
        }

    def build_system_prompt(self, scene_text: str = "") -> str:
        """从结构化字段生成 system_prompt。

        原则
        ----
        * 口音用**行为描述**（语速、用词、节奏），**绝不**用假拼写
          （写 "Roj" 冒充印度口音是错的，模型会念成真的 Roj）
        * 给出明确的人设边界与教学目标
        * 保持简洁，避免抢走对话
        """
        traits = [t.strip() for t in self.traits.split(",") if t.strip()]
        traits_txt = "、".join(traits) if traits else "友善、自然"

        lines = [
            f"你是 {self.name}。",
        ]
        if self.city or self.job:
            where = f"住在{self.city}" if self.city else ""
            what = f"是一名{self.job}" if self.job else ""
            lines.append(f"你{where}{what}。".replace("你是", "你").lstrip("你"))
        lines.append(f"性格：{traits_txt}。")
        if self.style:
            lines.append(f"说话风格：{self.style}")
        if scene_text:
            lines.append(f"当前场景：{scene_text}")
        if self.extra:
            lines.append(f"额外设定：{self.extra}")

        lines.append(
            "\n【口语练习规则】\n"
            "- 全程用英语对话。每次回复 1–3 句，像真人闲聊，不要长篇大论。\n"
            "- 如果对方表达有语法或用词问题，**用「重述」的方式自然示范正确说法**，"
            "不要直接指出错误，也不要解释语法。\n"
            "- 你会主动追问、给反应（惊讶、认同、好奇），让对话持续下去。\n"
            "- 不要扮演老师，不要写旁白或动作描述（如 *smiles*）。"
        )
        return "\n".join(x for x in lines if x)


@dataclass
class CustomScene:
    """用户创建的场景。"""

    id: str
    name: str
    #: 场景模板 key（见 SCENE_TEMPLATES），空 = 完全自定义
    template: str = ""
    place: str = ""
    goal: str = ""
    #: 用户补充的细节
    details: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "template": self.template,
            "place": self.place,
            "goal": self.goal,
            "details": self.details,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "custom": True,
            "prompt_suffix": self.build_prompt_suffix(),
        }

    def build_prompt_suffix(self) -> str:
        """给 LLM 的场景提示。"""
        bits = []
        tpl = SCENE_TEMPLATES.get(self.template, {})
        if tpl:
            bits.append(tpl.get("prompt", ""))
        if self.place:
            bits.append(f"地点：{self.place}。")
        if self.goal:
            bits.append(f"这次对话的目标：{self.goal}。")
        if self.details:
            bits.append(f"背景细节：{self.details}")
        return " ".join(b for b in bits if b).strip()


#: 场景模板（给用户选，也能直接被内置模式复用）
SCENE_TEMPLATES: dict[str, dict[str, str]] = {
    "cafe": {
        "name": "咖啡店点单",
        "place": "一家繁忙的独立咖啡店",
        "goal": "点一杯饮品，可能加点吃的",
        "prompt": "你们在咖啡店。对方是店员，正在柜台后面。",
    },
    "airport": {
        "name": "机场值机",
        "place": "机场值机柜台",
        "goal": "办理登机、托运行李、问登机口",
        "prompt": "你们在机场值机柜台。对方是地勤人员。",
    },
    "interview": {
        "name": "求职面试",
        "place": "公司会议室",
        "goal": "介绍自己、回答面试问题、反问公司",
        "prompt": "这是一场求职面试。对方是面试官，会问你的经历和动机。",
    },
    "hospital": {
        "name": "看医生",
        "place": "诊所诊室",
        "goal": "描述症状、回答病史、听懂医嘱",
        "prompt": "你们在诊所。对方是医生，会询问你的症状和病史。",
    },
    "hotel": {
        "name": "酒店入住",
        "place": "酒店前台",
        "goal": "办入住、问设施、反映房间问题",
        "prompt": "你们在酒店前台。对方是前台接待。",
    },
    "landlord": {
        "name": "租房看房",
        "place": "待出租的公寓",
        "goal": "看房、问租金和条款、提要求",
        "prompt": "你正在看一套出租公寓。对方是房东或中介。",
    },
    "phone": {
        "name": "客服电话",
        "place": "电话中",
        "goal": "反映问题、要求解决、确认后续",
        "prompt": "你们在通电话。对方是客服，会先确认你的身份和问题。",
    },
    "networking": {
        "name": "社交场合",
        "place": "行业交流活动",
        "goal": "介绍自己、了解对方、交换联系方式",
        "prompt": "你们在一个行业交流活动上刚认识。对方也是参加者。",
    },
    "custom": {
        "name": "完全自定义",
        "place": "",
        "goal": "",
        "prompt": "",
    },
}


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
class CustomStore:
    """自定义角色与场景的 JSON 存储（原子写入）。"""

    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(str(data_dir), "custom.json")
        self._data: dict[str, Any] = self._load()

    # ---- 读写 ----
    def _load(self) -> dict[str, Any]:
        if not os.path.isfile(self.path):
            return {"personas": [], "scenes": [], "voice_overrides": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                return {"personas": [], "scenes": []}
            d.setdefault("personas", [])
            d.setdefault("scenes", [])
            d.setdefault("voice_overrides", {})
            return d
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 custom.json 失败（将忽略）: %s", exc)
            return {"personas": [], "scenes": []}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---- 角色 ----
    def list_personas(self) -> list[CustomPersona]:
        out = []
        for d in self._data.get("personas", []):
            try:
                out.append(CustomPersona(**d))
            except Exception as exc:  # noqa: BLE001
                log.debug("跳过损坏的自定义角色: %s", exc)
        return out

    def get_persona(self, pid: str) -> CustomPersona | None:
        for p in self.list_personas():
            if p.id == pid:
                return p
        return None

    def upsert_persona(self, payload: dict[str, Any]) -> CustomPersona:
        pid = str(payload.get("id") or "").strip()
        if not pid:
            pid = CUSTOM_PREFIX + uuid.uuid4().hex[:8]
        else:
            pid = pid if pid.startswith(CUSTOM_PREFIX) else CUSTOM_PREFIX + pid
            if not _ID_RE.match(pid):
                raise ValueError("id 只能包含小写字母、数字、下划线、连字符")

        existing = self.get_persona(pid)
        now = time.time()
        gender = str(payload.get("gender") or "male")[:10]
        # 阶段四：不再按口音选声线。voice 只作为 Edge 降级声线，
        # 用户没给就按性别挑一个稳定默认值（真正的 AI 音色由 Fish 分配）。
        voice = str(payload.get("voice") or "").strip()[:64]
        if not voice:
            voice = (
                "en-US-AvaMultilingualNeural"
                if gender.strip().lower() == "female"
                else "en-GB-RyanNeural"
            )
        persona = CustomPersona(
            id=pid,
            name=str(payload.get("name") or "未命名角色")[:40],
            city=str(payload.get("city") or "")[:40],
            job=str(payload.get("job") or "")[:60],
            voice=voice,
            gender=gender,
            traits=str(payload.get("traits") or "")[:200],
            style=str(payload.get("style") or "")[:300],
            opening_line=str(payload.get("opening_line") or "")[:300],
            difficulty=max(1, min(5, int(payload.get("difficulty") or 3))),
            scene_id=str(payload.get("scene_id") or "")[:42],
            extra=str(payload.get("extra") or "")[:500],
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        rows = [p for p in self._data["personas"] if p.get("id") != pid]
        rows.append(asdict(persona))
        self._data["personas"] = rows
        self._save()
        return persona

    def delete_persona(self, pid: str) -> bool:
        before = len(self._data["personas"])
        self._data["personas"] = [
            p for p in self._data["personas"] if p.get("id") != pid
        ]
        if len(self._data["personas"]) != before:
            self._save()
            return True
        return False

    # ---- 场景 ----
    def list_scenes(self) -> list[CustomScene]:
        out = []
        for d in self._data.get("scenes", []):
            try:
                out.append(CustomScene(**d))
            except Exception as exc:  # noqa: BLE001
                log.debug("跳过损坏的自定义场景: %s", exc)
        return out

    def get_scene(self, sid: str) -> CustomScene | None:
        for s in self.list_scenes():
            if s.id == sid:
                return s
        return None

    def upsert_scene(self, payload: dict[str, Any]) -> CustomScene:
        sid = str(payload.get("id") or "").strip()
        if not sid:
            sid = CUSTOM_PREFIX + uuid.uuid4().hex[:8]
        existing = self.get_scene(sid)
        now = time.time()
        scene = CustomScene(
            id=sid,
            name=str(payload.get("name") or "未命名场景")[:40],
            template=str(payload.get("template") or "")[:40],
            place=str(payload.get("place") or "")[:120],
            goal=str(payload.get("goal") or "")[:200],
            details=str(payload.get("details") or "")[:600],
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        rows = [s for s in self._data["scenes"] if s.get("id") != sid]
        rows.append(asdict(scene))
        self._data["scenes"] = rows
        self._save()
        return scene

    def delete_scene(self, sid: str) -> bool:
        before = len(self._data["scenes"])
        self._data["scenes"] = [
            s for s in self._data["scenes"] if s.get("id") != sid
        ]
        if len(self._data["scenes"]) != before:
            self._save()
            return True
        return False

    # ---- 场景模板 ----
    @staticmethod
    def templates() -> list[dict[str, str]]:
        return [
            {"key": k, **v} for k, v in SCENE_TEMPLATES.items()
        ]

    # ---- 口音覆盖（给内置角色换声线）----
    def load_voice_overrides(self) -> dict[str, str]:
        v = self._data.get("voice_overrides") or {}
        if not isinstance(v, dict):
            return {}
        return {str(k): str(val) for k, val in v.items() if isinstance(val, str)}

    def set_voice_override(self, persona_id: str, voice: str) -> dict[str, str]:
        """给角色换声线。空 voice = 取消覆盖（回到内置默认）。"""
        ov = self.load_voice_overrides()
        pid = str(persona_id or "").strip()
        if not pid:
            raise ValueError("persona_id 不能为空")
        if voice:
            ov[pid] = str(voice).strip()
        else:
            ov.pop(pid, None)
        self._data["voice_overrides"] = ov
        self._save()
        return ov

    def clear_voice_overrides(self) -> None:
        self._data["voice_overrides"] = {}
        self._save()

    def voice_overrides(self) -> dict[str, str]:
        return self.load_voice_overrides()
