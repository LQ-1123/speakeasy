"""管理 API · 用户管理（``/api/admin/users*``）—— 第 3 期（P3）。

接口
----
``GET    /users``                                   用户列表（搜索 / 分页 / 排序）
``GET    /users/{uid}``                             用户详情（画像 / 设备 / 好友 / 动态）
``GET    /users/{uid}/candidate-personas``          还没加为好友的角色（带匹配提示）
``POST   /users/{uid}/friends``                     好友指派（申请 / 直接成为好友）
``DELETE /users/{uid}/friends/{friendship_id}``     解除好友
``POST   /users/{uid}/push-test``                   给该用户的已启用设备发测试推送

数据来源（全部是**现有**表，本模块不建表）
------------------------------------------
- 身份：``v3_users``（``device_key`` 只回尾 6 位，绝不整串下发）
- 设备：``client_devices``（由 ``app.v3.client_api.ensure_client_schema`` 建）
- 关系：``friendships``（``status ∈ accepted/pending``）+ V2 ``friends``（聊天主体）
- 画像：``db.get_profile(user_id=...)``（V2 ``user_profiles``）
- 用户自己的动态：``user_posts``
- 通知/推送：**复用** ``social_events`` → ``app.v3.client_events`` outbox → JPush 这条现有链路

复用而不是另写
--------------
- 直接加好友 / 解除好友：复用 ``V3Service.add_friend`` / ``V3Service.remove_friend``
  （它们同时维护 V2 ``friends`` 行，聊天侧才看得到这个好友）
- 好友申请：复用 ``V3Service._ensure_request_friend``（``friendships.status='pending'``
  + ``friends.status='pending_request'``），再补一条 ``social_events`` 事件让用户收到通知
  ——这正是现有「AI 主动加好友」走的那条通道（见 ``service._materialize_one``）。
- 测试推送：现有 ``client_events.dispatch_push`` 只处理 outbox 队列，没有"发一条任意推送"
  的公共函数，所以这里按它的**同一份请求形状**（同端点 / 同 auth / 同 android 结构）
  发一次；凭据统一走 ``app.v3.push_config.effective()``（env > 管理台 > .env），
  不自己读环境变量，否则管理台改的推送配置会不生效。

失败约定
--------
业务失败统一 ``200 + {"ok": false, "issues": [...]}``；只有参数本身非法（缺必填、
枚举越界）才 ``400``。数据库未就绪同样返回 200 + ok=false，不抛 500。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Query, Request

from app.admin.api import deps
from app.admin import user_scope
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
router = APIRouter(prefix="/users")

#: 数据库未就绪时的统一降级响应（沿用项目既有 200 + ok=False 约定）。
_DB_NOT_READY: dict[str, Any] = {"ok": False, "issues": ["数据库未就绪"]}

#: 允许的排序字段（对应三条 SQL 排序表达式，避免把用户输入拼进 ORDER BY）。
_SORTS: dict[str, str] = {
    "last_seen": "COALESCE(NULLIF(u.last_seen_at, ''), u.created_at) DESC, u.id",
    "created": "u.created_at DESC, u.id",
    "posts": "post_count DESC, COALESCE(NULLIF(u.last_seen_at, ''), u.created_at) DESC",
}

#: 设备号只回尾号，避免管理台把可伪造身份的整串设备号复制出去。
DEVICE_TAIL_CHARS = 6

#: 好友指派的两个模式。
FRIEND_MODES: tuple[str, ...] = ("request", "direct")

#: JPush 推送端点（与 ``app.v3.client_events.dispatch_push`` 同一个）。
JPUSH_PUSH_URL = "https://api.jpush.cn/v3/push"

# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

def _iso_from_epoch(value: Any) -> str:
    """``client_devices.updated_at`` 是 REAL 时间戳 → ISO 字符串（解析失败原样返回）。"""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
    except (TypeError, ValueError, OSError):
        return str(value or "")

def _device_tail(device_key: Any) -> str:
    """设备号尾号（只回尾 6 位；空值回空串）。"""
    value = str(device_key or "").strip()
    return value[-DEVICE_TAIL_CHARS:] if value else ""

def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return default

def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)

def _service(request: Request) -> Any:
    """V3 服务（``app.state.v3``）；未装配返回 ``None``。"""
    return getattr(request.app.state, "v3", None)

def _audit(request: Request, action: str, target: str, summary: str) -> None:
    """写操作的审计三元组（由 ``require_admin`` 那条链自动落库）。"""
    request.state.audit_action = action
    request.state.audit_target = target
    request.state.audit_summary = summary

def _persona_avatar(persona: Any) -> str:
    """角色头像 URL（Avataaars 兜底 SVG）；缺字段时回空串，绝不抛错。"""
    try:
        from app.v3.avatar import persona_avatar_url  # noqa: PLC0415

        return persona_avatar_url(dict(persona))
    except Exception:  # noqa: BLE001 - 头像失败不能拖垮列表
        return ""

def _persona_exists(db: Any, persona_id: str) -> bool:
    with db._lock:  # noqa: SLF001 - 与 DB 层共用同一把锁
        row = db.conn.execute("SELECT 1 FROM personas WHERE id=?", (persona_id,)).fetchone()
    return row is not None

def _user_exists(db: Any, user_id: str) -> bool:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT 1 FROM v3_users WHERE id=?", (user_id,)).fetchone()
    return row is not None

def _user_interests(profile: dict[str, Any]) -> set[str]:
    """用户兴趣（``fact_sheet.interests``，大小写归一）。"""
    try:
        fact = profile.get("fact_sheet") or {}
        raw = fact.get("interests") or []
        return {str(item).strip().lower() for item in raw if str(item).strip()}
    except (TypeError, ValueError, AttributeError):
        return set()

def _user_city(profile: dict[str, Any]) -> str:
    try:
        return str((profile.get("fact_sheet") or {}).get("profile", {}).get("city") or "").strip()
    except (TypeError, ValueError, AttributeError):
        return ""

def _user_languages(profile: dict[str, Any]) -> set[str]:
    """用户会/在学的语言（母语 + 目标语言）。"""
    langs: set[str] = set()
    native = str(profile.get("native_language") or "").strip().lower()
    if native:
        langs.add(native)
        langs.add(native.split("-")[0])
    targets = profile.get("target_languages") or []
    if isinstance(targets, str):
        targets = [targets]
    for item in targets:
        text = str(item or "").strip().lower()
        if text:
            langs.add(text)
            langs.add(text.split("-")[0])
    return langs

# --------------------------------------------------------------------------- #
# 用户列表
# --------------------------------------------------------------------------- #
@router.get("")
async def list_users(
    request: Request,
    search: str = Query("", description="按 id / 昵称 / 设备号模糊搜索"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    sort: str = Query("last_seen", description="last_seen | created | posts"),
    scope: str = Query("all", description="all | real（有使用痕迹）| empty（仅注册未使用）"),
) -> dict:
    """用户列表：搜索 + 分页 + 排序 + 口径过滤，带动态/好友/设备计数。

    ``device_key`` 只回尾 6 位（``device_key_tail``），完整设备号不出管理台。

    ``scope`` 的口径定义在 ``app.admin.user_scope``（与总览共用同一份判定）。
    默认 ``all`` 保持原行为；``real`` / ``empty`` 用于把"注册残留"和"真用过的"
    分开看——线上 ``v3_users`` 的行数远大于人数，混在一起会误导。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    sort_value = str(sort or "last_seen").strip()
    if sort_value not in _SORTS:
        raise HTTPException(
            status_code=400,
            detail=f"sort 不合法: {sort_value}（可选 {', '.join(_SORTS)}）",
        )
    scope_value = str(scope or "all").strip()
    if scope_value not in user_scope.SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope 不合法: {scope_value}（可选 {', '.join(user_scope.SCOPES)}）",
        )

    # 有使用痕迹的用户 id：逐表查询（单表缺失只少一个信号），再交给 SQL 过滤。
    # 用户表规模很小（管理台场景），显式 id 集合比把 5 张表的 EXISTS 拼进一条
    # SQL 更可控——任意一张表在老库里缺失时，那条大 SQL 会整体报错。
    real: set[str] = user_scope.real_ids(db) if scope_value != "all" else set()

    where = ""
    params: list[Any] = []
    clauses: list[str] = []

    query = str(search or "").strip()
    if query:
        like = f"%{query}%"
        clauses.append("(u.id LIKE ? OR u.display_name LIKE ? OR u.device_key LIKE ?)")
        params.extend([like, like, like])

    if scope_value == "real" or scope_value == "empty":
        if real:
            marks = ",".join("?" for _ in real)
            op = "IN" if scope_value == "real" else "NOT IN"
            clauses.append(f"u.id {op} ({marks})")
            params.extend(sorted(real))
        elif scope_value == "real":
            # 一个都没有：real 结果必为空，用恒假条件而不是省掉（省掉会返回全部）
            clauses.append("0")
        # scope=empty 且 real 为空时无需条件：全部都是空注册

    if clauses:
        where = " WHERE " + " AND ".join(clauses)

    count_sql = "SELECT COUNT(*) AS n FROM v3_users u" + where
    list_sql = (
        "SELECT u.id, u.display_name, u.device_key, u.created_at, u.last_seen_at,"
        " COALESCE(up.post_count, 0) AS post_count,"
        " (SELECT COUNT(*) FROM friendships f"
        "   WHERE f.user_id=u.id AND f.status='accepted') AS friend_count,"
        " (SELECT COUNT(*) FROM client_devices d WHERE d.user_id=u.id) AS device_count"
        " FROM v3_users u"
        " LEFT JOIN (SELECT user_id, COUNT(*) AS post_count FROM user_posts GROUP BY user_id) up"
        " ON up.user_id = u.id"
        f"{where} ORDER BY {_SORTS[sort_value]} LIMIT ? OFFSET ?"
    )

    try:
        with db._lock:  # noqa: SLF001
            total = int(db.conn.execute(count_sql, tuple(params)).fetchone()["n"] or 0)
            rows = db.conn.execute(list_sql, (*params, int(limit), int(offset))).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - 极老库
        log.debug("读取用户列表失败（忽略）: %s", exc)
        return {"ok": True, "total": 0, "offset": int(offset), "limit": int(limit), "items": []}

    # 列表里逐行标出"是否有痕迹"，前端不用再自己推断（前后端口径必须一致，
    # 各自算一遍迟早会出现"筛出来是空壳但没标出来"这种互相打脸的情况）。
    all_real = real if scope_value != "all" else user_scope.real_ids(db)

    items = [
        {
            "id": str(row["id"]),
            "display_name": str(row["display_name"] or ""),
            "device_key_tail": _device_tail(row["device_key"]),
            "created_at": str(row["created_at"] or ""),
            "last_seen_at": str(row["last_seen_at"] or ""),
            "has_trace": str(row["id"]) in all_real,
            "counts": {
                "posts": int(row["post_count"] or 0),
                "friends": int(row["friend_count"] or 0),
                "devices": int(row["device_count"] or 0),
            },
        }
        for row in rows
    ]
    return {
        "ok": True,
        "total": total,
        "offset": int(offset),
        "limit": int(limit),
        "scope": scope_value,
        # 全局口径汇总（不受 scope 过滤影响），前端用来显示"共 N 条，其中 M 条有痕迹"
        "summary": user_scope.counts(db),
        "items": items,
    }

