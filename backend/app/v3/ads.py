"""V3 · 商业合作（广告）闭环：活动/素材/投放/频控/事件/留痕。

产品约束（对应升级方案第三期）
------------------------------
- **只对已核验成年、主动开启合作推荐的用户**投放；未成年/未知年龄/未开启一律不投。
- 聊天里：先正常回答，再以人物口吻呈现**审核过的模板 + 商品事实**；不虚构功效/价格/使用体验，
  商业段落带「广告·品牌合作」标识，不进入长期记忆与人物公共经历。
- 动态里：素材独立存储，只在个人推荐流混排，返回独立类型与投放 ID；不进入人物主页/偶遇/评论。
- 频控（内测默认）：两渠道合计每 24 小时 ≤2 次；聊天 ≤1 次/24h 且两次之间 ≥10 个用户回合；
  动态前 3 条不插、两条广告之间 ≥8 条普通动态；同品牌 7 天冷却；用户关闭后 30 天屏蔽该活动。
- 服务端**原子预留**额度（``BEGIN IMMEDIATE``），过期预留由惰性/后台释放。
- 留痕：匹配理由、活动与事实版本、最终文案、消息/素材引用、事件；触发消息与最多两条前文
  脱敏摘录合计 ≤1000 字符、30 天删除；品牌资质/审核内容/投放记录保留 ≥3 年（不删）。

本模块不含任何 LLM 调用：商业文案只从审核通过的素材模板与事实拼装，避免模型自由发挥。
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.logging_utils import get_logger
from app.v3 import accounts, taxonomy

log = get_logger(__name__)

#: 频控（内测默认，方案第三期）
TOTAL_PER_24H = 2
CHAT_PER_24H = 1
CHAT_TURN_GAP = 10
FEED_FIRST_SKIP = 3
FEED_POST_GAP = 8
BRAND_COOLDOWN_DAYS = 7
CLOSE_BLOCK_DAYS = 30
RESERVATION_TTL_S = 5 * 60
AUDIT_KEEP_DAYS = 30
ARCHIVE_KEEP_DAYS = 3 * 365

CHANNELS = ("chat", "feed")
CAMPAIGN_STATUS = ("draft", "reviewed", "enabled", "paused", "ended")
CREATIVE_STATUS = ("draft", "approved", "rejected")
DELIVERY_STATUS = (
    "reserved", "delivered", "impression", "clicked", "closed", "reported", "released",
)
EVENT_TYPES = ("impression", "click", "close", "report")

#: 聊天投放时明确表示「不需要/不确定/拒绝」的表述 → 不投
_REFUSAL_RE = re.compile(
    r"(算了吧|不用了?|不需要|不想买|不要了?|没兴趣|随便看看|再看看|不确定|再说吧|"
    r"no thanks|not interested|not sure|maybe later|no need|i'?m just browsing)",
    re.IGNORECASE,
)
#: 心理困扰/情感依赖类表述 → 不营销（方案明确禁止）
_DISTRESS_RE = re.compile(
    r"(焦虑|抑郁|崩溃|失眠|孤独|想哭|难过|压力好大|活着没意思|想死|"
    r"anxious|depress|panic|insomnia|lonely|hopeless|suicidal|can'?t sleep)",
    re.IGNORECASE,
)
#: 泛聊/寒暄，不构成购买需求
_VAGUE_RE = re.compile(
    r"^(hi|hello|hey|你好|在吗|早上好|晚安|好的|嗯+|ok|okay|谢谢|thanks|what'?s up)[!！。.\s]*$",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S+00:00").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _loads(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(str(raw)) if str(raw or "").strip() else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def ensure_ads_schema(db: Any) -> None:
    if db is None:
        return
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.executescript(DDL)
        setattr(db, "_ads_schema_ready", True)
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_ads_schema 失败（广告能力不可用）: %s: %s", type(exc).__name__, exc)


DDL = """
CREATE TABLE IF NOT EXISTS ads_brands (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    industry TEXT NOT NULL DEFAULT '',
    qualification TEXT NOT NULL DEFAULT '',
    landing_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ads_campaigns (
    id TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL,
    title TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    required_tags TEXT NOT NULL DEFAULT '[]',
    constraints TEXT NOT NULL DEFAULT '{}',
    facts TEXT NOT NULL DEFAULT '[]',
    landing_url TEXT NOT NULL DEFAULT '',
    channels TEXT NOT NULL DEFAULT '["chat","feed"]',
    valid_from TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ads_campaigns_brand ON ads_campaigns(brand_id, status);
CREATE TABLE IF NOT EXISTS ads_campaign_versions (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, version)
);
CREATE TABLE IF NOT EXISTS ads_creatives (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    campaign_version INTEGER NOT NULL,
    channel TEXT NOT NULL DEFAULT 'chat',
    title TEXT NOT NULL,
    body_template TEXT NOT NULL,
    facts TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    reviewed_by TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ads_creatives_campaign ON ads_creatives(campaign_id, status, channel);
CREATE TABLE IF NOT EXISTS ads_assignments (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'suggested',
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, persona_id)
);
CREATE TABLE IF NOT EXISTS ads_deliveries (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    campaign_version INTEGER NOT NULL DEFAULT 1,
    brand_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    persona_id TEXT NOT NULL DEFAULT '',
    creative_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'reserved',
    reason TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT -1,
    reserved_at TEXT NOT NULL,
    confirmed_at TEXT NOT NULL DEFAULT '',
    last_event_at TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ads_deliveries_user ON ads_deliveries(user_id, reserved_at DESC);
CREATE INDEX IF NOT EXISTS idx_ads_deliveries_campaign ON ads_deliveries(campaign_id, status);
CREATE TABLE IF NOT EXISTS ads_events (
    id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ads_events_delivery ON ads_events(delivery_id, created_at);
CREATE TABLE IF NOT EXISTS ads_chat_audit (
    delivery_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    campaign_version INTEGER NOT NULL DEFAULT 1,
    match_reason TEXT NOT NULL DEFAULT '{}',
    trigger_text TEXT NOT NULL DEFAULT '',
    pre_context TEXT NOT NULL DEFAULT '',
    copy TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    delete_after TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ads_audit_created ON ads_chat_audit(created_at);
CREATE TABLE IF NOT EXISTS ads_feed_state (
    user_id TEXT PRIMARY KEY,
    items_since_ad INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ads_blocks (
    user_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    blocked_until TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'close',
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, campaign_id)
);
"""


def _ensure(db: Any) -> bool:
    if db is None:
        return False
    if getattr(db, "_ads_schema_ready", False):
        return True
    ensure_ads_schema(db)
    return bool(getattr(db, "_ads_schema_ready", False))


# --------------------------------------------------------------------------- #
# 品牌 / 活动 / 素材
# --------------------------------------------------------------------------- #
def create_brand(
    db: Any, *, name: str, industry: str = "", qualification: str = "",
    landing_url: str = "", note: str = "",
) -> dict:
    bid = _new_id("br")
    now = _iso()
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO ads_brands (id, name, industry, qualification, landing_url, status,"
            " note, created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?,?)",
            (bid, str(name)[:80], str(industry)[:40], str(qualification)[:2000],
             str(landing_url)[:500], str(note)[:500], now, now),
        )
    return get_brand(db, bid) or {}


def update_brand(db: Any, brand_id: str, **fields: Any) -> dict:
    allowed = {"name", "industry", "qualification", "landing_url", "status", "note"}
    payload = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not payload:
        return get_brand(db, brand_id) or {}
    sets = ", ".join(f"{key}=?" for key in payload)
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            f"UPDATE ads_brands SET {sets}, updated_at=? WHERE id=?",
            (*payload.values(), _iso(), brand_id),
        )
    return get_brand(db, brand_id) or {}


def get_brand(db: Any, brand_id: str) -> dict | None:
    if not _ensure(db):
        return None
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM ads_brands WHERE id=?", (brand_id,)).fetchone()
    return dict(row) if row is not None else None


def list_brands(db: Any, *, limit: int = 100) -> list[dict]:
    if not _ensure(db):
        return []
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT * FROM ads_brands ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(r) for r in rows]


def _campaign_payload(
    *, brand_id: str, title: str, purpose: str, required_tags: Iterable[str],
    constraints: dict, facts: list[str], landing_url: str, channels: Iterable[str],
    valid_from: str, valid_until: str,
) -> dict:
    return {
        "brand_id": brand_id,
        "title": str(title)[:120],
        "purpose": str(purpose)[:200],
        "required_tags": taxonomy.canonical_tags(required_tags, limit=12),
        "constraints": constraints if isinstance(constraints, dict) else {},
        "facts": [str(item)[:300] for item in (facts or []) if str(item).strip()][:10],
        "landing_url": str(landing_url)[:500],
        "channels": [c for c in channels if c in CHANNELS] or ["chat"],
        "valid_from": str(valid_from or ""),
        "valid_until": str(valid_until or ""),
    }


def create_campaign(
    db: Any, *, brand_id: str, title: str, purpose: str = "",
    required_tags: Iterable[str] = (), constraints: dict | None = None,
    facts: list[str] | None = None, landing_url: str = "",
    channels: Iterable[str] = ("chat", "feed"), valid_from: str = "", valid_until: str = "",
    created_by: str = "",
) -> dict:
    if get_brand(db, brand_id) is None:
        raise ValueError("品牌不存在")
    payload = _campaign_payload(
        brand_id=brand_id, title=title, purpose=purpose, required_tags=required_tags,
        constraints=constraints or {}, facts=facts or [], landing_url=landing_url,
        channels=channels, valid_from=valid_from, valid_until=valid_until,
    )
    cid = _new_id("cmp")
    now = _iso()
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO ads_campaigns (id, brand_id, title, purpose, required_tags, constraints,"
            " facts, landing_url, channels, valid_from, valid_until, status, version,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'draft',1,?,?)",
            (cid, payload["brand_id"], payload["title"], payload["purpose"],
             _dumps(payload["required_tags"]), _dumps(payload["constraints"]),
             _dumps(payload["facts"]), payload["landing_url"], _dumps(payload["channels"]),
             payload["valid_from"], payload["valid_until"], now, now),
        )
        db.conn.execute(
            "INSERT INTO ads_campaign_versions (id, campaign_id, version, payload,"
            " created_by, created_at) VALUES (?,?,?,?,?,?)",
            (_new_id("cv"), cid, 1, _dumps(payload), str(created_by)[:64], now),
        )
    return get_campaign(db, cid) or {}


def get_campaign(db: Any, campaign_id: str) -> dict | None:
    if not _ensure(db):
        return None
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM ads_campaigns WHERE id=?", (campaign_id,)).fetchone()
    return _campaign_public(row) if row is not None else None


def _campaign_public(row: Any) -> dict:
    data = dict(row)
    for key in ("required_tags", "constraints", "facts", "channels"):
        data[key] = _loads(data.get(key), [] if key != "constraints" else {})
    return data


def list_campaigns(db: Any, *, status: str = "", limit: int = 100) -> list[dict]:
    if not _ensure(db):
        return []
    sql = "SELECT * FROM ads_campaigns"
    params: list[Any] = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(sql, tuple(params)).fetchall()
    return [_campaign_public(r) for r in rows]


def update_campaign(db: Any, campaign_id: str, *, created_by: str = "", **fields: Any) -> dict:
    """修改活动：**已发布（reviewed/enabled/paused）的修改会产生新版本并回到 draft**。"""
    current = get_campaign(db, campaign_id)
    if current is None:
        raise ValueError("活动不存在")
    merged = {
        "brand_id": current["brand_id"],
        "title": current["title"],
        "purpose": current["purpose"],
        "required_tags": current["required_tags"],
        "constraints": current["constraints"],
        "facts": current["facts"],
        "landing_url": current["landing_url"],
        "channels": current["channels"],
        "valid_from": current["valid_from"],
        "valid_until": current["valid_until"],
    }
    for key in merged:
        if key in fields and fields[key] is not None:
            merged[key] = fields[key]
    if merged["brand_id"] and get_brand(db, str(merged["brand_id"])) is None:
        raise ValueError("品牌不存在")
    payload = _campaign_payload(**merged)
    needs_review = current["status"] in ("reviewed", "enabled", "paused")
    version = int(current["version"]) + (1 if needs_review else 0)
    status = "draft" if needs_review else current["status"]
    now = _iso()
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "UPDATE ads_campaigns SET brand_id=?, title=?, purpose=?, required_tags=?,"
            " constraints=?, facts=?, landing_url=?, channels=?, valid_from=?, valid_until=?,"
            " status=?, version=?, updated_at=? WHERE id=?",
            (payload["brand_id"], payload["title"], payload["purpose"],
             _dumps(payload["required_tags"]), _dumps(payload["constraints"]),
             _dumps(payload["facts"]), payload["landing_url"], _dumps(payload["channels"]),
             payload["valid_from"], payload["valid_until"], status, version, now, campaign_id),
        )
        db.conn.execute(
            "INSERT OR REPLACE INTO ads_campaign_versions (id, campaign_id, version, payload,"
            " created_by, created_at) VALUES (?,?,?,?,?,?)",
            (_new_id("cv"), campaign_id, version, _dumps(payload), str(created_by)[:64], now),
        )
    return get_campaign(db, campaign_id) or {}


def set_campaign_status(db: Any, campaign_id: str, status: str) -> dict:
    """状态机：draft→reviewed→enabled→paused→enabled；任意状态可 ended。"""
    allowed = {
        "draft": {"reviewed", "ended"},
        "reviewed": {"enabled", "draft", "ended"},
        "enabled": {"paused", "ended"},
        "paused": {"enabled", "ended"},
        "ended": set(),
    }
    current = get_campaign(db, campaign_id)
    if current is None:
        raise ValueError("活动不存在")
    status = str(status or "").strip().lower()
    if status not in CAMPAIGN_STATUS:
        raise ValueError("非法状态")
    if status not in allowed.get(str(current["status"]), set()):
        raise ValueError(f"不允许从 {current['status']} 变到 {status}")
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "UPDATE ads_campaigns SET status=?, updated_at=? WHERE id=?",
            (status, _iso(), campaign_id),
        )
    return get_campaign(db, campaign_id) or {}


def list_campaign_versions(db: Any, campaign_id: str) -> list[dict]:
    if not _ensure(db):
        return []
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT * FROM ads_campaign_versions WHERE campaign_id=? ORDER BY version DESC",
            (campaign_id,),
        ).fetchall()
    return [
        {"version": int(r["version"]), "payload": _loads(r["payload"], {}),
         "created_by": r["created_by"], "created_at": r["created_at"]}
        for r in rows
    ]


def restore_campaign_version(db: Any, campaign_id: str, version: int, *, created_by: str = "") -> dict:
    """历史版本可还原：把指定版本内容写回并生成一个新 draft 版本（不删除历史）。"""
    versions = {item["version"]: item["payload"] for item in list_campaign_versions(db, campaign_id)}
    payload = versions.get(int(version))
    if payload is None:
        raise ValueError("版本不存在")
    current = get_campaign(db, campaign_id)
    if current is None:
        raise ValueError("活动不存在")
    new_version = int(current["version"]) + 1
    now = _iso()
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "UPDATE ads_campaigns SET brand_id=?, title=?, purpose=?, required_tags=?,"
            " constraints=?, facts=?, landing_url=?, channels=?, valid_from=?, valid_until=?,"
            " status='draft', version=?, updated_at=? WHERE id=?",
            (payload["brand_id"], payload["title"], payload["purpose"],
             _dumps(payload["required_tags"]), _dumps(payload["constraints"]),
             _dumps(payload["facts"]), payload["landing_url"], _dumps(payload["channels"]),
             payload["valid_from"], payload["valid_until"], new_version, now, campaign_id),
        )
        db.conn.execute(
            "INSERT OR REPLACE INTO ads_campaign_versions (id, campaign_id, version, payload,"
            " created_by, created_at) VALUES (?,?,?,?,?,?)",
            (_new_id("cv"), campaign_id, new_version, _dumps(payload), str(created_by)[:64], now),
        )
    return get_campaign(db, campaign_id) or {}


def add_creative(
    db: Any, *, campaign_id: str, channel: str, title: str, body_template: str,
    facts: list[str] | None = None,
) -> dict:
    campaign = get_campaign(db, campaign_id)
    if campaign is None:
        raise ValueError("活动不存在")
    if channel not in CHANNELS:
        raise ValueError("非法渠道")
    if channel not in campaign["channels"]:
        raise ValueError("该活动未启用此渠道")
    body = str(body_template or "").strip()[:600]
    if not body:
        raise ValueError("素材文案不能为空")
    cid = _new_id("cr")
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO ads_creatives (id, campaign_id, campaign_version, channel, title,"
            " body_template, facts, status, created_at) VALUES (?,?,?,?,?,?,?,'draft',?)",
            (cid, campaign_id, int(campaign["version"]), channel, str(title)[:120], body,
             _dumps([str(x)[:300] for x in (facts or []) if str(x).strip()][:10]), _iso()),
        )
    return get_creative(db, cid) or {}


def get_creative(db: Any, creative_id: str) -> dict | None:
    if not _ensure(db):
        return None
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM ads_creatives WHERE id=?", (creative_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["facts"] = _loads(data.get("facts"), [])
    return data


def review_creative(db: Any, creative_id: str, *, approved: bool, reviewer: str = "", note: str = "") -> dict:
    creative = get_creative(db, creative_id)
    if creative is None:
        raise ValueError("素材不存在")
    if not approved and not note.strip():
        raise ValueError("驳回必须填写原因")
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "UPDATE ads_creatives SET status=?, reviewed_by=?, reviewed_at=? WHERE id=?",
            ("approved" if approved else "rejected", str(reviewer)[:64], _iso(), creative_id),
        )
        if note:
            db.conn.execute(
                "INSERT INTO ads_events (id, delivery_id, user_id, type, payload, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (_new_id("ae"), "creative:" + creative_id, "", "review_note",
                 _dumps({"approved": bool(approved), "note": str(note)[:500]}), _iso()),
            )
    return get_creative(db, creative_id) or {}


def list_creatives(db: Any, campaign_id: str = "", *, channel: str = "", approved_only: bool = False) -> list[dict]:
    if not _ensure(db):
        return []
    sql = "SELECT * FROM ads_creatives WHERE 1=1"
    params: list[Any] = []
    if campaign_id:
        sql += " AND campaign_id=?"
        params.append(campaign_id)
    if channel:
        sql += " AND channel=?"
        params.append(channel)
    if approved_only:
        sql += " AND status='approved'"
    sql += " ORDER BY created_at DESC"
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(sql, tuple(params)).fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["facts"] = _loads(data.get("facts"), [])
        out.append(data)
    return out


# --------------------------------------------------------------------------- #
# 角色分配（系统建议 + 运营确认）
# --------------------------------------------------------------------------- #
def suggest_persona_assignments(db: Any, campaign_id: str, *, limit: int = 10) -> list[dict]:
    """按兴趣标签/职业/语言给建议，不用性别硬匹配。"""
    campaign = get_campaign(db, campaign_id)
    if campaign is None:
        raise ValueError("活动不存在")
    tags = {str(t) for t in campaign.get("required_tags") or []}
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT id, nickname, occupation, language, interests, personality FROM personas"
            " WHERE status IN ('active','waiting_voice')"
        ).fetchall()
    scored: list[tuple[float, dict]] = []
    for row in rows:
        persona_tags = {str(t) for t in _loads(row["interests"], [])}
        overlap = len(tags & persona_tags)
        text = f"{row['occupation'] or ''} {row['personality'] or ''}".lower()
        text_hits = sum(1 for tag in tags if tag.replace("-", " ") in text)
        score = overlap * 2.0 + text_hits * 0.5
        scored.append((score, dict(row)))
    scored.sort(key=lambda item: (-item[0], str(item[1]["id"])))
    out = []
    for score, persona in scored[: max(1, int(limit))]:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO ads_assignments (id, campaign_id, persona_id, score, status, created_at)"
                " VALUES (?,?,?,?,'suggested',?) ON CONFLICT(campaign_id, persona_id)"
                " DO UPDATE SET score=excluded.score",
                (_new_id("as"), campaign_id, str(persona["id"]), float(score), _iso()),
            )
        out.append({"persona_id": str(persona["id"]), "nickname": persona["nickname"], "score": round(score, 2)})
    return out


def confirm_assignment(db: Any, campaign_id: str, persona_id: str, *, confirmed: bool = True) -> dict:
    """运营确认/拒绝角色分配；确认可直接落库（不必先有系统建议）。"""
    status = "confirmed" if confirmed else "rejected"
    with db._lock, db.conn:  # noqa: SLF001
        if confirmed:
            db.conn.execute(
                "INSERT INTO ads_assignments (id, campaign_id, persona_id, score, status, created_at)"
                " VALUES (?,?,?,0,?,?) ON CONFLICT(campaign_id, persona_id)"
                " DO UPDATE SET status=excluded.status",
                (_new_id("as"), campaign_id, persona_id, status, _iso()),
            )
            changed = 1
        else:
            cur = db.conn.execute(
                "UPDATE ads_assignments SET status=? WHERE campaign_id=? AND persona_id=?",
                (status, campaign_id, persona_id),
            )
            changed = int(cur.rowcount or 0)
    if not changed:
        raise ValueError("分配记录不存在")
    return {"campaign_id": campaign_id, "persona_id": persona_id, "status": status}


def list_assignments(db: Any, campaign_id: str) -> list[dict]:
    if not _ensure(db):
        return []
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT * FROM ads_assignments WHERE campaign_id=? ORDER BY score DESC", (campaign_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 资格与需求识别
# --------------------------------------------------------------------------- #
def ad_eligible(db: Any, user_id: str) -> tuple[bool, str]:
    """成年 + 已核验 + 主动开启合作推荐，才可能看到广告。"""
    if not user_id or user_id == "local":
        return False, "no_account"
    if not _ensure(db):
        return False, "ads_disabled"
    account = accounts.account_for_user(db, user_id)
    if account is None:
        return False, "no_account"
    caps = accounts.capabilities(db, user_id)
    if caps["age"]["group"] != "adult" or not caps["age"]["verified"]:
        return False, "not_adult_verified"
    if not caps["features"].get("ads", False):
        return False, "ads_not_allowed"
    pref = _user_preferences(db, user_id)
    if not pref.get("ads"):
        return False, "ads_opt_out"
    return True, ""


def _user_preferences(db: Any, user_id: str) -> dict:
    try:
        from app.v3 import client_api  # noqa: PLC0415

        return client_api.preferences(db, user_id)
    except Exception:  # noqa: BLE001
        return {"ads": False}


def detect_demand(text: str, *, recent_context: str = "") -> dict:
    """规则版需求识别：泛聊/否定/不确定/心理困扰 → 不投；命中标签才可能投。"""
    raw = str(text or "").strip()
    if not raw:
        return {"matched": False, "reason": "empty", "tags": []}
    if _VAGUE_RE.match(raw):
        return {"matched": False, "reason": "vague", "tags": []}
    if _REFUSAL_RE.search(raw):
        return {"matched": False, "reason": "refusal", "tags": []}
    if _DISTRESS_RE.search(raw):
        return {"matched": False, "reason": "distress", "tags": []}
    blob = f"{raw} {recent_context}".lower()
    tags: list[str] = []
    for alias, tag_id in taxonomy.ALIASES.items():
        if alias and alias in blob and tag_id not in tags:
            tags.append(tag_id)
    for tag_id in taxonomy.TAGS:
        if tag_id in blob and tag_id not in tags:
            tags.append(tag_id)
    if not tags:
        return {"matched": False, "reason": "no_need_signal", "tags": []}
    return {"matched": True, "reason": "need_signal", "tags": tags[:6]}


# --------------------------------------------------------------------------- #
# 频控与原子预留
# --------------------------------------------------------------------------- #
def _released_expired(db: Any, now: datetime) -> None:
    cutoff = _iso(now - timedelta(seconds=RESERVATION_TTL_S))
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE ads_deliveries SET status='released', last_event_at=?"
                " WHERE status='reserved' AND reserved_at<=?",
                (_iso(now), cutoff),
            )
    except Exception:  # noqa: BLE001
        pass


def _count_deliveries(db: Any, user_id: str, since: str, *, channel: str = "") -> int:
    sql = "SELECT COUNT(*) AS n FROM ads_deliveries WHERE user_id=? AND reserved_at>=?" \
          " AND status IN ('reserved','delivered','impression','clicked')"
    params: list[Any] = [user_id, since]
    if channel:
        sql += " AND channel=?"
        params.append(channel)
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(sql, tuple(params)).fetchone()
    return int(row["n"] if row is not None else 0)


def _last_delivery(db: Any, user_id: str, *, channel: str = "") -> dict | None:
    sql = ("SELECT * FROM ads_deliveries WHERE user_id=?"
           " AND status IN ('reserved','delivered','impression','clicked')")
    params: list[Any] = [user_id]
    if channel:
        sql += " AND channel=?"
        params.append(channel)
    sql += " ORDER BY reserved_at DESC LIMIT 1"
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row is not None else None


def _user_turns_since(db: Any, friend_id: str, since: str) -> int:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE friend_id=? AND sender='user'"
            " AND created_at>=?",
            (friend_id, since),
        ).fetchone()
    return int(row["n"] if row is not None else 0)


def _max_user_rowid(db: Any, friend_id: str) -> int:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT COALESCE(MAX(rowid),0) AS n FROM messages WHERE friend_id=? AND sender='user'",
            (friend_id,),
        ).fetchone()
    return int(row["n"] if row is not None else 0)


def _user_turns_after_rowid(db: Any, friend_id: str, rowid: int) -> int:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE friend_id=? AND sender='user' AND rowid>?",
            (friend_id, int(rowid)),
        ).fetchone()
    return int(row["n"] if row is not None else 0)


def _turns_since_last_chat_ad(db: Any, friend_id: str, last: dict) -> int:
    """两次聊天广告之间的用户回合数：优先按「预留时的消息 rowid」计算（不受时钟影响）。"""
    context = _loads(last.get("context"), {}) or {}
    if "after_rowid" in context:
        return _user_turns_after_rowid(db, friend_id, int(context.get("after_rowid") or 0))
    return _user_turns_since(db, friend_id, str(last.get("reserved_at") or ""))


def _brand_blocked(db: Any, user_id: str, brand_id: str) -> bool:
    since = _iso(_now() - timedelta(days=BRAND_COOLDOWN_DAYS))
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT 1 FROM ads_deliveries WHERE user_id=? AND brand_id=?"
            " AND status IN ('reserved','delivered','impression','clicked')"
            " AND reserved_at>=? LIMIT 1",
            (user_id, brand_id, since),
        ).fetchone()
    return row is not None


def _user_blocked(db: Any, user_id: str, campaign_id: str) -> bool:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT blocked_until FROM ads_blocks WHERE user_id=? AND campaign_id=?",
            (user_id, campaign_id),
        ).fetchone()
    if row is None:
        return False
    until = _parse_iso(row["blocked_until"])
    return until is not None and until > _now()


def _candidate_campaigns(db: Any, *, channel: str, persona_id: str = "") -> list[dict]:
    now = _iso()
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT c.*, b.name AS brand_name, b.landing_url AS brand_landing,"
            " b.qualification AS brand_qualification FROM ads_campaigns c"
            " JOIN ads_brands b ON b.id=c.brand_id"
            " WHERE c.status='enabled' AND b.status='active'",
        ).fetchall()
    out = []
    for row in rows:
        data = _campaign_public(row)
        if channel not in data["channels"]:
            continue
        if data["valid_from"] and str(data["valid_from"]) > now:
            continue
        if data["valid_until"] and str(data["valid_until"]) < now:
            continue
        if persona_id:
            with db._lock:  # noqa: SLF001
                assignment = db.conn.execute(
                    "SELECT 1 FROM ads_assignments WHERE campaign_id=? AND persona_id=?"
                    " AND status='confirmed' LIMIT 1",
                    (data["id"], persona_id),
                ).fetchone()
            if assignment is None:
                continue
        out.append(data)
    return out


def _pick_creative(db: Any, campaign_id: str, channel: str) -> dict | None:
    items = list_creatives(db, campaign_id, channel=channel, approved_only=True)
    return items[0] if items else None


def compose_ad_copy(creative: dict, facts: Iterable[str] | None = None) -> str:
    """审核模板 + 商品事实拼装；只做占位替换，不允许模型自由发挥。"""
    body = str(creative.get("body_template") or "").strip()
    fact_list = [str(x).strip() for x in (facts or creative.get("facts") or []) if str(x).strip()]
    if fact_list and "{facts}" in body:
        body = body.replace("{facts}", "；".join(fact_list[:3]))
    elif fact_list and "{fact}" in body:
        body = body.replace("{fact}", fact_list[0])
    return body


# --------------------------------------------------------------------------- #
# 聊天投放
# --------------------------------------------------------------------------- #
def try_chat_ad(
    db: Any,
    *,
    user_id: str,
    friend_id: str,
    persona_id: str,
    user_text: str,
    pre_context: str = "",
    now: datetime | None = None,
) -> dict | None:
    """判断并原子预留一次聊天投放；返回 ``{delivery_id, campaign, creative, copy, ...}``。"""
    moment = now or _now()
    eligible, reason = ad_eligible(db, user_id)
    if not eligible:
        return None
    demand = detect_demand(user_text, recent_context=pre_context)
    if not demand["matched"]:
        return None
    _released_expired(db, moment)
    since24 = _iso(moment - timedelta(hours=24))
    if _count_deliveries(db, user_id, since24) >= TOTAL_PER_24H:
        return None
    if _count_deliveries(db, user_id, since24, channel="chat") >= CHAT_PER_24H:
        return None
    last_chat = _last_delivery(db, user_id, channel="chat")
    if last_chat is not None:
        if _turns_since_last_chat_ad(db, friend_id, last_chat) < CHAT_TURN_GAP:
            return None
    wanted = set(demand["tags"])
    campaigns = _candidate_campaigns(db, channel="chat", persona_id=persona_id)
    campaigns.sort(key=lambda item: (-len(wanted & set(item["required_tags"])), item["created_at"]))
    for campaign in campaigns:
        overlap = wanted & set(campaign["required_tags"] or [])
        if not overlap:
            continue
        if _brand_blocked(db, user_id, campaign["brand_id"]):
            continue
        if _user_blocked(db, user_id, campaign["id"]):
            continue
        creative = _pick_creative(db, campaign["id"], "chat")
        if creative is None:
            continue
        copy = compose_ad_copy(creative, campaign.get("facts"))
        if not copy:
            continue
        delivery_id = _reserve(db, user_id=user_id, campaign=campaign, channel="chat",
                               persona_id=persona_id, creative=creative,
                               reason=_dumps({"demand": demand["reason"], "tags": demand["tags"],
                                              "overlap": sorted(overlap)}),
                               moment=moment,
                               context_extra={"after_rowid": _max_user_rowid(db, friend_id)})
        if delivery_id:
            _write_audit(db, delivery_id, user_id, campaign, user_text, pre_context, copy, moment)
            return {
                "delivery_id": delivery_id,
                "campaign_id": campaign["id"],
                "campaign_version": int(campaign["version"]),
                "brand_id": campaign["brand_id"],
                "brand_name": campaign.get("brand_name", ""),
                "persona_id": persona_id,
                "creative_id": creative["id"],
                "copy": copy,
                "label": "广告 · 品牌合作",
                "landing_url": campaign.get("landing_url") or campaign.get("brand_landing") or "",
                "facts": campaign.get("facts") or [],
            }
        return None
    return None


def _reserve(
    db: Any, *, user_id: str, campaign: dict, channel: str, persona_id: str,
    creative: dict, reason: str, moment: datetime, context_extra: dict | None = None,
) -> str | None:
    """在写事务里再次核验频控并插入 reserved 行（原子预留）。"""
    delivery_id = _new_id("ad")
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute("BEGIN IMMEDIATE")
            since24 = _iso(moment - timedelta(hours=24))
            row = db.conn.execute(
                "SELECT COUNT(*) AS n FROM ads_deliveries WHERE user_id=? AND reserved_at>=?"
                " AND status IN ('reserved','delivered','impression','clicked')",
                (user_id, since24),
            ).fetchone()
            if int(row["n"] if row else 0) >= TOTAL_PER_24H:
                db.conn.rollback()
                return None
            row = db.conn.execute(
                "SELECT COUNT(*) AS n FROM ads_deliveries WHERE user_id=? AND reserved_at>=?"
                " AND channel=? AND status IN ('reserved','delivered','impression','clicked')",
                (user_id, since24, channel),
            ).fetchone()
            if int(row["n"] if row else 0) >= CHAT_PER_24H:
                db.conn.rollback()
                return None
            db.conn.execute(
                "INSERT INTO ads_deliveries (id, user_id, campaign_id, campaign_version, brand_id,"
                " channel, persona_id, creative_id, status, reason, position, reserved_at,"
                " confirmed_at, last_event_at, context) VALUES (?,?,?,?,?,?,?,?,'reserved',?,?,?,?,?,?)",
                (delivery_id, user_id, campaign["id"], int(campaign["version"]),
                 campaign["brand_id"], channel, persona_id, creative["id"], reason, -1,
                 _iso(moment), "", "", _dumps(context_extra or {})),
            )
            db.conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("广告额度预留失败（忽略）: %s", exc)
        try:
            with db._lock, db.conn:  # noqa: SLF001
                db.conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    return delivery_id


def _write_audit(
    db: Any, delivery_id: str, user_id: str, campaign: dict, trigger: str,
    pre_context: str, copy: str, moment: datetime,
) -> None:
    """留痕：触发消息 + 最多两条前文脱敏摘录，合计 ≤1000 字符，30 天后删除。"""
    trigger_text = str(trigger or "").strip()[:400]
    context = str(pre_context or "").strip()
    budget = max(0, 1000 - len(trigger_text))
    context = context[-budget:] if budget else ""
    delete_after = _iso(moment + timedelta(days=AUDIT_KEEP_DAYS))
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT OR REPLACE INTO ads_chat_audit (delivery_id, user_id, campaign_id,"
                " campaign_version, match_reason, trigger_text, pre_context, copy, created_at,"
                " delete_after) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (delivery_id, user_id, campaign["id"], int(campaign["version"]),
                 _dumps({"required_tags": campaign.get("required_tags") or [],
                         "demand": "need_signal"}),
                 trigger_text, context, copy, _iso(moment), delete_after),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("广告留痕失败（忽略）: %s", exc)


def confirm_delivery(db: Any, delivery_id: str, *, message_id: str = "", now: datetime | None = None) -> None:
    try:
        with db._lock, db.conn:  # noqa: SLF001
            row = db.conn.execute(
                "SELECT context FROM ads_deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            context = _loads(row["context"], {}) if row is not None else {}
            context["message_id"] = str(message_id or "")
            db.conn.execute(
                "UPDATE ads_deliveries SET status='delivered', confirmed_at=?, last_event_at=?,"
                " context=? WHERE id=? AND status='reserved'",
                (_iso(now), _iso(now), _dumps(context), delivery_id),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("广告确认失败（忽略）: %s", exc)


# --------------------------------------------------------------------------- #
# 动态投放
# --------------------------------------------------------------------------- #
def try_feed_ad(
    db: Any, *, user_id: str, page_offset: int, items_in_page: int,
    persona_tags: dict[str, list[str]] | None = None, now: datetime | None = None,
) -> dict | None:
    """为一页推荐流尝试插一条广告（返回投放视图或 None）。"""
    eligible, _ = ad_eligible(db, user_id)
    if not eligible:
        return None
    moment = now or _now()
    _released_expired(db, moment)
    since24 = _iso(moment - timedelta(hours=24))
    if _count_deliveries(db, user_id, since24) >= TOTAL_PER_24H:
        return None
    # 前 3 条不插 + 两条广告之间至少 8 条普通动态
    if page_offset < FEED_FIRST_SKIP:
        _bump_feed_state(db, user_id, items_in_page)
        return None
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT items_since_ad FROM ads_feed_state WHERE user_id=?", (user_id,)
        ).fetchone()
    since_ad = int(row["items_since_ad"] if row is not None else page_offset)
    if since_ad < FEED_POST_GAP and page_offset >= FEED_FIRST_SKIP:
        _bump_feed_state(db, user_id, items_in_page)
        return None
    prefs = _user_preferences(db, user_id)
    try:
        from app.v3 import recommend  # noqa: PLC0415

        wanted = set(recommend.user_interest_ids(db, user_id))
    except Exception:  # noqa: BLE001
        wanted = set()
    campaigns = _candidate_campaigns(db, channel="feed")
    campaigns.sort(key=lambda item: (-len(wanted & set(item["required_tags"])), item["created_at"]))
    for campaign in campaigns:
        if _brand_blocked(db, user_id, campaign["brand_id"]):
            continue
        if _user_blocked(db, user_id, campaign["id"]):
            continue
        creative = _pick_creative(db, campaign["id"], "feed")
        if creative is None:
            continue
        copy = compose_ad_copy(creative, campaign.get("facts"))
        if not copy:
            continue
        # 槽位：首页第一屏从第 4 条后插；其余页插在页面中部
        slot = None
        if page_offset <= 0:
            slot = min(FEED_FIRST_SKIP, max(0, items_in_page))
        elif items_in_page:
            slot = max(0, items_in_page // 2)
        delivery_id = _reserve(db, user_id=user_id, campaign=campaign, channel="feed",
                               persona_id="", creative=creative,
                               reason=_dumps({"slot": slot, "page_offset": page_offset}),
                               moment=moment)
        if not delivery_id:
            return None
        confirm_delivery(db, delivery_id, now=moment)
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO ads_feed_state (user_id, items_since_ad, updated_at)"
                " VALUES (?,0,?) ON CONFLICT(user_id) DO UPDATE SET items_since_ad=0,"
                " updated_at=excluded.updated_at",
                (user_id, _iso(moment)),
            )
        return {
            "kind": "sponsored",
            "id": delivery_id,
            "delivery_id": delivery_id,
            "campaign_id": campaign["id"],
            "brand_id": campaign["brand_id"],
            "brand_name": campaign.get("brand_name", ""),
            "creative_id": creative["id"],
            "title": creative.get("title") or campaign.get("title") or "",
            "content": copy,
            "label": "广告",
            "landing_url": campaign.get("landing_url") or campaign.get("brand_landing") or "",
            "slot": slot,
        }
    return None


def _bump_feed_state(db: Any, user_id: str, count: int) -> None:
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO ads_feed_state (user_id, items_since_ad, updated_at)"
                " VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET"
                " items_since_ad=items_since_ad+?, updated_at=excluded.updated_at",
                (user_id, int(count), _iso(), int(count)),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("feed 广告间隔计数失败（忽略）: %s", exc)


# --------------------------------------------------------------------------- #
# 用户事件
# --------------------------------------------------------------------------- #
def record_event(
    db: Any, *, user_id: str, delivery_id: str, event_type: str,
    payload: dict | None = None, now: datetime | None = None,
) -> dict:
    """记录可见曝光/点击/关闭/举报，并推进投放状态（幂等友好）。"""
    kind = str(event_type or "").strip().lower()
    if kind not in EVENT_TYPES:
        raise ValueError("非法事件类型")
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT * FROM ads_deliveries WHERE id=? AND user_id=?", (delivery_id, user_id)
        ).fetchone()
    if row is None:
        raise ValueError("投放记录不存在")
    moment = now or _now()
    status = str(row["status"])
    next_status = status
    if kind == "impression" and status in ("reserved", "delivered", "impression"):
        next_status = "impression"
    elif kind == "click" and status in ("reserved", "delivered", "impression", "clicked"):
        next_status = "clicked"
    elif kind == "close":
        next_status = "closed"
    elif kind == "report":
        next_status = "reported"
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO ads_events (id, delivery_id, user_id, type, payload, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (_new_id("ae"), delivery_id, user_id, kind, _dumps(payload or {}), _iso(moment)),
        )
        db.conn.execute(
            "UPDATE ads_deliveries SET status=?, last_event_at=? WHERE id=?",
            (next_status, _iso(moment), delivery_id),
        )
        if kind in ("close", "report"):
            blocked_until = _iso(moment + timedelta(days=CLOSE_BLOCK_DAYS))
            db.conn.execute(
                "INSERT INTO ads_blocks (user_id, campaign_id, blocked_until, reason, created_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(user_id, campaign_id) DO UPDATE SET"
                " blocked_until=excluded.blocked_until, reason=excluded.reason",
                (user_id, str(row["campaign_id"]), blocked_until, kind, _iso(moment)),
            )
    return {
        "ok": True,
        "delivery_id": delivery_id,
        "event": kind,
        "status": next_status,
        "blocked_until": (
            _iso(moment + timedelta(days=CLOSE_BLOCK_DAYS)) if kind in ("close", "report") else ""
        ),
    }


# --------------------------------------------------------------------------- #
# 预览 / 统计 / 留痕清理
# --------------------------------------------------------------------------- #
def preview(db: Any, campaign_id: str, *, channel: str = "chat") -> dict:
    campaign = get_campaign(db, campaign_id)
    if campaign is None:
        raise ValueError("活动不存在")
    creative = _pick_creative(db, campaign_id, channel)
    copy = compose_ad_copy(creative, campaign.get("facts")) if creative else ""
    return {
        "campaign_id": campaign_id,
        "version": int(campaign["version"]),
        "channel": channel,
        "status": campaign["status"],
        "label": "广告 · 品牌合作" if channel == "chat" else "广告",
        "copy": copy,
        "facts": campaign.get("facts") or [],
        "landing_url": campaign.get("landing_url") or "",
        "creative_approved": creative is not None,
    }


def campaign_stats(db: Any, campaign_id: str = "") -> dict:
    if not _ensure(db):
        return {"items": []}
    sql = (
        "SELECT campaign_id,"
        " SUM(CASE WHEN status IN ('delivered','impression','clicked') THEN 1 ELSE 0 END) AS delivered,"
        " SUM(CASE WHEN status IN ('impression','clicked') THEN 1 ELSE 0 END) AS impressions,"
        " SUM(CASE WHEN status='clicked' THEN 1 ELSE 0 END) AS clicks,"
        " SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closes,"
        " SUM(CASE WHEN status='reported' THEN 1 ELSE 0 END) AS reports"
        " FROM ads_deliveries"
    )
    params: list[Any] = []
    if campaign_id:
        sql += " WHERE campaign_id=?"
        params.append(campaign_id)
    sql += " GROUP BY campaign_id"
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(sql, tuple(params)).fetchall()
    return {"items": [dict(r) for r in rows]}


def list_deliveries(db: Any, *, user_id: str = "", campaign_id: str = "", limit: int = 100) -> list[dict]:
    if not _ensure(db):
        return []
    sql = "SELECT * FROM ads_deliveries WHERE 1=1"
    params: list[Any] = []
    if user_id:
        sql += " AND user_id=?"
        params.append(user_id)
    if campaign_id:
        sql += " AND campaign_id=?"
        params.append(campaign_id)
    sql += " ORDER BY reserved_at DESC LIMIT ?"
    params.append(int(limit))
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def cleanup_audit(db: Any, *, now: datetime | None = None) -> int:
    """30 天后删除聊天投放留痕（广告档案表本身保留 ≥3 年，不删）。"""
    moment = now or _now()
    try:
        with db._lock, db.conn:  # noqa: SLF001
            cur = db.conn.execute(
                "DELETE FROM ads_chat_audit WHERE delete_after<>'' AND delete_after<=?",
                (_iso(moment),),
            )
            return int(cur.rowcount or 0)
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "ARCHIVE_KEEP_DAYS",
    "AUDIT_KEEP_DAYS",
    "BRAND_COOLDOWN_DAYS",
    "CHANNELS",
    "CHAT_PER_24H",
    "CHAT_TURN_GAP",
    "CLOSE_BLOCK_DAYS",
    "DELIVERY_STATUS",
    "EVENT_TYPES",
    "FEED_FIRST_SKIP",
    "FEED_POST_GAP",
    "TOTAL_PER_24H",
    "add_creative",
    "ad_eligible",
    "campaign_stats",
    "cleanup_audit",
    "compose_ad_copy",
    "confirm_assignment",
    "confirm_delivery",
    "create_brand",
    "create_campaign",
    "detect_demand",
    "ensure_ads_schema",
    "get_brand",
    "get_campaign",
    "get_creative",
    "list_assignments",
    "list_brands",
    "list_campaign_versions",
    "list_campaigns",
    "list_creatives",
    "list_deliveries",
    "preview",
    "record_event",
    "restore_campaign_version",
    "review_creative",
    "set_campaign_status",
    "suggest_persona_assignments",
    "try_chat_ad",
    "try_feed_ad",
    "update_brand",
    "update_campaign",
]
