"""时区与当地时间/作息状态。

PRD §3.4：组装 Prompt 时按好友 IANA 时区计算当地时间，并映射生活状态标签。
本模块是纯函数，不依赖网络。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_WEEKDAYS_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

#: (起, 止) 小时区间 → 状态；跨午夜区间需拆开。
_PERIODS_ZH: list[tuple[int, int, dict]] = [
    (5, 8, {"period": "early_morning", "icon": "🌅", "activity_zh": "刚起床，在弄早餐",
            "activity_en": "just woke up and is making breakfast", "awake": True}),
    (8, 11, {"period": "morning", "icon": "☕", "activity_zh": "在通勤/开始上午的活儿",
             "activity_en": "commuting or starting the morning", "awake": True}),
    (11, 13, {"period": "noon", "icon": "🍜", "activity_zh": "准备吃午饭",
              "activity_en": "getting lunch", "awake": True}),
    (13, 17, {"period": "afternoon", "icon": "💻", "activity_zh": "下午在忙工作/上课",
              "activity_en": "busy with work or classes this afternoon", "awake": True}),
    (17, 19, {"period": "evening", "icon": "🏃", "activity_zh": "傍晚收工/在回家路上",
              "activity_en": "wrapping up and heading home", "awake": True}),
    (19, 22, {"period": "night", "icon": "🍝", "activity_zh": "晚饭后在家放松",
              "activity_en": "relaxing at home after dinner", "awake": True}),
    (22, 24, {"period": "late_night", "icon": "📱", "activity_zh": "睡前刷手机",
              "activity_en": "scrolling on the phone before bed", "awake": True}),
    (0, 2, {"period": "late_night", "icon": "🌙", "activity_zh": "深夜还醒着",
            "activity_en": "still up late at night", "awake": True}),
    (2, 5, {"period": "sleeping", "icon": "😴", "activity_zh": "在睡觉（回消息会慢）",
            "activity_en": "sleeping, so replies may be slow", "awake": False}),
]


def local_now(tz_name: str, now: datetime | None = None) -> datetime:
    """把 ``now``（默认当前）转到好友时区。非法时区回退 UTC。"""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    base = now or datetime.now(tz)
    if base.tzinfo is None:
        base = base.replace(tzinfo=tz)
    return base.astimezone(tz)


def _period_for(hour: int) -> dict:
    for start, end, info in _PERIODS_ZH:
        if start <= hour < end:
            return info
    return _PERIODS_ZH[1][2]


def local_state(tz_name: str, now: datetime | None = None) -> dict:
    """好友的当地时间 + 作息状态（供 UI 与 prompt 使用）。"""
    dt = local_now(tz_name, now)
    info = _period_for(dt.hour)
    return {
        "local_time": dt.strftime("%H:%M"),
        "local_date": dt.strftime("%Y-%m-%d"),
        "weekday": _WEEKDAYS_ZH[dt.weekday()],
        "period": info["period"],
        "activity": info["activity_zh"],
        "activity_en": info["activity_en"],
        "icon": info["icon"],
        "is_awake": info["awake"],
        "timezone": tz_name or "UTC",
        "hour": dt.hour,
    }


def local_date_str(tz_name: str, now: datetime | None = None) -> str:
    """好友时区的日期（YYYY-MM-DD），用于朋友圈幂等。"""
    return local_now(tz_name, now).strftime("%Y-%m-%d")


def moment_local_label(tz_name: str, city: str, now: datetime | None = None) -> str:
    """朋友圈展示用：``08:45 AM · London``。"""
    dt = local_now(tz_name, now)
    return f"{dt.strftime('%I:%M %p').lstrip('0')} · {city or tz_name}"


def format_for_prompt(state: dict) -> str:
    """把 local_state 转成一行英文 prompt 片段。"""
    return (
        f"Your local time is {state['local_time']} ({state['weekday']}, {state['period']}); "
        f"right now you are {state['activity_en']}."
    )
