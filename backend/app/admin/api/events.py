"""管理 API · 角色事件与公共事件池（``/api/admin/events``、``/api/admin/event-pool`）。

接口
----
``GET    /events``                      角色事件列表（``persona_events``，带角色昵称）
``POST   /events``                      手动补一条角色事件
``GET    /event-pool``                  公共事件池列表（含领取记录）
``POST   /event-pool``                  新建事件池条目
``PUT    /event-pool/{pid}``            编辑
``DELETE /event-pool/{pid}``            删除（claims 由 ``ON DELETE CASCADE`` 一起删）
``POST   /event-pool/{pid}/preview``    **预演**：哪些角色会领取 + 分项得分（不落库）
``POST   /event-pool/{pid}/claim``      真正领取（写 claims + 生成 persona_events）

复用而不是另写
--------------
- 角色事件写入一律走 ``app.v3.life_events.add_event``（它负责类型/状态归一、
  ``dedupe_key`` 幂等、``day``/时间兜底）；本模块不直接 INSERT ``persona_events``。
- 领取记录靠表上的 ``UNIQUE(pool_id, persona_id)`` 保幂等：重复调用只会
  命中 ``skipped``，不会让同一角色领两次、也不会重复生成事件。

评分（preview / claim 共用同一套）
-----------------------------------
四个维度都在现有 ``personas`` 行上，不调模型、不联网：

============  ======  ====================================================
维度          权重    取数
============  ======  ====================================================
职业契合      0.30    ``occupation`` × 事件关键词（``conditions.occupation`` 优先）
兴趣匹配      0.30    ``interests``  ×（标题/描述/类型/``conditions.interests``）
城市匹配      0.20    ``city`` ×（``conditions.city`` 或 ``location``）
表达 DNA      0.20    ``speech_dna`` → 主动/亲近/爱玩/话多（见 ``_dna_score``）
============  ======  ====================================================

``score`` 是 0~100 的总分，``score_detail`` 里逐项给出**每个维度的贡献值**、
权重与命中证据，运营能看出"为什么是 TA"。

失败约定
--------
业务失败统一 ``200 + {"ok": false, "issues": [...]}``；只有参数本身非法（缺必填、
类型不对）才 ``400``。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from app.admin.api import deps
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
#: 无 prefix：本模块同时对外提供 ``/events`` 与 ``/event-pool`` 两组路径。
router = APIRouter()

_DB_NOT_READY: dict[str, Any] = {"ok": False, "issues": ["数据库未就绪"]}

#: 评分权重（改这里同时影响 preview 与 claim 落库的分数）。
SCORE_WEIGHTS: dict[str, float] = {
    "occupation": 0.30,
    "interests": 0.30,
    "city": 0.20,
    "dna": 0.20,
}

#: 事件池状态（本项目自用取值；``claim`` 领满时写 ``filled``）。
POOL_STATUSES: tuple[str, ...] = ("open", "filled", "closed", "cancelled")

# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return default

def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)

def _audit(request: Request, action: str, target: str, summary: str) -> None:
    request.state.audit_action = action
    request.state.audit_target = target
    request.state.audit_summary = summary

def _persona_avatar(persona: Any) -> str:
    try:
        from app.v3.avatar import persona_avatar_url  # noqa: PLC0415

        return persona_avatar_url(dict(persona))
    except Exception:  # noqa: BLE001 - 头像失败不能拖垮列表
        return ""

def _event_types() -> tuple[str, ...]:
    """合法角色事件类型（单一事实来源：``life_events.EVENT_TYPES``）。"""
    from app.v3 import life_events  # noqa: PLC0415

    return tuple(life_events.EVENT_TYPES)

def _event_statuses() -> tuple[str, ...]:
    from app.v3 import life_events  # noqa: PLC0415

    return tuple(life_events.EVENT_STATUSES)

def _pool_row(db: Any, pid: str) -> Any:
    with db._lock:  # noqa: SLF001 - 与 DB 层共用同一把锁
        return db.conn.execute("SELECT * FROM event_pool WHERE id=?", (pid,)).fetchone()

def _pool_conditions(row: Any) -> dict[str, Any]:
    raw = _loads(row["conditions"] if "conditions" in row.keys() else "{}", {})
    return raw if isinstance(raw, dict) else {}

def _pool_claims(db: Any, pid: str) -> list[dict[str, Any]]:
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT c.persona_id, c.score, c.claimed_at, c.event_id,"
            " p.nickname, p.avatar, p.avatar_seed, p.avatar_style, p.avatar_options,"
            " p.gender, p.country, p.age"
            " FROM event_pool_claims c LEFT JOIN personas p ON p.id=c.persona_id"
            " WHERE c.pool_id=? ORDER BY c.claimed_at, c.persona_id",
            (pid,),
        ).fetchall()
    return [
        {
            "persona_id": str(row["persona_id"] or ""),
            "persona_nickname": str(row["nickname"] or ""),
            "score": float(row["score"] or 0),
            "claimed_at": str(row["claimed_at"] or ""),
            "event_id": str(row["event_id"] or ""),
        }
        for row in rows
    ]

def _pool_public(db: Any, row: Any) -> dict[str, Any]:
    pid = str(row["id"] or "")
    claims = _pool_claims(db, pid)
    slots = int(row["slots"] or 1)
    return {
        "id": pid,
        "title": str(row["title"] or ""),
        "description": str(row["description"] or ""),
        "event_type": str(row["event_type"] or ""),
        "start_at": str(row["start_at"] or ""),
        "location": str(row["location"] or ""),
        "slots": slots,
        "conditions": _pool_conditions(row),
        "status": str(row["status"] or "open"),
        "created_at": str(row["created_at"] or ""),
        "claims": claims,
        "claimed_count": len(claims),
        "remaining_slots": max(0, slots - len(claims)),
    }

# --------------------------------------------------------------------------- #
# 评分：职业 / 兴趣 / 城市 / 表达 DNA
# --------------------------------------------------------------------------- #
def _norm_list(value: Any) -> list[str]:
    """把 conditions 里的目标列表归一成小写字符串列表（支持 str / list）。"""
    if isinstance(value, str):
        values = [part for part in value.replace("/", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    return [str(item).strip().lower() for item in values if str(item).strip()]

def _text_blob(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts).lower()

def _hit(needle: str, blob: str) -> bool:
    """关键词命中：双向子串（中文兴趣词没有空格，拆词不可靠）。"""
    token = str(needle or "").strip().lower()
    if not token:
        return False
    return token in blob or any(word in blob for word in token.replace("_", " ").split())

def _dna_traits(persona: Any) -> dict[str, float]:
    """表达 DNA → 10 个 0..1 特质；复用 ``event_posts`` 的单一实现。"""
    try:
        from app.v3 import event_posts  # noqa: PLC0415

        return event_posts.dna_traits_for(persona)
    except Exception:  # noqa: BLE001 - DNA 解析失败不拖垮评分
        return {}

def _dna_score(persona: Any) -> tuple[float, dict[str, float]]:
    """表达 DNA 分数：越主动 / 越亲近 / 越好玩 / 越话多，越适合参与公共事件。"""
    traits = _dna_traits(persona)
    if not traits:
        return 0.5, {}
    readiness = (
        0.40 * float(traits.get("initiative", 0.5))
        + 0.25 * (1.0 - float(traits.get("social_distance", 0.45)))
        + 0.20 * float(traits.get("playfulness", 0.5))
        + 0.15 * float(traits.get("verbosity", 0.32))
    )
    return max(0.0, min(1.0, readiness)), traits

def score_persona(pool: Any, persona: Any) -> dict[str, Any]:
    """给「一个角色 × 一个事件池条目」打分（0~100），返回分项明细。

    纯函数：不写库、不联网、不调模型，``preview`` 与 ``claim`` 共用，
    保证「预演看到的分数」与「真正领取落库的分数」逐位一致。
    """
    conditions = _pool_conditions(pool)
    title = str(pool["title"] or "")
    description = str(pool["description"] or "")
    event_type = str(pool["event_type"] or "")
    location = str(pool["location"] or "")
    blob = _text_blob(title, description, event_type.replace("_", " "), location)

    target_interests = _norm_list(conditions.get("interests"))
    target_occupations = _norm_list(conditions.get("occupation"))
    if target_interests:
        blob = _text_blob(blob, " ".join(target_interests))
    if target_occupations:
        blob = _text_blob(blob, " ".join(target_occupations))

    # 1) 职业契合
    occupation = str(persona["occupation"] or "").strip().lower()
    occupation_hit = False
    if target_occupations:
        occupation_hit = any(
            _hit(occupation, target) or _hit(target, occupation)
            for target in target_occupations
            if occupation
        )
        occupation_score = 1.0 if occupation_hit else 0.05
    else:
        occupation_hit = bool(occupation) and _hit(occupation, blob)
        occupation_score = 0.75 if occupation_hit else 0.15

    # 2) 兴趣匹配
    persona_interests = _norm_list(
        _loads(persona["interests"] if "interests" in persona.keys() else "[]", [])
    )
    matched: list[str] = []
    for interest in persona_interests:
        if target_interests:
            if any(_hit(interest, target) or _hit(target, interest) for target in target_interests):
                matched.append(interest)
        elif _hit(interest, blob):
            matched.append(interest)
    denom = max(1, min(3, len(target_interests) or len(persona_interests) or 1))
    interest_score = 0.10 + 0.90 * min(1.0, len(matched) / denom)

    # 3) 城市匹配（事件没写城市 → 中性 0.5，不惩罚）
    pool_city = str(conditions.get("city") or location or "").strip().lower()
    persona_city = str(persona["city"] or "").strip().lower()
    city_hit = False
    if not pool_city:
        city_score = 0.50
    elif not persona_city:
        city_score = 0.30
    elif pool_city == persona_city or pool_city in persona_city or persona_city in pool_city:
        city_hit = True
        city_score = 1.0
    elif any(word in pool_city for word in persona_city.replace(",", " ").split() if word):
        city_hit = True
        city_score = 0.60
    else:
        city_score = 0.5 if any(_hit(word, pool_city) for word in persona_city.split()) else 0.05

    # 4) 表达 DNA
    dna_score, traits = _dna_score(persona)

    total = (
        SCORE_WEIGHTS["occupation"] * occupation_score
        + SCORE_WEIGHTS["interests"] * interest_score
        + SCORE_WEIGHTS["city"] * city_score
        + SCORE_WEIGHTS["dna"] * dna_score
    )
    detail = {
        "weights": dict(SCORE_WEIGHTS),
        "occupation": {
            "score": round(occupation_score, 4),
            "contribution": round(SCORE_WEIGHTS["occupation"] * occupation_score * 100, 2),
            "persona_occupation": str(persona["occupation"] or ""),
            "matched": occupation_hit,
            "targets": target_occupations,
        },
        "interests": {
            "score": round(interest_score, 4),
            "contribution": round(SCORE_WEIGHTS["interests"] * interest_score * 100, 2),
            "persona_interests": persona_interests,
            "matched": matched,
            "targets": target_interests,
        },
        "city": {
            "score": round(city_score, 4),
            "contribution": round(SCORE_WEIGHTS["city"] * city_score * 100, 2),
            "persona_city": str(persona["city"] or ""),
            "event_city": pool_city,
            "matched": city_hit,
        },
        "dna": {
            "score": round(dna_score, 4),
            "contribution": round(SCORE_WEIGHTS["dna"] * dna_score * 100, 2),
            "traits": {key: round(value, 4) for key, value in traits.items()},
        },
    }
    return {"score": round(total * 100, 2), "detail": detail}

def _score_all(
    db: Any, pool: Any, *, exclude: set[str] | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """给全部活跃角色打分，按总分降序（同分按昵称，保证可复现）。"""
    skip = {str(pid) for pid in (exclude or set())}
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT id, nickname, avatar, avatar_seed, avatar_style, avatar_options,"
            " gender, country, age, occupation, city, language, interests, speech_dna"
            " FROM personas WHERE status='active'"
        ).fetchall()
    scored: list[dict[str, Any]] = []
    for row in rows:
        persona_id = str(row["id"] or "")
        if persona_id in skip:
            continue
        result = score_persona(pool, row)
        scored.append(
            {
                "persona_id": persona_id,
                "persona_nickname": str(row["nickname"] or ""),
                "persona_avatar": _persona_avatar(row),
                "score": result["score"],
                "score_detail": result["detail"],
            }
        )
    scored.sort(key=lambda item: (-item["score"], item["persona_nickname"], item["persona_id"]))
    if limit is not None:
        return scored[: max(0, int(limit))]
    return scored

# --------------------------------------------------------------------------- #
# 角色事件（persona_events）
# --------------------------------------------------------------------------- #
@router.get("/events")
async def list_events(
    request: Request,
    status: str = Query("", description="按 event_status 筛选，空=全部"),
    persona_id: str = Query("", description="按角色筛选"),
    source_type: str = Query("", description="按来源筛选（rule/llm/event_pool/manual…）"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """角色事件列表（``persona_events``），带角色昵称。

    只读；表缺失（极老库）返回空列表而不是 500。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    where: list[str] = []
    params: list[Any] = []
    status_value = str(status or "").strip()
    if status_value:
        where.append("e.event_status=?")
        params.append(status_value)
    persona_value = str(persona_id or "").strip()
    if persona_value:
        where.append("e.persona_id=?")
        params.append(persona_value)
    source_value = str(source_type or "").strip()
    if source_value:
        where.append("e.source_type=?")
        params.append(source_value)
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    try:
        with db._lock:  # noqa: SLF001
            total = int(
                db.conn.execute(
                    f"SELECT COUNT(*) AS n FROM persona_events e{clause}", tuple(params)
                ).fetchone()["n"]
                or 0
            )
            rows = db.conn.execute(
                "SELECT e.*, p.nickname, p.avatar, p.avatar_seed, p.avatar_style,"
                " p.avatar_options, p.gender, p.country, p.age"
                f" FROM persona_events e LEFT JOIN personas p ON p.id=e.persona_id{clause}"
                " ORDER BY e.start_time DESC, e.id DESC LIMIT ? OFFSET ?",
                (*params, int(limit), int(offset)),
            ).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - 极老库没有 persona_events
        log.debug("读取角色事件失败（忽略）: %s", exc)
        return {"ok": True, "total": 0, "offset": int(offset), "limit": int(limit), "items": []}

    items = [
        {
            "id": str(row["id"] or ""),
            "persona_id": str(row["persona_id"] or ""),
            "persona_nickname": str(row["nickname"] or ""),
            "event_type": str(row["event_type"] or ""),
            "title": str(row["title"] or ""),
            "description": str(row["description"] or ""),
            "start_time": str(row["start_time"] or ""),
            "end_time": str(row["end_time"] or ""),
            "location": str(row["location"] or ""),
            "event_status": str(row["event_status"] or ""),
            "source_type": str(row["source_type"] or ""),
            "source_reference": str(row["source_reference"] or ""),
            "visibility": str(row["visibility"] or ""),
            "day": str(row["day"] or ""),
            "created_at": str(row["created_at"] or ""),
        }
        for row in rows
    ]
    return {"ok": True, "total": total, "offset": int(offset), "limit": int(limit), "items": items}

