"""V3 · 种子头像生成器（确定性 SVG，不依赖任何图片资源/网络）。

设计
----
- 输入 seed（Persona id）+ 可选属性（性别 gender / 地域 region / 年龄 age）
  → 输出固定的一张插画头像；同样的输入永远得到同一张脸。
- 全部用 sha256(派生串) 的整数决定五官/发型/配色，因此可复现、无状态。
- 输出 SVG（约 2–5 KB），接口带 immutable 缓存，客户端 `<img>` 直接用。

三个可区分维度（受插画风格限制，只能做「可识别的视觉线索」）
------------------------------------------------------------
1. **人种/地域**：肤色板 + 发色/发质（东亚直发、非洲卷发、南亚黑发、拉美深棕、白人棕/金/红发）
2. **性别**：发型集合、胡须概率、耳环、唇色
3. **年龄**：青年/成年/中年三档 → 灰白发概率、皱纹、眼镜概率、发型偏好

注意：插画只能表达「刻板但友好的视觉线索」，不代表真实人群；AI 人像生成才能做到接近真人的区分。
"""

from __future__ import annotations

import hashlib
import json

#: 地域 → 肤色（与前端 avatar.ts 的 SKIN_BY_REGION 对齐）
SKIN_BY_REGION: dict[str, tuple[str, ...]] = {
    "east_asian": ("f5d0c5", "f0c8b4", "e8b89a", "f7d9c4"),
    "south_asian": ("c68642", "a8683a", "8d5524", "b5713f"),
    "latino": ("e0ac69", "c68642", "d4956a", "b97a4a"),
    "black": ("8d5524", "6b4226", "a26a42", "7a4a2b"),
    "white": ("ffdbac", "f1c27d", "e0ac69", "f7d6b5"),
}
_ALL_SKIN = tuple(v for group in SKIN_BY_REGION.values() for v in group)

#: 地域 → 发色（东亚/南亚/非洲偏黑，白人含棕/金/红）
HAIR_BY_REGION: dict[str, tuple[str, ...]] = {
    "east_asian": ("1c1410", "2c1b18", "3a2418", "241a14"),
    "south_asian": ("1c1410", "2c1b18", "3a2418"),
    "latino": ("1c1410", "2c1b18", "3a2418", "4a312c"),
    "black": ("1c1410", "2c1b18", "33231b"),
    "white": ("2c1b18", "4a312c", "724133", "a55728", "b58143", "8a3b1f"),
}
_ALL_HAIR = tuple(dict.fromkeys(v for group in HAIR_BY_REGION.values() for v in group))

#: 灰白发（年龄线索用）
GRAY_HAIR = ("9b9b9b", "b9b9b9", "d8d8d8", "cfcfcf")

SHIRT_COLORS = (
    "3f6f57", "2f5d8a", "8a5a2f", "7a3f4f", "4b4b6a", "2f7a6a",
    "6a4a8a", "8a6a2f", "3a6a8a", "7a4a3a",
)
BACKGROUNDS = (
    ("e8f3ec", "cfe6d8"), ("eaf0f8", "d4e0f0"), ("f8efe6", "f0dcc8"),
    ("f3eaf3", "e4d4e8"), ("eef3e6", "dcead0"), ("f6eaea", "f0d6d6"),
    ("e8f1f4", "cfe4ea"), ("f4f0e8", "e8dfd0"),
)


#: 年龄 → 档位（<25 青年 / 25-32 成年 / >=33 中年）
def age_band(age: int | str | None) -> str:
    try:
        value = int(age)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "adult"
    if value < 25:
        return "young"
    if value < 33:
        return "adult"
    return "mature"


def _stream(seed: str) -> list[int]:
    """把输入摊成稳定的伪随机整数流。"""
    digest = hashlib.sha256(("speakeasy-avatar:" + (seed or "anonymous")).encode("utf-8")).digest()
    out: list[int] = []
    for i in range(0, len(digest), 2):
        out.append(digest[i] * 256 + digest[i + 1])
    return out


