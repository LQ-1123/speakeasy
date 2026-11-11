"""管理 API · 总览（``GET /api/admin/overview``）。

一个聚合接口喂满管理台首屏（用户 / 角色 / 今日产出 / 服务健康 / 后台任务 / 推送），
避免首屏并发打 8 个请求。**只读**：不调 LLM、不写库、不改状态。

容错原则（本模块最重要的一点）
------------------------------
总览是管理台的落地页，它 500 会让整个控制台不可用——哪怕坏掉的只是"推送统计"这种
边角信息。因此**每一条查询都单独容错**：表/列不存在（极老库）、连接未就绪、聚合值
为 NULL，一律退化成 ``0`` 而不是抛异常。沿用 ``V3Service.stats_persona`` 里
``scalar()`` 的写法：捕获 ``sqlite3.Error`` → 记 debug 日志 → 返回 0。

唯一的例外是鉴权：未登录仍然 403（``deps.guard``），否则总览会泄漏用户规模与
服务配置状态。

字段口径
--------
``users.total``
    ``v3_users`` 行数（设备身份）。
``users.active_today``
    ``last_seen_at >= 今天 00:00（本地时区）`` 的用户数。``last_seen_at`` 存的是
    UTC ISO8601，所以先把本地日界换算成 UTC 再比字符串——ISO8601 UTC 字符串的
    字典序与时间序一致，比 ``date()`` 函数更精确且不受 SQLite 时区影响。
``personas.total`` / ``personas.active``
    ``personas`` 行数 / ``status='active'`` 的行数（``waiting_voice`` 等未就绪状态
    不计入 active——它们还不可用）。
``today.events``
    ``persona_events.day = 今天（本地）`` 的事件数。用 ``day`` 列而不是
    ``created_at``：``day`` 是生活事件系统的既有口径，跨时区重算才一致。
``today.posts_published``
    ``persona_posts.created_at >= 今天 00:00（本地）``。
``today.posts_pending``
    ``post_queue.status='queued'`` 的条数（P3 起该表已在 ``schema.DDL`` 里建好）。
    老库没有这张表时容错返回 0，总览不会因此 500——见 :func:`_count_pending_posts`。
``services.*``
    ``{configured, ok, source}``。``source`` 取值 ``'env' | 'admin' | ''``
    （空串 = 分不出/无配置）。``ok`` 在此接口里表示"配置齐全且无已知故障"，
    **不做联网探测**——总览每 30 秒刷新一次，探测会把首屏拖到秒级。
    真要测连通性用 ``POST /api/admin/config/{target}/test``。
``jobs.failed_recent``
    最近 5 条 ``status='failed'`` 的任务（``payload`` 不下发：可能含用户内容）。
``jobs.running`` / ``jobs.queued``
    ``running`` 为 ``status='running'``；``queued`` 为 ``status IN ('queued','retry')``
    ——``retry`` 同样是"等着被执行"，只数 ``queued`` 会让退避中的任务凭空消失。
``push.*``
    ``client_events.push_status`` 分布（``pending/sent/failed/unconfigured``）。
    ``suppressed``（用户已读/关通知）与 ``expired``（超过 1 天）不属于这四类，不返回。
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

from app.admin import config as admin_config
from app.admin import user_scope
from app.admin.api import deps
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
router = APIRouter()

#: ``jobs.failed_recent`` 的条数上限（够运维定位问题，又不会把首屏撑大）。
FAILED_RECENT_LIMIT = 5

#: ``services`` 的键顺序 = 管理台表格的行顺序。
SERVICE_KEYS: tuple[str, ...] = ("llm", "stt", "tts", "deepgram", "jpush")

# --------------------------------------------------------------------------- #
# 时间工具
# --------------------------------------------------------------------------- #
def _local_day_start_utc() -> str:
    """今天 00:00（本地时区）对应的 UTC ISO8601 字符串。

    与 ``persona_events.day`` / ``daily_world.day_key`` 用同一个时区（``SE_TZ``），
    这样「今天」在事件与总览里是同一天。时区模块不可用时退化成 UTC 当天。
    """
    try:
        from app.v3.daily_world import now_local  # noqa: PLC0415

        local_midnight = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except Exception:  # noqa: BLE001 - 时区不可用不该让总览失败
        return time.strftime("%Y-%m-%dT00:00:00+00:00", time.gmtime())

def _today_key() -> str:
    """今天的 day key（本地时区，格式 ``YYYY-MM-DD``）。"""
    try:
        from app.v3.daily_world import day_key  # noqa: PLC0415

        return str(day_key())
    except Exception:  # noqa: BLE001
        return time.strftime("%Y-%m-%d", time.gmtime())

# --------------------------------------------------------------------------- #
# 容错查询原语
# --------------------------------------------------------------------------- #
def _scalar(db: Any, sql: str, params: tuple = ()) -> int:
    """单值聚合查询；任何失败都返回 0（表不存在 / 列缺失 / 连接未就绪）。"""
    if db is None:
        return 0
    try:
        with db._lock:  # noqa: SLF001 - 与 DB 层共用同一把锁
            row = db.conn.execute(sql, params).fetchone()
        return int(row[0] or 0) if row is not None else 0
    except (sqlite3.Error, RuntimeError, AttributeError) as exc:
        log.debug("overview 查询失败（忽略）: %s", exc)
        return 0

def _rows(db: Any, sql: str, params: tuple = ()) -> list[Any]:
    """多行查询；任何失败都返回空列表。"""
    if db is None:
        return []
    try:
        with db._lock:  # noqa: SLF001
            return list(db.conn.execute(sql, params).fetchall())
    except (sqlite3.Error, RuntimeError, AttributeError) as exc:
        log.debug("overview 查询失败（忽略）: %s", exc)
        return []

def _count(db: Any, sql: str, params: tuple = ()) -> int:
    return _scalar(db, sql, params)

# --------------------------------------------------------------------------- #
# 各分区
# --------------------------------------------------------------------------- #
def _users(db: Any) -> dict[str, int]:
    """用户规模。

    口径见 ``app.admin.user_scope``：``total`` 是 ``v3_users`` 原始行数，
    ``real`` 是有使用痕迹的（设备/好友/动态/消息/moment），``empty`` 是注册后毫无
    痕迹的残留行。**别把 ``total`` 读成"多少人在用"**——客户端每次在空白
    localStorage 里启动都会新造一行（无痕窗口、重装 App、清数据），线上实测
    91 行里 75 行是残留。``registered`` 是注册过推送设备的数量，最接近
    "真的装了 App 并授权通知的人"。
    """
    scope = user_scope.counts(db)
    return {
        "total": scope["total"],
        "real": scope["real"],
        "empty": scope["empty"],
        "registered": scope["registered"],
        "active_today": _count(
            db, "SELECT COUNT(*) FROM v3_users WHERE last_seen_at >= ?", (_local_day_start_utc(),)
        ),
    }

def _personas(db: Any) -> dict[str, int]:
    return {
        "total": _count(db, "SELECT COUNT(*) FROM personas"),
        "active": _count(db, "SELECT COUNT(*) FROM personas WHERE status='active'"),
    }

def _count_pending_posts(db: Any) -> int:
    """待发动态数：``post_queue`` 里 ``status='queued'`` 的条数。

    P3 起 ``post_queue`` 已在 ``app.v3.schema.DDL`` 里建好（契约 §2 的关键决策——
    待发队列用独立表，不给 ``persona_posts`` 加状态列），所以这里直接计数。
    极老库没有该表时 ``_count`` 的容错会返回 0，总览不会因此 500。
    """
    return _count(db, "SELECT COUNT(*) FROM post_queue WHERE status='queued'")

def _today(db: Any) -> dict[str, int]:
    day = _today_key()
    day_start = _local_day_start_utc()
    events = _count(db, "SELECT COUNT(*) FROM persona_events WHERE day=?", (day,))
    if events == 0:
        # 老库可能没有 day 列（或 day 全空）：按创建时间兜底，只补不回退结果。
        # 注意这条只在主查询为 0 时才跑，避免正常库多付一次查询。
        events = _count(
            db, "SELECT COUNT(*) FROM persona_events WHERE created_at >= ?", (day_start,)
        )
    return {
        "events": events,
        "posts_pending": _count_pending_posts(db),
        "posts_published": _count(
            db, "SELECT COUNT(*) FROM persona_posts WHERE created_at >= ?", (day_start,)
        ),
    }

def _source(value: Any) -> str:
    """把各配置存储的来源标记统一成 ``'env' | 'admin' | ''``。

    现存标记有三种写法：LLM/STT 用 ``env`` / ``file``，Deepgram 用 ``env`` /
    ``app`` / ``none``。对外只暴露契约里的三个值，其余（含缺失）一律空串。
    """
    raw = str(value or "").strip().lower()
    if raw in ("file", "app", "admin"):
        return "admin"
    if raw == "env":
        return "env"
    return ""

def _tts_from_admin_file(services_obj: Any) -> bool:
    """TTS 配置是否由管理台写过。

    ``TTSConfigStore`` 只读 ``<data_dir>/tts_config.json``，**没有 env 回退**；
    所以"文件存在"≈"管理台配置过"，文件缺失则是代码默认值（provider=edge）。
    """
    try:
        engines = getattr(services_obj, "engines", None)
        data_dir = str(getattr(getattr(engines, "settings", None), "data_path", "") or "")
        if not data_dir:
            return False
        return os.path.exists(os.path.join(data_dir, "tts_config.json"))
    except Exception:  # noqa: BLE001
        return False

def _jpush_configured() -> bool:
    """JPush 是否配置：本期只读环境变量（管理台写入的密钥是后续期的事）。"""
    key = str(os.getenv("JPUSH_APP_KEY", "") or "").strip()
    secret = str(os.getenv("JPUSH_MASTER_SECRET", "") or "").strip()
    return bool(key and secret)

def _status_entry(configured: bool, ok: bool, source: str) -> dict[str, Any]:
    return {"configured": bool(configured), "ok": bool(ok), "source": _source(source)}

def _services(request: Request) -> dict[str, dict[str, Any]]:
    """五个外部服务的配置状态（**不做联网探测**，见模块 docstring）。"""
    services_obj = getattr(request.app.state, "v2", None)
    snapshot: dict[str, Any] = {}
    try:
        snapshot = admin_config.snapshot(services_obj)
    except Exception as exc:  # noqa: BLE001 - 快照失败也要给出完整五项
        log.debug("overview: 配置快照失败（忽略）: %s", exc)
        snapshot = {}

    llm = snapshot.get("llm") if isinstance(snapshot.get("llm"), dict) else {}
    stt = snapshot.get("stt") if isinstance(snapshot.get("stt"), dict) else {}
    tts = snapshot.get("tts") if isinstance(snapshot.get("tts"), dict) else {}
    deepgram = snapshot.get("deepgram") if isinstance(snapshot.get("deepgram"), dict) else {}
    fish = snapshot.get("fish") if isinstance(snapshot.get("fish"), dict) else {}

    llm_configured = bool(llm.get("configured"))
    stt_configured = bool(stt.get("configured"))
    tts_configured = bool(tts.get("configured"))

    # ok = 配置齐全 + 无已知故障。TTS 用 Fish 时再看熔断器状态：
    # Fish 连续失败会熔断降级到 Edge，此时"配置了但不 ok"才是事实。
    tts_provider = str(tts.get("provider") or "")
    fish_reachable = bool(fish.get("reachable", True))
    tts_ok = tts_configured and (tts_provider != "fish" or fish_reachable)

    if not deepgram:
        # 快照可能整体失败（引擎未装配）；Deepgram 状态不依赖引擎，单独取一次。
        try:
            from app.voice.deepgram_stream import deepgram_status  # noqa: PLC0415

            deepgram = deepgram_status()
        except Exception as exc:  # noqa: BLE001
            log.debug("overview: deepgram 状态不可用（忽略）: %s", exc)
            deepgram = {}

    jpush_configured = _jpush_configured()

    return {
        "llm": _status_entry(
            llm_configured, llm_configured and "error" not in llm, llm.get("source", "")
        ),
        "stt": _status_entry(
            stt_configured, stt_configured and "error" not in stt, stt.get("source", "")
        ),
        "tts": _status_entry(
            tts_configured,
            tts_ok,
            "admin" if _tts_from_admin_file(services_obj) else "",
        ),
        "deepgram": _status_entry(
            bool(deepgram.get("configured") or deepgram.get("available")),
            bool(deepgram.get("configured") or deepgram.get("available")),
            deepgram.get("source", ""),
        ),
        "jpush": _status_entry(jpush_configured, jpush_configured, "env" if jpush_configured else ""),
    }

def _jobs(db: Any) -> dict[str, Any]:
    """后台任务概况：最近失败 + 运行中 + 待执行。"""
    failed_recent: list[dict[str, Any]] = []
    for row in _rows(
        db,
        "SELECT id, kind, status, attempts, last_error, updated_at, created_at"
        " FROM background_jobs WHERE status='failed' ORDER BY updated_at DESC LIMIT ?",
        (FAILED_RECENT_LIMIT,),
    ):
        try:
            failed_recent.append(dict(row))
        except (TypeError, ValueError):  # pragma: no cover - 非 Row 实现
            continue
    return {
        "failed_recent": failed_recent,
        "running": _count(db, "SELECT COUNT(*) FROM background_jobs WHERE status='running'"),
        # retry 也在等着被执行（退避窗口内），只数 queued 会漏掉它们。
        "queued": _count(
            db, "SELECT COUNT(*) FROM background_jobs WHERE status IN ('queued','retry')"
        ),
    }

def _push(db: Any) -> dict[str, int]:
    """推送送达统计（``client_events.push_status`` 分布）。"""
    counts = {"pending": 0, "sent": 0, "failed": 0, "unconfigured": 0}
    for row in _rows(db, "SELECT push_status, COUNT(*) FROM client_events GROUP BY push_status"):
        try:
            status = str(row[0] or "")
            value = int(row[1] or 0)
        except (IndexError, TypeError, ValueError):  # pragma: no cover
            continue
        if status in counts:
            counts[status] = value
    return counts

# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@router.get("/overview")
async def admin_overview(request: Request) -> dict:
    """管理台总览快照（只读，全字段容错）。"""
    db = deps.db(request)
    return {
        "users": _users(db),
        "personas": _personas(db),
        "today": _today(db),
        "services": _services(request),
        "jobs": _jobs(db),
        "push": _push(db),
        "server_time": time.time(),
        "server_time_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

__all__ = ["SERVICE_KEYS", "router"]
