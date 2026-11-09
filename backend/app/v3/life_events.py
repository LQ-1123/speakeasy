"""Persona 生活事件引擎：**规则管作息，LLM 管叙事**。

设计动机
========
升级前的 Persona 只在用户聊天时"存在"：没有自己的日程、没有今天发生过什么，
所以它只能对用户做出反应，永远无法主动说"我今天下午刚把项目交掉了"。
本模块给每个人物一条**连续的生活线**，分成两层：

1. **规则层（零 LLM、确定性）**：按职业/时区生成作息块（工作、上课、吃饭、通勤），
   负责"人物在正常的日常里"。它便宜、可预测、永不失败，是生活线的地基。
2. **叙事层（LLM，每人每天 ≤1 次调用）**：只生成**有叙事价值**的事件
   （完成项目 / 和朋友争论 / 周末旅行 / 遇到麻烦 / 获得成就）。
   LLM 不可用、超时、返回垃圾、被环境变量关掉 → 直接返回 ``[]``，
   上层用规则事件兜底。**生活线不会因为模型挂掉而消失。**

"计划不等于事实"（本模块最重要的不变量）
==========================================
事件有生命周期 ``planned → ongoing → completed``（另有 ``cancelled``）：

- ``planned`` 只是**打算**，绝不能当已经发生的事写进 prompt 或动态；
- 只有 ``completed`` 才能被当成"过去真实发生过"的谈资；
- ``cancelled`` 一律从事实检索中剔除。

因此对外接口是分开的：:func:`relevant_events` 只返回 completed，
:func:`upcoming_events` 只返回 planned，:func:`event_fact_block` 对 planned
强制写成 ``"PLAN (not happened yet): ..."``。任何调用方都拿不到"计划当事实"的文本。

幂等与容错
==========
- 每个事件有全局唯一的 ``dedupe_key``（见 §5.3 规范），重复写入返回已有 id；
- 整天的批量生成用 ``v3_meta['life_events_day:<day>']`` 做标记，同一天只跑一次；
- 所有 SQL 都容错：老库没有新表时记一条日志并降级为"没有这段信息"，
  **绝不把异常抛进聊天 / 后台主循环**。

时间约定
========
- 作息块是**人物本地时间**（``personas.timezone``）；
- 落库统一 UTC ISO（``YYYY-MM-DDTHH:MM:SS+00:00``），与 ``daily_world.iso`` 一致；
- ``day`` 字段存人物本地的 ``YYYY-MM-DD``（幂等/按天检索用）。

规模控制
========
完整作息（6-8 个块）仍然保存在 ``persona_schedules`` 里，供 :func:`current_activity`
推断"此刻在做什么"；但**落库的事件每天每人只取最重要的 3-4 条**
（工作/学习 > 吃饭 > 其他，见 :func:`_rule_events_cap`）。
原因是 ``persona_events`` 会长期累积：12 人 × 8 条 × 365 天 ≈ 3.5 万行/年，
而没有信息流价值的是"通勤""晚上在家"这类填充块。
需要完整还原时设 ``SE_LIFE_RULE_EVENTS_MAX=99``。

情绪接线
========
``refresh_statuses`` 里，事件**真正迁移到 completed 的那一次**会对事件主体与参与者
调用 :func:`app.v3.emotion.apply_event` 并写回 ``personas.emotion_state``。
幂等由"UPDATE 带旧状态条件 + rowcount==1"保证：同一事件不会被重复叠加情绪。
计划/取消的事件不改变情绪（还没发生的事不会让人累）。

关系演进
========
同一次迁移里，取**参与集合 = {事件主体 persona_id} ∪ participants**（去重、去空、去脏 id），
集合 ≥ 2 时对每一对人物调用 :func:`bump_relationship`：``shared_events += 1``、
``last_shared_at`` 更新，并在共同经历达到 :data:`CLOSENESS_BUMPS` (2/5/9/14) 时把
``closeness`` +1（封顶 5、**单调不减**，不会把既有手写关系降级）。关系不存在不凭空造。
主体必须并入集合：真实数据里 LLM 叙事事件的 ``participants`` 不含主体
（``p_adrian + ['p_callum','p_mei']`` = 三人一起去 pub quiz），否则"和谁一起经历"
永远沉淀不下来。``event_posts.comment_probability`` 直接读 ``closeness``，
所以"一起经历多了"会自然表现为"更常互相评论"。

关系张力（``tension`` 0..5，内部状态，不直接展示数字）
----------------------------------------------------
同一对人物还会随事件类型有摩擦：完成 ``conflict`` → ``tension +1`` 并记
``last_conflict_at``；完成 ``social / relationship / achievement / entertainment``
→ ``tension -1``（和解，不为负）；其他类型不动。张力≥3 的熟人会在叙事 prompt 里
被标成 ``(currently a bit tense with you)``，并要求不要写成愉快的共同活动参与者。
``event_posts`` 用 tension 做评论惩罚，所以"吵过架 → 不爱互动 → 和好 → 又互动"
是同一套状态驱动的。``raise_tension`` / ``ease_tension`` 同样只在完成迁移那次执行一次（幂等）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import sqlite3
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Iterable, Sequence

from app.logging_utils import get_logger
from app.v3.daily_world import day_key, iso, now_local, parse_iso

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# 常量（契约，跨模块共用）
# --------------------------------------------------------------------------- #
EVENT_TYPES: tuple[str, ...] = (
    "daily_routine", "work", "study", "meal", "entertainment", "travel",
    "social", "relationship", "achievement", "conflict", "personal_milestone",
    "unexpected_event",
)
EVENT_STATUSES: tuple[str, ...] = ("planned", "ongoing", "completed", "cancelled")
OPEN_STATUSES: tuple[str, ...] = ("planned", "ongoing")
DONE_STATUSES: tuple[str, ...] = ("completed",)

#: 规则引擎会产出的类型（作息 = 日常，不制造戏剧性）
RULE_EVENT_TYPES: tuple[str, ...] = ("work", "study", "meal", "daily_routine")
#: LLM 只允许产出"有叙事价值"的类型（普通作息交给规则层，省调用也避免重复）
NARRATIVE_EVENT_TYPES: tuple[str, ...] = (
    "achievement", "conflict", "social", "entertainment", "travel",
    "relationship", "personal_milestone", "unexpected_event",
)

#: 人物关系类型
RELATION_TYPES: tuple[str, ...] = ("friend", "colleague")

#: 关系亲密度区间：1..5（与 ``event_posts._relationship_closeness`` 的 1..5 语义一致）
MAX_CLOSENESS = 5
DEFAULT_CLOSENESS = 2
#: 共同经历达到这些次数时 closeness +1（**单调不减**、封顶 5）
CLOSENESS_BUMPS: tuple[int, ...] = (2, 5, 9, 14)

#: 关系摩擦度区间：0..5（**内部状态**，只影响评论概率/文案，不直接展示给用户）
MAX_TENSION = 5
DEFAULT_TENSION = 0
#: 完成迁移时按事件类型调整张力（只改这两个集合里的类型）
TENSION_RAISE_TYPES: tuple[str, ...] = ("conflict",)
TENSION_EASE_TYPES: tuple[str, ...] = (
    "social", "relationship", "achievement", "entertainment",
)
#: 叙事 prompt 里标记「有点别扭」的张力阈值
TENSE_PROMPT_THRESHOLD = 3

#: 整批生成的幂等标记前缀（v3_meta）
META_PREFIX = "life_events_day:"

#: 超期仍在 ongoing 的宽限（end 为空/脏数据时的兜底）
STALE_GRACE_HOURS = 6

#: 相关性检索回看窗口（天）
RELEVANT_LOOKBACK_DAYS = 45

#: 规则事件每天每人的默认上限（完整作息仍在 persona_schedules 里）
RULE_EVENTS_PER_DAY_DEFAULT = 4

#: 每天每人最多几条 LLM 叙事事件（0 = 关闭叙事，省调用）
NARRATIVE_EVENTS_PER_DAY_DEFAULT = 2

DEFAULT_TZ = "UTC"

#: 作息块 kind → 事件类型（契约规定规则事件只用这 4 种）
_KIND_TO_EVENT_TYPE: dict[str, str] = {
    "work": "work",
    "study": "study",
    "meal": "meal",
    "commute": "daily_routine",
    "personal": "daily_routine",
    "social": "daily_routine",
    "errand": "daily_routine",
    "exercise": "daily_routine",
    "rest": "daily_routine",
}


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _zone(name: str) -> Any:
    """时区名 → tzinfo；非法时区回退 UTC（作息表不能因为时区脏数据全丢）。"""
    text = str(name or "").strip() or DEFAULT_TZ
    if text.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return ZoneInfo(text)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError / ValueError / OSError
        return timezone.utc


def _as_utc(value: Any) -> datetime | None:
    """把 dict/datetime/ISO 字符串统一成带时区的 UTC datetime。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    parsed = parse_iso(text)
    if parsed is not None:
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now_utc() -> datetime:
    return _as_utc(now_local()) or datetime.now(timezone.utc)


def _hhmm_to_min(value: str) -> int | None:
    """``"09:30"`` → 570；非法返回 None（用于读库/外部输入）。"""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*$", str(value or ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _min_to_hhmm(total: int) -> str:
    total = max(0, min(24 * 60 - 1, int(total)))
    return f"{total // 60:02d}:{total % 60:02d}"


