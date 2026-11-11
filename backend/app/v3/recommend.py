"""V3 · 个性化推荐流（用户专属快照 + 综合排序 + 20% 探索）。

为什么需要快照
--------------
旧信息流是「每次请求按 created_at 合并两类动态」，翻页靠 ``created_at|id`` 游标。
一旦排序引入兴趣/好友/已看等**非时间**因素，下一页用时间游标就会漏条目或重复；
而且同一用户在两次刷新之间看到的顺序会跳。方案因此是：**每次用户开始新的一次浏览
时生成一份快照（有序的 ``[{kind,id}]``）**，之后翻页只读快照 + 偏移量，
``next_cursor = "snap:<snapshot_id>:<offset>"``，30 分钟内稳定、可复现。

排序（分数越高越靠前）
----------------------
- 兴趣命中：动态 tags ∩ 用户兴趣 × 3；人物 interests ∩ 用户兴趣 × 2
- 好友关系：accepted +4 / pending +1
- 近 7 天与人物有互动（social_events）× 2
- 新鲜度：按小时指数衰减（半衰期 36h，满分 2）
- 已看降权：``v3_feed_seen`` 中 48h 内出现过 −3（仍保留在流里，避免空流）
- **本人动态全部包含且排在最前**（``is_mine`` 语义由 service 序列化保持）
- **20% 探索位**：快照容量里留 20% 给「按分数本会被截断/排在最后」的低分候选，
  用 ``random.Random(snapshot_id)`` 打乱后按固定步长插入正序，保证同一快照内稳定。

标签统一走 :mod:`app.v3.taxonomy`：写入数据库与打分时都用稳定 ID，
显示名由 service 层用 ``labels_map`` 下发。

表
--
- ``v3_feed_snapshots(id, user_id, created_at, expires_at, payload)``：快照本体（payload 是 JSON）
- ``v3_feed_seen(user_id, item_key, seen_at)``：已看登记，``item_key = "<kind>:<id>"``
"""

from __future__ import annotations

import json
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.logging_utils import get_logger
from app.v3 import taxonomy

log = get_logger(__name__)

#: 快照有效期：30 分钟内翻页稳定，过期后下一次请求自动重建
SNAPSHOT_TTL_S = 30 * 60
#: 单份快照默认容量（页大小由 API 的 limit 决定，与快照容量无关）
SNAPSHOT_LIMIT = 300
#: 探索位比例
EXPLORE_RATIO = 0.2
#: 探索位插入步长（每 5 条正序插入 1 条探索）
EXPLORE_STEP = max(1, int(round(1 / EXPLORE_RATIO)))
#: 已看降权的时间窗（小时）
SEEN_RECENT_H = 48.0
#: 「近期互动」时间窗（天）
INTERACTION_RECENT_DAYS = 7
#: 新鲜度半衰期（小时）
FRESH_HALF_LIFE_H = 36.0
#: 候选抓取倍数：快照容量 × N（再按分数截断）
CANDIDATE_FETCH_FACTOR = 3
#: 候选抓取硬上限，避免极端库把内存打爆
CANDIDATE_FETCH_MAX = 3000

#: 排序权重
W_POST_TAG = 3.0
W_PERSONA_INTEREST = 2.0
W_FRIEND_ACCEPTED = 4.0
W_FRIEND_PENDING = 1.0
W_RECENT_INTERACTION = 2.0
W_FRESH_MAX = 2.0
W_SEEN_RECENT = -3.0

