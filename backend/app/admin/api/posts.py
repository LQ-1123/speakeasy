"""管理 API · 动态待发队列（``/api/admin/posts*``）—— 第 3 期（P3）。

接口
----
``GET    /posts/personas``                     可选角色（写动态用）
``GET    /posts/pending``                      待发队列（``post_queue.status='queued'``）
``GET    /posts/published``                    真已发动态（``persona_posts``）
``POST   /posts``                              手动排一条（或立即发布）
``PUT    /posts/{queue_id}``                   编辑待发（只有 queued 能改）
``DELETE /posts/{queue_id}``                   取消（status='cancelled'，不物理删除）
``POST   /posts/{queue_id}/publish-now``       立即发布（写 persona_posts + 回填）

发布路径复用（关键）
--------------------
「写入 ``persona_posts``」**不另写 INSERT**，而是调用自动发动态同一条路：
``app.v3.event_posts._insert_post``。它维护的字段/约束一个都不能漏——

- ``id``（``pp_`` 前缀）、``visibility='public'``、``like_count=0`` / ``comment_count=0``
- ``created_at`` / ``published_at``（同时写，信息流两种排序都能用）
- ``post_kind`` / ``post_type='life_event'`` / ``source_event_id`` / ``metadata``
- 部分唯一索引 ``uq_pp_event_persona (source_event_id, persona_id) WHERE source_event_id<>''``
  ——这也是**发布幂等**的来源：同一个 ``source_event_id`` + 角色，第二次调用只会
  返回已存在的那条动态，不会重复发。

``_insert_post`` 唯一不覆盖的是**图片**（它专用于纯文字事件动态），所以带图时在同一把锁
里补一条 ``UPDATE media/post_kind/alt_text/source_ref``——只补字段，不另写发布逻辑。

幂等约定
--------
- ``publish-now`` 用 ``source_event_id='queue:{queue_id}'``（或该队列条目自己的
  ``source_event_id``）作为幂等键；重复点同一个条目返回同一条动态；
- 条目已是 ``published`` 且回填过 ``published_post_id`` → 直接返回该 id（``already=true``）；
- ``DELETE``（取消）对已取消条目幂等返回成功；已发布的拒绝取消。

失败约定
--------
业务失败统一 ``200 + {"ok": false, "issues": [...]}``；只有参数本身非法（缺必填、
body 类型不对）才 ``400``。
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
router = APIRouter(prefix="/posts")

_DB_NOT_READY: dict[str, Any] = {"ok": False, "issues": ["数据库未就绪"]}

#: ``post_queue`` 的合法状态（与契约 §2 一致）。
QUEUE_STATUSES: tuple[str, ...] = ("queued", "published", "cancelled")

#: 队列单条动态的公共查询列（带角色昵称/头像字段，供前端直接渲染）。
_QUEUE_COLUMNS = (
    "q.id, q.persona_id, q.content, q.media, q.source_type, q.source_event_id,"
    " q.status, q.publish_at, q.created_at, q.published_post_id,"
    " p.nickname, p.avatar, p.avatar_seed, p.avatar_style, p.avatar_options,"
    " p.gender, p.country, p.age"
)

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

def _max_post_chars() -> int:
    """正文长度上限（复用 ``event_posts.MAX_POST_CHARS``，不另立常量）。"""
    from app.v3 import event_posts  # noqa: PLC0415

    return int(event_posts.MAX_POST_CHARS)

def _persona_avatar(persona: Any) -> str:
    try:
        from app.v3.avatar import persona_avatar_url  # noqa: PLC0415

        return persona_avatar_url(dict(persona))
    except Exception:  # noqa: BLE001 - 头像失败不能拖垮列表
        return ""

def _normalize_media(value: Any) -> list[str]:
    """媒体路径归一：``str`` / ``list[str]`` → ``list[str]``（去空、去重保序）。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out

def _persona_row(db: Any, persona_id: str) -> Any:
    with db._lock:  # noqa: SLF001 - 与 DB 层共用同一把锁
        return db.conn.execute(
            "SELECT id, nickname, avatar, avatar_seed, avatar_style, avatar_options,"
            " gender, country, age, language FROM personas WHERE id=?",
            (persona_id,),
        ).fetchone()