# --------------------------------------------------------------------------- #
# 用户详情
# --------------------------------------------------------------------------- #
@router.get("/{uid}")
async def get_user(request: Request, uid: str) -> dict:
    """用户详情：基础信息 + 画像 + 设备 + 好友 + 自己发过的动态。

    **不返回聊天记录**：产品决定管理台不看用户对话（``messages`` 表这里完全不碰）。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    user_id = str(uid or "").strip()
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT id, device_key, display_name, created_at, last_seen_at"
            " FROM v3_users WHERE id=?",
            (user_id,),
        ).fetchone()
    if row is None:
        return {"ok": False, "issues": ["用户不存在"]}

    user = {
        "id": str(row["id"]),
        "display_name": str(row["display_name"] or ""),
        "device_key_tail": _device_tail(row["device_key"]),
        "created_at": str(row["created_at"] or ""),
        "last_seen_at": str(row["last_seen_at"] or ""),
    }

    profile: dict[str, Any] = {}
    try:
        profile = db.get_profile(user_id=user_id) or {}
    except Exception as exc:  # noqa: BLE001 - 画像缺失不该让详情 500
        log.debug("读取用户画像失败（忽略）: %s", exc)

    devices: list[dict[str, Any]] = []
    try:
        with db._lock:  # noqa: SLF001
            device_rows = db.conn.execute(
                "SELECT registration_id, platform, vendor, enabled, updated_at"
                " FROM client_devices WHERE user_id=? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        devices = [
            {
                "registration_id": str(item["registration_id"]),
                "platform": str(item["platform"] or ""),
                "vendor": str(item["vendor"] or ""),
                "enabled": bool(item["enabled"]),
                "updated_at": _iso_from_epoch(item["updated_at"]),
            }
            for item in device_rows
        ]
    except sqlite3.Error as exc:  # pragma: no cover - 老库没有 client_devices
        log.debug("读取用户设备失败（忽略）: %s", exc)

    friends: list[dict[str, Any]] = []
    try:
        with db._lock:  # noqa: SLF001
            friend_rows = db.conn.execute(
                "SELECT f.id, f.persona_id, f.status, f.created_at,"
                " p.nickname, p.avatar, p.avatar_seed, p.avatar_style, p.avatar_options,"
                " p.gender, p.country, p.age"
                " FROM friendships f LEFT JOIN personas p ON p.id=f.persona_id"
                " WHERE f.user_id=? ORDER BY f.created_at DESC",
                (user_id,),
            ).fetchall()
        friends = [
            {
                "id": str(item["id"]),
                "persona_id": str(item["persona_id"] or ""),
                "persona_nickname": str(item["nickname"] or ""),
                "persona_avatar": _persona_avatar(item),
                "status": str(item["status"] or ""),
                "created_at": str(item["created_at"] or ""),
            }
            for item in friend_rows
        ]
    except sqlite3.Error as exc:  # pragma: no cover
        log.debug("读取用户好友失败（忽略）: %s", exc)

    posts: list[dict[str, Any]] = []
    post_total = 0
    try:
        with db._lock:  # noqa: SLF001
            post_total = int(
                db.conn.execute(
                    "SELECT COUNT(*) AS n FROM user_posts WHERE user_id=?", (user_id,)
                ).fetchone()["n"]
                or 0
            )
            post_rows = db.conn.execute(
                "SELECT id, content, media, created_at FROM user_posts"
                " WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 20",
                (user_id,),
            ).fetchall()
        posts = [
            {
                "id": str(item["id"]),
                "content": str(item["content"] or ""),
                "created_at": str(item["created_at"] or ""),
                "media": _loads(item["media"], []),
            }
            for item in post_rows
        ]
    except sqlite3.Error as exc:  # pragma: no cover
        log.debug("读取用户动态失败（忽略）: %s", exc)

    counts = {
        "posts": post_total,
        "friends": len(friends),
        "devices": len(devices),
        "accepted_friends": sum(1 for item in friends if item["status"] == "accepted"),
    }
    return {
        "ok": True,
        "user": user,
        "profile": profile,
        "devices": devices,
        "friends": friends,
        "posts": posts,
        "counts": counts,
    }

# --------------------------------------------------------------------------- #
# 候选角色（还没加好友的），带匹配提示
# --------------------------------------------------------------------------- #
@router.get("/{uid}/candidate-personas")
async def list_candidate_personas(
    request: Request,
    uid: str,
    q: str = Query("", description="按昵称 / 职业 / 城市 / 简介模糊搜索"),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """还不是该用户好友的活跃角色，附 ``match_hint``（同城市 / 同语言 / 共同兴趣）。

    ``match_hint`` 是**解释性**字段：列表先按命中项多少降序，再按昵称稳定排序，
    让运营一眼看出「为什么推荐 TA」。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    user_id = str(uid or "").strip()
    if not _user_exists(db, user_id):
        return {"ok": False, "issues": ["用户不存在"]}

    profile: dict[str, Any] = {}
    try:
        profile = db.get_profile(user_id=user_id) or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("读取用户画像失败（忽略）: %s", exc)
    interests = _user_interests(profile)
    city = _user_city(profile).lower()
    languages = _user_languages(profile)

    sql = (
        "SELECT p.id, p.nickname, p.avatar, p.avatar_seed, p.avatar_style, p.avatar_options,"
        " p.gender, p.country, p.age, p.occupation, p.city, p.language, p.interests, p.bio"
        " FROM personas p WHERE p.status='active'"
        " AND NOT EXISTS (SELECT 1 FROM friendships f"
        "   WHERE f.user_id=? AND f.persona_id=p.id)"
    )
    params: list[Any] = [user_id]
    query = str(q or "").strip()
    if query:
        like = f"%{query}%"
        sql += " AND (p.nickname LIKE ? OR p.occupation LIKE ? OR p.city LIKE ? OR p.bio LIKE ?)"
        params.extend([like, like, like, like])
    sql += " ORDER BY p.nickname LIMIT ?"
    params.append(int(limit))

    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover
        log.debug("读取候选角色失败（忽略）: %s", exc)
        return {"ok": True, "items": []}

    items: list[dict[str, Any]] = []
    for row in rows:
        persona_interests = {
            str(item).strip().lower()
            for item in (_loads(row["interests"], []) or [])
            if str(item).strip()
        }
        shared = sorted(interests & persona_interests)
        persona_city = str(row["city"] or "").strip().lower()
        persona_lang = str(row["language"] or "").strip().lower()
        same_city = bool(city) and bool(persona_city) and (
            city == persona_city or city in persona_city or persona_city in city
        )
        same_language = bool(persona_lang) and persona_lang in languages
        reasons: list[str] = []
        if same_city:
            reasons.append(f"同城市：{row['city']}")
        if same_language:
            reasons.append(f"同语言：{persona_lang}")
        if shared:
            reasons.append("共同兴趣：" + "、".join(shared[:5]))
        items.append(
            {
                "id": str(row["id"]),
                "nickname": str(row["nickname"] or ""),
                "avatar": _persona_avatar(row),
                "occupation": str(row["occupation"] or ""),
                "city": str(row["city"] or ""),
                "language": persona_lang,
                "interests": sorted(persona_interests),
                "bio": str(row["bio"] or ""),
                "match_hint": {
                    "same_city": same_city,
                    "same_language": same_language,
                    "shared_interests": shared,
                    "reasons": reasons,
                    "score": len(reasons) * 30 + len(shared) * 10,
                },
            }
        )

    # 命中项多的排前面；同分按昵称（稳定、可复现）
    items.sort(key=lambda item: (-item["match_hint"]["score"], item["nickname"]))
    return {"ok": True, "items": items}