class _Pick:
    def __init__(self, nums: list[int]) -> None:
        self.nums = nums
        self.i = 0

    def __call__(self, seq):
        value = self.nums[self.i % len(self.nums)]
        self.i += 1
        return seq[value % len(seq)]

    def chance(self, percent: int) -> bool:
        value = self.nums[self.i % len(self.nums)]
        self.i += 1
        return (value % 100) < percent


def _hair(style: str, color: str, gender: str) -> str:
    """发型图层（画在头部之后、五官之前）。"""
    c = f"#{color}"
    if style == "buzz":
        return (
            f'<path d="M34 52c0-16 13-26 30-26s30 10 30 26c0 3-3 4-5 2-6-8-15-12-25-12'
            f's-19 4-25 12c-2 2-5 1-5-2z" fill="{c}" opacity="0.92"/>'
        )
    if style == "short":
        return (
            f'<path d="M32 60c-2-20 12-34 32-34s34 14 32 34c-1 6-4 7-7 4-3-12-13-20-25-20'
            f's-22 8-25 20c-3 3-6 2-7-4z" fill="{c}"/>'
        )
    if style == "side_part":
        return (
            f'<path d="M31 58c0-19 14-32 33-32 16 0 28 9 31 24 1 5-2 8-5 5-6-10-16-15-28-15'
            f'-9 0-17 3-24 9-4 3-7 2-7-1z" fill="{c}"/>'
        )
    if style == "bob":
        return (
            f'<path d="M28 60c0-21 16-35 36-35s36 14 36 35c0 12-5 18-9 18-2 0-3-2-3-6'
            f'0-14-10-24-24-24s-24 10-24 24c0 4-1 6-3 6-4 0-9-6-9-18z" fill="{c}"/>'
        )
    if style == "long":
        return (
            f'<path d="M27 62c0-22 16-37 37-37s37 15 37 37c0 16-3 26-6 30-2 3-6 2-6-2'
            f'1-19 1-32-2-40-4-10-12-17-23-17s-19 7-23 17c-3 8-3 21-2 40 0 4-4 5-6 2'
            f'-3-4-6-14-6-30z" fill="{c}"/>'
        )
    if style == "bun":
        return (
            f'<circle cx="64" cy="24" r="9" fill="{c}"/>'
            f'<path d="M33 56c0-17 14-29 31-29s31 12 31 29c0 3-3 5-6 2-5-9-14-14-25-14'
            f's-20 5-25 14c-3 3-6 1-6-2z" fill="{c}"/>'
        )
    if style == "curly":
        return (
            f'<g fill="{c}"><circle cx="40" cy="44" r="12"/><circle cx="64" cy="34" r="14"/>'
            f'<circle cx="88" cy="44" r="12"/><circle cx="30" cy="58" r="9"/>'
            f'<circle cx="98" cy="58" r="9"/></g>'
        )
    if style == "afro":
        return (
            f'<g fill="{c}"><circle cx="64" cy="30" r="16"/><circle cx="42" cy="38" r="13"/>'
            f'<circle cx="86" cy="38" r="13"/><circle cx="30" cy="52" r="10"/>'
            f'<circle cx="98" cy="52" r="10"/></g>'
        )
    # ponytail
    return (
        f'<path d="M32 58c0-18 14-31 32-31s32 13 32 31c0 4-3 6-6 3-5-10-15-16-26-16'
        f's-21 6-26 16c-3 3-6 1-6-3z" fill="{c}"/>'
        f'<path d="M96 50c8 4 12 12 12 22s-5 16-10 16c-4 0-6-3-4-6 3-5 4-10 4-16 0-6-2-11-4-14z"'
        f' fill="{c}" opacity="0.95"/>'
    )