def _persona_public(row: Any) -> dict[str, Any]:
    if row is None:
        return {"persona_id": "", "persona_nickname": "", "persona_avatar": ""}
    return {
        "persona_id": str(row["id"] or ""),
        "persona_nickname": str(row["nickname"] or ""),
        "persona_avatar": _persona_avatar(row),
    }

def _validate_content(content: str, media: list[str]) -> list[str]:
    issues: list[str] = []
    limit = _max_post_chars()
    if len(content) > limit:
        issues.append(f"正文超过 {limit} 字（当前 {len(content)} 字）")
    if not content.strip() and not media:
        issues.append("正文与图片不能同时为空")
    return issues

def _queue_public(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"] or ""),
        "persona_id": str(row["persona_id"] or ""),
        "persona_nickname": str(row["nickname"] or ""),
        "persona_avatar": _persona_avatar(row),
        "content": str(row["content"] or ""),
        "media": _loads(row["media"], []),
        "source_type": str(row["source_type"] or "manual"),
        "source_event_id": str(row["source_event_id"] or ""),
        "status": str(row["status"] or ""),
        "publish_at": str(row["publish_at"] or ""),
        "created_at": str(row["created_at"] or ""),
        "published_post_id": str(row["published_post_id"] or ""),
    }

def _queue_row(db: Any, queue_id: str) -> Any:
    with db._lock:  # noqa: SLF001
        return db.conn.execute(
            f"SELECT {_QUEUE_COLUMNS} FROM post_queue q"
            " LEFT JOIN personas p ON p.id=q.persona_id WHERE q.id=?",
            (queue_id,),
        ).fetchone()

# --------------------------------------------------------------------------- #
# 发布：复用 event_posts 的写入路径
# --------------------------------------------------------------------------- #
def _publish_persona_post(
    db: Any,
    *,
    persona: Any,
    content: str,
    media: list[str],
    source_event_id: str,
) -> tuple[dict[str, Any], bool]:
    """把一条内容发布到 ``persona_posts``（复用自动发动态的同一条路）。

    返回 ``(post_public, created)``；``created=False`` 表示命中 ``_insert_post`` 的
    ``(source_event_id, persona_id)`` 幂等判定，返回的是已存在的那条。
    """
    from app.v3 import event_posts  # noqa: PLC0415

    persona_dict = dict(persona)
    # ``_insert_post`` 只为事件动态设计，事件事实只影响 metadata，这里给一个
    # 明确标记为「运营手动发布」的合成事件，避免伪造不存在的世界事件语义。
    event = {
        "id": source_event_id,
        "title": str(content or "").strip()[:60] or "手动动态",
        "description": "",
        "event_type": "manual_post",
        "event_status": "completed",
        "start_time": _now_iso(),
        "location": "",
        "participants": [],
    }
    before = _find_by_source_event(db, source_event_id, str(persona_dict.get("id") or ""))
    post = event_posts._insert_post(  # noqa: SLF001 - 发布唯一入口
        db,
        persona=persona_dict,
        event=event,
        event_id=source_event_id,
        content=content,
    )
    post_id = str((post or {}).get("id") or "")
    if media and post_id:
        # ``_insert_post`` 不写 media（事件动态是纯文字）：只补这几个字段，不重写行。
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE persona_posts SET media=?, post_kind=?, alt_text=?, source_ref=?"
                " WHERE id=?",
                (
                    _dumps(media),
                    "life_photo",
                    str(content or "").strip()[:120],
                    _dumps({"kind": "admin_upload"}),
                    post_id,
                ),
            )
    detail = _post_public(db, post_id) if post_id else {}
    created = before is None
    return detail, created

def _find_by_source_event(db: Any, source_event_id: str, persona_id: str) -> Any:
    if not source_event_id or not persona_id:
        return None
    with db._lock:  # noqa: SLF001
        return db.conn.execute(
            "SELECT id FROM persona_posts WHERE source_event_id=? AND persona_id=? LIMIT 1",
            (source_event_id, persona_id),
        ).fetchone()

