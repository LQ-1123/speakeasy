"""多用户升级 · 一期：私聊情绪按「用户 × 人物」隔离。

背景（为什么需要这张表）
------------------------
升级前，一轮私聊结束会把情绪写回 ``personas.emotion_state``。而 ``personas`` 是
**全用户共享**的人物行，于是 A 和同一个人物聊完，B 再遇到这个人物时情绪已经被
A 改过了——这既是数据隔离问题，也是人物设定问题（公共情绪应该只由世界事件驱动）。

一期把情绪拆成两层：

- **公共基线** ``personas.emotion_state``：只由世界事件链路
  （:mod:`app.v3.life_events` 里的 ``apply_event`` / ``refresh_statuses``）写入；
  人物动态、事件事实等公共内容继续读它（``event_posts.py`` 不改）。
- **关系情绪** ``persona_user_emotion(user_id, persona_id)``：只由「这个用户与这个
  人物」的私聊写入。每个用户各一份，互不可见、互不影响。

本模块只做这一件事：建表 + 读写关系情绪。聊天链路的取舍（先读关系情绪、没有记录
时回退公共基线）在 :mod:`app.v3.persona_context` 里，避免把「情绪策略」散在存储层。

建表时机
--------
表放在本模块而不是 ``v3/schema.py``（一期由 Lead 在 ``app/main.py`` 统一接线
``ensure_privacy_schema(db)``）。:func:`ensure_privacy_schema` 是幂等的；
此外首次读写关系情绪时也会惰性自愈一次，所以即使启动接线遗漏，聊天链路也不会
因为缺表而报错，只是那一轮读不到关系情绪（回退公共基线）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

#: 关系情绪表 DDL（幂等）。一期只新增这一张表，不动 `schema.py` 的公共结构。
PERSONA_USER_EMOTION_DDL = """
CREATE TABLE IF NOT EXISTS persona_user_emotion (
    user_id TEXT,
    persona_id TEXT,
    state TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, persona_id)
);
"""

#: 关系情绪表名（测试 / 排查可引用，避免两处硬编码字符串）
TABLE = "persona_user_emotion"

#: 挂在 db 实例上的「表已就绪」标记：避免每次读写都跑一遍 CREATE TABLE IF NOT EXISTS
_READY_ATTR = "_privacy_schema_ready"


def _now_text() -> str:
    """与 ``emotion.py`` / ``personas.emotion_state`` 一致的 ISO 时间戳写法。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _loads_state(raw: Any) -> dict:
    """把库里的 ``state``（JSON 文本）解成 dict；脏数据一律降级为 ``{}``。"""
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None or raw == "":
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _cell(row: Any, name: str) -> Any:
    """兼容 sqlite3.Row 与普通 tuple 两种读取方式（测试里可能换连接包装）。"""
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        try:
            return row[0]
        except (IndexError, TypeError):
            return None


def ensure_privacy_schema(db: Any) -> None:
    """幂等建表：``persona_user_emotion``（由 Lead 在 ``main.py`` lifespan 接线）。

    - 可以反复调用；表已存在时只做一次 ``CREATE TABLE IF NOT EXISTS``。
    - 任何失败只记日志、不抛异常：隐私隔离是增强能力，不能反过来阻断聊天启动。
    """
    if db is None:
        return
    try:
        with db._lock, db.conn:  # noqa: SLF001 - 与 DB 层共用连接
            db.conn.executescript(PERSONA_USER_EMOTION_DDL)
        setattr(db, _READY_ATTR, True)
    except Exception as exc:  # noqa: BLE001 - 建表失败不回退整体启动
        log.warning("ensure_privacy_schema 失败（关系情绪暂不可用）: %s: %s", type(exc).__name__, exc)


def _ensure(db: Any) -> bool:
    """确保表可用；返回是否可用（首次调用会惰性建一次表）。"""
    if db is None:
        return False
    if getattr(db, _READY_ATTR, False):
        return True
    ensure_privacy_schema(db)
    return bool(getattr(db, _READY_ATTR, False))


def get_user_persona_emotion(db: Any, user_id: str, persona_id: str) -> dict:
    """读「用户 × 人物」关系情绪；没有记录 / 读失败返回 ``{}``（调用方回退公共基线）。

    返回的是**库里的原始状态**（未 normalize / 未 decay）——规范化和衰减属于情绪
    语义，放在 :mod:`app.v3.persona_context` 的聊天计划里做，存储层保持纯粹。
    """
    uid = str(user_id or "").strip()
    pid = str(persona_id or "").strip()
    if not uid or not pid or not _ensure(db):
        return {}
    try:
        with db._lock:  # noqa: SLF001
            row = db.conn.execute(
                "SELECT state FROM persona_user_emotion WHERE user_id=? AND persona_id=?",
                (uid, pid),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - 老库没表时静默降级
        log.debug("读关系情绪失败（回退公共基线）: %s", exc)
        return {}
    if row is None:
        return {}
    return _loads_state(_cell(row, "state"))


def save_user_persona_emotion(
    db: Any, user_id: str, persona_id: str, state: dict
) -> None:
    """写「用户 × 人物」关系情绪（UPSERT，绝不触碰 ``personas.emotion_state``）。

    - 只影响 ``(user_id, persona_id)`` 这一行，天然按用户隔离；
    - 落库前补 ``updated_at``（读回来按它做时间衰减）；
    - 任何失败只记日志：情绪写回失败不该影响已经发出的回复。
    """
    uid = str(user_id or "").strip()
    pid = str(persona_id or "").strip()
    if not uid or not pid or not _ensure(db):
        return
    data = _loads_state(state)
    stamp = str(data.get("updated_at") or "").strip() or _now_text()
    data["updated_at"] = stamp
    payload = json.dumps(data, ensure_ascii=False)
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO persona_user_emotion (user_id, persona_id, state, updated_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(user_id, persona_id) DO UPDATE SET"
                " state=excluded.state, updated_at=excluded.updated_at",
                (uid, pid, payload, stamp),
            )
    except Exception as exc:  # noqa: BLE001 - 写回失败不影响聊天
        log.debug("写关系情绪失败（忽略）: %s", exc)


__all__ = [
    "PERSONA_USER_EMOTION_DDL",
    "TABLE",
    "ensure_privacy_schema",
    "get_user_persona_emotion",
    "save_user_persona_emotion",
]