@router.post("/events")
async def create_event(request: Request, body: dict = Body(...)) -> dict:
    """手动给某个角色补一条生活事件（写入 ``persona_events``）。

    - 写入走 ``life_events.add_event``（归一类型/状态 + ``dedupe_key`` 幂等）；
    - 未给 ``start_time`` 时用当前时间，``day`` 随开始时间推导；
    - 手动事件默认带一个**唯一** dedupe_key（运营每点一次就是要新增一条），
      重复提交同一条内容不会因为哈希相同而被合并。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    data = body if isinstance(body, dict) else {}
    persona_id = str(data.get("persona_id") or "").strip()
    event_type = str(data.get("event_type") or "").strip().lower()
    if not persona_id:
        raise HTTPException(status_code=400, detail="persona_id 不能为空")
    if not event_type:
        raise HTTPException(status_code=400, detail="event_type 不能为空")

    valid_types = _event_types()
    if event_type not in valid_types:
        return {
            "ok": False,
            "issues": [f"未知事件类型: {event_type}（可选 {', '.join(valid_types)}）"],
        }

    with db._lock:  # noqa: SLF001
        persona = db.conn.execute(
            "SELECT id, nickname FROM personas WHERE id=?", (persona_id,)
        ).fetchone()
    if persona is None:
        return {"ok": False, "issues": ["角色不存在"]}

    start_time = str(data.get("start_time") or "").strip() or _now_iso()
    visibility = str(data.get("visibility") or "private").strip() or "private"
    payload = {
        "persona_id": persona_id,
        "event_type": event_type,
        "title": str(data.get("title") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "start_time": start_time,
        "location": str(data.get("location") or "").strip(),
        "day": str(data.get("day") or "").strip(),
        "visibility": visibility,
        "source_type": "manual",
        "dedupe_key": str(data.get("dedupe_key") or "").strip()
        or f"manual:{uuid.uuid4().hex}",
    }
    try:
        from app.v3 import life_events  # noqa: PLC0415

        event_id, created = life_events.add_event(db, payload)
    except Exception as exc:  # noqa: BLE001 - 写入失败按业务失败返回
        log.warning("手动补事件失败: %s", exc)
        return {"ok": False, "issues": [f"事件写入失败：{type(exc).__name__}: {exc}"]}

    if not event_id:
        return {"ok": False, "issues": ["事件写入失败（时间或人物参数非法）"]}

    _audit(
        request,
        "events.create",
        f"events/{event_id}",
        f"手动补事件 persona={persona_id} type={event_type} created={created}",
    )
    return {
        "ok": True,
        "id": event_id,
        "created": bool(created),
        "item": {
            "id": event_id,
            "persona_id": persona_id,
            "persona_nickname": str(persona["nickname"] or ""),
            "event_type": event_type,
            "title": payload["title"],
            "start_time": start_time,
            "day": payload["day"],
            "visibility": visibility,
            "source_type": "manual",
        },
    }

# --------------------------------------------------------------------------- #
# 公共事件池
# --------------------------------------------------------------------------- #
@router.get("/event-pool")
async def list_event_pool(
    request: Request,
    status: str = Query("", description="按状态筛选（open/filled/closed/cancelled）"),
) -> dict:
    """事件池列表（含每条已领取的角色、分数与时间）。"""
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    sql = "SELECT * FROM event_pool"
    params: list[Any] = []
    status_value = str(status or "").strip()
    if status_value:
        sql += " WHERE status=?"
        params.append(status_value)
    sql += " ORDER BY created_at DESC, id DESC"

    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - 极老库
        log.debug("读取事件池失败（忽略）: %s", exc)
        return {"ok": True, "total": 0, "items": []}

    items = [_pool_public(db, row) for row in rows]
    return {"ok": True, "total": len(items), "items": items}

@router.post("/event-pool")
async def create_event_pool(request: Request, body: dict = Body(...)) -> dict:
    """新建事件池条目（运营手写世界事件，角色各自领取、各自演绎）。"""
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    data = body if isinstance(body, dict) else {}
    title = str(data.get("title") or "").strip()
    event_type = str(data.get("event_type") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title 不能为空")
    if not event_type:
        raise HTTPException(status_code=400, detail="event_type 不能为空")

    conditions = data.get("conditions")
    if conditions is None:
        conditions_value: dict[str, Any] = {}
    elif isinstance(conditions, dict):
        conditions_value = conditions
    else:
        raise HTTPException(status_code=400, detail="conditions 必须是对象")

    try:
        slots = int(data.get("slots") or 1)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="slots 必须是整数") from None
    if slots < 1:
        raise HTTPException(status_code=400, detail="slots 必须 >= 1")
    slots = min(slots, 100)

    pool_id = f"pool_{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO event_pool (id, title, description, event_type, start_at,"
                " location, slots, conditions, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,'open',?)",
                (
                    pool_id,
                    title,
                    str(data.get("description") or "").strip(),
                    event_type,
                    str(data.get("start_at") or "").strip(),
                    str(data.get("location") or "").strip(),
                    slots,
                    _dumps(conditions_value),
                    now,
                ),
            )
    except sqlite3.Error as exc:
        log.warning("新建事件池失败: %s", exc)
        return {"ok": False, "issues": [f"写入失败：{type(exc).__name__}"]}

    _audit(
        request,
        "event-pool.create",
        f"event-pool/{pool_id}",
        f"新建事件池 title={title} type={event_type} slots={slots}",
    )
    return {
        "ok": True,
        "id": pool_id,
        "item": {
            "id": pool_id,
            "title": title,
            "description": str(data.get("description") or "").strip(),
            "event_type": event_type,
            "start_at": str(data.get("start_at") or "").strip(),
            "location": str(data.get("location") or "").strip(),
            "slots": slots,
            "conditions": conditions_value,
            "status": "open",
            "created_at": now,
            "claims": [],
        },
    }

@router.put("/event-pool/{pid}")
async def update_event_pool(request: Request, pid: str, body: dict = Body(...)) -> dict:
    """编辑事件池条目（只改请求里出现的字段）。"""
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    pool_id = str(pid or "").strip()
    row = _pool_row(db, pool_id)
    if row is None:
        return {"ok": False, "issues": ["事件不存在"]}

    data = body if isinstance(body, dict) else {}
    updates: dict[str, Any] = {}
    for name in ("title", "description", "event_type", "start_at", "location", "status"):
        if name in data:
            updates[name] = str(data.get(name) or "").strip()
    if "slots" in data:
        try:
            slots = int(data.get("slots") or 1)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="slots 必须是整数") from None
        if slots < 1:
            raise HTTPException(status_code=400, detail="slots 必须 >= 1")
        updates["slots"] = min(slots, 100)
    if "conditions" in data:
        conditions = data.get("conditions")
        if conditions is None:
            updates["conditions"] = _dumps({})
        elif isinstance(conditions, dict):
            updates["conditions"] = _dumps(conditions)
        else:
            raise HTTPException(status_code=400, detail="conditions 必须是对象")
    if "status" in updates:
        status_value = updates["status"]
        if status_value not in POOL_STATUSES:
            return {
                "ok": False,
                "issues": [f"未知状态: {status_value}（可选 {', '.join(POOL_STATUSES)}）"],
            }
    if not updates:
        return {"ok": False, "issues": ["没有需要修改的字段"]}

    sets = ", ".join(f"{name}=?" for name in updates)
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                f"UPDATE event_pool SET {sets} WHERE id=?",
                (*updates.values(), pool_id),
            )
    except sqlite3.Error as exc:  # pragma: no cover
        log.warning("更新事件池失败: %s", exc)
        return {"ok": False, "issues": [f"写入失败：{type(exc).__name__}"]}

    _audit(
        request,
        "event-pool.update",
        f"event-pool/{pool_id}",
        f"编辑事件池字段：{', '.join(updates)}",
    )
    fresh = _pool_row(db, pool_id)
    return {"ok": True, "item": _pool_public(db, fresh) if fresh is not None else {}}

@router.delete("/event-pool/{pid}")
async def delete_event_pool(request: Request, pid: str) -> dict:
    """删除事件池条目（``event_pool_claims`` 由外键 ``ON DELETE CASCADE`` 一起删）。"""
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    pool_id = str(pid or "").strip()
    row = _pool_row(db, pool_id)
    if row is None:
        return {"ok": False, "issues": ["事件不存在"], "deleted": False}

    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute("DELETE FROM event_pool WHERE id=?", (pool_id,))
    except sqlite3.Error as exc:  # pragma: no cover
        log.warning("删除事件池失败: %s", exc)
        return {"ok": False, "issues": [f"删除失败：{type(exc).__name__}"], "deleted": False}

    _audit(
        request,
        "event-pool.delete",
        f"event-pool/{pool_id}",
        f"删除事件池 title={row['title']}",
    )
    return {"ok": True, "deleted": True, "id": pool_id}

@router.post("/event-pool/{pid}/preview")
async def preview_event_pool(
    request: Request, pid: str, body: dict = Body(default={})
) -> dict:
    """预演：哪些角色会领取、各自总分与**分项得分明细**（不写库）。

    已经领过的角色不在候选里（它们已经领到了）；``limit`` 默认 12、上限 100。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    pool_id = str(pid or "").strip()
    row = _pool_row(db, pool_id)
    if row is None:
        return {"ok": False, "issues": ["事件不存在"]}

    data = body if isinstance(body, dict) else {}
    try:
        limit = int(data.get("limit") or 12)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit 必须是整数") from None
    limit = max(1, min(limit, 100))

    claimed = {item["persona_id"] for item in _pool_claims(db, pool_id)}
    scored = _score_all(db, row, exclude=claimed, limit=limit)
    _audit_skip_read(request)
    return {
        "ok": True,
        "pool_id": pool_id,
        "items": scored,
        "weights": dict(SCORE_WEIGHTS),
        "slots": int(row["slots"] or 1),
        "claimed": len(claimed),
        "claimed_count": len(claimed),
        "remaining_slots": max(0, int(row["slots"] or 1) - len(claimed)),
    }