def _post_public(db: Any, post_id: str) -> dict[str, Any]:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT pp.id, pp.persona_id, pp.content, pp.media, pp.tags, pp.visibility,"
            " pp.post_kind, pp.post_type, pp.like_count, pp.comment_count, pp.created_at,"
            " pp.published_at, pp.source_event_id,"
            " p.nickname, p.avatar, p.avatar_seed, p.avatar_style, p.avatar_options,"
            " p.gender, p.country, p.age"
            " FROM persona_posts pp LEFT JOIN personas p ON p.id=pp.persona_id"
            " WHERE pp.id=?",
            (post_id,),
        ).fetchone()
    if row is None:
        return {}
    return {
        "id": str(row["id"] or ""),
        "persona_id": str(row["persona_id"] or ""),
        "persona_nickname": str(row["nickname"] or ""),
        "persona_avatar": _persona_avatar(row),
        "content": str(row["content"] or ""),
        "media": _loads(row["media"], []),
        "tags": _loads(row["tags"], []),
        "visibility": str(row["visibility"] or ""),
        "post_kind": str(row["post_kind"] or ""),
        "post_type": str(row["post_type"] or ""),
        "like_count": int(row["like_count"] or 0),
        "comment_count": int(row["comment_count"] or 0),
        "created_at": str(row["created_at"] or ""),
        "published_at": str(row["published_at"] or ""),
        "source_event_id": str(row["source_event_id"] or ""),
    }

# --------------------------------------------------------------------------- #
# 只读列表
# --------------------------------------------------------------------------- #
@router.get("/personas")
async def list_post_personas(request: Request) -> dict:
    """可写动态的角色（活跃角色：id + 昵称 + 头像）。"""
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                "SELECT id, nickname, avatar, avatar_seed, avatar_style, avatar_options,"
                " gender, country, age FROM personas WHERE status='active' ORDER BY nickname"
            ).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover
        log.debug("读取角色列表失败（忽略）: %s", exc)
        return {"ok": True, "items": []}
    return {
        "ok": True,
        "items": [
            {
                "id": str(row["id"] or ""),
                "nickname": str(row["nickname"] or ""),
                "avatar": _persona_avatar(row),
            }
            for row in rows
        ],
    }

