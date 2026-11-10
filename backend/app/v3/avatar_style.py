"""Avataaars 头像部件（LLM 侧写 + 确定性兜底）。

为什么要 LLM 侧写
-----------------
Avataaars 有 34 种发型、9 种服装、7 种配饰、12 种表情…… 靠 hash 随机虽然稳定，
但常常和人物设定对不上（给厨师随机发色/给程序员戴墨镜）。
让 LLM 读一遍人物资料（国籍、年龄、职业、性格、兴趣），选出一组「像这个人」的部件，
一次性存进 ``personas.avatar_options``，之后所有客户端都用同一组部件渲染。

兜底
----
没有 LLM（mock/未配置）或 LLM 输出不合法时，用确定性映射：
地域 → 肤色，性别 → 发型池，年龄 → 灰发/眼镜概率，其余用 hash 选。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

STYLE_ID = "avataaars"

#: Avataaars 允许的枚举（与 DiceBear @dicebear/avataaars 的 schema 一致）
TOP = (
    "hat", "hijab", "turban", "winterHat1", "winterHat02", "winterHat03", "winterHat04",
    "bob", "bun", "curly", "curvy", "dreads", "frida", "fro", "froBand",
    "longButNotTooLong", "miaWallace", "shavedSides", "straight02", "straight01",
    "straightAndStrand", "dreads01", "dreads02", "frizzle", "shaggy", "shaggyMullet",
    "shortCurly", "shortFlat", "shortRound", "shortWaved", "sides", "theCaesar",
    "theCaesarAndSidePart", "bigHair",
)
TOP_FEMALE = (
    "bob", "bun", "curly", "curvy", "bigHair", "longButNotTooLong", "miaWallace",
    "straight01", "straight02", "straightAndStrand", "frida",
)
TOP_MALE = (
    "shortCurly", "shortFlat", "shortRound", "shortWaved", "theCaesar",
    "theCaesarAndSidePart", "sides", "shaggy", "shaggyMullet", "frizzle",
)
HAIR_COLOR = ("a55728", "2c1b18", "b58143", "d6b370", "724133", "4a312c", "ecdcbf", "e8e1e1")
HAIR_COLOR_NATURAL = ("a55728", "2c1b18", "b58143", "d6b370", "724133", "4a312c")
HAIR_COLOR_GRAY = ("e8e1e1", "ecdcbf")
FACIAL_HAIR = ("beardLight", "beardMajestic", "beardMedium", "moustacheFancy", "moustacheMagnum")
ACCESSORIES = ("prescription01", "prescription02", "round", "wayfarers", "sunglasses")
ACCESSORIES_COLOR = ("262e33", "65c9ff", "5199e4", "25557c", "929598", "3c4f5c")
CLOTHING = (
    "blazerAndShirt", "blazerAndSweater", "collarAndSweater", "graphicShirt",
    "hoodie", "overall", "shirtCrewNeck", "shirtScoopNeck", "shirtVNeck",
)
CLOTHING_GRAPHIC = ("bat", "bear", "cumbia", "deer", "diamond", "hola", "pizza", "resist", "skull", "skullOutline")
EYES = ("closed", "default", "happy", "side", "squint", "surprised", "wink", "hearts")
EYEBROWS = ("angryNatural", "defaultNatural", "flatNatural", "frownNatural", "raisedExcitedNatural", "sadConcernedNatural", "upDownNatural")
MOUTH = ("concerned", "default", "disbelief", "grimace", "serious", "smile", "twinkle")
SKIN = ("614335", "d08b5b", "ae5d29", "edb98a", "ffdbb4", "fd9841", "f8d25c")
CLOTHES_COLOR = ("262e33", "65c9ff", "5199e4", "25557c", "3c4f5c", "e6e6e6", "929598", "b1e2ff", "a7ffc4", "ffdeb7", "ffafb9", "ffffb1", "ff5c5c")
BACKGROUND = ("e8f3ec", "eaf0f8", "f8efe6", "f3eaf3", "eef3e6", "f6eaea", "e8f1f4", "f4f0e8")

SKIN_BY_REGION: dict[str, tuple[str, ...]] = {
    "east_asian": ("ffdbb4", "edb98a", "f8d25c"),
    "south_asian": ("ae5d29", "d08b5b"),
    "latino": ("d08b5b", "ae5d29", "edb98a"),
    "black": ("614335",),
    "white": ("ffdbb4", "f8d25c", "edb98a"),
}


def _region_of(country: str) -> str:
    c = (country or "").lower()
    if any(k in c for k in ("china", "japan", "korea", "taiwan", "hong kong")):
        return "east_asian"
    if any(k in c for k in ("india", "pakistan", "bangladesh", "sri lanka", "nepal")):
        return "south_asian"
    if any(k in c for k in ("brazil", "mexico", "argentina", "colombia", "chile", "peru")):
        return "latino"
    if any(k in c for k in ("nigeria", "kenya", "ghana", "south africa", "ethiopia")):
        return "black"
    return "white"


def _pick(nums: list[int], seq, i: int):
    return seq[nums[i % len(nums)] % len(seq)]


def _hash_nums(*parts: Any) -> list[int]:
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(("speakeasy-avataaars:" + raw).encode("utf-8")).digest()
    return [digest[i] * 256 + digest[i + 1] for i in range(0, len(digest), 2)]


def default_options(persona: dict) -> dict:
    """确定性映射：地域→肤色、性别→发型池、年龄→灰发/眼镜，其余 hash 选。"""
    seed = str(persona.get("id") or persona.get("nickname") or "anonymous")
    gender = str(persona.get("gender") or "").lower()
    region = _region_of(str(persona.get("country") or ""))
    try:
        age = int(persona.get("age") or 28)
    except (TypeError, ValueError):
        age = 28
    band = "young" if age < 25 else "adult" if age < 33 else "mature"
    nums = _hash_nums(seed, gender, region, age)

    tops = TOP_FEMALE if gender == "female" else TOP_MALE if gender == "male" else TOP
    hair_colors = HAIR_COLOR_GRAY + HAIR_COLOR_NATURAL if band == "mature" else HAIR_COLOR_NATURAL

    options: dict[str, Any] = {
        "top": [_pick(nums, tops, 0)],
        "topProbability": 100,
        "hairColor": [_pick(nums, hair_colors, 1)],
        "skinColor": [_pick(nums, SKIN_BY_REGION.get(region, SKIN), 2)],
        "clothing": [_pick(nums, CLOTHING, 3)],
        "clothesColor": [_pick(nums, CLOTHES_COLOR, 4)],
        "eyes": [_pick(nums, EYES, 5)],
        "eyebrows": [_pick(nums, EYEBROWS, 6)],
        "mouth": [_pick(nums, MOUTH, 7)],
        "backgroundColor": [_pick(nums, BACKGROUND, 8)],
        "accessoriesProbability": {"young": 8, "adult": 15, "mature": 40}[band],
        "accessories": list(ACCESSORIES),
        "accessoriesColor": list(ACCESSORIES_COLOR),
        "facialHairProbability": 0,
    }
    if gender == "male" and band != "young":
        options["facialHair"] = [_pick(nums, FACIAL_HAIR, 9)]
        options["facialHairProbability"] = 100 if band == "mature" else 55
        options["facialHairColor"] = [_pick(nums, hair_colors, 10)]
    return options


#: 用于校验 LLM 输出的枚举白名单
ALLOWED: dict[str, tuple[str, ...]] = {
    "top": TOP,
    "hairColor": HAIR_COLOR,
    "skinColor": SKIN,
    "clothing": CLOTHING,
    "clothingGraphic": CLOTHING_GRAPHIC,
    "clothesColor": CLOTHES_COLOR,
    "eyes": EYES,
    "eyebrows": EYEBROWS,
    "mouth": MOUTH,
    "accessories": ACCESSORIES,
    "accessoriesColor": ACCESSORIES_COLOR,
    "facialHair": FACIAL_HAIR,
    "facialHairColor": HAIR_COLOR,
    "backgroundColor": BACKGROUND,
}

_PROMPT = """You are an avatar art director. Given an AI persona's profile, pick the
Avataaars components that best match this person's look and vibe.