# --------------------------------------------------------------------------- #
# 好友指派 / 解除
# --------------------------------------------------------------------------- #
def _friendship_row(db: Any, user_id: str, persona_id: str) -> Any:
    with db._lock:  # noqa: SLF001
        return db.conn.execute(
            "SELECT * FROM friendships WHERE user_id=? AND persona_id=?",
            (user_id, persona_id),
        ).fetchone()

def _insert_request_event(
    db: Any,
    *,
    user_id: str,
    persona_id: str,
    message: str,
) -> bool:
    """写一条 ``social_events`` 好友申请事件（现有通知/推送链路的入口）。

    ``dedupe_key`` 唯一，重复指派不会产生第二条通知（幂等的第二道保险）。
    返回是否**新建**了事件。
    """
    now = _now_iso()
    persona_name = ""
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT nickname FROM personas WHERE id=?", (persona_id,)
        ).fetchone()
    if row is not None:
        persona_name = str(row["nickname"] or "")
    payload = {
        "message": message,
        "persona_name": persona_name,
        "source": "admin",
    }
    event_id = f"se_{uuid.uuid4().hex[:12]}"
    try:
        with db._lock, db.conn:  # noqa: SLF001
            cursor = db.conn.execute(
                "INSERT OR IGNORE INTO social_events"
                " (id, user_id, persona_id, post_id, event_type, payload, status, is_read,"
                "  dedupe_key, scheduled_at, created_at)"
                " VALUES (?,?,?,'','friend_request',?,'pending',0,?,?,?)",
                (
                    event_id,
                    user_id,
                    persona_id,
                    _dumps(payload),
                    f"admin_friend_request:{user_id}:{persona_id}",
                    now,
                    now,
                ),
            )
            created = cursor.rowcount > 0
    except sqlite3.Error as exc:  # pragma: no cover - 极老库没有 social_events
        log.debug("写好友申请通知失败（忽略）: %s", exc)
        return False
    return created