def _hhmm_time(value: str) -> dtime:
    total = _hhmm_to_min(value)
    if total is None:
        return dtime(hour=0, minute=0)
    return dtime(hour=total // 60, minute=total % 60)


def _slug(text: str, *, limit: int = 40) -> str:
    """标题 → dedupe_key 用的 slug（只保留 ascii 小写字母数字，其它折叠成 ``-``）。"""
    value = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (value[:limit].strip("-") or "event")


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, type(default)):
        return raw
    if raw is None or raw == "":
        return default
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _as_dict(value: Any) -> dict:
    """``metadata`` 之类字段：既接受已解析的 dict，也接受 DB 里的 JSON 字符串。

    ``persona_events.metadata`` 在库里是 TEXT(JSON)，直接 ``SELECT *`` 出来的行
    里是 ``'{}'`` 字符串；而跨模块传递的事件 dict 里是 dict。两种都要能用，
    否则外部调用方把 DB 行直接传给 :func:`event_fact_block` 就会崩。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: Any) -> list:
    """``participants`` / ``related_personas``：list 或 DB 里的 JSON 字符串都接受。"""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, list):
            return parsed
        if parsed is None:
            return []
        return [parsed]
    return []


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def life_llm_enabled() -> bool:
    """``SE_LIFE_LLM`` 开关（默认开启）。关掉后只跑规则事件，零 LLM 调用。"""
    raw = str(os.getenv("SE_LIFE_LLM", "1")).strip().lower()
    return raw not in ("0", "false", "off", "no")


# --------------------------------------------------------------------------- #
# 职业原型 → 作息
# --------------------------------------------------------------------------- #
#: 职业关键词 → 原型（顺序有意义：先匹配更具体的）
_ARCHETYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("student", ("student", "pupil", "undergraduate", "student", "学生", "大学生", "研究生")),
    ("freelance", (
        "freelance", "freelancer", "self-employed", "self employed", "independent",
        "自由职业", "独立", "contractor",
    )),
    ("service", (
        "barista", "chef", "cook", "cafe", "café", "coffee shop", "shop", "store",
        "waiter", "waitress", "bartender", "bar ", "restaurant", "bakery", "florist",
        "咖啡", "店主", "服务", "店员", "餐厅",
    )),
    ("teacher", ("teacher", "professor", "lecturer", "tutor", "教师", "老师", "教授")),
    ("creative", (
        "designer", "design", "illustrator", "photographer", "architect", "artist",
        "creative", "filmmaker", "musician", "writer", "设计", "插画", "摄影师", "建筑师",
    )),
)

#: 原型作息表：weekday / weekend = [(start, end, kind, label)]，
#: work_hours 的 days 用 ISO 星期（1=周一 … 7=周日）。
#: 差异必须"肉眼可见"：学生有课、设计师有项目/客户、自由职业不规律、服务业早晚班。
_SCHEDULES: dict[str, dict[str, Any]] = {
    "office": {
        "weekday": [
            ("07:30", "08:00", "personal", "Slow start, coffee and the news"),
            ("08:00", "08:45", "commute", "Commuting to the office"),
            ("09:00", "12:30", "work", "Deep work on the project"),
            ("12:30", "13:30", "meal", "Lunch break"),
            ("13:30", "17:30", "work", "Meetings and afternoon work"),
            ("17:30", "18:15", "commute", "Commuting home"),
            ("18:45", "19:30", "meal", "Dinner"),
            ("20:00", "21:30", "personal", "Evening at home"),
        ],
        "weekend": [
            ("09:00", "10:00", "meal", "Slow weekend breakfast"),
            ("10:30", "12:30", "errand", "Errands around the neighbourhood"),
            ("13:00", "14:00", "meal", "Lunch"),
            ("15:00", "17:30", "personal", "Long walk and a coffee"),
            ("19:00", "20:00", "meal", "Dinner"),
            ("20:30", "22:00", "personal", "Evening at home"),
        ],
        "work_hours": {"start": "09:00", "end": "17:30", "days": [1, 2, 3, 4, 5]},
    },
    "student": {
        "weekday": [
            ("07:45", "08:30", "commute", "Commuting to campus"),
            ("09:00", "11:30", "study", "Morning lectures"),
            ("11:45", "12:30", "meal", "Lunch on campus"),
            ("13:00", "15:30", "study", "Library study session"),
            ("15:45", "17:15", "study", "Seminar and group project"),
            ("18:00", "18:45", "meal", "Dinner with classmates"),
            ("19:30", "21:30", "social", "Student club meeting"),
        ],
        "weekend": [
            ("10:00", "11:30", "personal", "Sleeping in, then laundry"),
            ("12:00", "13:00", "meal", "Late brunch"),
            ("14:00", "17:00", "study", "Assignment deadline pressure"),
            ("18:30", "19:30", "meal", "Dinner"),
            ("20:30", "22:30", "personal", "Gaming with friends online"),
        ],
        "work_hours": {"start": "09:00", "end": "17:15", "days": [1, 2, 3, 4, 5]},
    },
    "creative": {
        "weekday": [
            ("09:30", "11:00", "work", "Studio time on the client project"),
            ("11:15", "12:15", "work", "Client call and feedback"),
            ("12:30", "13:30", "meal", "Lunch"),
            ("14:00", "18:00", "work", "Design revisions"),
            ("18:30", "19:30", "meal", "Dinner"),
            ("20:30", "22:30", "personal", "Sketchbook and music at home"),
        ],
        "weekend": [
            ("11:00", "13:00", "personal", "Personal project"),
            ("13:30", "14:30", "meal", "Lunch"),
            ("15:00", "17:00", "personal", "Gallery or a long walk"),
            ("19:00", "20:00", "meal", "Dinner"),
        ],
        "work_hours": {"start": "09:30", "end": "18:00", "days": [1, 2, 3, 4, 5]},
    },
    "freelance": {
        # 不规律：上午才开工、下午办事、夜里补活
        "weekday": [
            ("10:30", "13:00", "work", "Focused work block"),
            ("13:00", "14:00", "meal", "Late lunch"),
            ("14:30", "16:30", "work", "Client work and admin"),
            ("16:30", "17:15", "errand", "Errand run"),
            ("21:00", "23:00", "work", "Late work session"),
            ("23:15", "23:45", "personal", "Winding down"),
        ],
        "weekend": [
            ("11:30", "13:30", "work", "Catching up on a deadline"),
            ("14:00", "15:00", "meal", "Late lunch"),
            ("16:00", "18:00", "personal", "Whatever the day gives"),
            ("19:30", "20:30", "meal", "Dinner"),
        ],
        "work_hours": {"start": "10:30", "end": "23:00", "days": [1, 2, 3, 4, 5, 6]},
    },
    "service": {
        # 早晚班：开早店，周末照常营业
        "weekday": [
            ("06:00", "07:00", "personal", "Opening the café"),
            ("07:00", "11:00", "work", "Morning rush at the counter"),
            ("11:00", "11:30", "meal", "Staff breakfast break"),
            ("11:30", "15:00", "work", "Serving the lunch crowd"),
            ("15:00", "16:00", "meal", "Late lunch"),
            ("16:00", "17:00", "errand", "Restocking and errands"),
            ("19:30", "20:30", "meal", "Dinner"),
            ("21:00", "22:30", "personal", "Home, finally off my feet"),
        ],
        "weekend": [
            ("06:00", "07:00", "personal", "Opening the café"),
            ("07:00", "12:00", "work", "Weekend brunch crowd"),
            ("12:00", "13:00", "meal", "Quick lunch"),
            ("13:00", "17:00", "work", "Afternoon service"),
            ("17:30", "18:30", "meal", "Dinner"),
            ("20:00", "22:00", "personal", "Evening in"),
        ],
        "work_hours": {"start": "06:00", "end": "17:00", "days": [1, 2, 3, 4, 5, 6, 7]},
    },
    "teacher": {
        "weekday": [
            ("07:30", "08:15", "commute", "Commuting to school"),
            ("08:30", "12:00", "work", "Morning classes"),
            ("12:00", "13:00", "meal", "Lunch break"),
            ("13:30", "16:00", "work", "Afternoon lessons"),
            ("16:00", "17:00", "work", "Marking papers"),
            ("17:30", "18:00", "commute", "Commuting home"),
            ("19:00", "20:00", "meal", "Dinner"),
            ("20:30", "22:00", "personal", "Evening at home"),
        ],
        "weekend": [
            ("09:30", "10:30", "meal", "Slow weekend breakfast"),
            ("11:00", "13:00", "errand", "Groceries and laundry"),
            ("13:30", "14:30", "meal", "Lunch"),
            ("15:30", "17:30", "personal", "Long walk"),
            ("19:00", "20:00", "meal", "Dinner"),
        ],
        "work_hours": {"start": "08:30", "end": "17:00", "days": [1, 2, 3, 4, 5]},
    },
}


def _archetype(persona: dict) -> str:
    """职业 → 作息原型。识别不了就按上班族处理（最保守、最像普通人）。"""
    persona = persona if isinstance(persona, dict) else {}
    text = " ".join(
        str(persona.get(key) or "").lower()
        for key in ("occupation", "job", "archetype_id")
    )
    for name, keywords in _ARCHETYPE_KEYWORDS:
        if any(kw in text for kw in keywords):
            return name
    return "office"


def _schedule_jitter(persona_id: str, archetype: str) -> int:
    """同职业的不同人物也略有差异（±15 分钟），确定性、可复现。"""
    digest = hashlib.sha256(f"{persona_id}:{archetype}:schedule".encode("utf-8")).digest()
    return (digest[0] % 3 - 1) * 15


def _shift_block(start: str, end: str, shift: int) -> tuple[str, str]:
    start_min = _hhmm_to_min(start) or 0
    end_min = _hhmm_to_min(end) or 0
    new_start = max(5, min(24 * 60 - 10, start_min + shift))
    new_end = max(new_start + 5, min(24 * 60 - 1, end_min + shift))
    return _min_to_hhmm(new_start), _min_to_hhmm(new_end)


def build_schedule(persona: dict) -> dict:
    """按职业/年龄/性格/时区**确定性**生成作息。

    返回 ``{"persona_id","archetype","timezone","weekday_blocks","weekend_blocks","work_hours"}``：
    - ``weekday_blocks`` / ``weekend_blocks``:
      ``[{"start":"09:00","end":"12:00","kind":"work","label":"..."}]``
    - ``work_hours``: ``{"start":"09:30","end":"18:00","days":[1,2,3,4,5]}``（ISO 星期）
    """
    persona = persona if isinstance(persona, dict) else {}
    pid = str(persona.get("id") or persona.get("persona_id") or "")
    archetype = _archetype(persona)
    spec = _SCHEDULES.get(archetype) or _SCHEDULES["office"]
    shift = _schedule_jitter(pid, archetype) if pid else 0

    def _blocks(raw: Iterable[Sequence[str]]) -> list[dict]:
        out: list[dict] = []
        for item in raw:
            start, end, kind, label = item[0], item[1], item[2], item[3]
            shifted_start, shifted_end = _shift_block(start, end, shift)
            out.append({
                "start": shifted_start,
                "end": shifted_end,
                "kind": str(kind),
                "label": str(label),
            })
        return out

    work_hours = dict(spec.get("work_hours") or {})
    if work_hours:
        work_hours["start"], work_hours["end"] = _shift_block(
            str(work_hours.get("start") or "09:00"),
            str(work_hours.get("end") or "17:00"),
            shift,
        )
        work_hours["days"] = [int(d) for d in (work_hours.get("days") or [1, 2, 3, 4, 5])]

    return {
        "persona_id": pid,
        "archetype": archetype,
        "timezone": str(persona.get("timezone") or DEFAULT_TZ),
        "weekday_blocks": _blocks(spec.get("weekday") or []),
        "weekend_blocks": _blocks(spec.get("weekend") or []),
        "work_hours": work_hours,
    }


# --------------------------------------------------------------------------- #
# 调度表落库（幂等）
# --------------------------------------------------------------------------- #
def _persona_row(db: Any, persona_id: str) -> dict:
    if db is None or not persona_id:
        return {}
    try:
        with db._lock:  # noqa: SLF001 - 与 DB 层共用连接与锁
            row = db.conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
        return dict(row) if row is not None else {}
    except sqlite3.Error as exc:
        log.debug("读 persona 行失败（忽略）: %s", exc)
        return {}


def _read_schedule(db: Any, persona_id: str) -> dict | None:
    if db is None:
        return None
    try:
        with db._lock:  # noqa: SLF001
            row = db.conn.execute(
                "SELECT * FROM persona_schedules WHERE persona_id=?", (persona_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        log.debug("读 persona_schedules 失败（忽略）: %s", exc)
        return None
    if row is None:
        return None
    return {
        "persona_id": persona_id,
        "timezone": str(row["timezone"] or DEFAULT_TZ),
        "weekday_blocks": _loads(row["weekday_blocks"], []),
        "weekend_blocks": _loads(row["weekend_blocks"], []),
        "work_hours": _loads(row["work_hours"], {}),
        # 原型不落库（可以由 persona 现有字段确定性重算），这里留空由 ensure_schedule 补齐
        "archetype": "",
    }


def _write_schedule(db: Any, persona_id: str, schedule: dict) -> bool:
    if db is None or not persona_id:
        return False
    now = iso(_now_utc())
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO persona_schedules"
                " (persona_id, timezone, weekday_blocks, weekend_blocks, work_hours,"
                "  created_at, updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(persona_id) DO UPDATE SET timezone=excluded.timezone,"
                " weekday_blocks=excluded.weekday_blocks, weekend_blocks=excluded.weekend_blocks,"
                " work_hours=excluded.work_hours, updated_at=excluded.updated_at",
                (
                    persona_id,
                    str(schedule.get("timezone") or DEFAULT_TZ),
                    json.dumps(schedule.get("weekday_blocks") or [], ensure_ascii=False),
                    json.dumps(schedule.get("weekend_blocks") or [], ensure_ascii=False),
                    json.dumps(schedule.get("work_hours") or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return True
    except sqlite3.Error as exc:  # 老库没有表 → 降级为内存作息
        log.warning("写入 persona_schedules 失败（降级为内存作息）: %s", exc)
        return False


def ensure_schedule(db: Any, persona_id: str) -> dict:
    """读/建 ``persona_schedules``（幂等）。老库缺表时返回内存作息，不抛异常。"""
    pid = str(persona_id or "").strip()
    if not pid:
        return {}
    stored = _read_schedule(db, pid)
    if stored is not None and stored.get("weekday_blocks"):
        stored["archetype"] = stored.get("archetype") or _archetype(_persona_row(db, pid))
        return stored
    persona = _persona_row(db, pid) or {"id": pid}
    persona.setdefault("id", pid)
    schedule = build_schedule(persona)
    _write_schedule(db, pid, schedule)
    return schedule


# --------------------------------------------------------------------------- #
# 规则事件（纯函数）
# --------------------------------------------------------------------------- #
#: 落库优先级：工作/学习（信息流里最像"生活"的）> 吃饭 > 通勤/社交 > 杂事/在家
_BLOCK_PRIORITY: dict[str, int] = {
    "work": 0,
    "study": 0,
    "meal": 1,
    "commute": 2,
    "social": 2,
    "exercise": 2,
    "errand": 3,
    "personal": 3,
    "rest": 4,
}

#: 规则事件每天每人的上限（可用 ``SE_LIFE_RULE_EVENTS_MAX`` 覆盖；>=3）
RULE_EVENTS_ENV = "SE_LIFE_RULE_EVENTS_MAX"


def _rule_events_cap() -> int:
    """每天每人落库几条规则事件。默认 4（完整作息仍在 ``persona_schedules`` 里）。"""
    raw = os.getenv(RULE_EVENTS_ENV)
    if raw is None or str(raw).strip() == "":
        return RULE_EVENTS_PER_DAY_DEFAULT
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return RULE_EVENTS_PER_DAY_DEFAULT
    return max(3, value)


def _select_blocks(blocks: list[dict], cap: int) -> list[dict]:
    """从当天作息块里挑 ``cap`` 个最有信息量的（保持时间顺序）。

    完整作息留在 ``persona_schedules``（``current_activity`` 用），
    这里只是控制 ``persona_events`` 的长期增长；超过上限时按
    :data:`_BLOCK_PRIORITY` 丢掉"通勤/在家/杂事"这类填充块。
    """
    valid = [block for block in blocks if isinstance(block, dict)]
    if cap <= 0 or len(valid) <= cap:
        return valid
    ranked = sorted(
        enumerate(valid),
        key=lambda item: (_BLOCK_PRIORITY.get(str(item[1].get("kind") or ""), 3), item[0]),
    )
    keep = {index for index, _block in ranked[:cap]}
    return [block for index, block in enumerate(valid) if index in keep]


def _schedule_from_persona(persona: dict) -> dict:
    """优先用调用方传入/已落库的作息（``weekday_blocks`` 在顶层），否则现场推导。"""
    if isinstance(persona.get("weekday_blocks"), list) and persona.get("weekday_blocks"):
        return {
            "archetype": str(persona.get("archetype") or _archetype(persona)),
            "timezone": str(persona.get("timezone") or DEFAULT_TZ),
            "weekday_blocks": persona.get("weekday_blocks") or [],
            "weekend_blocks": persona.get("weekend_blocks") or [],
            "work_hours": persona.get("work_hours") or {},
        }
    return build_schedule(persona)


def rule_events_for_day(persona: dict, day: str, *, now: Any = None) -> list[dict]:
    """规则事件（**纯函数**）：当天作息块 → planned 事件。

    - 只产出 ``work / study / meal / daily_routine``（普通作息交给规则，LLM 不管这些）；
    - 时间按人物本地时区展开，落库用 UTC ISO；
    - 不写库、不掷骰子：同一 ``(persona, day)`` 永远得到同一批事件（dedupe_key 因此稳定）；
    - 每人每天最多 :func:`_rule_events_cap` 条（默认 4，避免事件表膨胀）。
    """
    persona = persona if isinstance(persona, dict) else {}
    pid = str(persona.get("id") or persona.get("persona_id") or "").strip()
    if not pid:
        return []
    try:
        target = date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return []
    schedule = _schedule_from_persona(persona)
    tz_name = str(schedule.get("timezone") or persona.get("timezone") or DEFAULT_TZ)
    tz = _zone(tz_name)
    archetype = str(schedule.get("archetype") or _archetype(persona))
    blocks = schedule.get("weekday_blocks") if target.weekday() <= 4 else schedule.get("weekend_blocks")
    selected = _select_blocks(list(blocks or []), _rule_events_cap())
    now_dt = _as_utc(now)
    events: list[dict] = []
    for block in selected:
        if not isinstance(block, dict):
            continue
        start_min = _hhmm_to_min(str(block.get("start") or ""))
        end_min = _hhmm_to_min(str(block.get("end") or ""))
        if start_min is None or end_min is None or end_min <= start_min:
            continue
        start_local = datetime.combine(target, dtime(hour=start_min // 60, minute=start_min % 60), tzinfo=tz)
        end_local = datetime.combine(target, dtime(hour=end_min // 60, minute=end_min % 60), tzinfo=tz)
        start_utc = _as_utc(start_local)
        end_utc = _as_utc(end_local)
        if start_utc is None or end_utc is None:
            continue
        kind = str(block.get("kind") or "personal")
        event_type = _KIND_TO_EVENT_TYPE.get(kind, "daily_routine")
        title = str(block.get("label") or kind).strip()
        if not title:
            continue
        start_iso = iso(start_utc)
        events.append({
            "persona_id": pid,
            "event_type": event_type,
            "title": title,
            "description": "",
            "start_time": start_iso,
            "end_time": iso(end_utc),
            "participants": [],
            # 规则事件不编造具体地点：城市由 persona 本身携带，写了反而变成"at Manchester"
            "location": "",
            "related_personas": [],
            "event_status": "planned",
            "source_type": "rule",
            "source_reference": f"schedule:{kind}",
            "visibility": "private",
            "metadata": {
                "archetype": archetype,
                "block_kind": kind,
                "timezone": tz_name,
                "in_past": bool(now_dt is not None and end_utc <= now_dt),
            },
            "dedupe_key": f"{pid}:{event_type}:{start_iso[:13]}:{_slug(title)}",
            "day": str(day),
        })
    events.sort(key=lambda item: item["start_time"])
    return events


# --------------------------------------------------------------------------- #
# 冲突检测（纯函数）
# --------------------------------------------------------------------------- #
def _event_personas(event: dict) -> set[str]:
    """事件牵涉到的人物 = 主体 + 参与者（都算"这个人没空"）。"""
    people = {str(event.get("persona_id") or "")}
    for pid in _as_list(event.get("participants")):
        people.add(str(pid))
    return {p for p in people if p}


def detect_conflicts(
    events: list[dict], persona_ids: list[str], start: str, end: str
) -> list[dict]:
    """返回与 ``[start,end)`` 冲突的既有事件（同一 persona 时间重叠）。

    纯函数：不做 SQL、不改入参。``events`` 传入既有事件即可；``persona_ids``
    里任意一个人出现在既有事件的「主体或参与者」里、且时间重叠 → 判定冲突。
    已取消(cancelled)的事件不算冲突；时间窗半开（``end == 另一端 start`` 不冲突）。
    ``end`` 为空/非法时按「瞬时事件」处理（只有落在窗内才算冲突）。
    """
    people = {str(pid) for pid in (persona_ids or []) if str(pid)}
    start_dt = _as_utc(start)
    if not people or start_dt is None:
        return []
    end_dt = _as_utc(end)
    if end_dt is None or end_dt <= start_dt:
        end_dt = start_dt + timedelta(minutes=1)

    conflicts: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if str(event.get("event_status") or "") == "cancelled":
            continue
        if not (_event_personas(event) & people):
            continue
        ev_start = _as_utc(event.get("start_time"))
        if ev_start is None:
            continue
        ev_end = _as_utc(event.get("end_time"))
        if ev_end is None or ev_end < ev_start:
            ev_end = ev_start
        # 半开区间重叠： [s1,e1) ∩ [s2,e2) ≠ ∅
        if ev_start < end_dt and ev_end > start_dt:
            conflicts.append(event)
        elif ev_end == ev_start and start_dt <= ev_start < end_dt:
            # 瞬时事件落在窗内
            conflicts.append(event)
    return conflicts


# --------------------------------------------------------------------------- #
# 人物关系种子（幂等）
# --------------------------------------------------------------------------- #
def _normalize_personas(personas: Any) -> list[dict]:
    out: list[dict] = []
    for item in personas or []:
        if isinstance(item, dict):
            row = dict(item)
        elif hasattr(item, "keys"):
            row = {key: item[key] for key in item.keys()}
        else:
            continue
        pid = str(row.get("id") or "").strip()
        if not pid:
            continue
        row["id"] = pid
        out.append(row)
    # 去重 + 稳定顺序（保证 ensure_relationships 可复现）
    seen: set[str] = set()
    unique: list[dict] = []
    for row in sorted(out, key=lambda r: r["id"]):
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        unique.append(row)
    return unique


def _permanent_personas(db: Any) -> list[dict]:
    """要过「持续生活」的人物 = 活跃且**不是游客**。

    注意：不能用 ``lifecycle='permanent'`` 过滤。老库里这一列是通过
    ``ALTER TABLE ... ADD COLUMN lifecycle TEXT NOT NULL DEFAULT ''`` 加的，
    所以历史种子人物是空串 ``''``（新表 DDL 的默认值才是 ``'permanent'``）。
    语义应该是「不是 guest」：``''``（老库历史数据）、``'permanent'``、
    ``'adopted'``（游客被互动后转正）都应该有自己的生活；只有 ``'guest'``
    （24h 后回收的临时人物）不建作息/关系/事件。
    """
    if db is None:
        return []
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                "SELECT * FROM personas WHERE status='active'"
                " AND COALESCE(lifecycle, '') <> 'guest' ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        log.debug("读 personas 失败（忽略）: %s", exc)
        return []


def _existing_pairs(db: Any) -> set[tuple[str, str]]:
    if db is None:
        return set()
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute("SELECT persona_a, persona_b FROM persona_relationships").fetchall()
    except sqlite3.Error as exc:
        log.debug("读 persona_relationships 失败（忽略）: %s", exc)
        return set()
    pairs: set[tuple[str, str]] = set()
    for row in rows:
        a, b = str(row["persona_a"] or ""), str(row["persona_b"] or "")
        if a and b:
            pairs.add((min(a, b), max(a, b)))
    return pairs


def _interest_set(persona: dict) -> set[str]:
    raw = persona.get("interests")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(x).strip().lower() for x in raw if str(x).strip()}


def _pair_score(a: dict, b: dict) -> int:
    """两个人物有多"说得上话"：同语言/同城/同国/同行/同兴趣。"""
    score = 0
    if str(a.get("language") or "") and str(a.get("language") or "") == str(b.get("language") or ""):
        score += 3
    if str(a.get("city") or "") and str(a.get("city") or "") == str(b.get("city") or ""):
        score += 3
    if str(a.get("country") or "") and str(a.get("country") or "") == str(b.get("country") or ""):
        score += 1
    if str(a.get("timezone") or "") and str(a.get("timezone") or "") == str(b.get("timezone") or ""):
        score += 1
    if _archetype(a) == _archetype(b):
        score += 2
    score += min(3, len(_interest_set(a) & _interest_set(b)))
    return score


def _relation_for(a: dict, b: dict) -> tuple[str, int]:
    """关系类型 + 亲密度（1..5）。同行 → colleague，其余 → friend。"""
    same_city = str(a.get("city") or "") and str(a.get("city") or "") == str(b.get("city") or "")
    same_lang = str(a.get("language") or "") and str(a.get("language") or "") == str(b.get("language") or "")
    if _archetype(a) == _archetype(b) and not same_city:
        relation = "colleague"
    else:
        relation = "friend"
    if same_city and same_lang:
        closeness = 4
    elif same_city or same_lang:
        closeness = 3
    else:
        closeness = 2
    return relation, max(1, min(5, closeness))


def ensure_relationships(db: Any, personas: Any = None) -> int:
    """给种子人物配对成 ``friend`` / ``colleague``（**幂等**），返回新建条数。

    - 按「同语言 > 同城 > 同行 > 同兴趣」打分，给每个还没有熟人的 Character 配 1 个；
    - 保证每位人物**至少 1 个熟人**（人物不能只认识用户）；
    - ``personas`` 省略时取库里全部**非游客**人物（``lifecycle <> 'guest'``，
      兼容老库的空串 ``''`` 历史数据与 ``'adopted'`` 转正人物）；
    - 已有关系不重复插入；老库缺表时静默返回 0。
    """
    people = _normalize_personas(personas) if personas else _permanent_personas(db)
    if len(people) < 2 or db is None:
        return 0
    existing = _existing_pairs(db)
    connected = {pid for pair in existing for pid in pair}
    created = 0
    now = iso(_now_utc())
    for persona in people:
        pid = persona["id"]
        if pid in connected:
            continue
        best: dict | None = None
        best_key: tuple[int, str] | None = None
        for other in people:
            oid = other["id"]
            if oid == pid:
                continue
            tie = hashlib.sha256(f"{pid}:{oid}".encode("utf-8")).hexdigest()[:8]
            key = (-_pair_score(persona, other), tie)
            if best_key is None or key < best_key:
                best_key, best = key, other
        if best is None:
            continue
        a, b = sorted([pid, best["id"]])
        if (a, b) in existing:
            connected.add(pid)
            continue
        relation, closeness = _relation_for(persona, best)
        try:
            with db._lock, db.conn:  # noqa: SLF001
                db.conn.execute(
                    "INSERT OR IGNORE INTO persona_relationships"
                    " (id, persona_a, persona_b, relation_type, closeness, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (f"pr_{uuid.uuid4().hex[:12]}", a, b, relation, closeness, now, now),
                )
        except sqlite3.Error as exc:
            log.warning("写入 persona_relationships 失败（忽略）: %s", exc)
            continue
        existing.add((a, b))
        connected.update({a, b})
        created += 1
        log.debug("新建人物关系 %s <-> %s (%s)", a, b, relation)
    return created


def _acquaintances(db: Any, persona_id: str) -> list[dict]:
    """人物的熟人列表（双向查询 persona_relationships，附昵称与共同经历数）。"""
    pid = str(persona_id or "").strip()
    if db is None or not pid:
        return []
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                "SELECT * FROM persona_relationships WHERE persona_a=? OR persona_b=?",
                (pid, pid),
            ).fetchall()
    except sqlite3.Error as exc:
        log.debug("读熟人失败（忽略）: %s", exc)
        return []
    other_ids: list[str] = []
    meta: dict[str, dict] = {}
    for row in rows:
        rel = _row_to_relationship(row)
        other = rel["persona_b"] if rel["persona_a"] == pid else rel["persona_a"]
        if not other or other == pid:
            continue
        if other not in meta:
            other_ids.append(other)
        meta[other] = {
            "relation_type": rel["relation_type"],
            "closeness": rel["closeness"],
            "shared_events": rel["shared_events"],
            "tension": rel["tension"],
            "last_conflict_at": rel["last_conflict_at"],
        }
    if not other_ids:
        return []
    names: dict[str, str] = {}
    try:
        placeholders = ",".join("?" for _ in other_ids)
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                f"SELECT id, nickname, occupation, city FROM personas WHERE id IN ({placeholders})",
                tuple(other_ids),
            ).fetchall()
        for row in rows:
            names[str(row["id"])] = str(row["nickname"] or row["id"])
    except sqlite3.Error as exc:
        log.debug("读熟人昵称失败（忽略）: %s", exc)
    out: list[dict] = []
    for oid in other_ids:
        out.append({
            "id": oid,
            "nickname": names.get(oid, oid),
            "relation_type": meta[oid]["relation_type"],
            "closeness": meta[oid]["closeness"],
            "shared_events": meta[oid]["shared_events"],
            "tension": meta[oid]["tension"],
            "last_conflict_at": meta[oid]["last_conflict_at"],
        })
    return out


# --------------------------------------------------------------------------- #
# 人物关系演进（共同经历 → closeness，冲突/和解 → tension）
# --------------------------------------------------------------------------- #
def _normalize_pair(persona_a: Any, persona_b: Any) -> tuple[str, str]:
    """把一对人物 id 归一化成稳定顺序；自身/空 id 返回 ``("", "")``。"""
    a = str(persona_a or "").strip()
    b = str(persona_b or "").strip()
    if not a or not b or a == b:
        return "", ""
    return (a, b) if a <= b else (b, a)


def _row_to_relationship(row: Any) -> dict:
    """``persona_relationships`` 行 → dict（老库缺新列时给默认值）。"""
    keys = set(row.keys()) if hasattr(row, "keys") else set()

    def pick(name: str, default: Any = "") -> Any:
        return row[name] if name in keys else default

    try:
        closeness = int(pick("closeness", DEFAULT_CLOSENESS) or DEFAULT_CLOSENESS)
    except (TypeError, ValueError):
        closeness = DEFAULT_CLOSENESS
    try:
        shared = int(pick("shared_events", 0) or 0)
    except (TypeError, ValueError):
        shared = 0
    try:
        tension = int(pick("tension", DEFAULT_TENSION) or 0)
    except (TypeError, ValueError):
        tension = DEFAULT_TENSION
    return {
        "id": str(pick("id", "")),
        "persona_a": str(pick("persona_a", "")),
        "persona_b": str(pick("persona_b", "")),
        "relation_type": str(pick("relation_type", "friend") or "friend"),
        "closeness": max(1, min(MAX_CLOSENESS, closeness)),
        "shared_events": max(0, shared),
        "last_shared_at": str(pick("last_shared_at", "") or ""),
        "tension": max(0, min(MAX_TENSION, tension)),
        "last_conflict_at": str(pick("last_conflict_at", "") or ""),
        "created_at": str(pick("created_at", "") or ""),
        "updated_at": str(pick("updated_at", "") or ""),
    }


def _relationship_row(db: Any, persona_a: str, persona_b: str) -> Any:
    """双向查一条关系行（调用方需持有 ``db._lock``；SQL 两种存储方向都认）。"""
    return db.conn.execute(
        "SELECT * FROM persona_relationships"
        " WHERE (persona_a=? AND persona_b=?) OR (persona_a=? AND persona_b=?)"
        " ORDER BY id LIMIT 1",
        (persona_a, persona_b, persona_b, persona_a),
    ).fetchone()


def relationship_between(db: Any, persona_a: str, persona_b: str) -> dict | None:
    """双向查询两人之间的关系；不存在返回 ``None``（方向已归一化）。

    返回 ``{'id','persona_a','persona_b','relation_type','closeness',
    'shared_events','last_shared_at', ...}``。A→B 与 B→A 查到的是**同一条**记录。
    """
    pair = _normalize_pair(persona_a, persona_b)
    if not pair or db is None:
        return None
    try:
        with db._lock:  # noqa: SLF001
            row = _relationship_row(db, pair[0], pair[1])
    except sqlite3.Error as exc:
        log.debug("读 persona_relationships 失败（忽略）: %s", exc)
        return None
    return _row_to_relationship(row) if row is not None else None


def _bump_relationship_in_tx(db: Any, persona_a: str, persona_b: str, now_iso: str) -> dict | None:
    """关系 +1 的**事务内**实现：调用方必须已持有 ``db._lock`` 且处在同一事务里。

    不能在这里再开 ``with db.conn``：嵌套的 connection context manager 会提前 commit，
    把「事件状态迁移 + 情绪 + 关系」拆成多个事务。
    """
    pair = _normalize_pair(persona_a, persona_b)
    if not pair:
        return None
    row = _relationship_row(db, pair[0], pair[1])
    if row is None:
        # 关系不存在就不动：不凭空造关系（种子由 ensure_relationships 负责）
        return None
    current = _row_to_relationship(row)
    shared = current["shared_events"] + 1
    closeness = current["closeness"]
    if shared in CLOSENESS_BUMPS:
        closeness = min(MAX_CLOSENESS, closeness + 1)  # 只升不降、封顶 5
    db.conn.execute(
        "UPDATE persona_relationships SET shared_events=?, last_shared_at=?,"
        " closeness=?, updated_at=? WHERE id=?",
        (shared, now_iso, closeness, now_iso, current["id"]),
    )
    updated = dict(current)
    updated.update({"shared_events": shared, "last_shared_at": now_iso,
                    "closeness": closeness, "updated_at": now_iso})
    return updated


def bump_relationship(db: Any, persona_a: str, persona_b: str, *, now: Any = None) -> dict | None:
    """记一次「共同经历」并可能加深关系，返回更新后的关系 dict。

    - ``shared_events += 1``，``last_shared_at = now``；
    - ``closeness`` **单调不减**：``shared_events ∈ CLOSENESS_BUMPS (2,5,9,14)`` 时 +1，
      封顶 :data:`MAX_CLOSENESS`；既有手写的 closeness 不会被按比例重算而降低；
    - 关系不存在 → ``None``（**不凭空造关系**）；
    - **幂等由调用方负责**：本函数每调一次就 +1，``refresh_statuses`` 只在事件
      真正迁移到 completed 的那一次调用它。
    """
    if db is None or not _normalize_pair(persona_a, persona_b):
        return None
    now_iso = iso(_as_utc(now) or _now_utc())
    try:
        with db._lock, db.conn:  # noqa: SLF001 - 读+写同一事务，避免并发丢更新
            return _bump_relationship_in_tx(db, persona_a, persona_b, now_iso)
    except sqlite3.Error as exc:
        log.warning("更新 persona_relationships 失败（忽略）: %s", exc)
        return None


def _tension_step(delta: Any, default: int = 1) -> int:
    """``delta`` 规范化：取绝对值、整数；脏输入回退默认值（负数不会被当成反向）。"""
    try:
        step = int(abs(float(delta)))
    except (TypeError, ValueError):
        step = default
    return max(0, step)


def _adjust_tension_in_tx(
    db: Any, persona_a: str, persona_b: str, step: int, now_iso: str, *, touch_conflict: bool
) -> dict | None:
    """张力调整的**事务内**实现（调用方持有 ``db._lock`` 且处在同一事务里）。

    - ``step > 0``（冲突）→ ``tension += step``，clamp 1..:data:`MAX_TENSION` 区间 0..5，
      并更新 ``last_conflict_at``；
    - ``step < 0``（和解）→ ``tension += step``，clamp 不为负，不改 ``last_conflict_at``；
    - 关系不存在 → ``None``（不凭空造关系）。
    """
    pair = _normalize_pair(persona_a, persona_b)
    if not pair:
        return None
    row = _relationship_row(db, pair[0], pair[1])
    if row is None:
        return None
    current = _row_to_relationship(row)
    tension = max(0, min(MAX_TENSION, current["tension"] + int(step)))
    if touch_conflict:
        db.conn.execute(
            "UPDATE persona_relationships SET tension=?, last_conflict_at=?, updated_at=?"
            " WHERE id=?",
            (tension, now_iso, now_iso, current["id"]),
        )
    else:
        db.conn.execute(
            "UPDATE persona_relationships SET tension=?, updated_at=? WHERE id=?",
            (tension, now_iso, current["id"]),
        )
    updated = dict(current)
    updated.update({"tension": tension, "updated_at": now_iso})
    if touch_conflict:
        updated["last_conflict_at"] = now_iso
    return updated


def raise_tension(db: Any, persona_a: str, persona_b: str, *, now: Any = None,
                  delta: int = 1) -> dict | None:
    """摩擦 ``+delta``（默认 1），clamp 0..:data:`MAX_TENSION`；更新 ``last_conflict_at``。

    关系不存在 → ``None``（**不凭空造关系**）。``delta`` 取绝对值并取整，
    脏输入回退 1（``0`` 表示不改张力，仅返回当前关系）。
    """
    if db is None or not _normalize_pair(persona_a, persona_b):
        return None
    step = _tension_step(delta)
    now_iso = iso(_as_utc(now) or _now_utc())
    try:
        with db._lock, db.conn:  # noqa: SLF001 - 读+写同一事务
            return _adjust_tension_in_tx(db, persona_a, persona_b, step, now_iso,
                                         touch_conflict=step > 0)
    except sqlite3.Error as exc:
        log.warning("提升关系张力失败（忽略）: %s", exc)
        return None


def ease_tension(db: Any, persona_a: str, persona_b: str, *, now: Any = None,
                 delta: int = 1) -> dict | None:
    """摩擦 ``-delta``（默认 1），clamp 0..:data:`MAX_TENSION`（不会为负）。

    关系不存在 → ``None``；``delta`` 规范化同 :func:`raise_tension`。
    """
    if db is None or not _normalize_pair(persona_a, persona_b):
        return None
    step = _tension_step(delta)
    now_iso = iso(_as_utc(now) or _now_utc())
    try:
        with db._lock, db.conn:  # noqa: SLF001
            return _adjust_tension_in_tx(db, persona_a, persona_b, -step, now_iso,
                                         touch_conflict=False)
    except sqlite3.Error as exc:
        log.warning("降低关系张力失败（忽略）: %s", exc)
        return None


#: ``relationship_summary`` 的 ``recent_event`` 只查最近这么多条 completed 事件
#: （一次查询覆盖所有关系对，避免 N+1；5 条关系最多 5 对，60 条事件足够命中）
_RECENT_EVENT_SCAN_LIMIT = 60


def _recent_events_by_pair(
    db: Any, persona_id: str, other_ids: Sequence[str]
) -> dict[tuple[str, str], dict]:
    """一次查询得到「该人物与每个熟人」最近一条**共同参与**的 completed 事件。

    参与集合口径与 :func:`_apply_completion_relationships` 一致：
    ``{event.persona_id} ∪ participants`` 同时包含两人 → 才算共同参与。
    只读、按 ``start_time DESC`` 取前 :data:`_RECENT_EVENT_SCAN_LIMIT` 条，
    然后在 Python 里一次遍历算出所有 pair 的最近事件（**无 N+1**）。
    """
    pid = str(persona_id or "").strip()
    wanted = {str(oid) for oid in other_ids if str(oid) and str(oid) != pid}
    if not pid or not wanted:
        return {}
    entries: dict[tuple[str, str], dict] = {}
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                "SELECT * FROM persona_events"
                " WHERE event_status='completed' AND (persona_id=? OR participants LIKE ?)"
                " ORDER BY start_time DESC LIMIT ?",
                (pid, f'%"{pid}"%', _RECENT_EVENT_SCAN_LIMIT),
            ).fetchall()
    except sqlite3.Error as exc:
        log.debug("读共同经历事件失败（忽略）: %s", exc)
        return {}
    for row in rows:
        event = _row_to_event(row)
        involved = {str(event.get("persona_id") or "")}
        involved.update(str(item) for item in _as_list(event.get("participants")))
        involved.discard("")
        if pid not in involved:
            continue
        for other in involved & wanted:
            key = _normalize_pair(pid, other)
            if not key or key in entries:
                continue  # 已按 start_time DESC 遍历，第一次命中就是最近的一条
            entries[key] = {
                "title": str(event.get("title") or ""),
                "event_type": str(event.get("event_type") or ""),
                "start_time": str(event.get("start_time") or ""),
            }
        if len(entries) >= len(wanted):
            break
    return entries


def relationship_summary(db: Any, persona_id: str, *, limit: int = 5) -> list[dict]:
    """人物主页/观测用：熟人昵称 + 关系类型 + closeness + 共同经历数 + 张力 + 关系小史。

    返回字段：``relation_id, persona_id, nickname, relation_type, closeness,
    shared_events, last_shared_at, tension, last_conflict_at, recent_event``。
    ``recent_event`` = 该对**共同参与过**的最近一条 completed 事件
    （``{'title','event_type','start_time'}``；没有则 ``None``）。

    排序：``shared_events DESC, closeness DESC, tension ASC``（同分按 persona_id 稳定），
    只取前 ``limit`` (≤50) 条；事件查询对全部关系对只做**一次**（无 N+1）。
    """
    pid = str(persona_id or "").strip()
    if not pid or db is None:
        return []
    try:
        top = max(1, min(50, int(limit or 1)))
    except (TypeError, ValueError):
        top = 5
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                "SELECT * FROM persona_relationships WHERE persona_a=? OR persona_b=?",
                (pid, pid),
            ).fetchall()
    except sqlite3.Error as exc:
        log.debug("读 persona_relationships 失败（忽略）: %s", exc)
        return []
    entries: list[tuple[dict, str]] = []
    other_ids: list[str] = []
    for row in rows:
        rel = _row_to_relationship(row)
        other = rel["persona_b"] if rel["persona_a"] == pid else rel["persona_a"]
        if not other or other == pid:
            continue
        entries.append((rel, other))
        if other not in other_ids:
            other_ids.append(other)
    if not entries:
        return []
    names: dict[str, str] = {}
    try:
        placeholders = ",".join("?" for _ in other_ids)
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                f"SELECT id, nickname FROM personas WHERE id IN ({placeholders})",
                tuple(other_ids),
            ).fetchall()
        for row in rows:
            names[str(row["id"])] = str(row["nickname"] or row["id"])
    except sqlite3.Error as exc:
        log.debug("读熟人昵称失败（忽略）: %s", exc)
    out = [
        {
            "relation_id": rel["id"],
            "persona_id": other,
            "nickname": names.get(other, other),
            "relation_type": rel["relation_type"],
            "closeness": rel["closeness"],
            "shared_events": rel["shared_events"],
            "last_shared_at": rel["last_shared_at"],
            "tension": rel["tension"],
            "last_conflict_at": rel["last_conflict_at"],
            "recent_event": None,
        }
        for rel, other in entries
    ]
    out.sort(key=lambda item: (
        -item["shared_events"], -item["closeness"], item["tension"], item["persona_id"],
    ))
    limited = out[:top]
    # 只为最终返回的这几对查"关系小史"（一次查询，无 N+1）
    if limited:
        recent = _recent_events_by_pair(db, pid, [item["persona_id"] for item in limited])
        for item in limited:
            item["recent_event"] = recent.get(_normalize_pair(pid, item["persona_id"]))
    return limited


# --------------------------------------------------------------------------- #
# 写入 / 状态推进
# --------------------------------------------------------------------------- #
_EVENT_COLUMNS: tuple[str, ...] = (
    "id", "persona_id", "event_type", "title", "description", "start_time", "end_time",
    "participants", "location", "related_personas", "event_status", "source_type",
    "source_reference", "visibility", "metadata", "dedupe_key", "day",
    "created_at", "updated_at", "completed_at",
)


def _event_columns(event: dict) -> dict | None:
    """事件 dict → 数据库列（含校验与默认值）。非法事件返回 None。"""
    event = event if isinstance(event, dict) else {}
    pid = str(event.get("persona_id") or "").strip()
    if not pid:
        return None
    start_dt = _as_utc(event.get("start_time"))
    if start_dt is None:
        return None
    end_dt = _as_utc(event.get("end_time"))
    if end_dt is not None and end_dt <= start_dt:
        end_dt = start_dt + timedelta(minutes=30)
    event_type = str(event.get("event_type") or "daily_routine").strip().lower()
    if event_type not in EVENT_TYPES:
        event_type = "daily_routine"
    status = str(event.get("event_status") or "planned").strip().lower()
    if status not in EVENT_STATUSES:
        status = "planned"
    title = _clip(event.get("title") or "", 160) or event_type.replace("_", " ")
    start_iso = iso(start_dt)
    end_iso = iso(end_dt) if end_dt is not None else ""
    # day：优先调用方给的（人物本地日），否则用开始时间（UTC）的日期兜底
    day = str(event.get("day") or "").strip() or start_iso[:10]
    dedupe_key = str(event.get("dedupe_key") or "").strip()
    if not dedupe_key:
        dedupe_key = f"{pid}:{event_type}:{start_iso[:13]}:{_slug(title)}"
    participants = [str(x) for x in _as_list(event.get("participants")) if str(x)]
    related = event.get("related_personas")
    if related is None:
        related_list = list(participants)
    else:
        related_list = [str(x) for x in _as_list(related) if str(x)]
    now = iso(_now_utc())
    metadata = _as_dict(event.get("metadata"))
    return {
        "id": str(event.get("id") or f"pe_{uuid.uuid4().hex[:12]}"),
        "persona_id": pid,
        "event_type": event_type,
        "title": title,
        "description": _clip(event.get("description") or "", 600),
        "start_time": start_iso,
        "end_time": end_iso,
        "participants": json.dumps(participants, ensure_ascii=False),
        "location": _clip(event.get("location") or "", 120),
        "related_personas": json.dumps(related_list, ensure_ascii=False),
        "event_status": status,
        "source_type": str(event.get("source_type") or "rule"),
        "source_reference": str(event.get("source_reference") or ""),
        "visibility": str(event.get("visibility") or "private"),
        "metadata": json.dumps(metadata, ensure_ascii=False),
        "dedupe_key": dedupe_key,
        "day": day,
        "created_at": str(event.get("created_at") or now),
        "updated_at": now,
        "completed_at": str(event.get("completed_at") or (now if status == "completed" else "")),
    }


def _id_by_dedupe(db: Any, dedupe_key: str) -> str:
    try:
        with db._lock:  # noqa: SLF001
            row = db.conn.execute(
                "SELECT id FROM persona_events WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
        return str(row["id"]) if row is not None else ""
    except sqlite3.Error as exc:
        log.debug("按 dedupe_key 查事件失败（忽略）: %s", exc)
        return ""


def add_event(db: Any, event: dict) -> tuple[str, bool]:
    """幂等写入事件，返回 ``(id, created)``。

    ``dedupe_key`` 已存在 → 返回已有 id + ``False``（后台重复跑不会产生重复事件）。
    老库没有 ``persona_events`` 表 → 记日志并返回 ``("", False)``，绝不抛异常。
    """
    row = _event_columns(event)
    if row is None:
        return "", False
    if db is None:
        return "", False
    placeholders = ",".join("?" for _ in _EVENT_COLUMNS)
    sql = f"INSERT INTO persona_events ({', '.join(_EVENT_COLUMNS)}) VALUES ({placeholders})"
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(sql, tuple(row[key] for key in _EVENT_COLUMNS))
        return row["id"], True
    except sqlite3.IntegrityError:
        existing = _id_by_dedupe(db, row["dedupe_key"])
        return existing, False
    except sqlite3.Error as exc:
        log.warning("写入 persona_events 失败（忽略）: %s", exc)
        return "", False


def _emotion_vector(state: dict) -> tuple:
    """用于比较情绪是否真的变了（mood + 三个数值）。"""
    return (
        str(state.get("mood") or ""),
        round(float(state.get("energy") or 0.0), 3),
        round(float(state.get("stress") or 0.0), 3),
        round(float(state.get("social_battery") or 0.0), 3),
    )


def _apply_completion_emotion(db: Any, event: dict, *, now_iso: str) -> int:
    """事件完成时给「主体 + 参与者」各应用一次情绪变化，返回写入的人物数。

    必须在"状态迁移 UPDATE 成功(rowcount==1)"之后的**同一个事务里**调用：
    这样"迁移""情绪""关系"要么一起提交、要么一起回滚，重复调用 ``refresh_statuses``
    不会重复叠加（第二次 select 不到该事件，UPDATE 也不会再命中）。

    ``event`` 是已解析的事件 dict，且 ``event_status`` 由调用方置为 ``"completed"``。
    """
    try:
        from app.v3 import emotion as emotion_mod  # noqa: PLC0415 - 保持依赖单向、避免导入期耦合

        involved = [str(event.get("persona_id") or "")]
        involved.extend(str(pid) for pid in _as_list(event.get("participants")))
        people = list(dict.fromkeys(pid for pid in involved if pid))
        written = 0
        for pid in people:
            state_row = db.conn.execute(
                "SELECT emotion_state FROM personas WHERE id=?", (pid,)
            ).fetchone()
            if state_row is None:
                continue
            keys = set(state_row.keys())
            raw = state_row["emotion_state"] if "emotion_state" in keys else ""
            before = emotion_mod.normalize_state(_loads(raw, {}) if raw else {})
            after = emotion_mod.apply_event(before, event)
            if _emotion_vector(after) == _emotion_vector(before):
                continue
            after["updated_at"] = now_iso
            db.conn.execute(
                "UPDATE personas SET emotion_state=? WHERE id=?",
                (json.dumps(after, ensure_ascii=False), pid),
            )
            written += 1
        return written
    except Exception as exc:  # noqa: BLE001 - 情绪写失败绝不能挡住状态推进
        log.debug("事件完成后的情绪更新失败（忽略）: %s", exc)
        return 0


def _existing_persona_ids(db: Any, persona_ids: Sequence[str]) -> set[str]:
    """只保留真实存在于 ``personas`` 表的 id（过滤脏 participants，避免无意义查询/误配）。

    查询失败时**退化为不过滤**：真正的兜底是"没有关系行就不 bump"，
    这里只是优化，不能因此让关系演进整体失效。
    """
    ids = [str(pid) for pid in dict.fromkeys(persona_ids) if str(pid)]
    if not ids:
        return set()
    try:
        placeholders = ",".join("?" for _ in ids)
        with db._lock:  # noqa: SLF001 - RLock 可重入；不新开事务
            rows = db.conn.execute(
                f"SELECT id FROM personas WHERE id IN ({placeholders})", tuple(ids)
            ).fetchall()
        return {str(row["id"]) for row in rows}
    except sqlite3.Error as exc:
        log.debug("校验 personas id 失败（退化为不过滤）: %s", exc)
        return set(ids)


def _tension_step_for_event(event_type: str) -> int:
    """事件类型 → 张力方向：conflict +1；和解类 -1；其他 0（不动张力）。"""
    kind = str(event_type or "").strip().lower()
    if kind in TENSION_RAISE_TYPES:
        return 1
    if kind in TENSION_EASE_TYPES:
        return -1
    return 0


def _apply_completion_relationships(db: Any, event: dict, *, now_iso: str) -> int:
    """事件完成时给**参与集合里的每一对人物**记共同经历，并按事件类型调整张力。

    参与集合 = ``{event.persona_id} ∪ participants``（去重、去空串、去不在 personas 表里的脏 id）。
    真实数据里 LLM 叙事事件的 ``participants`` **不含主体**（例如
    ``persona_id='p_adrian', participants=['p_callum','p_mei']`` 表示三个人一起去了 pub quiz），
    所以主体必须并入集合，否则"主体↔参与者"永远不会变近。

    每对人物：
    - 一律 :func:`_bump_relationship_in_tx`（争吵也是一次共同经历）；
    - ``conflict`` → ``tension +1``（并记 ``last_conflict_at``）；
      ``social/relationship/achievement/entertainment`` → ``tension -1``（和解，不为负）；
      其他类型（work/meal/daily_routine/…）→ 只 bump，不动 tension。

    - 只在事件真正迁移到 ``completed`` 的那一次被调用（幂等由调用方保证）；
    - 集合少于 2 人 → 不产生"共同经历"（例如 ``participants=[]`` 的独角事件）；
    - 关系不存在 → 两个内部函数都返回 None，**不凭空造关系**；
    - 集合去重后两两组合，同一对在一次事件里只处理一次；
    - 与情绪写入同一事务（本函数不自己开 ``with db.conn``）。
    """
    try:
        raw = [str(event.get("persona_id") or "")]
        raw.extend(str(pid) for pid in _as_list(event.get("participants")))
        people = list(dict.fromkeys(pid.strip() for pid in raw if pid and pid.strip()))
        if len(people) < 2:
            return 0
        existing = _existing_persona_ids(db, people)
        people = [pid for pid in people if pid in existing]
        if len(people) < 2:
            return 0
        tension_step = _tension_step_for_event(str(event.get("event_type") or ""))
        bumped = 0
        for index, first in enumerate(people):
            for second in people[index + 1:]:
                if _bump_relationship_in_tx(db, first, second, now_iso) is None:
                    continue  # 关系不存在：不造关系，也不动张力
                bumped += 1
                if tension_step:
                    _adjust_tension_in_tx(
                        db, first, second, tension_step, now_iso,
                        touch_conflict=tension_step > 0,
                    )
        return bumped
    except Exception as exc:  # noqa: BLE001 - 关系写失败不能挡住状态推进
        log.debug("事件完成后的关系更新失败（忽略）: %s", exc)
        return 0


def refresh_statuses(db: Any, *, now: Any = None) -> int:
    """按 start/end 推进状态，返回**变更行数**。事件完成时顺带更新情绪与人物关系。

    状态迁移（全部幂等）：
    1. ``planned`` 且已到开始时间、窗口未过 → ``ongoing``（一次批量 UPDATE）
    2. ``planned`` / ``ongoing`` 且窗口已过（或没有 end_time 且开始超过 6h）→ ``completed``；
       逐行 ``UPDATE ... WHERE id=? AND event_status IN ('planned','ongoing')``，
       **只有 rowcount==1 的那一次**才在同一个事务里：
       - 对事件主体与参与者应用 :func:`app.v3.emotion.apply_event`（写 ``personas.emotion_state``）
       - 对**参与集合（主体 ∪ participants）**两两配对：``bump_relationship``
         （``shared_events`` +1、closeness 单调加深）＋按事件类型调整 ``tension``
         （conflict +1、social/relationship/achievement/entertainment -1、其他不动）

    ``cancelled`` 永远不动、也不影响情绪/关系。重复调用返回 0（不会重复叠加）。
    """
    if db is None:
        return 0
    now_dt = _as_utc(now) or _now_utc()
    now_iso = iso(now_dt)
    stale_iso = iso(now_dt - timedelta(hours=STALE_GRACE_HOURS))
    changed = 0

    # 先找出本次即将完成的候选（只读；后面逐行做条件 UPDATE 来抢"迁移权"）
    try:
        with db._lock:  # noqa: SLF001
            due_rows = db.conn.execute(
                "SELECT * FROM persona_events WHERE event_status IN ('planned','ongoing')"
                " AND ((end_time!='' AND end_time<=?) OR (end_time='' AND start_time<=?))",
                (now_iso, stale_iso),
            ).fetchall()
    except sqlite3.Error as exc:
        log.warning("refresh_statuses 查询失败（忽略）: %s", exc)
        return 0

    ongoing_sql = (
        "UPDATE persona_events SET event_status='ongoing', updated_at=?"
        " WHERE event_status='planned' AND start_time<=?"
        " AND ((end_time!='' AND end_time>?) OR end_time='')"
    )
    complete_sql = (
        "UPDATE persona_events SET event_status='completed', completed_at=?, updated_at=?"
        " WHERE id=? AND event_status IN ('planned','ongoing')"
    )
    try:
        with db._lock, db.conn:  # noqa: SLF001 - 迁移/情绪/关系同一事务，一起提交或回滚
            changed += int(
                db.conn.execute(ongoing_sql, (now_iso, now_iso, now_iso)).rowcount or 0
            )
            for row in due_rows:
                cursor = db.conn.execute(complete_sql, (now_iso, now_iso, row["id"]))
                if int(cursor.rowcount or 0) != 1:
                    continue  # 并发/已处理：这次没抢到迁移权，不重复叠加情绪与关系
                changed += 1
                event = _row_to_event(row)
                event["event_status"] = "completed"  # 本次迁移的目标状态：足量变化
                _apply_completion_emotion(db, event, now_iso=now_iso)
                _apply_completion_relationships(db, event, now_iso=now_iso)
    except sqlite3.Error as exc:
        log.warning("refresh_statuses 失败（忽略）: %s", exc)
    return changed


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def _row_to_event(row: Any) -> dict:
    keys = set(row.keys()) if hasattr(row, "keys") else set()

    def pick(name: str, default: Any = "") -> Any:
        return row[name] if name in keys else default

    participants = _loads(pick("participants", "[]"), [])
    related = _loads(pick("related_personas", "[]"), [])
    metadata = _loads(pick("metadata", "{}"), {})
    event = {
        "id": str(pick("id", "")),
        "persona_id": str(pick("persona_id", "")),
        "event_type": str(pick("event_type", "daily_routine")),
        "title": str(pick("title", "")),
        "description": str(pick("description", "")),
        "start_time": str(pick("start_time", "")),
        "end_time": str(pick("end_time", "")),
        "participants": [str(x) for x in (participants or [])],
        "location": str(pick("location", "")),
        "related_personas": [str(x) for x in (related or [])],
        "event_status": str(pick("event_status", "planned")),
        "source_type": str(pick("source_type", "rule")),
        "source_reference": str(pick("source_reference", "")),
        "visibility": str(pick("visibility", "private")),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "dedupe_key": str(pick("dedupe_key", "")),
        "day": str(pick("day", "")),
        "created_at": str(pick("created_at", "")),
        "updated_at": str(pick("updated_at", "")),
        "completed_at": str(pick("completed_at", "")),
    }
    # 便捷别名：persona_context / behavior 需要「此刻在做什么」的一句话标签
    event["label"] = event["title"]
    return event


def _query_events(
    db: Any,
    *,
    where: str,
    params: Sequence[Any],
    order: str,
    limit: int,
) -> list[dict]:
    if db is None or not where:
        return []
    sql = f"SELECT * FROM persona_events WHERE {where} ORDER BY {order} LIMIT ?"
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(sql, tuple(params) + (max(1, min(300, int(limit or 1))),)).fetchall()
    except sqlite3.Error as exc:
        log.debug("查询 persona_events 失败（忽略）: %s", exc)
        return []
    return [_row_to_event(row) for row in rows]


def events_for_persona(
    db: Any,
    persona_id: str,
    *,
    statuses: Iterable[str] | None = None,
    since_days: int = 7,
    limit: int = 6,
) -> list[dict]:
    """某人物最近的事件（默认最近 7 天，按开始时间倒序）。"""
    pid = str(persona_id or "").strip()
    if not pid:
        return []
    clauses = ["persona_id=?"]
    params: list[Any] = [pid]
    wanted = [s for s in (statuses or []) if s in EVENT_STATUSES]
    if wanted:
        clauses.append(f"event_status IN ({','.join('?' for _ in wanted)})")
        params.extend(wanted)
    try:
        days = int(since_days or 0)
    except (TypeError, ValueError):
        days = 0
    if days > 0:
        clauses.append("start_time>=?")
        params.append(iso(_now_utc() - timedelta(days=days)))
    return _query_events(db, where=" AND ".join(clauses), params=params, order="start_time DESC", limit=limit)


def current_activity(db: Any, persona_id: str, *, now: Any = None) -> dict | None:
    """此刻正在发生的事：优先 ``ongoing`` 事件，没有则从作息推断（**不写库**）。

    返回的 dict 一定带 ``label``（一句话"在做什么"）与 ``start_time``（ISO），
    供 ``persona_context`` 拼 prompt、``behavior`` 判断场景。
    """
    pid = str(persona_id or "").strip()
    if not pid:
        return None
    now_dt = _as_utc(now) or _now_utc()
    now_iso = iso(now_dt)
    rows = _query_events(
        db,
        where=(
            "persona_id=? AND event_status IN ('ongoing','planned')"
            " AND start_time<=? AND (end_time='' OR end_time>?)"
        ),
        params=[pid, now_iso, now_iso],
        order="start_time DESC",
        limit=1,
    )
    if rows:
        event = rows[0]
        event["event_status"] = "ongoing"
        event["metadata"] = {**_as_dict(event.get("metadata")), "inferred": False}
        return event

    # 没有事件（例如今天还没批量生成）→ 从作息现场推断，绝不写库
    try:
        persona = _persona_row(db, pid) or {"id": pid}
        persona.setdefault("id", pid)
        schedule = ensure_schedule(db, pid) if db is not None else build_schedule(persona)
        tz_name = str(schedule.get("timezone") or persona.get("timezone") or DEFAULT_TZ)
        tz = _zone(tz_name)
        local_dt = now_dt.astimezone(tz)
        target = local_dt.date()
        blocks = (
            schedule.get("weekday_blocks") if target.weekday() <= 4 else schedule.get("weekend_blocks")
        ) or []
        current_min = local_dt.hour * 60 + local_dt.minute
        for block in blocks:
            if not isinstance(block, dict):
                continue
            start_min = _hhmm_to_min(str(block.get("start") or ""))
            end_min = _hhmm_to_min(str(block.get("end") or ""))
            if start_min is None or end_min is None or not (start_min <= current_min < end_min):
                continue
            kind = str(block.get("kind") or "personal")
            start_local = datetime.combine(
                target, dtime(hour=start_min // 60, minute=start_min % 60), tzinfo=tz
            )
            end_local = datetime.combine(
                target, dtime(hour=end_min // 60, minute=end_min % 60), tzinfo=tz
            )
            start_utc, end_utc = _as_utc(start_local), _as_utc(end_local)
            if start_utc is None or end_utc is None:
                continue
            title = str(block.get("label") or kind)
            return {
                "id": "",
                "persona_id": pid,
                "event_type": _KIND_TO_EVENT_TYPE.get(kind, "daily_routine"),
                "title": title,
                "label": title,
                "description": "",
                "start_time": iso(start_utc),
                "end_time": iso(end_utc),
                "participants": [],
                "location": "",
                "related_personas": [],
                "event_status": "ongoing",
                "source_type": "rule",
                "source_reference": f"schedule:{kind}",
                "visibility": "private",
                "metadata": {
                    "archetype": schedule.get("archetype") or "",
                    "block_kind": kind,
                    "timezone": tz_name,
                    "inferred": True,
                },
                "dedupe_key": "",
                "day": str(target),
                "created_at": "",
                "updated_at": "",
                "completed_at": "",
            }
    except Exception as exc:  # noqa: BLE001 - 推断失败就是"没有这条信息"
        log.debug("current_activity 推断失败（忽略）: %s", exc)
    return None


_WORD_RE = re.compile(r"[a-z0-9']{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

#: 英文查询里没有信息量的词
_STOPWORDS = frozenset({
    "the", "and", "you", "your", "yours", "for", "with", "that", "this", "have",
    "has", "had", "was", "were", "are", "is", "am", "be", "been", "about", "what",
    "when", "where", "how", "why", "who", "did", "does", "do", "today", "yesterday",
    "tomorrow", "just", "now", "not", "but", "from", "they", "them", "his", "her",
    "our", "its", "it", "me", "my", "we", "us", "哦", "嗯", "啊", "了", "在", "是",
})


def _query_terms(text: str) -> set[str]:
    """查询词：拉丁词 + 中文二元组（中文没有空格，用 bigram 做词面重合）。"""
    low = str(text or "").lower()
    terms: set[str] = set()
    for word in _WORD_RE.findall(low):
        if word not in _STOPWORDS and len(word) >= 2:
            terms.add(word)
    for run in _CJK_RE.findall(low):
        if len(run) == 1:
            if run not in _STOPWORDS:
                terms.add(run)
            continue
        for i in range(len(run) - 1):
            terms.add(run[i : i + 2])
    return terms


def _relevance_score(event: dict, terms: set[str]) -> float:
    """词面重合（title 权重最高）+ 时间衰减（越近越容易被提起）。"""
    if not terms:
        return 0.0
    metadata = _as_dict(event.get("metadata"))
    title = str(event.get("title") or "").lower()
    body = " ".join([
        str(event.get("description") or ""),
        str(event.get("location") or ""),
        str(metadata.get("archetype") or ""),
    ]).lower()
    tags = {str(x).lower() for x in _as_list(metadata.get("tags")) if str(x)}
    score = 0.0
    for term in terms:
        if term in title:
            score += 3.0
        elif term in body:
            score += 1.0
        if term in tags:
            score += 1.0
    if score <= 0:
        return 0.0
    start_dt = _as_utc(event.get("start_time"))
    if start_dt is not None:
        days = max(0.0, (_now_utc() - start_dt).total_seconds() / 86400.0)
        score += 2.0 / (1.0 + days / 7.0)  # 时间衰减：一周后只剩一半权重
    return score


def relevant_events(db: Any, persona_id: str, query_text: str, *, limit: int = 3) -> list[dict]:
    """相关性检索：**只返回 completed**（已发生的事实）的事件。

    打分 = title/description/location/tags 与查询的词面重合 + 时间衰减。
    若一个都没命中（例如用户聊的话题和人物生活无关），退回"最近完成的事件"，
    这样人物仍然有真实可说的东西；绝不会返回 planned / cancelled。
    """
    pid = str(persona_id or "").strip()
    if not pid:
        return []
    try:
        top = max(1, min(50, int(limit or 1)))
    except (TypeError, ValueError):
        top = 3
    pool = _query_events(
        db,
        where="persona_id=? AND event_status='completed'",
        params=[pid],
        order="start_time DESC",
        limit=max(top * 8, 24),
    )
    terms = _query_terms(query_text)
    scored = [(_relevance_score(event, terms), event) for event in pool]
    hits = [pair for pair in scored if pair[0] > 0]
    if not hits:
        return pool[:top]
    hits.sort(key=lambda pair: (-pair[0], pair[1].get("start_time") or ""))
    return [event for _score, event in hits[:top]]


def upcoming_events(db: Any, persona_id: str, *, limit: int = 2) -> list[dict]:
    """**还没发生**的最近计划（只能作为"计划"注入，不能当已发生事实）。"""
    pid = str(persona_id or "").strip()
    if not pid:
        return []
    now_iso = iso(_now_utc())
    return _query_events(
        db,
        where="persona_id=? AND event_status='planned' AND start_time>?",
        params=[pid, now_iso],
        order="start_time ASC",
        limit=limit,
    )


def _local_when(event: dict) -> str:
    """事件时间 → 人物本地 ``"Mon 14:00"``（prompt 里给人读的时间）。

    ``event`` 可能是 :func:`_row_to_event` 解析过的 dict，也可能是 ``dict(row)``
    这种「``metadata`` 仍是 JSON 字符串」的原始 DB 行，必须都支持。
    """
    start_dt = _as_utc(event.get("start_time"))
    if start_dt is None:
        return ""
    metadata = _as_dict(event.get("metadata"))
    tz_name = str(metadata.get("timezone") or DEFAULT_TZ)
    local = start_dt.astimezone(_zone(tz_name))
    return local.strftime("%a %H:%M")


def event_fact_block(event: dict, *, include_status: bool = True) -> str:
    """单条事件 → prompt 用的一行事实。

    - ``planned`` → **永远**以 ``"PLAN (not happened yet): "`` 开头（安全不变式：
      即使 ``include_status=False`` 也不能让计划被读成事实）；
    - ``completed`` → ``"DONE: "``（``include_status=False`` 时去掉前缀）；
    - ``ongoing`` → ``"HAPPENING NOW: "``；
    - ``cancelled`` / 空事件 → 返回 ``""``（调用方过滤）。

    ``event`` 既可以是 :func:`_row_to_event` 的结果，也可以是 ``persona_events``
    的原始行（``metadata`` / ``participants`` 是 JSON 字符串）——外部调用方
    （``persona_context`` / ``event_posts`` / 集成测试）两种都会传。
    """
    if not isinstance(event, dict) or not event:
        return ""
    status = str(event.get("event_status") or "planned")
    if status == "cancelled":
        return ""
    title = str(event.get("title") or "").strip()
    if not title:
        return ""
    metadata = _as_dict(event.get("metadata"))
    detail: list[str] = []
    when = _local_when(event)
    if when:
        detail.append(when)
    location = str(event.get("location") or "").strip()
    if location:
        detail.append(f"at {location}")
    names = [str(x) for x in _as_list(metadata.get("participant_names")) if str(x)]
    if names:
        detail.append("with " + ", ".join(names[:3]))
    description = str(event.get("description") or "").strip()
    body = title + (f" ({', '.join(detail)})" if detail else "")
    if description and description != title:
        body = f"{body} — {_clip(description, 180)}"
    if status == "planned":
        return f"PLAN (not happened yet): {body}"
    if status == "ongoing":
        return f"HAPPENING NOW: {body}" if include_status else body
    prefix = "DONE: " if include_status else ""
    return f"{prefix}{body}"


# --------------------------------------------------------------------------- #
# LLM 叙事事件
# --------------------------------------------------------------------------- #
_NARRATIVE_PROMPT = """You are writing ONE ordinary day in the private life of a persona \
from a language-learning social app.