_DDL = """
CREATE TABLE IF NOT EXISTS v3_feed_snapshots (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v3_feed_snapshots_user
    ON v3_feed_snapshots(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_v3_feed_snapshots_expires
    ON v3_feed_snapshots(expires_at);
CREATE TABLE IF NOT EXISTS v3_feed_seen (
    user_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (user_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_v3_feed_seen_user
    ON v3_feed_seen(user_id, seen_at DESC);
"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now_utc()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_iso(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ts(raw: Any) -> float:
    dt = _parse_iso(raw)
    return dt.timestamp() if dt is not None else 0.0


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def item_key(kind: str, item_id: Any) -> str:
    """条目稳定键：``<kind>:<id>``（已看登记 / 快照去重用）。"""
    return f"{str(kind or '').strip()}:{str(item_id or '').strip()}"


# --------------------------------------------------------------------------- #
# 建表
# --------------------------------------------------------------------------- #
def ensure_recommend_schema(db: Any) -> None:
    """幂等建表（Lead 在 main.py 统一接线；feed() 里也会懒建，保证测试/老库可用）。"""
    if db is None:
        return
    with db._lock, db.conn:  # noqa: SLF001 - 与 DB 层共用连接与锁
        db.conn.executescript(_DDL)


# --------------------------------------------------------------------------- #
# 用户画像
# --------------------------------------------------------------------------- #
def _profile(db: Any, user_id: str) -> dict:
    try:
        return dict(db.get_profile(user_id=user_id) or {})
    except Exception as exc:  # noqa: BLE001 - 画像读取失败不应让信息流 500
        log.debug("读取用户画像失败(%s): %s", user_id, exc)
        return {}


def _fact_sheet(profile: dict) -> dict:
    fact = (profile or {}).get("fact_sheet") or {}
    if isinstance(fact, str):
        try:
            fact = json.loads(fact)
        except (TypeError, ValueError):
            fact = {}
    return fact if isinstance(fact, dict) else {}


def profile_interest_ids(profile: dict) -> list[str]:
    """从资料 dict 里取兴趣的**稳定标签 ID**（统一走 taxonomy）。"""
    tags = _fact_sheet(profile).get("interests")
    return taxonomy.canonical_tags(tags)


def user_interest_ids(db: Any, user_id: str) -> list[str]:
    """按 user_id 读取兴趣（修复旧代码 ``get_profile()`` 不传 user_id 读到 local 的 bug）。"""
    return profile_interest_ids(_profile(db, user_id))


# --------------------------------------------------------------------------- #
# 游标
# --------------------------------------------------------------------------- #
def make_cursor(snapshot_id: str, offset: int) -> str:
    return f"snap:{str(snapshot_id).strip()}:{max(0, int(offset))}"


def parse_cursor(cursor: Any) -> tuple[str, int] | None:
    """``snap:<id>:<offset>`` → ``(id, offset)``；非法返回 None（调用方重建快照）。"""
    parts = str(cursor or "").split(":")
    if len(parts) != 3 or parts[0] != "snap":
        return None
    snapshot_id = parts[1].strip()
    try:
        offset = int(parts[2])
    except (TypeError, ValueError):
        return None
    if not snapshot_id or offset < 0:
        return None
    return snapshot_id, offset


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #
def _age_hours(created_at: Any, now: datetime) -> float:
    dt = _parse_iso(created_at)
    if dt is None:
        return 10 ** 6  # 时间不可解析：当作很旧，只吃「已看」等负分
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def _fresh_score(age_h: float) -> float:
    if age_h <= 0:
        return W_FRESH_MAX
    return W_FRESH_MAX * (0.5 ** (age_h / FRESH_HALF_LIFE_H))


def _candidate_rows(db: Any, *, targets: list[str], now: datetime, fetch_limit: int) -> list[Any]:
    sql = (
        "SELECT pp.id AS id, pp.persona_id AS persona_id, pp.tags AS tags,"
        " pp.created_at AS created_at"
        " FROM persona_posts pp"
        " JOIN personas p ON p.id = pp.persona_id"
        " WHERE pp.visibility='public' AND p.status='active'"
        " AND (pp.expires_at='' OR pp.expires_at>?)"
    )
    params: list[Any] = [_iso(now)]
    if targets:
        marks = ",".join("?" * len(targets))
        sql += f" AND pp.language IN ({marks})"
        params.extend(targets)
    sql += " ORDER BY pp.created_at DESC, pp.id DESC LIMIT ?"
    params.append(int(fetch_limit))
    with db._lock:  # noqa: SLF001
        return db.conn.execute(sql, tuple(params)).fetchall()


def _own_post_rows(db: Any, user_id: str) -> list[Any]:
    """本人动态**全部**参与快照（public/friends/private 三种可见性都算）。"""
    with db._lock:  # noqa: SLF001
        return db.conn.execute(
            "SELECT id, created_at FROM user_posts WHERE user_id=?"
            " ORDER BY created_at DESC, id DESC",
            (user_id,),
        ).fetchall()


def _persona_interest_map(db: Any) -> dict[str, set[str]]:
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT id, interests FROM personas WHERE status='active'"
        ).fetchall()
    return {
        str(r["id"]): set(taxonomy.canonical_tags(_loads(r["interests"], [])))
        for r in rows
    }


def _friendship_sets(db: Any, user_id: str) -> tuple[set[str], set[str]]:
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT persona_id, status FROM friendships WHERE user_id=?", (user_id,)
        ).fetchall()
    accepted = {str(r["persona_id"]) for r in rows if str(r["status"]) == "accepted"}
    pending = {str(r["persona_id"]) for r in rows if str(r["status"]) == "pending"}
    return accepted, pending


def _recent_interaction_ids(db: Any, user_id: str, now: datetime) -> set[str]:
    """近 7 天与当前用户产生过社交事件的人物（点赞/评论/好友申请都算互动）。"""
    since = _iso(now - timedelta(days=INTERACTION_RECENT_DAYS))
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT DISTINCT persona_id FROM social_events"
            " WHERE user_id=? AND created_at>=? AND COALESCE(persona_id,'')<>''",
            (user_id, since),
        ).fetchall()
    return {str(r["persona_id"]) for r in rows}


def _seen_keys(db: Any, user_id: str, now: datetime) -> set[str]:
    since = _iso(now - timedelta(hours=SEEN_RECENT_H))
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT item_key FROM v3_feed_seen WHERE user_id=? AND seen_at>=?",
            (user_id, since),
        ).fetchall()
    return {str(r["item_key"]) for r in rows}


def _score_candidates(
    db: Any,
    user_id: str,
    *,
    interests: set[str],
    targets: list[str],
    now: datetime,
    fetch_limit: int,
) -> list[dict]:
    persona_interests = _persona_interest_map(db)
    accepted, pending = _friendship_sets(db, user_id)
    recent = _recent_interaction_ids(db, user_id, now)
    seen = _seen_keys(db, user_id, now)
    persona_posts = _candidate_rows(db, targets=targets, now=now, fetch_limit=fetch_limit)

    candidates: list[dict] = []
    for row in persona_posts:
        pid = str(row["persona_id"])
        key = item_key("persona_post", row["id"])
        post_tags = set(taxonomy.canonical_tags(_loads(row["tags"], [])))
        score = W_POST_TAG * len(post_tags & interests)
        score += W_PERSONA_INTEREST * len(persona_interests.get(pid, set()) & interests)
        if pid in accepted:
            score += W_FRIEND_ACCEPTED
        elif pid in pending:
            score += W_FRIEND_PENDING
        if pid in recent:
            score += W_RECENT_INTERACTION
        score += _fresh_score(_age_hours(row["created_at"], now))
        is_seen = key in seen
        if is_seen:
            score += W_SEEN_RECENT
        candidates.append(
            {
                "kind": "persona_post",
                "id": str(row["id"]),
                "key": key,
                "persona_id": pid,
                "score": round(float(score), 6),
                "ts": _ts(row["created_at"]),
                "seen": is_seen,
            }
        )
    candidates.sort(key=lambda c: (-c["score"], -c["ts"], c["key"]))
    return candidates


# --------------------------------------------------------------------------- #
# 探索位
# --------------------------------------------------------------------------- #
def _split_exploration(
    ranked: list[dict], *, capacity: int, rng: random.Random
) -> tuple[list[dict], list[dict]]:
    """把已排序候选切成「正序」+「探索」两段。

    - 正序 = 按分数最高的 (n - explore_n) 条；
    - 探索 = 本会被截断 / 排在最后的低分候选，优先挑 48h 内没看过的，
      用 ``rng``（按快照 id 播种）打乱 → 同快照稳定、不同快照有变化。
    """
    if not ranked or capacity <= 0:
        return [], []
    n = min(len(ranked), capacity)
    explore_n = int(round(EXPLORE_RATIO * n))
    if explore_n <= 0 or n - explore_n <= 0:
        return ranked[:n], []

    regular = ranked[: n - explore_n]
    # 未进入正序的低分候选：候选多于容量时是「被截断的」，否则是最低分的 20%
    pool = ranked[n - explore_n:]
    unseen = [c for c in pool if not c["seen"]]
    seen = [c for c in pool if c["seen"]]
    rng.shuffle(unseen)
    rng.shuffle(seen)
    explore = (unseen + seen)[:explore_n]
    return regular, explore


def _interleave(regular: list[dict], explore: list[dict]) -> list[dict]:
    """把探索条目按固定步长插进正序（首条永远是正序最高分）。"""
    if not explore:
        return list(regular)
    out: list[dict] = []
    index = 0
    for slot, item in enumerate(regular, start=1):
        out.append(item)
        if index < len(explore) and slot % EXPLORE_STEP == 0:
            out.append(explore[index])
            index += 1
    if index < len(explore):
        out.extend(explore[index:])
    return out


# --------------------------------------------------------------------------- #
# 快照读写
# --------------------------------------------------------------------------- #
def build_snapshot(
    db: Any, user_id: str, *, limit: int = SNAPSHOT_LIMIT, now: datetime | None = None
) -> dict:
    """构建并落库一份用户专属快照。

    返回 ``{"id", "user_id", "created_at", "expires_at", "items": [{"kind","id"}]}``；
    ``items`` 有序且**本人动态在最前**。同一用户每次新建快照都会得到新的 id，
    因此 diff 快照之间排序可以变化，但单份快照内翻页永远稳定。
    """
    ensure_recommend_schema(db)
    now = now or _now_utc()
    uid = str(user_id or "")
    limit = max(1, int(limit))

    profile = _profile(db, uid)
    interests = set(profile_interest_ids(profile))
    targets = [str(x) for x in (profile.get("target_languages") or []) if str(x).strip()]

    fetch_limit = min(
        max(int(limit) * CANDIDATE_FETCH_FACTOR, limit + 1), CANDIDATE_FETCH_MAX
    )
    ranked = _score_candidates(
        db, uid, interests=interests, targets=targets, now=now, fetch_limit=fetch_limit
    )

    own_items = [
        {"kind": "user_post", "id": str(row["id"])} for row in _own_post_rows(db, uid)
    ]
    capacity = max(0, limit - len(own_items))

    snapshot_id = f"fs_{uuid.uuid4().hex}"
    rng = random.Random(snapshot_id)
    regular, explore = _split_exploration(ranked, capacity=capacity, rng=rng)
    ordered = own_items + [
        {"kind": item["kind"], "id": item["id"]}
        for item in _interleave(regular, explore)
    ]

    created_at = _iso(now)
    expires_at = _iso(now + timedelta(seconds=SNAPSHOT_TTL_S))
    payload = _dumps({"items": ordered, "explore": len(explore)})
    with db._lock, db.conn:  # noqa: SLF001
        # 顺手清理过期快照，避免表无限增长
        db.conn.execute("DELETE FROM v3_feed_snapshots WHERE expires_at<=?", (created_at,))
        db.conn.execute(
            "INSERT INTO v3_feed_snapshots (id, user_id, created_at, expires_at, payload)"
            " VALUES (?,?,?,?,?)",
            (snapshot_id, uid, created_at, expires_at, payload),
        )
    return {
        "id": snapshot_id,
        "user_id": uid,
        "created_at": created_at,
        "expires_at": expires_at,
        "items": ordered,
    }


def load_snapshot(
    db: Any, user_id: str, cursor: Any, *, now: datetime | None = None
) -> tuple[dict, int] | None:
    """按游标读取快照 → ``(snapshot, offset)``；非法 / 非本人 / 过期返回 None（调用方重建）。"""
    parsed = parse_cursor(cursor)
    if parsed is None:
        return None
    snapshot_id, offset = parsed
    now = now or _now_utc()
    try:
        with db._lock:  # noqa: SLF001
            row = db.conn.execute(
                "SELECT * FROM v3_feed_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
    except sqlite3.Error:  # 表还没建（老库/未接线）→ 交给调用方重建
        return None
    if row is None:
        return None
    uid = str(user_id or "")
    if str(row["user_id"]) != uid:
        # 快照归属校验：不能用别人的快照游标
        return None
    expires_at = str(row["expires_at"] or "")
    if expires_at and expires_at <= _iso(now):
        return None
    payload = _loads(row["payload"], {})
    raw_items = payload.get("items") if isinstance(payload, dict) else payload
    items = [
        {"kind": str(item["kind"]), "id": str(item["id"])}
        for item in (raw_items or [])
        if isinstance(item, dict) and item.get("kind") and item.get("id")
    ]
    if offset > len(items):
        return None
    return (
        {
            "id": str(row["id"]),
            "user_id": uid,
            "created_at": str(row["created_at"]),
            "expires_at": expires_at,
            "items": items,
        },
        offset,
    )


def _normalize_items(items: Iterable[Any] | None) -> list[str]:
    keys: list[str] = []
    for raw in items or []:
        if isinstance(raw, dict):
            key = item_key(raw.get("kind"), raw.get("id"))
        elif isinstance(raw, (tuple, list)) and len(raw) >= 2:
            key = item_key(raw[0], raw[1])
        else:
            key = str(raw or "").strip()
        if not key or key.endswith(":") or key.startswith(":"):
            continue
        if key not in keys:
            keys.append(key)
    return keys


def mark_seen(
    db: Any, user_id: str, items: Iterable[Any] | None, *, now: datetime | None = None
) -> int:
    """登记「已看」（幂等 upsert，重复登记只刷新时间）。

    ``items`` 接受 ``[{"kind","id"}]``（feed 条目）、``[(kind,id)]`` 或已拼好的
    ``"<kind>:<id>"`` 字符串。
    """
    keys = _normalize_items(items)
    if not keys:
        return 0
    stamp = _iso(now)
    uid = str(user_id or "")
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.executemany(
            "INSERT OR REPLACE INTO v3_feed_seen (user_id, item_key, seen_at)"
            " VALUES (?,?,?)",
            [(uid, key, stamp) for key in keys],
        )
    return len(keys)


__all__ = [
    "EXPLORE_RATIO",
    "SNAPSHOT_LIMIT",
    "SNAPSHOT_TTL_S",
    "build_snapshot",
    "ensure_recommend_schema",
    "item_key",
    "load_snapshot",
    "make_cursor",
    "mark_seen",
    "parse_cursor",
    "profile_interest_ids",
    "user_interest_ids",
]