@router.post("/{uid}/friends")
async def assign_friend(request: Request, uid: str, body: dict = Body(...)) -> dict:
    """好友指派：``mode='request'`` 发好友申请，``mode='direct'`` 直接成为好友。

    幂等（本端点的第一硬约束）：
    - 已是 ``accepted`` → ``{ok:false, issues:["已经是好友"]}``
    - 已有 ``pending`` 申请 → ``{ok:false, issues:["已有好友申请"]}``
    两条都在**写入前**判定，不会产生重复 ``friendships`` 行（表上还有
    ``UNIQUE(user_id, persona_id)`` 兜底）。

    消息内容：落库（``friendships.request_message`` / 通知 payload），
    但**不进审计 summary**（审计只记模式与角色 id）。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    user_id = str(uid or "").strip()
    data = body if isinstance(body, dict) else {}
    persona_id = str(data.get("persona_id") or "").strip()
    mode = str(data.get("mode") or "direct").strip().lower()
    message = str(data.get("message") or "").strip()[:200]
    notify = data.get("notify", True)
    notify = True if notify is None else bool(notify)

    if not persona_id:
        raise HTTPException(status_code=400, detail="persona_id 不能为空")
    if mode not in FRIEND_MODES:
        raise HTTPException(
            status_code=400, detail=f"mode 不合法: {mode}（可选 {', '.join(FRIEND_MODES)}）"
        )
    if not _user_exists(db, user_id):
        return {"ok": False, "issues": ["用户不存在"]}
    if not _persona_exists(db, persona_id):
        return {"ok": False, "issues": ["角色不存在"]}

    existing = _friendship_row(db, user_id, persona_id)
    if existing is not None:
        status = str(existing["status"] or "")
        if status == "accepted":
            return {"ok": False, "issues": ["已经是好友"]}
        if status == "pending":
            return {"ok": False, "issues": ["已有好友申请"]}

    service = _service(request)
    if service is None:
        return {"ok": False, "issues": ["好友服务未就绪"]}

    try:
        if mode == "request":
            result = service._ensure_request_friend(  # noqa: SLF001 - 现有唯一申请通道
                user_id, persona_id, message
            )
            notified = (
                _insert_request_event(db, user_id=user_id, persona_id=persona_id, message=message)
                if notify
                else False
            )
        else:
            result = service.add_friend(user_id, persona_id, request_message=message)
            notified = False
    except KeyError:
        return {"ok": False, "issues": ["角色不存在"]}
    except Exception as exc:  # noqa: BLE001 - 现有链路的失败按业务失败返回
        log.warning("好友指派失败: %s", exc)
        return {"ok": False, "issues": [f"好友指派失败：{type(exc).__name__}: {exc}"]}

    friendship = result.get("friendship") or {}
    _audit(
        request,
        "users.friends",
        f"users/{user_id}/friends/{persona_id}",
        f"好友指派 mode={mode} persona={persona_id} notify={notify}",
    )
    return {
        "ok": True,
        "mode": mode,
        "created": True,
        "notified": bool(notified),
        "friendship": {
            "id": str(friendship.get("id") or ""),
            "user_id": user_id,
            "persona_id": persona_id,
            "status": str(friendship.get("status") or ""),
            "friend_id": str(result.get("friend_id") or friendship.get("friend_id") or ""),
            "created_at": str(friendship.get("created_at") or ""),
        },
    }

@router.delete("/{uid}/friends/{friendship_id}")
async def remove_friend(request: Request, uid: str, friendship_id: str) -> dict:
    """解除好友（删 ``friendships`` + 对应的 V2 ``friends`` 行，复用服务层实现）。"""
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    user_id = str(uid or "").strip()
    fid = str(friendship_id or "").strip()
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT * FROM friendships WHERE id=? AND user_id=?", (fid, user_id)
        ).fetchone()
    if row is None:
        return {"ok": False, "issues": ["好友关系不存在"], "removed": False}

    persona_id = str(row["persona_id"] or "")
    service = _service(request)
    removed = False
    if service is not None:
        try:
            removed = bool(service.remove_friend(user_id, persona_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("解除好友失败（回退为只删关系行）: %s", exc)
    if not removed:
        # 服务层不可用/未找到：至少删掉关系行，避免管理台点了没反应
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute("DELETE FROM friendships WHERE id=?", (fid,))
        removed = True

    _audit(
        request,
        "users.friends.delete",
        f"users/{user_id}/friends/{fid}",
        f"解除好友 persona={persona_id}",
    )
    return {"ok": True, "removed": True, "friendship_id": fid, "persona_id": persona_id}

# --------------------------------------------------------------------------- #
# 测试推送
# --------------------------------------------------------------------------- #
def _jpush_credentials() -> dict[str, str]:
    """JPush 凭据：统一由 ``app.v3.push_config.effective()`` 解析。

    来源优先级 **环境变量 > 管理台保存 > settings(.env)**，与 ``dispatch_push``
    用的是同一个入口——这里不自己读 ``os.getenv``，否则管理台改的配置会不生效。
    """
    from app.v3 import push_config  # noqa: PLC0415

    return push_config.effective()

@router.post("/{uid}/push-test")
async def push_test(request: Request, uid: str) -> dict:
    """给该用户**所有 enabled 设备**发一条测试推送。

    - 未配置 JPush → ``{ok:false, issues:["JPush 未配置"]}``
    - 没有已启用设备 → ``{ok:false, issues:["该用户没有已启用的推送设备"]}``
    - 成功 → ``{ok:true, sent, devices, message}``：``devices`` = 该用户 enabled
      设备数，``sent`` = 本次真正发往的设备条数（一次 JPush 请求覆盖全部设备）。

    请求形状与 ``app.v3.client_events.dispatch_push`` 完全一致（同端点 / 同 auth /
    同 android 结构），只是 audience 换成注册 id 列表。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    user_id = str(uid or "").strip()
    if not _user_exists(db, user_id):
        return {"ok": False, "issues": ["用户不存在"]}

    try:
        with db._lock:  # noqa: SLF001
            devices = [
                str(row["registration_id"])
                for row in db.conn.execute(
                    "SELECT registration_id FROM client_devices"
                    " WHERE user_id=? AND enabled=1",
                    (user_id,),
                ).fetchall()
                if str(row["registration_id"] or "").strip()
            ]
    except sqlite3.Error as exc:  # pragma: no cover - 老库没有 client_devices
        log.debug("读取推送设备失败（忽略）: %s", exc)
        devices = []

    if not devices:
        return {"ok": False, "issues": ["该用户没有已启用的推送设备"]}

    creds = _jpush_credentials()
    key = str(creds.get("app_key") or "")
    secret = str(creds.get("master_secret") or "")
    if not key or not secret:
        return {"ok": False, "issues": ["JPush 未配置"]}

    payload = {
        "platform": "android",
        "audience": {"registration_id": devices},
        "notification": {
            "android": {
                "alert": "管理台测试推送",
                "title": "SpeakEasy",
                "extras": {"route": "#/", "push_test": True},
            }
        },
        "options": {"time_to_live": 60},
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, auth=(key, secret)) as client:
            response = await client.post(JPUSH_PUSH_URL, json=payload)
    except httpx.HTTPError as exc:
        log.warning("测试推送失败: %s", exc)
        return {
            "ok": False,
            "issues": [f"推送失败：{type(exc).__name__}"],
            "devices": len(devices),
            "sent": 0,
            "message": f"推送失败：{type(exc).__name__}",
        }

    _audit(
        request,
        "users.push-test",
        f"users/{user_id}",
        f"测试推送 → {len(devices)} 台设备（JPush {response.status_code}）",
    )
    if not response.is_success:
        message = f"JPush 返回 {response.status_code}"
        return {
            "ok": False,
            "issues": [message],
            "devices": len(devices),
            "sent": 0,
            "message": message,
        }
    return {
        "ok": True,
        "sent": len(devices),
        "devices": len(devices),
        "message": f"测试推送已发送到 {len(devices)} 台设备",
    }

__all__ = [
    "DEVICE_TAIL_CHARS",
    "FRIEND_MODES",
    "JPUSH_PUSH_URL",
    "router",
]