Persona
- name: {nickname}
- age: {age}
- job: {occupation}
- lives in: {city}, {country}
- speaks: {language}
- bio: {bio}
- personality: {personality}
- interests: {interests}
- local date: {day}

Today they already have this fixed routine (do NOT repeat or overlap these):
{blocks}

People they actually know (participants may ONLY be these ids, or an empty list):
{acquaintances}

Write {max_events} event(s) at most, and only events worth telling a friend about.
Allowed event_type values: {types}
Rules:
1. Stay inside their job, city, age and interests. Ordinary, believable life:
   small wins, an argument, a trip, a setback, a good evening. No fame, no fantasy,
   no crime, nothing that needs money they do not have.
2. Times are LOCAL 24h "HH:MM", inside {day}, and end_time must be later than start_time.
   Events may not overlap the routine above or each other.
3. participants: ids from the acquaintance list only. Solo events are fine.
4. Some acquaintances are marked "(currently a bit tense with you)". Do NOT write those
   people into cheerful shared activities (no happy trips, parties or dinners together).
   If you use them, write the tension instead: a disagreement, an unanswered message,
   avoiding each other. Other acquaintances can appear in normal friendly events.
5. Fewer events (or zero) is perfectly fine - quality over quantity.
6. title <= 80 chars, description <= 200 chars. English is fine: this is an internal
   fact sheet, another module re-writes it in the persona's own language.