def _eyes(style: str) -> str:
    if style == "closed":
        return (
            '<path d="M47 74q6 5 12 0" stroke="#3a2b24" stroke-width="2.6" fill="none" stroke-linecap="round"/>'
            '<path d="M69 74q6 5 12 0" stroke="#3a2b24" stroke-width="2.6" fill="none" stroke-linecap="round"/>'
        )
    if style == "wink":
        return (
            '<ellipse cx="53" cy="73" rx="3.2" ry="4.2" fill="#3a2b24"/>'
            '<path d="M70 73q6 4 11 0" stroke="#3a2b24" stroke-width="2.6" fill="none" stroke-linecap="round"/>'
        )
    if style == "happy":
        return (
            '<path d="M47 75q6-6 12 0" stroke="#3a2b24" stroke-width="2.8" fill="none" stroke-linecap="round"/>'
            '<path d="M69 75q6-6 12 0" stroke="#3a2b24" stroke-width="2.8" fill="none" stroke-linecap="round"/>'
        )
    return (
        '<ellipse cx="53" cy="73" rx="3.4" ry="4.4" fill="#3a2b24"/>'
        '<ellipse cx="75" cy="73" rx="3.4" ry="4.4" fill="#3a2b24"/>'
    )


def _mouth(style: str, lip_color: str = "#a4544b") -> str:
    if style == "bigSmile":
        return f'<path d="M54 90q10 10 20 0" stroke="{lip_color}" stroke-width="3" fill="none" stroke-linecap="round"/>'
    if style == "smirk":
        return f'<path d="M55 91q9 6 19-2" stroke="{lip_color}" stroke-width="2.8" fill="none" stroke-linecap="round"/>'
    if style == "neutral":
        return f'<path d="M56 91h16" stroke="{lip_color}" stroke-width="2.6" stroke-linecap="round"/>'
    return f'<path d="M55 90q9 8 18 0" stroke="{lip_color}" stroke-width="2.8" fill="none" stroke-linecap="round"/>'


def _wrinkles(band: str, skin: str) -> str:
    """年龄线索：眼角细纹 + 法令纹。"""
    if band == "young":
        return ""
    color = _shade(skin, 0.82)
    strength = 0.9 if band == "mature" else 0.5
    parts = [
        f'<path d="M46 78q4 2 8 0" stroke="{color}" stroke-width="1.2" fill="none" opacity="{strength}" stroke-linecap="round"/>',
        f'<path d="M74 78q4 2 8 0" stroke="{color}" stroke-width="1.2" fill="none" opacity="{strength}" stroke-linecap="round"/>',
    ]
    if band == "mature":
        parts.append(
            f'<path d="M52 89q4 3 12 0" stroke="{color}" stroke-width="1.1" fill="none" opacity="0.55" stroke-linecap="round"/>'
        )
        parts.append(
            f'<path d="M58 62q-3 8 0 12" stroke="{color}" stroke-width="1" fill="none" opacity="0.35" stroke-linecap="round"/>'
            f'<path d="M70 62q3 8 0 12" stroke="{color}" stroke-width="1" fill="none" opacity="0.35" stroke-linecap="round"/>'
        )
    return "".join(parts)


