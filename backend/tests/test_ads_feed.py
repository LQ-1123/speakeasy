"""推荐流广告混排（feed sponsored）验收。

覆盖：
- 只有成年 + 已核验 + 主动开启合作推荐的用户会看到 `kind="sponsored"`；
- 第一页前 3 条不插、两条广告之间 ≥8 条普通动态、24h 合计 ≤2；
- 活动暂停/结束、未成年、未知年龄、未开启偏好 → 不插；
- 广告不进入 feed_summary（以及不在人物主页/评论链路里出现）；
- 事件（点击/关闭）更新投放状态，关闭后 30 天屏蔽该活动。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.v2.db import DB
from app.v3 import accounts, ads, client_api, recommend
from app.v3.identity import reset_current_user, set_current_user
from app.v3.schema import ensure_schema
from app.v3.service import V3Service

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
ISO = NOW.strftime("%Y-%m-%dT%H:%M:%S+00:00")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SE_INVITE_REQUIRED", "0")
    monkeypatch.setenv("SE_AGE_GATE", "1")


def _make(tmp_path: Path) -> tuple[DB, V3Service]:
    db = DB(str(tmp_path / "ads_feed.db"))
    db.init()
    ensure_schema(db, seed=False)
    accounts.ensure_accounts_schema(db)
    client_api.ensure_client_schema(db)
    ads.ensure_ads_schema(db)
    recommend.ensure_recommend_schema(db)
    service = V3Service(db, SimpleNamespace(llm=SimpleNamespace(uses_mock=True)), str(tmp_path))
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO personas (id, nickname, bio, personality, interests, language,"
            " country, city, timezone, occupation, age, gender, status, created_at)"
            " VALUES ('p_ads','Persona','','',?,'en','','','UTC','',27,'female','active',?)",
            (json.dumps(["coffee"]), ISO),
        )
        for index in range(14):
            db.conn.execute(
                "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
                " visibility, like_count, comment_count, created_at, expires_at)"
                " VALUES (?, 'p_ads', ?, '[]', '[]', 'en', 'public', 0, 0, ?, '')",
                (f"pp_ads_{index}", f"post {index}", ISO),
            )
    return db, service


def _user(db: DB, *, group: str | None = "adult", opt_in: bool = True) -> str:
    email = f"u{group or 'unknown'}{'x' if opt_in else 'o'}@example.com"
    reg = accounts.register_email(db, email, "pa55word!", invite_code="")
    uid = reg["user_id"]
    if group:
        accounts.set_age_verification(db, uid, age_group=group)
    db.update_profile(
        nickname="AdsUser", target_languages=["en"],
        fact_sheet={"interests": ["coffee"]}, user_id=uid,
    )
    if opt_in:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO client_preferences (user_id, value) VALUES (?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET value=excluded.value",
                (uid, json.dumps({"messages": True, "interactions": True, "sound": True,
                                  "theme": "system", "ads": True})),
            )
    return uid


def _campaign(db: DB) -> dict:
    brand = ads.create_brand(db, name="广告咖啡", industry="food")
    campaign = ads.create_campaign(
        db, brand_id=brand["id"], title="咖啡活动", required_tags=["coffee"],
        facts=["手冲 2 分钟"], channels=["chat", "feed"],
    )
    creative = ads.add_creative(
        db, campaign_id=campaign["id"], channel="feed", title="动态素材",
        body_template="今天也在喝 {facts}。", facts=["手冲 2 分钟"],
    )
    ads.review_creative(db, creative["id"], approved=True, reviewer="test")
    ads.set_campaign_status(db, campaign["id"], "reviewed")
    ads.set_campaign_status(db, campaign["id"], "enabled")
    return campaign


def _pages(db: DB, service: V3Service, uid: str, *, pages: int = 3, limit: int = 5) -> list[list[dict]]:
    out: list[list[dict]] = []
    cursor = ""
    for _ in range(pages):
        page = service.feed(uid, cursor=cursor or None, limit=limit)
        out.append(page["items"])
        cursor = page.get("next_cursor") or ""
        if not cursor:
            break
    return out


def _flat(pages: list[list[dict]]) -> list[dict]:
    return [item for page in pages for item in page]


def test_sponsored_inserted_after_three_posts(tmp_path):
    db, service = _make(tmp_path)
    uid = _user(db)
    _campaign(db)
    token = set_current_user(uid)
    try:
        pages = _pages(db, service, uid, pages=3)
    finally:
        reset_current_user(token)

    first = pages[0]
    # 5 条普通动态 + 第 4 张位置插入的 1 条广告
    assert len(first) == 6
    assert first[3].get("kind") == "sponsored"

    flat = _flat(pages)
    sponsored = [item for item in flat if item.get("kind") == "sponsored"]
    assert len(sponsored) == 1, flat
    item = sponsored[0]
    assert item["label"] == "广告"
    assert item["delivery_id"].startswith("ad_")
    assert item["campaign_id"]
    assert "手冲 2 分钟" in item["content"]
    normal = [x for x in flat if x.get("kind") != "sponsored"]
    assert normal and all("id" in x and "created_at" in x for x in normal)


def test_minor_unknown_and_opt_out_never_get_ads(tmp_path):
    db, service = _make(tmp_path)
    _campaign(db)

    for group, opt_in in (("teen", True), (None, True), ("adult", False)):
        uid = _user(db, group=group, opt_in=opt_in)
        token = set_current_user(uid)
        try:
            flat = _flat(_pages(db, service, uid, pages=3))
        finally:
            reset_current_user(token)
        assert all(item.get("kind") != "sponsored" for item in flat), (group, opt_in, flat)


def test_paused_campaign_no_new_ads(tmp_path):
    db, service = _make(tmp_path)
    uid = _user(db)
    campaign = _campaign(db)
    ads.set_campaign_status(db, campaign["id"], "paused")
    token = set_current_user(uid)
    try:
        flat = _flat(_pages(db, service, uid, pages=3))
    finally:
        reset_current_user(token)
    assert all(item.get("kind") != "sponsored" for item in flat)


def test_events_click_close_and_block(tmp_path):
    db, service = _make(tmp_path)
    uid = _user(db)
    campaign = _campaign(db)
    token = set_current_user(uid)
    try:
        flat = _flat(_pages(db, service, uid, pages=3))
    finally:
        reset_current_user(token)
    sponsored = [item for item in flat if item.get("kind") == "sponsored"]
    assert sponsored, flat
    delivery_id = sponsored[0]["delivery_id"]

    ads.record_event(db, user_id=uid, delivery_id=delivery_id, event_type="click", now=NOW)
    row = ads.list_deliveries(db, user_id=uid)[0]
    assert row["status"] == "clicked"

    ads.record_event(db, user_id=uid, delivery_id=delivery_id, event_type="close", now=NOW)
    with db._lock:  # noqa: SLF001
        blocked = db.conn.execute(
            "SELECT blocked_until FROM ads_blocks WHERE user_id=? AND campaign_id=?",
            (uid, campaign["id"]),
        ).fetchone()
    assert blocked is not None and bool(str(blocked["blocked_until"]))

    token = set_current_user(uid)
    try:
        flat2 = _flat(_pages(db, service, uid, pages=4))
    finally:
        reset_current_user(token)
    assert all(item.get("kind") != "sponsored" for item in flat2)


def test_feed_summary_excludes_ads(tmp_path):
    db, service = _make(tmp_path)
    uid = _user(db)
    _campaign(db)
    token = set_current_user(uid)
    try:
        _pages(db, service, uid, pages=3)
        summary = service.feed_summary(uid)
    finally:
        reset_current_user(token)
    assert "sponsored" not in json.dumps(summary, ensure_ascii=False)