@router.get("/pending")
async def list_pending_posts(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """待发队列：``post_queue.status='queued'``，按排期时间升序（无排期的排最后）。"""
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY
    try:
        with db._lock:  # noqa: SLF001
            total = int(
                db.conn.execute(
                    "SELECT COUNT(*) AS n FROM post_queue WHERE status='queued'"
                ).fetchone()["n"]
                or 0
            )
            rows = db.conn.execute(
                f"SELECT {_QUEUE_COLUMNS} FROM post_queue q"
                " LEFT JOIN personas p ON p.id=q.persona_id"
                " WHERE q.status='queued'"
                " ORDER BY CASE WHEN q.publish_at='' THEN 1 ELSE 0 END,"
                " q.publish_at, q.created_at DESC, q.id"
                " LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - 极老库
        log.debug("读取待发队列失败（忽略）: %s", exc)
        return {"ok": True, "total": 0, "offset": int(offset), "limit": int(limit), "items": []}
    return {
        "ok": True,
        "total": total,
        "offset": int(offset),
        "limit": int(limit),
        "items": [_queue_public(row) for row in rows],
    }

@router.get("/published")
async def list_published_posts(
    request: Request,
    persona_id: str = Query("", description="按角色筛选"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """已发布动态（``persona_posts`` 真已发，不做「待发」过滤——队列在独立表里）。"""
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    where = ""
    params: list[Any] = []
    persona_value = str(persona_id or "").strip()
    if persona_value:
        where = " WHERE pp.persona_id=?"
        params.append(persona_value)
    try:
        with db._lock:  # noqa: SLF001
            total = int(
                db.conn.execute(
                    f"SELECT COUNT(*) AS n FROM persona_posts pp{where}", tuple(params)
                ).fetchone()["n"]
                or 0
            )
            rows = db.conn.execute(
                "SELECT pp.id, pp.persona_id, pp.content, pp.media, pp.tags, pp.visibility,"
                " pp.post_kind, pp.post_type, pp.like_count, pp.comment_count, pp.created_at,"
                " pp.published_at, pp.source_event_id,"
                " p.nickname, p.avatar, p.avatar_seed, p.avatar_style, p.avatar_options,"
                " p.gender, p.country, p.age"
                f" FROM persona_posts pp LEFT JOIN personas p ON p.id=pp.persona_id{where}"
                " ORDER BY COALESCE(NULLIF(pp.published_at, ''), pp.created_at) DESC, pp.id DESC"
                " LIMIT ? OFFSET ?",
                (*params, int(limit), int(offset)),
            ).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover
        log.debug("读取已发布动态失败（忽略）: %s", exc)
        return {"ok": True, "total": 0, "offset": int(offset), "limit": int(limit), "items": []}

    items = [
        {
            "id": str(row["id"] or ""),
            "persona_id": str(row["persona_id"] or ""),
            "persona_nickname": str(row["nickname"] or ""),
            "persona_avatar": _persona_avatar(row),
            "content": str(row["content"] or ""),
            "media": _loads(row["media"], []),
            "visibility": str(row["visibility"] or ""),
            "post_type": str(row["post_type"] or ""),
            "like_count": int(row["like_count"] or 0),
            "comment_count": int(row["comment_count"] or 0),
            "created_at": str(row["created_at"] or ""),
            "published_at": str(row["published_at"] or ""),
            "source_event_id": str(row["source_event_id"] or ""),
        }
        for row in rows
    ]
    return {
        "ok": True,
        "total": total,
        "offset": int(offset),
        "limit": int(limit),
        "items": items,
    }

# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
@router.post("")
async def create_post(request: Request, body: dict = Body(...)) -> dict:
    """排一条动态：``publish_now=true`` 立即发到 ``persona_posts``，否则进待发队列。"""
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    data = body if isinstance(body, dict) else {}
    persona_id = str(data.get("persona_id") or "").strip()
    if not persona_id:
        raise HTTPException(status_code=400, detail="persona_id 不能为空")

    content = str(data.get("content") or "").strip()
    media = _normalize_media(data.get("media"))
    source_event_id = str(data.get("source_event_id") or "").strip()
    publish_at = str(data.get("publish_at") or "").strip()
    publish_now = bool(data.get("publish_now"))

    persona = _persona_row(db, persona_id)
    if persona is None:
        return {"ok": False, "issues": ["角色不存在"]}
    issues = _validate_content(content, media)
    if issues:
        return {"ok": False, "issues": issues}

    if publish_now:
        key = source_event_id or f"admin_post:{uuid.uuid4().hex[:12]}"
        detail, created = _publish_persona_post(
            db, persona=persona, content=content, media=media, source_event_id=key
        )
        _audit(
            request,
            "posts.publish",
            f"posts/{detail.get('id', '')}",
            f"立即发布 persona={persona_id} 长度={len(content)} 图片={len(media)} created={created}",
        )
        return {"ok": True, "published": True, "created": created, "post": detail}

    queue_id = f"pq_{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO post_queue (id, persona_id, content, media, source_type,"
                " source_event_id, status, publish_at, created_at, published_post_id)"
                " VALUES (?,?,?,?,?,?,'queued',?,?,'')",
                (
                    queue_id,
                    persona_id,
                    content,
                    _dumps(media),
                    "event" if source_event_id else "manual",
                    source_event_id,
                    publish_at,
                    now,
                ),
            )
    except sqlite3.Error as exc:
        log.warning("写入待发队列失败: %s", exc)
        return {"ok": False, "issues": [f"写入失败：{type(exc).__name__}"]}

    _audit(
        request,
        "posts.create",
        f"posts/{queue_id}",
        f"排期待发 persona={persona_id} 长度={len(content)} 图片={len(media)}",
    )
    row = _queue_row(db, queue_id)
    return {
        "ok": True,
        "published": False,
        "id": queue_id,
        "item": _queue_public(row) if row is not None else {},
    }

@router.put("/{queue_id}")
async def update_post(request: Request, queue_id: str, body: dict = Body(...)) -> dict:
    """编辑待发动态（**只有 queued 状态可改**；已发布的明确拒绝）。"""
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    qid = str(queue_id or "").strip()
    row = _queue_row(db, qid)
    if row is None:
        return {"ok": False, "issues": ["队列条目不存在"]}
    status = str(row["status"] or "")
    if status == "published":
        return {"ok": False, "issues": ["已发布，不能编辑"]}
    if status != "queued":
        return {"ok": False, "issues": [f"状态为 {status}，不能编辑"]}

    data = body if isinstance(body, dict) else {}
    updates: dict[str, Any] = {}
    if "content" in data:
        updates["content"] = str(data.get("content") or "").strip()
    if "media" in data:
        updates["media"] = _dumps(_normalize_media(data.get("media")))
    if "publish_at" in data:
        updates["publish_at"] = str(data.get("publish_at") or "").strip()
    if not updates:
        return {"ok": False, "issues": ["没有需要修改的字段"]}

    content = updates.get("content", str(row["content"] or ""))
    media = _normalize_media(
        updates.get("media", _loads(row["media"], []))
    )
    issues = _validate_content(str(content or ""), media)
    if issues:
        return {"ok": False, "issues": issues}

    sets = ", ".join(f"{name}=?" for name in updates)
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                f"UPDATE post_queue SET {sets} WHERE id=?", (*updates.values(), qid)
            )
    except sqlite3.Error as exc:  # pragma: no cover
        log.warning("更新待发队列失败: %s", exc)
        return {"ok": False, "issues": [f"写入失败：{type(exc).__name__}"]}

    _audit(
        request,
        "posts.update",
        f"posts/{qid}",
        f"编辑待发字段：{', '.join(updates)}",
    )
    fresh = _queue_row(db, qid)
    return {"ok": True, "item": _queue_public(fresh) if fresh is not None else {}}

@router.delete("/{queue_id}")
async def cancel_post(request: Request, queue_id: str) -> dict:
    """取消待发动态：``status='cancelled'``（**不物理删除**，留痕）。

    已取消 → 幂等返回成功；已发布 → 拒绝取消。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    qid = str(queue_id or "").strip()
    row = _queue_row(db, qid)
    if row is None:
        return {"ok": False, "issues": ["队列条目不存在"], "cancelled": False}
    status = str(row["status"] or "")
    if status == "published":
        return {"ok": False, "issues": ["已发布，不能取消"], "cancelled": False}
    if status == "cancelled":
        return {"ok": True, "cancelled": True, "already": True, "id": qid}

    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute("UPDATE post_queue SET status='cancelled' WHERE id=?", (qid,))
    except sqlite3.Error as exc:  # pragma: no cover
        log.warning("取消待发失败: %s", exc)
        return {"ok": False, "issues": [f"写入失败：{type(exc).__name__}"], "cancelled": False}

    _audit(request, "posts.cancel", f"posts/{qid}", f"取消待发 persona={row['persona_id']}")
    return {"ok": True, "cancelled": True, "id": qid}

@router.post("/{queue_id}/publish-now")
async def publish_now(request: Request, queue_id: str) -> dict:
    """立即发布队列条目：写 ``persona_posts`` + ``status='published'`` + 回填 id。

    **幂等**：``source_event_id='queue:{id}'``（或条目自带的 ``source_event_id``）
    让同一角色 + 同一幂等键只产生一条动态；已回填 ``published_post_id`` 时直接返回。
    """
    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY

    qid = str(queue_id or "").strip()
    row = _queue_row(db, qid)
    if row is None:
        return {"ok": False, "issues": ["队列条目不存在"]}
    status = str(row["status"] or "")
    if status == "published" and str(row["published_post_id"] or ""):
        return {
            "ok": True,
            "published": True,
            "already": True,
            "post_id": str(row["published_post_id"] or ""),
            "post": _post_public(db, str(row["published_post_id"] or "")),
        }
    if status == "cancelled":
        return {"ok": False, "issues": ["已取消，不能发布"]}

    content = str(row["content"] or "")
    media = _normalize_media(_loads(row["media"], []))
    issues = _validate_content(content, media)
    if issues:
        return {"ok": False, "issues": issues}

    persona = _persona_row(db, str(row["persona_id"] or ""))
    if persona is None:
        return {"ok": False, "issues": ["角色不存在"]}

    source_event_id = str(row["source_event_id"] or "").strip() or f"queue:{qid}"
    detail, created = _publish_persona_post(
        db,
        persona=persona,
        content=content,
        media=media,
        source_event_id=source_event_id,
    )
    post_id = str(detail.get("id") or "")
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE post_queue SET status='published', published_post_id=? WHERE id=?",
                (post_id, qid),
            )
    except sqlite3.Error as exc:  # pragma: no cover
        log.warning("回填发布结果失败: %s", exc)
        return {"ok": False, "issues": [f"回填失败：{type(exc).__name__}"], "post_id": post_id}

    _audit(
        request,
        "posts.publish",
        f"posts/{qid}",
        f"立即发布队列条目 persona={row['persona_id']} post={post_id} created={created}",
    )
    return {"ok": True, "published": True, "created": created, "post_id": post_id, "post": detail}

__all__ = ["QUEUE_STATUSES", "router"]