def avatar_svg(
    seed: str,
    *,
    gender: str = "",
    region: str = "",
    age: int | str | None = None,
    size: int = 128,
) -> str:
    """按 seed + 性别/地域/年龄生成确定性插画头像 SVG。"""
    gender = (gender or "").strip().lower()
    region = (region or "").strip().lower()
    band = age_band(age)

    # 年龄也进入种子：同一个人不同年龄档会得到不同的细节
    p = _Pick(_stream(f"{seed}|{gender}|{region}|{band}|{age}"))

    skin = p(SKIN_BY_REGION.get(region, _ALL_SKIN) if region else _ALL_SKIN)
    hair_pool = HAIR_BY_REGION.get(region, _ALL_HAIR)
    gray_chance = {"young": 0, "adult": 8, "mature": 38}[band]
    hair_color = p(GRAY_HAIR) if p.chance(gray_chance) else p(hair_pool)
    shirt = p(SHIRT_COLORS)
    bg = p(BACKGROUNDS)

    if region == "black":
        style_pool = ("afro", "curly", "buzz", "short")
    elif region == "east_asian":
        style_pool = ("short", "side_part", "bob", "long", "bun")
    elif region == "south_asian":
        style_pool = ("short", "side_part", "long", "bun", "bob")
    else:
        style_pool = ("short", "side_part", "bob", "long", "bun", "curly", "ponytail", "buzz")

    if gender == "female":
        female_pool = tuple(s for s in style_pool if s not in ("buzz", "afro")) or style_pool
        if band == "mature":
            female_pool = tuple(s for s in female_pool if s in ("bob", "bun", "short", "side_part")) or female_pool
        hair_style = p(female_pool)
    elif gender == "male":
        male_pool = tuple(s for s in style_pool if s not in ("long", "bob", "ponytail", "bun"))
        hair_style = p(male_pool or style_pool)
    else:
        hair_style = p(style_pool)

    eyes = p(("open", "happy", "wink", "closed"))
    mouth = p(("smile", "bigSmile", "smirk", "neutral"))
    lip_color = "#b0564f" if gender == "female" else "#a4544b"

    has_glasses = p.chance({"young": 10, "adult": 16, "mature": 32}[band])
    has_beard = gender == "male" and p.chance({"young": 16, "adult": 24, "mature": 20}[band])
    has_earrings = gender == "female" and p.chance({"young": 34, "adult": 26, "mature": 22}[band])
    has_blush = band != "mature"

    glasses = (
        '<g stroke="#2f2f33" stroke-width="2" fill="none">'
        '<rect x="43" y="67" width="22" height="15" rx="6"/>'
        '<rect x="67" y="67" width="22" height="15" rx="6"/>'
        '<path d="M65 73h2M43 71l-6-3M89 71l6-3"/></g>'
        if has_glasses
        else ""
    )
    beard = (
        f'<path d="M44 84c0 14 9 22 20 22s20-8 20-22c-5 6-12 9-20 9s-15-3-20-9z" fill="{_shade(hair_color)}" opacity="0.9"/>'
        if has_beard
        else ""
    )
    earrings = (
        f'<circle cx="34" cy="84" r="2.6" fill="#d8b55a"/><circle cx="94" cy="84" r="2.6" fill="#d8b55a"/>'
        if has_earrings
        else ""
    )
    wrinkles = _wrinkles(band, skin)

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="{size}" height="{size}" '
        f'role="img" aria-label="AI avatar">'
        f'<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="#{bg[0]}"/><stop offset="1" stop-color="#{bg[1]}"/>'
        f"</linearGradient></defs>"
        f'<rect width="128" height="128" fill="url(#bg)"/>'
        f'<path d="M18 128c0-19 20-29 46-29s46 10 46 29z" fill="#{shirt}"/>'
        f'<rect x="56" y="92" width="16" height="12" rx="6" fill="{_shade(skin, 0.95)}"/>'
        f'<ellipse cx="64" cy="72" rx="27" ry="31" fill="#{skin}"/>'
        f'<ellipse cx="37" cy="74" rx="4.5" ry="6" fill="{_shade(skin, 0.96)}"/>'
        f'<ellipse cx="91" cy="74" rx="4.5" ry="6" fill="{_shade(skin, 0.96)}"/>'
        f"{_hair(hair_style, hair_color, gender)}"
        f'<path d="M46 63q7-4 14 1" stroke="{_shade(hair_color)}" stroke-width="{2.4 if band == "young" else 2.8}" fill="none" stroke-linecap="round"/>'
        f'<path d="M68 64q7-5 14-1" stroke="{_shade(hair_color)}" stroke-width="{2.4 if band == "young" else 2.8}" fill="none" stroke-linecap="round"/>'
        f'<path d="M64 79v5" stroke="{_shade(skin, 0.85)}" stroke-width="2" stroke-linecap="round"/>'
        f"{_eyes(eyes)}{_mouth(mouth, lip_color)}{wrinkles}{glasses}{beard}{earrings}"
        + (
            f'<ellipse cx="46" cy="84" rx="5" ry="3" fill="#e08a7d" opacity="0.22"/>'
            f'<ellipse cx="82" cy="84" rx="5" ry="3" fill="#e08a7d" opacity="0.22"/>'
            if has_blush
            else ""
        )
        + f"</svg>"
    )