Rules:
- Choose values ONLY from the allowed lists.
- Skin color follows their ethnicity/region; hair style and clothing should fit
  their gender, age and job; eyes/mouth express their personality.
- A 23-year-old student should not look like a 60-year-old professor; a chef in
  Bologna should not wear a hoodie.
- Return JSON only:
{
 "skinColor": ["..."],
 "top": ["..."],
 "hairColor": ["..."],
 "clothing": ["..."],
 "clothesColor": ["..."],
 "eyes": ["..."],
 "eyebrows": ["..."],
 "mouth": ["..."],
 "facialHairProbability": 0,
 "accessoriesProbability": 0,
 "accessoriesColor": ["..."],
 "backgroundColor": ["..."]
}
"""


def _validate(data: dict, fallback: dict) -> dict:
    out = dict(fallback)
    for key, allowed in ALLOWED.items():
        value = data.get(key)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        cleaned = [str(v) for v in values if str(v) in allowed]
        if cleaned:
            out[key] = cleaned
    for prob_key in ("facialHairProbability", "accessoriesProbability", "topProbability"):
        value = data.get(prob_key)
        if isinstance(value, (int, float)) and 0 <= value <= 100:
            out[prob_key] = int(value)
    return out


async def llm_options(llm: Any, persona: dict) -> dict:
    """LLM 侧写：读人物资料 → 选一组 Avataaars 部件。失败回落到确定性映射。"""
    fallback = default_options(persona)
    if llm is None or getattr(llm, "uses_mock", False):
        return fallback
    try:
        from app.v2.llm_json import complete_json_safe  # noqa: PLC0415

        profile = {
            "nickname": persona.get("nickname"),
            "gender": persona.get("gender"),
            "age": persona.get("age"),
            "country": persona.get("country"),
            "city": persona.get("city"),
            "occupation": persona.get("occupation"),
            "bio": persona.get("bio"),
            "personality": persona.get("personality"),
            "interests": persona.get("interests"),
        }
        user = json.dumps(profile, ensure_ascii=False)
        data = await complete_json_safe(
            llm,
            [{"role": "system", "content": _PROMPT + "\nAllowed values:\n" + json.dumps(
                {k: list(v) for k, v in ALLOWED.items()}, ensure_ascii=False)},
             {"role": "user", "content": user}],
            schema_hint='{"skinColor":[],"top":[],"hairColor":[],"clothing":[],"mouth":[]}',
            fallback=lambda: {},
            temperature=0.4,
            max_tokens=500,
        )
        if not isinstance(data, dict) or not data:
            return fallback
        merged = _validate(data, fallback)
        log.info("头像侧写完成 %s: top=%s", persona.get("id"), merged.get("top"))
        return merged
    except Exception as exc:  # noqa: BLE001
        log.debug("LLM 头像侧写失败 %s: %s", persona.get("id"), exc)
        return fallback


async def ensure_avatar_options(
    db: Any, personas: list[dict], llm: Any, *, only_missing: bool = True
) -> int:
    """为缺少 avatar_options 的人物生成部件并写库（幂等）。返回更新人数。"""
    updated = 0
    for persona in personas:
        existing = str(persona.get("avatar_options") or "").strip()
        if only_missing and existing:
            # 已存在但可能含 style=circle（历史数据），清洗一次
            if '"style"' in existing:
                try:
                    old = json.loads(existing)
                except (ValueError, TypeError):
                    old = {}
                fixed = sanitize_options(old) if isinstance(old, dict) else {}
                with db._lock, db.conn:  # noqa: SLF001
                    db.conn.execute(
                        "UPDATE personas SET avatar_options=? WHERE id=?",
                        (json.dumps(fixed, ensure_ascii=False), str(persona["id"])),
                    )
                updated += 1
            continue
        options = await llm_options(llm, persona)
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE personas SET avatar_style=?, avatar_options=? WHERE id=?",
                (STYLE_ID, json.dumps(options, ensure_ascii=False), str(persona["id"])),
            )
        updated += 1
    return updated


def sanitize_options(options: dict) -> dict:
    """清掉会让渲染变空的选项（DiceBear avataaars 的 style=circle 会输出空画布）。"""
    cleaned = dict(options)
    cleaned.pop("style", None)
    return cleaned


def persona_avatar_meta(persona: dict) -> dict:
    """给 API 用的头像元数据（style + 部件），**所有接口必须走这里**。

    为什么要在读取时归一化
    ----------------------
    ``personas.avatar_options`` 是后补的列：种子人物、老库、刚生成的 LLM 人物都可能还没有值。
    如果接口对缺失值直接返回 ``{}``，客户端就会各自回落到 personas 插画 / 服务端 SVG，
    同一个人在不同页面变成两种画风。这里对缺失值用确定性 ``default_options(persona)``
    补齐（不调用 LLM、不写库、同一人物永远同一组部件），让所有 payload 的
    ``avatar_style + avatar_options`` 一致；而好友行由 ``schema.sync_persona_friends``
    按同一份 meta 同步，保证 friend 与 persona 渲染出同一张脸。
    """
    style = str(persona.get("avatar_style") or "").strip()
    raw = persona.get("avatar_options")
    options: dict = {}
    if isinstance(raw, dict):
        options = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                options = parsed
        except (ValueError, TypeError):
            options = {}
    if not options:
        # 确定性兜底：不依赖 LLM、不写库，老数据也能立刻和客户端 Avataaars 对上
        options = default_options(persona)
    cleaned = sanitize_options(options)
    if not cleaned:
        return {"avatar_style": "", "avatar_options": {}}
    return {"avatar_style": style or STYLE_ID, "avatar_options": cleaned}


__all__ = [
    "ACCESSORIES",
    "ALLOWED",
    "CLOTHING",
    "EYES",
    "MOUTH",
    "SKIN_BY_REGION",
    "STYLE_ID",
    "TOP",
    "TOP_FEMALE",
    "TOP_MALE",
    "default_options",
    "ensure_avatar_options",
    "llm_options",
    "persona_avatar_meta",
]