def _audit_skip_read(request: Request) -> None:
    """预演是只读接口；显式标记不留审计（防御：以后若被挂上写依赖也不误记）。"""
    try:
        request.state.audit_skip = True
    except Exception:  # noqa: BLE001 - state 不可写时忽略
        pass

@router.post("/event-pool/{pid}/claim")
async def claim_event_pool(
    request: Request, pid: str, body: dict = Body(default={})
) -> dict:
    """真正领取：写 ``event_pool_claims`` + 为每个角色生成一条 ``persona_events``。

    **幂等**：``UNIQUE(pool_id, persona_id)`` + 写前查重，重复调用同一批角色只会
    返回 ``skipped``；``claims.event_id`` 回填生成的事件 id。
    ``slots`` 领满时把 ``event_pool.status`` 置为 ``filled``。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    pool_id = str(pid or "").strip()
    row = _pool_row(db, pool_id)
    if row is None:
        return {"ok": False, "issues": ["事件不存在"]}

    data = body if isinstance(body, dict) else {}
    slots = int(row["slots"] or 1)
    existing_claims = _pool_claims(db, pool_id)
    claimed_ids = {item["persona_id"] for item in existing_claims}
    if len(claimed_ids) >= slots and str(row["status"] or "") == "filled":
        return {"ok": False, "issues": ["名额已满"], "claimed": [], "skipped": []}

    requested = data.get("persona_ids")
    skipped: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    if isinstance(requested, list) and requested:
        ids = [str(item).strip() for item in requested if str(item).strip()]
        if not ids:
            raise HTTPException(status_code=400, detail="persona_ids 不能为空数组")
        scored_all = {item["persona_id"]: item for item in _score_all(db, row)}
        for persona_id in ids:
            if persona_id in claimed_ids:
                skipped.append({"persona_id": persona_id, "reason": "已经领取过"})
                continue
            item = scored_all.get(persona_id)
            if item is None:
                skipped.append({"persona_id": persona_id, "reason": "角色不存在或非活跃"})
                continue
            if len(claimed_ids) + len(selected) >= slots:
                skipped.append({"persona_id": persona_id, "reason": "名额已满"})
                continue
            selected.append(item)
    else:
        # 前端「按排名自动领取」按钮会发空 body：按预演得分从高到低挑前
        # ``remaining`` 个角色（跳过已领过的），不设分数门槛、也不报错。
        remaining = max(0, slots - len(claimed_ids))
        candidates = _score_all(db, row, exclude=claimed_ids)
        selected = candidates[:remaining]

    claimed: list[dict[str, Any]] = []
    for item in selected:
        persona_id = item["persona_id"]
        claim_id = f"pc_{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        try:
            with db._lock, db.conn:  # noqa: SLF001
                cursor = db.conn.execute(
                    "INSERT OR IGNORE INTO event_pool_claims"
                    " (id, pool_id, persona_id, score, score_detail, event_id, claimed_at)"
                    " VALUES (?,?,?,?,?,'',?)",
                    (
                        claim_id,
                        pool_id,
                        persona_id,
                        float(item["score"]),
                        _dumps(item["score_detail"]),
                        now,
                    ),
                )
                inserted = cursor.rowcount > 0
        except sqlite3.Error as exc:  # pragma: no cover
            log.warning("写领取记录失败: %s", exc)
            skipped.append({"persona_id": persona_id, "reason": "写入失败"})
            continue
        if not inserted:  # 并发下被别人先领了：按 skipped 处理
            skipped.append({"persona_id": persona_id, "reason": "已经领取过"})
            continue

        event_id = _create_claimed_event(db, row, persona_id)
        if event_id:
            try:
                with db._lock, db.conn:  # noqa: SLF001
                    db.conn.execute(
                        "UPDATE event_pool_claims SET event_id=? WHERE id=?",
                        (event_id, claim_id),
                    )
            except sqlite3.Error as exc:  # pragma: no cover
                log.debug("回填 claims.event_id 失败（忽略）: %s", exc)
        claimed_ids.add(persona_id)
        claimed.append(
            {
                "persona_id": persona_id,
                "persona_nickname": item["persona_nickname"],
                "score": item["score"],
                "score_detail": item["score_detail"],
                "claim_id": claim_id,
                "event_id": event_id,
                "claimed_at": now,
            }
        )

    filled = False
    total_claimed = len(claimed_ids)
    if total_claimed >= slots and str(row["status"] or "") != "filled":
        try:
            with db._lock, db.conn:  # noqa: SLF001
                db.conn.execute("UPDATE event_pool SET status='filled' WHERE id=?", (pool_id,))
            filled = True
        except sqlite3.Error as exc:  # pragma: no cover
            log.debug("更新事件池状态失败（忽略）: %s", exc)

    if claimed:
        _audit(
            request,
            "event-pool.claim",
            f"event-pool/{pool_id}",
            "领取角色：" + ", ".join(item["persona_id"] for item in claimed),
        )
    summary = {
        "ok": True,
        "pool_id": pool_id,
        "claimed": claimed,
        "skipped": skipped,
        "filled": filled or str(row["status"] or "") == "filled",
        "claimed_count": total_claimed,
        "remaining_slots": max(0, slots - total_claimed),
    }
    if not claimed and not skipped:
        summary["issues"] = ["没有符合条件的角色"]
        summary["ok"] = False
    return summary

def _create_claimed_event(db: Any, pool: Any, persona_id: str) -> str:
    """为领取的角色生成一条 ``persona_events``（复用 ``life_events.add_event``）。

    返回事件 id；失败返回空串（领取记录仍然保留，运营可重试）。
    """
    pool_id = str(pool["id"] or "")
    conditions = _pool_conditions(pool)
    event_type = str(pool["event_type"] or "").strip().lower()
    valid_types = _event_types()
    if event_type not in valid_types:
        event_type = "social"
    payload = {
        "persona_id": persona_id,
        "event_type": event_type,
        "title": str(pool["title"] or ""),
        "description": str(pool["description"] or ""),
        "start_time": str(pool["start_at"] or "").strip() or _now_iso(),
        "location": str(pool["location"] or ""),
        "visibility": str(conditions.get("visibility") or "public"),
        "source_type": "event_pool",
        "source_reference": pool_id,
        "dedupe_key": f"event_pool:{pool_id}:{persona_id}",
        "metadata": {"pool_id": pool_id, "pool_title": str(pool["title"] or "")},
        "participants": [],
    }
    try:
        from app.v3 import life_events  # noqa: PLC0415

        event_id, _created = life_events.add_event(db, payload)
    except Exception as exc:  # noqa: BLE001 - 事件生成失败不能拖垮领取
        log.warning("事件池领取生成事件失败: %s", exc)
        return ""
    return event_id

__all__ = [
    "POOL_STATUSES",
    "SCORE_WEIGHTS",
    "router",
    "score_persona",
]