Output JSON only:
{{"events":[{{"event_type":"achievement","title":"...","description":"...",
 "start_time":"HH:MM","end_time":"HH:MM","participants":[],"location":"...",
 "visibility":"private"}}]}}
"""

#: LLM 没给时间时的兜底时长（分钟）
_DEFAULT_DURATION_MIN: dict[str, int] = {
    "achievement": 120, "conflict": 60, "social": 150, "entertainment": 150,
    "travel": 240, "relationship": 90, "personal_milestone": 120,
    "unexpected_event": 90,
}

#: LLM 没给时间时的候选开始小时（本地时间，避开深夜与"正好在上班"的高冲突时段）
_FALLBACK_START_HOURS: tuple[int, ...] = (9, 11, 14, 16, 19, 20)


def _narrative_prompt(
    persona: dict, day: str, acquaintances: list[dict], blocks: list[dict], max_events: int
) -> str:
    routine = "\n".join(
        f"- {block.get('start')}-{block.get('end')} {block.get('label')}"
        for block in blocks
        if isinstance(block, dict)
    ) or "- (no fixed routine)"

    def _person_line(item: dict) -> str:
        line = (
            f"- id={item['id']} — {item['nickname']}"
            f" ({item.get('relation_type')}, closeness {item.get('closeness')})"
        )
        # tension >= 3 的熟人：明确标记"最近有点别扭"，避免被写成愉快共同活动
        try:
            tense = int(item.get("tension") or 0) >= TENSE_PROMPT_THRESHOLD
        except (TypeError, ValueError):
            tense = False
        if tense:
            line += " (currently a bit tense with you)"
        return line

    people = "\n".join(_person_line(item) for item in acquaintances) or (
        "- (nobody listed: participants must be an empty list)"
    )
    return _NARRATIVE_PROMPT.format(
        nickname=_clip(persona.get("nickname") or "someone", 40),
        age=persona.get("age") or 27,
        occupation=_clip(persona.get("occupation") or "unknown", 60),
        city=_clip(persona.get("city") or "somewhere", 40),
        country=_clip(persona.get("country") or "", 40),
        language=str(persona.get("language") or "en"),
        bio=_clip(persona.get("bio") or "", 240),
        personality=_clip(persona.get("personality") or "", 240),
        interests=", ".join(sorted(_interest_set(persona))) or "unknown",
        day=day,
        blocks=routine,
        acquaintances=people,
        max_events=max(1, int(max_events)),
        types=", ".join(NARRATIVE_EVENT_TYPES),
    )


def _parse_start(raw: Any) -> tuple[int, int] | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        hour = int(raw)
        return (hour, 0) if 0 <= hour <= 23 else None
    text = str(raw).strip()
    if not text:
        return None
    if "T" in text or " " in text:  # 可能是完整 ISO 时间
        parsed = _as_utc(text)
        if parsed is not None:
            local_tz = timezone.utc
            local = parsed.astimezone(local_tz)
            return (local.hour, local.minute)
    total = _hhmm_to_min(text)
    if total is None:
        return None
    return (total // 60, total % 60)


def _coerce_narrative_event(
    raw: Any,
    persona: dict,
    day: str,
    index: int,
    acquaintances: list[dict],
    *,
    rng: random.Random | None = None,
) -> dict | None:
    """把 LLM 返回的一条原始事件校验/规范成事件 dict；不可用返回 None。

    校验点（LLM 会撒谎，必须挡住）：类型白名单、时间合法且 end>start、
    参与者只能是熟人、标题非空。
    """
    if not isinstance(raw, dict):
        return None
    event_type = str(raw.get("event_type") or "").strip().lower()
    if event_type not in NARRATIVE_EVENT_TYPES:
        return None
    pid = str(persona.get("id") or "").strip()
    if not pid:
        return None
    tz_name = str(persona.get("timezone") or DEFAULT_TZ)
    tz = _zone(tz_name)
    try:
        target = date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return None

    start = _parse_start(raw.get("start_time"))
    if start is None:
        rng = rng or random.Random(0)
        hour = rng.choice(_FALLBACK_START_HOURS)
        start = (hour, rng.choice((0, 30)))
    start_min = start[0] * 60 + start[1]
    end = _parse_start(raw.get("end_time"))
    duration = _DEFAULT_DURATION_MIN.get(event_type, 90)
    end_min = end[0] * 60 + end[1] if end is not None else start_min + duration
    if end_min <= start_min:
        end_min = start_min + duration
    end_min = min(24 * 60 - 1, end_min)
    if end_min <= start_min:
        return None

    start_local = datetime.combine(
        target, dtime(hour=start_min // 60, minute=start_min % 60), tzinfo=tz
    )
    end_local = datetime.combine(
        target, dtime(hour=end_min // 60, minute=end_min % 60), tzinfo=tz
    )
    start_utc, end_utc = _as_utc(start_local), _as_utc(end_local)
    if start_utc is None or end_utc is None:
        return None
    start_iso, end_iso = iso(start_utc), iso(end_utc)

    title = _clip(raw.get("title") or "", 160)
    if not title:
        return None
    description = _clip(raw.get("description") or "", 600)

    # 参与者：只认熟人（LLM 常把名字/id 混着写，两者都匹配）
    by_id = {item["id"]: item for item in acquaintances}
    by_name = {str(item["nickname"]).strip().lower(): item for item in acquaintances}
    participants: list[str] = []
    names: list[str] = []
    raw_people = raw.get("participants")
    if isinstance(raw_people, str):
        raw_people = [raw_people]
    if not isinstance(raw_people, list):
        raw_people = []
    for person in raw_people[:6]:
        key = str(person or "").strip()
        if not key:
            continue
        match = by_id.get(key) or by_name.get(key.lower())
        if match is None:
            continue
        if match["id"] in participants:
            continue
        participants.append(match["id"])
        names.append(str(match["nickname"]))

    visibility = str(raw.get("visibility") or "").strip().lower()
    if visibility not in ("private", "public"):
        public_types = ("achievement", "social", "travel", "personal_milestone")
        visibility = "public" if event_type in public_types else "private"
    # 地点只认 LLM 明确给出的具体地点；不拿城市名兜底（否则 prompt 里会出现 "at Lisbon"）
    location = _clip(raw.get("location") or "", 120)

    return {
        "persona_id": pid,
        "event_type": event_type,
        "title": title,
        "description": description,
        "start_time": start_iso,
        "end_time": end_iso,
        "participants": participants,
        "location": location,
        "related_personas": list(participants),
        "event_status": "planned",
        "source_type": "llm",
        "source_reference": "llm:life_events",
        "visibility": visibility,
        "metadata": {
            "timezone": tz_name,
            "generated_by": "llm",
            "participant_names": names,
            "index": index,
        },
        "dedupe_key": f"{pid}:{event_type}:{day}:{index}",
        "day": str(day),
    }


def _events_on_day(db: Any, day: str, persona_ids: Sequence[str]) -> list[dict]:
    """某天、某些人物主体的事件（冲突检测用；参与者重叠在 detect_conflicts 里判）。"""
    pids = [str(pid) for pid in dict.fromkeys(persona_ids) if str(pid)]
    if db is None or not pids:
        return []
    placeholders = ",".join("?" for _ in pids)
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                f"SELECT * FROM persona_events WHERE day=? AND persona_id IN ({placeholders})",
                (str(day), *pids),
            ).fetchall()
    except sqlite3.Error as exc:
        log.debug("读当天事件失败（忽略）: %s", exc)
        return []
    return [_row_to_event(row) for row in rows]


async def generate_narrative_events(
    db: Any,
    llm: Any,
    persona: dict,
    day: str,
    *,
    rng: random.Random | None = None,
    max_events: int = 2,
) -> list[dict]:
    """LLM 生成 0..``max_events`` 条有叙事价值的事件（**每人每天最多 1 次调用**）。

    约束：
    - ``SE_LIFE_LLM`` 关闭 / 没有 llm / mock llm → 返回 ``[]``（上层用规则兜底）；
    - 事件类型必须在 :data:`NARRATIVE_EVENT_TYPES` 内；
    - 参与者必须来自 ``persona_relationships`` 里的熟人（不认识的会被丢掉）；
    - 不能和当天已有事件（本人或熟人）时间冲突，也不能互相冲突；
    - 返回**未落库**的事件 dict，由 :func:`add_event` 幂等写入。
    """
    if not life_llm_enabled() or llm is None or getattr(llm, "uses_mock", False):
        return []
    persona = persona if isinstance(persona, dict) else {}
    pid = str(persona.get("id") or "").strip()
    if not pid:
        return []
    try:
        target = date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return []
    tz_name = str(persona.get("timezone") or DEFAULT_TZ)
    persona = {**persona, "timezone": tz_name}

    acquaintances = _acquaintances(db, pid)
    schedule = _schedule_from_persona(persona)
    blocks = (
        schedule.get("weekday_blocks") if target.weekday() <= 4 else schedule.get("weekend_blocks")
    ) or []
    try:
        limit = max(1, min(3, int(max_events or 1)))
    except (TypeError, ValueError):
        limit = 2

    prompt = _narrative_prompt(persona, str(day), acquaintances, blocks, limit)
    from app.v2.llm_json import complete_json_safe  # noqa: PLC0415 - 避免循环依赖

    try:
        data = await complete_json_safe(
            llm,
            [{"role": "user", "content": prompt}],
            schema_hint=(
                '{"events":[{"event_type","title","description","start_time",'
                '"end_time","participants","location","visibility"}]}'
            ),
            fallback=None,
            temperature=0.9,
            max_tokens=900,
        )
    except Exception as exc:  # noqa: BLE001 - LLM 失败 = 没有叙事事件，不是错误
        log.debug("叙事事件生成失败（回落到规则事件）: %s", exc)
        return []

    raw_events: Any = data
    if isinstance(data, dict):
        raw_events = data.get("events")
    if not isinstance(raw_events, list):
        return []

    existing = _events_on_day(db, str(day), [pid] + [item["id"] for item in acquaintances])
    accepted: list[dict] = []
    for index, raw in enumerate(raw_events[:limit], start=1):
        event = _coerce_narrative_event(raw, persona, str(day), index, acquaintances, rng=rng)
        if event is None:
            continue
        pool = [
            item for item in (existing + accepted)
            if str(item.get("dedupe_key") or "") != event["dedupe_key"]
        ]
        involved = [pid] + list(event["participants"])
        if detect_conflicts(pool, involved, event["start_time"], event["end_time"]):
            log.debug("叙事事件与既有安排冲突，丢弃: %s", event["title"])
            continue
        accepted.append(event)
    return accepted


# --------------------------------------------------------------------------- #
# 批量生成（幂等）与后台循环
# --------------------------------------------------------------------------- #
def _read_meta(db: Any, key: str) -> str:
    if db is None:
        return ""
    try:
        with db._lock:  # noqa: SLF001
            row = db.conn.execute("SELECT value FROM v3_meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else ""
    except sqlite3.Error as exc:
        log.debug("读 v3_meta 失败（忽略）: %s", exc)
        return ""


def _write_meta(db: Any, key: str, value: str) -> bool:
    if db is None:
        return False
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO v3_meta (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        return True
    except sqlite3.Error as exc:
        log.warning("写 v3_meta 失败（忽略）: %s", exc)
        return False


def _day_event_marker(marker: str) -> dict:
    try:
        data = json.loads(marker) if marker else {}
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _marker_is_usable(marker: str) -> bool:
    """幂等标记是否可信：必须是「**至少一个人物、至少一条事件**」的批次。

    历史 bug 会留下 ``{"events": 0, "personas": 0}`` 这种"空批次"标记
    （老库 ``lifecycle`` 过滤一条都没匹配上时写的）。如果继续信它，
    当天就永久跳过、人物永远没有生活。因此空批次一律视为**不可信**，
    下次调用重新尝试；真正的生成结果会覆盖这个标记。
    """
    if not marker:
        return False
    data = _day_event_marker(marker)
    try:
        return int(data.get("personas") or 0) > 0 and int(data.get("events") or 0) > 0
    except (TypeError, ValueError):
        return False


async def generate_day_events(
    db: Any,
    llm: Any,
    *,
    day: str | None = None,
    persona_ids: list[str] | None = None,
    force: bool = False,
) -> dict:
    """批量：给人物生成当天事件（规则打底 + LLM 叙事），**同一天只跑一次**。

    - 幂等标记：``v3_meta['life_events_day:<day>']``；``force=True`` 强行重跑，
      但 ``dedupe_key`` 已存在的事件不会重复写入（所以 ``created`` 可能仍是 0）。
    - **没有目标人物时不写幂等标记**（返回 ``created=0, skipped=False``，下次仍会重试），
      避免老库迁移/人物导入晚一步就永久跳过当天。
    - LLM 每个 persona 最多 1 次调用；失败/关闭时只用规则事件，绝不抛异常。
    - 顺手 ``ensure_relationships``（叙事事件需要有熟人可当参与者）。
    - 结束时 ``refresh_statuses`` 把今天已经过去的时间块追认为 completed。

    返回 ``{"day","events","created","source","personas"}``：
    ``events`` = 当天库里的事件总数，``created`` = 本次新增条数，
    ``source`` = ``"llm"`` / ``"rule"`` / ``"skipped"``。
    """
    day = str(day or day_key())
    marker_key = f"{META_PREFIX}{day}"
    marker = _read_meta(db, marker_key)
    # 只信"非空批次"的标记：{"events":0,"personas":0} 是历史 bug 留下的毒标记，
    # 继续信它会导致当天永久跳过 → 忽略它重新生成（生成完会覆盖）。
    if marker and not force and _marker_is_usable(marker):
        data = _day_event_marker(marker)
        return {
            "day": day,
            "events": int(data.get("events") or 0),
            "created": 0,
            "source": "skipped",
            "personas": int(data.get("personas") or 0),
            "skipped": True,
        }
    if marker and not force:
        log.info("生活事件 %s：忽略空的幂等标记 %s，重新尝试生成", day, marker[:200])

    # 选人物：显式 id 列表 > 全部「非游客」人物
    if persona_ids:
        wanted = [str(pid) for pid in persona_ids if str(pid)]
        personas = [row for row in (_persona_row(db, pid) for pid in wanted) if row]
    else:
        personas = _permanent_personas(db)

    if not personas:
        # 没有人物可生成时**绝不能写幂等标记**：否则当天会被永久跳过，
        # 老库迁移/人物导入稍晚一步就再也补不回来了。返回 skipped=False，下次还会重试。
        log.info("生活事件 %s：没有可生成的人物，本次不写幂等标记（下次重试）", day)
        return {
            "day": day,
            "events": 0,
            "created": 0,
            "source": "rule",
            "personas": 0,
            "skipped": False,
        }

    created = 0
    used_llm = False
    by_persona: dict[str, int] = {}
    if personas:
        try:
            ensure_relationships(db, personas)
        except Exception as exc:  # noqa: BLE001 - 关系种子失败不影响规则事件
            log.debug("ensure_relationships 失败（忽略）: %s", exc)

    for persona in personas:
        pid = str(persona.get("id") or "").strip()
        if not pid:
            continue
        try:
            schedule = ensure_schedule(db, pid)
        except Exception as exc:  # noqa: BLE001
            log.debug("ensure_schedule 失败（改用内存作息）: %s", exc)
            schedule = build_schedule(persona)
        merged = {**persona, **{k: v for k, v in schedule.items() if k != "persona_id"}}
        count = 0
        for event in rule_events_for_day(merged, day):
            _event_id, was_created = add_event(db, event)
            if was_created:
                created += 1
                count += 1

        if life_llm_enabled():
            try:
                narrative = (
                    await generate_narrative_events(
                        db, llm, merged, day, max_events=NARRATIVE_EVENTS_PER_DAY_DEFAULT
                    )
                    if NARRATIVE_EVENTS_PER_DAY_DEFAULT > 0
                    else []
                )
            except Exception as exc:  # noqa: BLE001 - 静默回落规则事件
                log.debug("叙事事件异常（回落到规则事件）: %s", exc)
                narrative = []
            for event in narrative:
                _event_id, was_created = add_event(db, event)
                if was_created:
                    created += 1
                    count += 1
                    used_llm = True
        by_persona[pid] = count

    try:
        refresh_statuses(db)
    except Exception as exc:  # noqa: BLE001
        log.debug("刷新事件状态失败（忽略）: %s", exc)

    total = 0
    try:
        with db._lock:  # noqa: SLF001
            row = db.conn.execute(
                "SELECT COUNT(*) AS n FROM persona_events WHERE day=?", (day,)
            ).fetchone()
        total = int(row["n"] or 0) if row is not None else 0
    except sqlite3.Error as exc:
        log.debug("统计当天事件失败（忽略）: %s", exc)

    source = "llm" if used_llm else "rule"
    _write_meta(
        db,
        marker_key,
        json.dumps(
            {"day": day, "events": total, "source": source, "personas": len(personas),
             "per_persona": by_persona},
            ensure_ascii=False,
        ),
    )
    log.info("生活事件 %s：新增 %d 条 / 当天共 %d 条（%s）", day, created, total, source)
    return {
        "day": day,
        "events": total,
        "created": created,
        "source": source,
        "personas": len(personas),
        "skipped": False,
    }


async def life_loop(db: Any, llm: Any, *, tick_s: float = 300.0) -> None:
    """后台循环：启动先 refresh + 生成今天的事件；之后每 tick 刷新状态，跨天补生成。

    与 :func:`app.v3.daily_world.daily_loop` 同样的形态：
    第一段做初始化（失败只记日志），之后 ``while True`` 每 tick 做幂等的小事。
    幂等性由 ``v3_meta`` 标记 + ``dedupe_key`` 保证，重启/重复调用不会产生重复事件。
    """
    try:
        refresh_statuses(db)
        await generate_day_events(db, llm)
    except Exception as exc:  # noqa: BLE001 - 后台初始化失败不能杀死进程
        log.warning("生活事件初始化失败: %s", exc)

    last_day = day_key()
    interval = max(0.5, float(tick_s or 300.0))
    while True:
        try:
            await asyncio.sleep(interval)
            refresh_statuses(db)
            today = day_key()
            marker = _read_meta(db, f"{META_PREFIX}{today}")
            # 空批次标记（例如老库过滤不到人物时留下的）不算"今天生成过" → 重试
            if today != last_day or not _marker_is_usable(marker):
                await generate_day_events(db, llm, day=today)
                last_day = today
        except asyncio.CancelledError:  # pragma: no cover - 正常退出路径
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("生活事件循环异常: %s", exc)



def refresh_from_lexicon() -> dict[str, int]:
    """把 ``events.json`` / ``rhythm.json`` 应用到事件引擎（类型集合 + 每天规则事件数）。

    只改"新事件怎么产生"，不改已落库的事件；类型停用后不再生成，但历史仍在。
    """
    applied = {"event_types": 0, "rule_per_day": 0}
    try:
        from app.v3 import lexicon  # noqa: PLC0415

        events = lexicon.load("events")
    except Exception:  # noqa: BLE001
        return applied
    if isinstance(events, dict):
        types = events.get("types")
        if isinstance(types, dict) and types:
            enabled = [
                key for key, spec in types.items()
                if isinstance(spec, dict) and spec.get("enabled", True)
            ]
            if enabled:
                def _build(base: tuple[str, ...], allowed: set[str]) -> tuple[str, ...]:
                    ordered = [key for key in base if key in allowed]
                    ordered += [key for key in enabled if key in allowed and key not in ordered]
                    return tuple(ordered)

                rule = {k for k in enabled if types[k].get("source") in ("rule", "both")}
                narrative = {k for k in enabled if types[k].get("source") in ("narrative", "both")}
                globals()["EVENT_TYPES"] = _build(tuple(EVENT_TYPES), set(enabled))
                globals()["RULE_EVENT_TYPES"] = _build(tuple(RULE_EVENT_TYPES), rule)
                globals()["NARRATIVE_EVENT_TYPES"] = _build(tuple(NARRATIVE_EVENT_TYPES), narrative)
                applied["event_types"] = len(globals()["EVENT_TYPES"])
    try:
        rhythm = lexicon.load("rhythm")
    except Exception:  # noqa: BLE001
        rhythm = None
    if isinstance(rhythm, dict) and isinstance(rhythm.get("params"), dict):
        raw = rhythm["params"].get("rule_events_per_day")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            globals()["RULE_EVENTS_PER_DAY_DEFAULT"] = max(1, min(8, int(raw)))
            applied["rule_per_day"] = globals()["RULE_EVENTS_PER_DAY_DEFAULT"]
        raw_narrative = rhythm["params"].get("narrative_events_per_day")
        if isinstance(raw_narrative, (int, float)) and not isinstance(raw_narrative, bool):
            globals()["NARRATIVE_EVENTS_PER_DAY_DEFAULT"] = max(0, min(6, int(raw_narrative)))
            applied["narrative_per_day"] = globals()["NARRATIVE_EVENTS_PER_DAY_DEFAULT"]
    return applied


__all__ = [
    "CLOSENESS_BUMPS",
    "DONE_STATUSES",
    "EVENT_STATUSES",
    "EVENT_TYPES",
    "MAX_CLOSENESS",
    "MAX_TENSION",
    "META_PREFIX",
    "NARRATIVE_EVENT_TYPES",
    "OPEN_STATUSES",
    "RELATION_TYPES",
    "refresh_from_lexicon",
    "RULE_EVENTS_PER_DAY_DEFAULT",
    "RULE_EVENT_TYPES",
    "TENSION_EASE_TYPES",
    "TENSION_RAISE_TYPES",
    "TENSE_PROMPT_THRESHOLD",
    "add_event",
    "build_schedule",
    "bump_relationship",
    "current_activity",
    "detect_conflicts",
    "ease_tension",
    "ensure_relationships",
    "ensure_schedule",
    "event_fact_block",
    "events_for_persona",
    "generate_day_events",
    "generate_narrative_events",
    "life_llm_enabled",
    "life_loop",
    "raise_tension",
    "refresh_statuses",
    "relationship_between",
    "relationship_summary",
    "relevant_events",
    "rule_events_for_day",
    "upcoming_events",
]