def _shade(hex_color: str, factor: float = 0.8) -> str:
    """把颜色调暗一点（用于头发描边/肤色阴影）。"""
    h = (hex_color or "000000").lstrip("#")
    if len(h) != 6:
        return "#" + (h or "000000")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return "#" + "".join(f"{max(0, int(c * factor)):02x}" for c in (r, g, b))


#: 国家 → 地域（与前端 avatarRegionOf 保持一致）
def avatar_region(country: str) -> str:
    c = (country or "").lower()
    if any(k in c for k in ("china", "japan", "korea", "taiwan", "hong kong")):
        return "east_asian"
    if any(k in c for k in ("india", "pakistan", "bangladesh", "sri lanka", "nepal")):
        return "south_asian"
    if any(k in c for k in ("brazil", "mexico", "argentina", "colombia", "chile", "peru")):
        return "latino"
    if any(k in c for k in ("nigeria", "kenya", "ghana", "south africa", "ethiopia")):
        return "black"
    if any(
        k in c
        for k in (
            "italy", "spain", "portugal", "greece", "france", "germany", "netherlands",
            "sweden", "norway", "denmark", "finland", "poland", "ireland",
            "united kingdom", "england", "scotland", "wales", "united states",
            "canada", "australia", "new zealand",
        )
    ):
        return "white"
    return ""


def persona_avatar_seed(persona: dict) -> str:
    """Persona 头像 URL 的种子：只用 id，保证「同一个人 = 同一张脸」。

    注意：新客户端渲染用的是接口下发的 ``avatar_seed``（与好友行同步，
    见 ``avatar_style.persona_avatar_meta`` / ``schema.sync_persona_friends``）；
    这里生成的是服务端 SVG 兜底 URL 的 seed，两者指向同一身份。该 URL 只给
    老客户端/非 JS 场景用，新客户端优先 avatar_style + avatar_options。
    """
    return str(persona.get("id") or persona.get("nickname") or "anonymous")


def _avatar_render_tag(persona: dict) -> str:
    """兜底 SVG URL 的版本号：头像部件变化 → URL 变化 → immutable 缓存立刻失效。

    「头像更新慢」还有一层老客户端原因：``/api/v3/avatar/*.svg`` 带
    ``Cache-Control: immutable``，avatar_options 改了但 URL 不变，浏览器会一直用旧图。
    把渲染输入（客户端 seed / style / options）摘要进 URL，换头像即换 URL。
    """
    client_seed = str(persona.get("avatar_seed") or "").strip()
    style = str(persona.get("avatar_style") or "").strip()
    raw = persona.get("avatar_options")
    if isinstance(raw, str):
        options = raw
    else:
        try:
            options = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            options = str(raw or "")
    payload = "|".join((persona_avatar_seed(persona), client_seed, style, options))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def persona_avatar_url(persona: dict) -> str:
    """Persona 头像 URL —— **只做老客户端/非 JS 场景的兜底**。

    新客户端一律用 ``avatar_style + avatar_options`` 离线渲染 Avataaars
    （见 ``avatar_style.persona_avatar_meta``），不会再出现「页面一个画风、
    聊天另一个画风」；这个 SVG 地址画风与 Avataaars 不同，所以不能当主头像来源。
    URL 里带 ``v=`` 渲染版本：头像部件一变 URL 就变，老客户端的图片缓存随之失效。
    性别/地域/年龄参数与客户端渲染使用同一份 persona 字段。
    """
    seed = persona_avatar_seed(persona)
    gender = str(persona.get("gender") or "").strip().lower()
    region = avatar_region(str(persona.get("country") or ""))
    query = [f"v={_avatar_render_tag(persona)}"]
    if gender in ("male", "female"):
        query.append(f"g={gender}")
    if region:
        query.append(f"r={region}")
    try:
        age = int(persona.get("age") or 0)
    except (TypeError, ValueError):
        age = 0
    if age:
        query.append(f"a={age}")
    return f"/api/v3/avatar/{seed}.svg?" + "&".join(query)


__all__ = ["age_band", "avatar_region", "avatar_svg", "persona_avatar_seed", "persona_avatar_url"]
