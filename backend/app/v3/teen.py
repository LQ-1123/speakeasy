"""V3 · 青少年模式（分龄门禁、监护绑定、时长与宵禁、推送抑制）。

产品边界（对应升级方案第一期「青少年模式」）
----------------------------------------------
- 年龄核验结论存在 ``v3_age_verifications``（见 :mod:`app.v3.accounts`），**独立于**
  可编辑的资料生日；本模块只消费结论，不收集证件。
- 14–17 岁：完成监护绑定后开放普通交流；未成年账号**不投广告、不开放虚拟伴侣/亲密关系**。
- 未知年龄 / 待核验：只保留注册处理与帮助入口（内容与聊天接口统一拒绝）。
- 内测默认：每天最多 60 分钟、连续 30 分钟提醒、22:00–次日 06:00 停用；
  监护人只能收紧，不能放宽到超过内测默认（``set_minor_settings`` 校验）。
- 门禁必须同时作用于 HTTP、通话/WebSocket、后台主动消息（推送抑制）。

数据表（本模块自建，幂等）
--------------------------
``v3_guardian_links`` 监护关系（pending/active/revoked；一个未成年人只保留一条有效关系）
``v3_minor_settings`` 未成年人限制配置（时长/宵禁/时区/屏蔽人物）
``v3_minor_usage``    按本地自然日的使用量（秒 + 连续会话起点 + 最近活动）

调用约定
--------
- 所有函数都不抛裸异常给请求层：:func:`enforce` 抛 ``PermissionError(code)``，
  :func:`enforce_request` 把它翻译成 ``HTTPException(403, detail="minor:<code>")``；
- ``account is None``（旧设备身份/local）直接放行，保持本地单用户联调可用；
- ``SE_AGE_GATE=0``（默认）时只返回状态不做限制，便于分龄阶段灰度开启。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request

from app.logging_utils import get_logger
from app.v3 import accounts
from app.v3.identity import LOCAL_USER_ID, current_user_id

log = get_logger(__name__)

TABLE = "v3_minor_settings"
GUARDIAN_TABLE = "v3_guardian_links"

#: 内测默认限制（只允许监护人收紧；单位：分钟 / HH:MM 本地时间）
DEFAULTS: dict[str, Any] = {
    "daily_minutes": 60,
    "session_minutes": 30,
    "curfew_start": "22:00",
    "curfew_end": "06:00",
    "timezone": "Asia/Shanghai",
    "blocked_personas": [],
}

#: 连续会话判定：两次活动间隔超过该秒数视为新会话
SESSION_GAP_S = 600
#: 单次心跳最多计入的秒数（客户端长时间挂起不应把整段空档算成使用）
MAX_HEARTBEAT_S = 180

DDL = """
CREATE TABLE IF NOT EXISTS v3_guardian_links (
    id TEXT PRIMARY KEY,
    guardian_user_id TEXT NOT NULL DEFAULT '',
    minor_user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    method TEXT NOT NULL DEFAULT 'ops',
    confirm_code TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL DEFAULT '',
    UNIQUE(minor_user_id)
);
CREATE INDEX IF NOT EXISTS idx_guardian_guardian ON v3_guardian_links(guardian_user_id, status);
CREATE TABLE IF NOT EXISTS v3_minor_settings (
    user_id TEXT PRIMARY KEY,
    daily_minutes INTEGER NOT NULL DEFAULT 60,
    session_minutes INTEGER NOT NULL DEFAULT 30,
    curfew_start TEXT NOT NULL DEFAULT '22:00',
    curfew_end TEXT NOT NULL DEFAULT '06:00',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    blocked_personas TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v3_minor_usage (
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    continuous_started_at TEXT NOT NULL DEFAULT '',
    last_turn_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, day)
);
"""


# --------------------------------------------------------------------------- #
# 时间/存储工具
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S+00:00").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return ZoneInfo(name or DEFAULTS["timezone"])
    except Exception:  # noqa: BLE001 - 无 tzdata 时退回 UTC
        return timezone.utc


def _local_now(settings: dict, now: datetime | None = None) -> datetime:
    return (now or _now()).astimezone(_zone(str(settings.get("timezone") or DEFAULTS["timezone"])))


def ensure_teen_schema(db: Any) -> None:
    """幂等建表；失败只记日志（门禁不可用不应阻断启动）。"""
    if db is None:
        return
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.executescript(DDL)
        setattr(db, "_teen_schema_ready", True)
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_teen_schema 失败（分龄门禁暂不可用）: %s: %s", type(exc).__name__, exc)


def _ensure(db: Any) -> bool:
    if db is None:
        return False
    if getattr(db, "_teen_schema_ready", False):
        return True
    ensure_teen_schema(db)
    return bool(getattr(db, "_teen_schema_ready", False))


def _row_one(db: Any, sql: str, params: tuple) -> Any:
    if not _ensure(db):
        return None
    try:
        with db._lock:  # noqa: SLF001
            return db.conn.execute(sql, params).fetchone()
    except Exception as exc:  # noqa: BLE001 - 缺表降级
        log.debug("teen 读失败: %s", exc)
        return None


def _parse_hhmm(value: Any, fallback: str) -> tuple[int, int]:
    try:
        hour, minute = str(value or "").split(":")[:2]
        h, m = int(hour), int(minute)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (TypeError, ValueError):
        pass
    h, m = str(fallback).split(":")
    return int(h), int(m)


def _minutes_of_day(value: str) -> int:
    h, m = _parse_hhmm(value, "00:00")
    return h * 60 + m


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def get_minor_settings(db: Any, user_id: str) -> dict:
    uid = str(user_id or "").strip()
    row = _row_one(db, "SELECT * FROM v3_minor_settings WHERE user_id=?", (uid,)) if uid else None
    data = dict(DEFAULTS)
    if row is not None:
        data.update(
            daily_minutes=int(row["daily_minutes"] or DEFAULTS["daily_minutes"]),
            session_minutes=int(row["session_minutes"] or DEFAULTS["session_minutes"]),
            curfew_start=str(row["curfew_start"] or DEFAULTS["curfew_start"]),
            curfew_end=str(row["curfew_end"] or DEFAULTS["curfew_end"]),
            timezone=str(row["timezone"] or DEFAULTS["timezone"]),
            blocked_personas=_loads(row["blocked_personas"], []),
        )
    return data


def _loads(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(str(raw)) if str(raw or "").strip() else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def set_minor_settings(
    db: Any,
    user_id: str,
    *,
    daily_minutes: int | None = None,
    session_minutes: int | None = None,
    curfew_start: str | None = None,
    curfew_end: str | None = None,
    timezone: str | None = None,
    blocked_personas: list[str] | None = None,
) -> dict:
    """写入未成年人限制；**只能收紧**（时长≤默认、宵禁必须在默认窗口内）。"""
    if not user_id:
        raise ValueError("缺少用户")
    current = get_minor_settings(db, user_id)
    daily = int(daily_minutes if daily_minutes is not None else current["daily_minutes"])
    session = int(session_minutes if session_minutes is not None else current["session_minutes"])
    start = str(curfew_start if curfew_start is not None else current["curfew_start"]).strip()
    end = str(curfew_end if curfew_end is not None else current["curfew_end"]).strip()
    zone_name = str(timezone if timezone is not None else current["timezone"]).strip()
    blocked = blocked_personas if blocked_personas is not None else current["blocked_personas"]

    if not 5 <= daily <= int(DEFAULTS["daily_minutes"]):
        raise ValueError(f"每日时长必须在 5–{DEFAULTS['daily_minutes']} 分钟（内测上限）")
    if not 5 <= session <= max(5, int(DEFAULTS["session_minutes"])):
        raise ValueError(f"连续会话提醒必须在 5–{DEFAULTS['session_minutes']} 分钟")
    _parse_hhmm(start, DEFAULTS["curfew_start"])
    _parse_hhmm(end, DEFAULTS["curfew_end"])
    # 默认宵禁 22:00–06:00：只允许把停用窗口变长（更早开始 / 更晚结束）
    default_start = _minutes_of_day(DEFAULTS["curfew_start"])
    default_end = _minutes_of_day(DEFAULTS["curfew_end"])
    start_min = _minutes_of_day(start)
    end_min = _minutes_of_day(end)
    if start_min == end_min:
        raise ValueError("宵禁开始与结束不能相同")
    # 内测默认窗口 22:00–06:00。监护人可以把窗口改得更严格（更早开始或更晚结束），
    # 但不允许整体挪到白天而事实上取消宵禁：窗口必须覆盖默认窗口。
    def _covers(default: int, start_min: int, end_min: int) -> bool:
        if start_min < end_min:  # 同日窗口
            return start_min <= default < end_min
        return default >= start_min or default < end_min

    if not (_covers(default_start, start_min, end_min) and _covers(default_end - 1, start_min, end_min)):
        raise ValueError("宵禁窗口必须覆盖内测默认的 22:00–06:00（只能收紧）")
    if not str(zone_name):
        raise ValueError("时区不能为空")
    clean_blocked = [str(item).strip()[:64] for item in (blocked or []) if str(item).strip()][:50]
    stamp = _iso()
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO v3_minor_settings (user_id, daily_minutes, session_minutes, curfew_start,"
            " curfew_end, timezone, blocked_personas, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET daily_minutes=excluded.daily_minutes,"
            " session_minutes=excluded.session_minutes, curfew_start=excluded.curfew_start,"
            " curfew_end=excluded.curfew_end, timezone=excluded.timezone,"
            " blocked_personas=excluded.blocked_personas, updated_at=excluded.updated_at",
            (user_id, daily, session, start, end, str(zone_name)[:64],
             json.dumps(clean_blocked, ensure_ascii=False), stamp),
        )
    return get_minor_settings(db, user_id)


# --------------------------------------------------------------------------- #
# 监护关系
# --------------------------------------------------------------------------- #
def guardian_link(db: Any, minor_user_id: str) -> dict | None:
    row = _row_one(db, "SELECT * FROM v3_guardian_links WHERE minor_user_id=?", (minor_user_id,))
    return dict(row) if row is not None else None


def guardian_status(db: Any, user_id: str) -> dict:
    """自查：我是未成年人 / 我是监护人 / 都不是。"""
    link = guardian_link(db, user_id)
    if link is not None and str(link["status"]) == "active":
        return {
            "role": "minor",
            "guardian_user_id": str(link["guardian_user_id"] or ""),
            "status": "active",
            "method": str(link["method"] or ""),
        }
    row = _row_one(
        db,
        "SELECT minor_user_id, status FROM v3_guardian_links WHERE guardian_user_id=? AND status='active'",
        (user_id,),
    )
    if row is not None:
        return {"role": "guardian", "minor_user_id": str(row["minor_user_id"]), "status": "active"}
    if link is not None and str(link["status"]) == "pending":
        return {"role": "minor", "guardian_user_id": "", "status": "pending"}
    return {"role": "none", "status": "none"}


def bind_guardian(
    db: Any,
    minor_user_id: str,
    *,
    guardian_user_id: str = "",
    method: str = "ops",
    note: str = "",
    actor: str = "ops",
) -> dict:
    """运营直接绑定监护关系（内测走运营核验；guardian_user_id 可后补）。"""
    if not minor_user_id or minor_user_id == LOCAL_USER_ID:
        raise ValueError("local 身份不能绑定监护关系")
    stamp = _iso()
    # 运营代绑时可能还没有监护人账号：用 ``ops`` 作为占位标记，
    # 表示「监护关系已由运营核验」，门禁据此放行（guardian_bound=True）。
    guardian_marker = str(guardian_user_id or "").strip()[:64] or "ops"
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO v3_guardian_links (id, guardian_user_id, minor_user_id, status, method,"
            " confirm_code, note, created_at, updated_at, revoked_at)"
            " VALUES (?,?,?,'active',?,'',?,?,?,'')"
            " ON CONFLICT(minor_user_id) DO UPDATE SET guardian_user_id=excluded.guardian_user_id,"
            " status='active', method=excluded.method, note=excluded.note,"
            " updated_at=excluded.updated_at, revoked_at=''",
            ("g_" + secrets.token_hex(8), guardian_marker, minor_user_id,
             str(method)[:40], str(note)[:400], stamp, stamp),
        )
        accounts.set_age_verification(
            db, minor_user_id, age_group="teen", status="verified", method=method,
            guardian_user_id=guardian_marker, note=note, actor=actor,
        )
    log.info("监护关系已绑定: minor=%s guardian=%s method=%s", minor_user_id, guardian_user_id, method)
    return guardian_link(db, minor_user_id) or {}


def request_guardian(db: Any, minor_user_id: str, *, note: str = "") -> dict:
    """未成年人发起监护确认：生成一次性确认码（内测由运营/监护人转交）。"""
    code = secrets.token_hex(4).upper()
    stamp = _iso()
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO v3_guardian_links (id, guardian_user_id, minor_user_id, status, method,"
            " confirm_code, note, created_at, updated_at, revoked_at)"
            " VALUES (?,?,?,'pending','code',?,?,?,?,'')"
            " ON CONFLICT(minor_user_id) DO UPDATE SET status='pending', method='code',"
            " confirm_code=excluded.confirm_code, note=excluded.note,"
            " updated_at=excluded.updated_at, revoked_at=''",
            ("g_" + secrets.token_hex(8), "", minor_user_id, code, str(note)[:400], stamp, stamp),
        )
    return {"code": code, "expires_hint": "请监护人登录后在 7 天内确认"}


def confirm_guardian(db: Any, guardian_user_id: str, code: str) -> dict:
    """监护人凭确认码完成绑定（同时写入 age_verifications=teen/verified）。"""
    raw = str(code or "").strip().upper()
    if not raw or not guardian_user_id:
        raise ValueError("确认码或监护人身份缺失")
    row = _row_one(
        db,
        "SELECT * FROM v3_guardian_links WHERE confirm_code=? AND status='pending'",
        (raw,),
    )
    if row is None:
        raise ValueError("确认码无效或已使用")
    minor = str(row["minor_user_id"])
    if minor == guardian_user_id:
        raise ValueError("监护人与未成年人不能是同一账号")
    stamp = _iso()
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "UPDATE v3_guardian_links SET guardian_user_id=?, status='active', method='code',"
            " confirm_code='', updated_at=? WHERE minor_user_id=?",
            (guardian_user_id, stamp, minor),
        )
    accounts.set_age_verification(
        db, minor, age_group="teen", status="verified", method="guardian_code",
        guardian_user_id=guardian_user_id, note="监护确认码", actor=guardian_user_id,
    )
    log.info("监护确认完成: minor=%s guardian=%s", minor, guardian_user_id)
    return {"minor_user_id": minor, "guardian_user_id": guardian_user_id, "status": "active"}


def revoke_guardian(db: Any, minor_user_id: str, *, actor: str = "ops") -> bool:
    stamp = _iso()
    with db._lock, db.conn:  # noqa: SLF001
        cur = db.conn.execute(
            "UPDATE v3_guardian_links SET status='revoked', updated_at=?, revoked_at=?"
            " WHERE minor_user_id=? AND status<>'revoked'",
            (stamp, stamp, minor_user_id),
        )
        changed = int(cur.rowcount or 0)
        if changed:
            # 撤销监护不清除年龄结论：14–17 岁仍然成立，只是「暂无监护人」，
            # 门禁应立刻回到 guardian_required（而不是未知年龄）。
            db.conn.execute(
                "UPDATE v3_age_verifications SET guardian_user_id='', updated_at=?"
                " WHERE user_id=? AND age_group='teen'",
                (stamp, minor_user_id),
            )
    if changed:
        log.info("监护关系已撤销: minor=%s actor=%s", minor_user_id, actor)
    return bool(changed)


# --------------------------------------------------------------------------- #
# 使用量
# --------------------------------------------------------------------------- #
def _day_key(settings: dict, now: datetime | None = None) -> str:
    return _local_now(settings, now).date().isoformat()


def usage_for(db: Any, user_id: str, *, now: datetime | None = None) -> dict:
    settings = get_minor_settings(db, user_id)
    day = _day_key(settings, now)
    row = _row_one(
        db, "SELECT seconds, continuous_started_at, last_turn_at FROM v3_minor_usage"
        " WHERE user_id=? AND day=?", (user_id, day),
    )
    seconds = float(row["seconds"] or 0.0) if row is not None else 0.0
    started = _parse_iso(row["continuous_started_at"]) if row is not None else None
    last = _parse_iso(row["last_turn_at"]) if row is not None else None
    continuous = 0.0
    if started is not None and last is not None:
        continuous = max(0.0, ((now or _now()) - started).total_seconds())
    return {
        "day": day,
        "seconds": round(seconds, 1),
        "minutes": round(seconds / 60.0, 1),
        "continuous_seconds": round(continuous, 1),
        "continuous_minutes": round(continuous / 60.0, 1),
        "continuous_started_at": _iso(started) if started is not None else "",
        "last_turn_at": _iso(last) if last is not None else "",
    }


def record_activity(db: Any, user_id: str, *, now: datetime | None = None) -> dict:
    """记一次活动（聊天/通话/动态浏览）：累计当日秒数并维护连续会话窗口。"""
    if not user_id or user_id == LOCAL_USER_ID or not _ensure(db):
        return {}
    moment = now or _now()
    settings = get_minor_settings(db, user_id)
    day = _day_key(settings, moment)
    usage = usage_for(db, user_id, now=moment)
    last = _parse_iso(usage["last_turn_at"])
    started = _parse_iso(usage["continuous_started_at"])
    delta = 0.0
    if last is not None:
        gap = max(0.0, (moment - last).total_seconds())
        delta = min(gap, float(MAX_HEARTBEAT_S)) if gap <= SESSION_GAP_S else 0.0
    if started is None or last is None or (moment - last).total_seconds() > SESSION_GAP_S:
        started = moment  # 新会话开始（间隔超过 10 分钟）
    stamp = _iso(moment)
    start_iso = _iso(started)
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO v3_minor_usage (user_id, day, seconds, continuous_started_at, last_turn_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(user_id, day) DO UPDATE SET seconds=seconds+?,"
            " continuous_started_at=?, last_turn_at=?",
            (user_id, day, float(usage["seconds"]) + delta, start_iso, stamp,
             delta, start_iso, stamp),
        )
    return usage_for(db, user_id, now=moment)


def reset_session(db: Any, user_id: str, *, now: datetime | None = None) -> None:
    """主动结束连续会话（退出/电话挂断）；只影响连续窗口，不清当日累计。"""
    if not user_id or not _ensure(db):
        return
    settings = get_minor_settings(db, user_id)
    day = _day_key(settings, now)
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "UPDATE v3_minor_usage SET continuous_started_at='' WHERE user_id=? AND day=?",
            (user_id, day),
        )


# --------------------------------------------------------------------------- #
# 门禁判定
# --------------------------------------------------------------------------- #
def _curfew(settings: dict, local_moment: datetime) -> bool:
    start = _minutes_of_day(str(settings.get("curfew_start") or DEFAULTS["curfew_start"]))
    end = _minutes_of_day(str(settings.get("curfew_end") or DEFAULTS["curfew_end"]))
    now_min = local_moment.hour * 60 + local_moment.minute
    if start == end:
        return False
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def _seconds_until_curfew_end(settings: dict, local_moment: datetime) -> int:
    end = _minutes_of_day(str(settings.get("curfew_end") or DEFAULTS["curfew_end"]))
    local_end = local_moment.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if local_end <= local_moment:
        local_end += timedelta(days=1)
    return int((local_end - local_moment).total_seconds())


def check_access(
    db: Any,
    user_id: str,
    *,
    feature: str = "chat",
    persona_id: str = "",
    now: datetime | None = None,
) -> dict:
    """统一门禁判定（不抛异常，返回结构化结论）。"""
    uid = str(user_id or "").strip() or current_user_id()
    result: dict[str, Any] = {
        "allowed": True,
        "reason": "",
        "feature": feature,
        "age_group": "unknown",
        "gate_enforced": accounts.age_gate_enforced(),
        "guardian_bound": False,
        "reminder": False,
        "retry_after_s": 0,
        "limits": {},
        "usage": {},
    }
    if db is None or uid == LOCAL_USER_ID:
        return result
    account = accounts.account_for_user(db, uid)
    if account is None:
        # 旧设备身份：内测联调用，不做分龄门禁
        return result
    age = accounts.age_verification_for(db, uid) or {}
    group = str(age.get("age_group") or "unknown")
    verified = str(age.get("status") or "") == "verified"
    result["age_group"] = group if verified else "unknown"
    result["guardian_bound"] = bool(str(age.get("guardian_user_id") or ""))
    caps = accounts.capabilities(db, uid)
    if not accounts.age_gate_enforced():
        return result  # 分龄门禁未开启：只上报状态，不限制
    if not caps["features"].get(feature, False) or result["age_group"] == "unknown":
        result.update(
            allowed=False,
            reason="age_unverified" if result["age_group"] == "unknown" else "feature_disabled",
        )
        return result
    if result["age_group"] != "teen":
        return result

    settings = get_minor_settings(db, uid)
    usage = usage_for(db, uid, now=now)
    result["limits"] = settings
    result["usage"] = usage
    if not result["guardian_bound"]:
        result.update(allowed=False, reason="guardian_required")
        return result
    if persona_id and str(persona_id) in {str(x) for x in settings.get("blocked_personas") or []}:
        result.update(allowed=False, reason="persona_blocked")
        return result
    local_moment = _local_now(settings, now)
    if _curfew(settings, local_moment):
        result.update(
            allowed=False,
            reason="curfew",
            retry_after_s=_seconds_until_curfew_end(settings, local_moment),
        )
        return result
    if usage["seconds"] >= int(settings["daily_minutes"]) * 60:
        result.update(allowed=False, reason="daily_limit")
        return result
    if usage["continuous_seconds"] >= int(settings["session_minutes"]) * 60:
        result["reminder"] = True
    return result


def enforce(
    db: Any,
    user_id: str,
    *,
    feature: str = "chat",
    persona_id: str = "",
    now: datetime | None = None,
) -> dict:
    """门禁判定，不允许时抛 ``PermissionError(reason)``（路由层转 403）。"""
    result = check_access(db, user_id, feature=feature, persona_id=persona_id, now=now)
    if not result["allowed"]:
        raise PermissionError(result["reason"] or "minor_blocked")
    return result


async def enforce_request(
    request: Request,
    user_id: str,
    *,
    feature: str = "chat",
    persona_id: str = "",
) -> dict:
    """路由层便捷入口：从 ``app.state.v2.db`` 取库并把拒绝翻译成 403。"""
    db = getattr(getattr(request.app.state, "v2", None), "db", None)
    try:
        return enforce(db, user_id, feature=feature, persona_id=persona_id)
    except PermissionError as exc:
        reason = str(exc) or "minor_blocked"
        raise HTTPException(status_code=403, detail=f"minor:{reason}") from None


def push_suppressed(db: Any, user_id: str, *, now: datetime | None = None) -> bool:
    """后台主动消息抑制：未成年人在宵禁或超时时段不推送（留给下次活跃）。"""
    if db is None or not user_id:
        return False
    access = check_access(db, user_id, feature="chat", now=now)
    return not access["allowed"] and access["reason"] in ("curfew", "daily_limit", "guardian_required", "age_unverified")


def status(db: Any, user_id: str, *, now: datetime | None = None) -> dict:
    """客户端/运营查询用的汇总：身份、限制、用量、当前是否可用。"""
    settings = get_minor_settings(db, user_id)
    access = check_access(db, user_id, now=now)
    return {
        "user_id": user_id,
        "guardian": guardian_status(db, user_id),
        "settings": settings,
        "usage": usage_for(db, user_id, now=now),
        "access": access,
        "age": accounts.capabilities(db, user_id)["age"],
    }


__all__ = [
    "DEFAULTS",
    "bind_guardian",
    "check_access",
    "confirm_guardian",
    "enforce",
    "enforce_request",
    "ensure_teen_schema",
    "get_minor_settings",
    "guardian_link",
    "guardian_status",
    "push_suppressed",
    "record_activity",
    "request_guardian",
    "reset_session",
    "revoke_guardian",
    "set_minor_settings",
    "status",
    "usage_for",
]
