"""V3 · 种子头像生成器（确定性 SVG，不依赖任何图片资源/网络）。

设计
----
- 输入一个 seed（这里就是 Persona 的 id）→ 输出固定的一张插画头像；
  同一个 seed 永远得到同一张脸，跨设备/跨重启一致。
- 全部用 sha256(seed) 派生的整数决定五官/发型/配色，因此**可复现、无状态**。
- 输出 SVG（约 2-4 KB），接口带 immutable 缓存，客户端 `<img>` 直接用。

风格接近 DiceBear personas：扁平插画、柔和背景、半身像，
和 V2 好友头像（前端 DiceBear）保持同一种「插画而非真人照片」的语感。
"""

from __future__ import annotations

import hashlib

#: 地域 → 肤色（与前端 avatar.ts 的 SKIN_BY_REGION 对齐）
SKIN_BY_REGION: dict[str, tuple[str, ...]] = {
    "east_asian": ("f5d0c5", "f0c8b4", "e8b89a"),
    "south_asian": ("c68642", "a8683a", "8d5524"),
    "latino": ("e0ac69", "c68642", "d4956a"),
    "black": ("8d5524", "6b4226", "a26a42"),
    "white": ("ffdbac", "f1c27d", "e0ac69"),
}
_ALL_SKIN = tuple(v for group in SKIN_BY_REGION.values() for v in group)

HAIR_COLORS = ("2c1b18", "3a2418", "4a312c", "5a3a24", "724133", "8a4b2a", "a55728", "b58143")
SHIRT_COLORS = (
    "3f6f57", "2f5d8a", "8a5a2f", "7a3f4f", "4b4b6a", "2f7a6a",
    "6a4a8a", "8a6a2f", "3a6a8a", "7a4a3a",
)
BACKGROUNDS = (
    ("e8f3ec", "cfe6d8"), ("eaf0f8", "d4e0f0"), ("f8efe6", "f0dcc8"),
    ("f3eaf3", "e4d4e8"), ("eef3e6", "dcead0"), ("f6eaea", "f0d6d6"),
    ("e8f1f4", "cfe4ea"), ("f4f0e8", "e8dfd0"),
)


def _stream(seed: str) -> list[int]:
    """把 seed 摊成稳定的伪随机整数流。"""
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


def _mouth(style: str) -> str:
    if style == "bigSmile":
        return (
            '<path d="M54 90q10 10 20 0" stroke="#a4544b" stroke-width="3" fill="none" stroke-linecap="round"/>'
        )
    if style == "smirk":
        return (
            '<path d="M55 91q9 6 19-2" stroke="#a4544b" stroke-width="2.8" fill="none" stroke-linecap="round"/>'
        )
    if style == "neutral":
        return '<path d="M56 91h16" stroke="#a4544b" stroke-width="2.6" stroke-linecap="round"/>'
    return (
        '<path d="M55 90q9 8 18 0" stroke="#a4544b" stroke-width="2.8" fill="none" stroke-linecap="round"/>'
    )


def avatar_svg(
    seed: str,
    *,
    gender: str = "",
    region: str = "",
    size: int = 128,
) -> str:
    """按 seed 生成确定性插画头像 SVG。"""
    p = _Pick(_stream(f"{seed}|{gender}|{region}"))
    skin = p(SKIN_BY_REGION.get(region, _ALL_SKIN) if region else _ALL_SKIN)
    hair_color = p(HAIR_COLORS)
    shirt = p(SHIRT_COLORS)
    bg = p(BACKGROUNDS)
    hair_style = p(("short", "side_part", "bob", "long", "bun", "curly", "ponytail", "buzz"))
    eyes = p(("open", "happy", "wink", "closed"))
    mouth = p(("smile", "bigSmile", "smirk", "neutral"))
    has_glasses = p.chance(16)
    has_beard = gender == "male" and p.chance(22)
    has_earrings = gender == "female" and p.chance(30)

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

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="{size}" height="{size}" '
        f'role="img" aria-label="AI avatar">'
        f'<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="#{bg[0]}"/><stop offset="1" stop-color="#{bg[1]}"/>'
        f"</linearGradient></defs>"
        f'<rect width="128" height="128" fill="url(#bg)"/>'
        # 肩膀/身体
        f'<path d="M18 128c0-19 20-29 46-29s46 10 46 29z" fill="#{shirt}"/>'
        f'<path d="M56 99h16v12c0 4-16 4-16 0z" fill="#{_shade(skin, 0.92)}"/>'
        # 脖子/头
        f'<rect x="56" y="92" width="16" height="12" rx="6" fill="{_shade(skin, 0.95)}"/>'
        f'<ellipse cx="64" cy="72" rx="27" ry="31" fill="#{skin}"/>'
        f'<ellipse cx="37" cy="74" rx="4.5" ry="6" fill="{_shade(skin, 0.96)}"/>'
        f'<ellipse cx="91" cy="74" rx="4.5" ry="6" fill="{_shade(skin, 0.96)}"/>'
        # 头发（后层 → 前层）
        f"{_hair(hair_style, hair_color, gender)}"
        # 眉毛
        f'<path d="M46 63q7-4 14 1" stroke="{_shade(hair_color)}" stroke-width="2.4" fill="none" stroke-linecap="round"/>'
        f'<path d="M68 64q7-5 14-1" stroke="{_shade(hair_color)}" stroke-width="2.4" fill="none" stroke-linecap="round"/>'
        # 鼻子
        f'<path d="M64 79v5" stroke="{_shade(skin, 0.85)}" stroke-width="2" stroke-linecap="round"/>'
        # 眼睛/嘴/配饰
        f"{_eyes(eyes)}{_mouth(mouth)}{glasses}{beard}{earrings}"
        # 腮红
        f'<ellipse cx="46" cy="84" rx="5" ry="3" fill="#e08a7d" opacity="0.22"/>'
        f'<ellipse cx="82" cy="84" rx="5" ry="3" fill="#e08a7d" opacity="0.22"/>'
        f"</svg>"
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
    """Persona 的头像种子：只用 id，保证「同一个人 = 同一张脸」。"""
    return str(persona.get("id") or persona.get("nickname") or "anonymous")


def persona_avatar_url(persona: dict) -> str:
    """Persona 头像 URL（种子化 SVG，带性别/地域以保持人种/性别一致）。"""
    seed = persona_avatar_seed(persona)
    gender = str(persona.get("gender") or "").strip().lower()
    region = avatar_region(str(persona.get("country") or ""))
    query = []
    if gender in ("male", "female"):
        query.append(f"g={gender}")
    if region:
        query.append(f"r={region}")
    suffix = ("?" + "&".join(query)) if query else ""
    return f"/api/v3/avatar/{seed}.svg{suffix}"


__all__ = ["avatar_region", "avatar_svg", "persona_avatar_seed", "persona_avatar_url"]
