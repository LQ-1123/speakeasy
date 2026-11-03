"""熟稔度状态机与好友裂变判定（v2）。

契约见 ``docs/V2_ARCHITECTURE.md`` §4.5：

- :func:`closeness_level` —— ``total_turns`` → 等级 1..5。阈值 10 / 30 / 80
  依次对应 Lv2 / Lv3 / Lv4；Lv5（“极熟”）起点见 :data:`LEVEL5_AT`。
  “Lv5 可继续”：到 Lv5 后轮数继续累计，但不再升级（仍是 5）。
- :func:`closeness_style` —— 注入 LLM prompt 的英文语气约束。
- :func:`should_refer` —— 好友裂变判定：``total_turns>=15`` 且等级 >=2
  且这个好友还没推荐过别人。
- :func:`referral_line` —— 好友 A 在聊天里介绍新朋友的一句话。

设计说明
--------
本模块是**纯函数**、零依赖、不碰数据库，方便单测与在 prompt 侧复用。

关于“未推荐过”：``friends`` 表没有持久化“已推荐”列，也不允许本模块改表结构，
所以 :func:`should_refer` 读取调用方传入的派生标记（好友 dict 里可带
``has_referred`` / ``already_referred`` / ``referral_done`` / ``referred_at``
之一，任一为真即视为已推荐）。API 层（router）可用
``friends.referred_by_friend_id == friend["id"]`` 现算后塞进 dict。
缺少这些字段时按“未推荐过”处理，即接口保持契约行为。
"""

from __future__ import annotations

from typing import Any

#: 最高等级
MAX_LEVEL = 5

#: Lv2 / Lv3 / Lv4 的轮数阈值（契约值，勿随意改）
CLOSENESS_THRESHOLDS: tuple[int, ...] = (10, 30, 80)

#: Lv5 起点。契约只定义了 10/30/80 三个阈值，Lv5 需要第四个；产品口径若调整只改这里。
LEVEL5_AT = 150

#: 裂变门槛
REFER_MIN_TURNS = 15
REFER_MIN_LEVEL = 2

#: 语气体征（英文、紧凑，直接拼进 system prompt）
_STYLES: dict[int, str] = {
    1: (
        "You are still getting to know them: polite, warm and curious. Keep it light and safe, "
        "ask simple questions about their day. No slang overload, no inside jokes, no teasing yet."
    ),
    2: (
        "You are friendly now: casual daily chat, light complaints about your own day, ordinary "
        "texting slang (gonna, lowkey, kinda) and gentle banter. Ask follow-ups but keep it easy."
    ),
    3: (
        "You are good friends: bring up things they told you before and ask how they went, tease "
        "them gently, use shortened casual English and the odd shared reference. Still warm, never mean."
    ),
    4: (
        "You are close: very casual and direct, inside jokes and callbacks, playful sarcasm, short "
        "messages. You can be blunt and affectionate and skip the polite small talk."
    ),
    5: (
        "You are basically family-level close: extremely relaxed, full of callbacks and running jokes, "
        "finishing each other's thoughts, zero formality. Closeness shows in the references, not long speeches."
    ),
}

#: 计算“已推荐过”时依次检查的派生字段
_REFER_DONE_KEYS: tuple[str, ...] = (
    "has_referred",
    "already_referred",
    "referral_done",
    "referred_at",
    "referred_friend_id",
)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def closeness_level(total_turns: int) -> int:
    """轮数 → 熟稔度等级（1..5）。

    >>> closeness_level(0), closeness_level(10), closeness_level(30), closeness_level(80)
    (1, 2, 3, 4)
    """
    turns = max(0, _as_int(total_turns, 0))
    level = 1
    for threshold in CLOSENESS_THRESHOLDS:
        if turns >= threshold:
            level += 1
        else:
            break
    if level >= 4 and turns >= LEVEL5_AT:
        level = 5
    return min(level, MAX_LEVEL)


def closeness_style(level: int) -> str:
    """等级 → 英文语气约束（越界自动夹到 1..5）。"""
    lv = _as_int(level, 1)
    lv = min(max(lv, 1), MAX_LEVEL)
    return _STYLES[lv]


def should_refer(friend: dict) -> bool:
    """该好友是否适合发起“裂变”（介绍新朋友给用户）。

    条件：``total_turns >= 15``、熟稔度 >= 2、尚未推荐过、且好友是 active 状态。
    """
    if not isinstance(friend, dict) or not friend:
        return False
    status = str(friend.get("status") or "active").strip().lower()
    if status and status != "active":
        return False

    turns = _as_int(friend.get("total_turns"), 0)
    if turns < REFER_MIN_TURNS:
        return False

    level = _as_int(friend.get("closeness_level"), -1)
    if level < 0:
        level = closeness_level(turns)
    if level < REFER_MIN_LEVEL:
        return False

    for key in _REFER_DONE_KEYS:
        if friend.get(key):
            return False
    return True


#: 介绍句式；``{new}`` 新朋友名，``{mid}`` 口语填充
_REFERRAL_TEMPLATES: tuple[str, ...] = (
    "Hey! You should meet {new} — {mid}I've got a feeling you two would hit it off. Want me to introduce you?",
    "Quick one: {new} is a mate of mine, {mid}and I reckon you'd get on really well. Say hi from me?",
    "Random thought — {new} just joined and needs a chat buddy, and you came to mind. Mind saying hi?",
    "Oh, {new} and I were just talking about you. You two should definitely chat — want an intro?",
)

_MIDS: tuple[str, ...] = ("", "honestly, ", "trust me, ")


def referral_line(friend: dict, new_friend_name: str) -> str:
    """好友 A 在聊天里自然介绍新朋友的一句话（英文，2 句以内）。"""
    data = friend if isinstance(friend, dict) else {}
    new_name = str(new_friend_name or "").strip() or "a new friend"
    seed = f"{data.get('name') or ''}|{new_name}"
    key = sum(ord(ch) for ch in seed)
    template_index = key % len(_REFERRAL_TEMPLATES)
    template = _REFERRAL_TEMPLATES[template_index]
    mid = _MIDS[key % len(_MIDS)] if template_index < 2 else ""
    line = template.format(new=new_name, mid=mid)
    # 紧凑化：压掉双空格、保证以英文标点结尾
    line = " ".join(line.split())
    return line


__all__ = [
    "CLOSENESS_THRESHOLDS",
    "LEVEL5_AT",
    "MAX_LEVEL",
    "REFER_MIN_LEVEL",
    "REFER_MIN_TURNS",
    "closeness_level",
    "closeness_style",
    "referral_line",
    "should_refer",
]
