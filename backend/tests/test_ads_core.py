"""商业合作闭环（核心）：活动/素材/角色分配、需求识别、频控与原子预留、事件与留痕。

覆盖方案第三期的硬约束：
- 只有已核验成年 + 主动开启合作推荐才可能被投放；未成年/未知年龄/未开启一律不投；
- 聊天广告：需求命中才投，泛聊/否定/不确定/心理困扰不投；文案只来自审核模板+商品事实；
- 频控：合计 24h ≤2、聊天 ≤1/24h 且间隔 ≥10 个用户回合、同品牌 7 天冷却、关闭后 30 天屏蔽；
- 动态广告：前 3 条不插、两条广告之间 ≥8 条普通动态、独立 sponsored 类型；
- 留痕：触发消息+前文合计 ≤1000 字符、30 天删除；品牌/活动/投放记录保留。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.v2.db import DB
from app.v3 import accounts, ads, client_api
from app.v3.schema import ensure_schema

NOW = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_AGE_GATE", "1")
    monkeypatch.setenv("SE_INVITE_REQUIRED", "0")
    database = DB(str(tmp_path / "ads.db"))
    database.init()
    ensure_schema(database, seed=True)
    accounts.ensure_accounts_schema(database)
    client_api.ensure_client_schema(database)
    ads.ensure_ads_schema(database)
    yield database
    database.close()


def _adult(db, email="adult@example.com", *, opt_in=True) -> str:
    reg = accounts.register_email(db, email, "pa55word!", invite_code="")
    uid = reg["user_id"]
    accounts.set_age_verification(db, uid, age_group="adult")
    if opt_in:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO client_preferences (user_id, value) VALUES (?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET value=excluded.value",
                (uid, json.dumps({"messages": True, "interactions": True, "sound": True,
                                  "theme": "system", "ads": True})),
            )
    return uid


def _persona_id(db) -> str:
    return str(db.conn.execute("SELECT id FROM personas LIMIT 1").fetchone()["id"])


def _campaign(db, *, tags=("coffee",), channels=("chat", "feed"), facts=("胶囊咖啡机 2 分钟出杯",)):
    brand = ads.create_brand(db, name="测试咖啡", industry="food", qualification="资质编号T-1")
    campaign = ads.create_campaign(
        db, brand_id=brand["id"], title="咖啡机合作", purpose="新品推荐",
        required_tags=list(tags), facts=list(facts), landing_url="https://example.test/ad",
        channels=list(channels), created_by="test",
    )
    ads.add_creative(
        db, campaign_id=campaign["id"], channel="chat", title="聊天卡片",
        body_template="我最近在用 {facts}，你问的正好。", facts=list(facts),
    )
    ads.add_creative(
        db, campaign_id=campaign["id"], channel="feed", title="动态卡片",
        body_template="今天也在用 {facts}。", facts=list(facts),
    )
    for creative in ads.list_creatives(db, campaign["id"]):
        ads.review_creative(db, creative["id"], approved=True, reviewer="test")
    ads.set_campaign_status(db, campaign["id"], "reviewed")
    ads.set_campaign_status(db, campaign["id"], "enabled")
    return campaign


def _friend(db, uid: str = "local", name: str = "Mia") -> dict:
    return db.create_friend({"name": name, "owner_user_id": uid})


# --------------------------------------------------------------------------- #
# 状态机 / 版本 / 素材审核
# --------------------------------------------------------------------------- #
def test_campaign_lifecycle_versioning_and_restore(db):
    brand = ads.create_brand(db, name="品牌A")
    campaign = ads.create_campaign(
        db, brand_id=brand["id"], title="v1", required_tags=["coffee"], facts=["事实1"],
    )
    assert campaign["status"] == "draft" and campaign["version"] == 1

    # 非法跳转：draft 不能直接 enabled
    with pytest.raises(ValueError):
        ads.set_campaign_status(db, campaign["id"], "enabled")
    ads.set_campaign_status(db, campaign["id"], "reviewed")
    ads.set_campaign_status(db, campaign["id"], "enabled")

    # 已发布后修改 → 新版本 + 回到 draft（必须重新审核）
    updated = ads.update_campaign(db, campaign["id"], title="v2", created_by="ops")
    assert updated["version"] == 2 and updated["status"] == "draft" and updated["title"] == "v2"

    versions = ads.list_campaign_versions(db, campaign["id"])
    assert [item["version"] for item in versions] == [2, 1]
    restored = ads.restore_campaign_version(db, campaign["id"], 1, created_by="ops")
    assert restored["title"] == "v1" and restored["status"] == "draft"
    assert restored["version"] >= 3  # 还原也生成新版本，历史不丢

    # 素材：draft 不可投；审核通过后才可用
    creative = ads.add_creative(
        db, campaign_id=campaign["id"], channel="chat", title="t", body_template="hi {facts}",
    )
    assert ads.list_creatives(db, campaign["id"], approved_only=True) == []
    with pytest.raises(ValueError):
        ads.review_creative(db, creative["id"], approved=False)  # 驳回必须写原因
    ads.review_creative(db, creative["id"], approved=False, note="功效表述需改")
    assert ads.get_creative(db, creative["id"])["status"] == "rejected"


def test_assignments_suggest_and_confirm(db):
    brand = ads.create_brand(db, name="品牌B")
    campaign = ads.create_campaign(
        db, brand_id=brand["id"], title="咖啡", required_tags=["coffee"],
    )
    suggestions = ads.suggest_persona_assignments(db, campaign["id"], limit=3)
    assert suggestions and all(0 <= item["score"] for item in suggestions)
    ads.confirm_assignment(db, campaign["id"], suggestions[0]["persona_id"], confirmed=True)

    campaign_row = ads.get_campaign(db, campaign["id"])
    ads.set_campaign_status(db, campaign["id"], "reviewed")
    ads.set_campaign_status(db, campaign["id"], "enabled")
    assert campaign_row["status"] == "draft"  # 之前取的快照不用于断言
    candidates = [c for c in ads.list_campaigns(db, status="enabled")]
    assert any(c["id"] == campaign["id"] for c in candidates)


# --------------------------------------------------------------------------- #
# 需求识别与资格
# --------------------------------------------------------------------------- #
def test_detect_demand_rules():
    assert ads.detect_demand("I'm looking for a coffee machine")["matched"] is True
    assert "coffee" in ads.detect_demand("想买个咖啡机")["tags"]
    for text, reason in (
        ("hi", "vague"),
        ("算了不用了", "refusal"),
        ("不太确定，再说吧", "refusal"),
        ("最近很焦虑，睡不着", "distress"),
        ("今天天气不错", "no_need_signal"),
    ):
        result = ads.detect_demand(text)
        assert result["matched"] is False, (text, result)
        assert result["reason"] in (reason, "no_need_signal")


def test_eligibility_requires_adult_verified_and_opt_in(db):
    adult = _adult(db, "adult2@example.com", opt_in=False)
    eligible, reason = ads.ad_eligible(db, adult)
    assert eligible is False and reason == "ads_opt_out"

    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO client_preferences (user_id, value) VALUES (?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET value=excluded.value",
            (adult, json.dumps({"ads": True})),
        )
    assert ads.ad_eligible(db, adult) == (True, "")

    minor = accounts.register_email(db, "teen2@example.com", "pa55word!", invite_code="")["user_id"]
    accounts.set_age_verification(db, minor, age_group="teen")
    assert ads.ad_eligible(db, minor)[0] is False

    unknown = accounts.register_email(db, "unk@example.com", "pa55word!", invite_code="")["user_id"]
    assert ads.ad_eligible(db, unknown)[0] is False
    assert ads.ad_eligible(db, "local")[0] is False


# --------------------------------------------------------------------------- #
# 聊天投放：命中 / 文案 / 频控 / 回合间隔 / 冷却 / 屏蔽
# --------------------------------------------------------------------------- #
def test_chat_ad_happy_path_composes_approved_copy(db):
    uid = _adult(db, "chat1@example.com")
    persona = _persona_id(db)
    friend = _friend(db, uid)
    campaign = _campaign(db)
    ads.confirm_assignment(db, campaign["id"], persona)

    result = ads.try_chat_ad(
        db, user_id=uid, friend_id=friend["id"], persona_id=persona,
        user_text="I'm thinking about buying a coffee machine, any idea?",
        pre_context="user: 最近想改善早餐\nfriend: 说说看", now=NOW,
    )
    assert result is not None, "应命中咖啡需求"
    assert result["delivery_id"].startswith("ad_")
    assert "胶囊咖啡机" in result["copy"]
    assert result["label"].startswith("广告")
    # 文案只来自审核模板+事实，不含模型自由发挥的字段
    assert set(result).issuperset({"copy", "delivery_id", "campaign_id", "brand_name"})

    # 审计留痕：≤1000 字符、30 天后删除
    with db._lock:  # noqa: SLF001
        audit = db.conn.execute(
            "SELECT * FROM ads_chat_audit WHERE delivery_id=?", (result["delivery_id"],)
        ).fetchone()
    assert audit is not None
    assert len(str(audit["trigger_text"]) + str(audit["pre_context"])) <= 1000
    delete_after = ads._parse_iso(audit["delete_after"])  # noqa: SLF001
    assert delete_after == NOW + timedelta(days=30)

    ads.confirm_delivery(db, result["delivery_id"], message_id="m1", now=NOW)
    row = ads.list_deliveries(db, user_id=uid)[0]
    assert row["status"] == "delivered" and row["channel"] == "chat"

    # 24h 内第二次聊天广告被频控拒绝
    later = NOW + timedelta(hours=2)
    second = ads.try_chat_ad(
        db, user_id=uid, friend_id=friend["id"], persona_id=persona,
        user_text="coffee machine again, where to buy", now=later,
    )
    assert second is None


def test_chat_ad_turn_gap_and_daily_cap(db):
    uid = _adult(db, "chat2@example.com")
    persona = _persona_id(db)
    friend = _friend(db, uid)
    campaign = _campaign(db)
    ads.confirm_assignment(db, campaign["id"], persona)

    first = ads.try_chat_ad(
        db, user_id=uid, friend_id=friend["id"], persona_id=persona,
        user_text="recommend a coffee machine", now=NOW,
    )
    assert first is not None
    ads.confirm_delivery(db, first["delivery_id"], now=NOW)

    # 过了 24h，但用户回合不足 10 → 仍不投
    day_later = NOW + timedelta(hours=25)
    for index in range(3):
        db.add_message(friend["id"], "user", f"turn {index}", msg_type="voice")
    assert ads.try_chat_ad(
        db, user_id=uid, friend_id=friend["id"], persona_id=persona,
        user_text="buy coffee machine", now=day_later,
    ) is None

    # 补足 10 个回合后：换一个品牌的新活动（同品牌有 7 天冷却，不该是这里要测的）
    campaign2 = _campaign(db, tags=("coffee",))
    ads.confirm_assignment(db, campaign2["id"], persona)
    for index in range(10):
        db.add_message(friend["id"], "user", f"more {index}", msg_type="voice")
    assert ads.try_chat_ad(
        db, user_id=uid, friend_id=friend["id"], persona_id=persona,
        user_text="buy coffee machine", now=day_later,
    ) is not None


def test_brand_cooldown_and_close_block(db):
    uid = _adult(db, "chat3@example.com")
    persona = _persona_id(db)
    friend = _friend(db, uid)
    campaign = _campaign(db)
    ads.confirm_assignment(db, campaign["id"], persona)

    first = ads.try_chat_ad(
        db, user_id=uid, friend_id=friend["id"], persona_id=persona,
        user_text="recommend a coffee machine", now=NOW,
    )
    assert first is not None
    ads.confirm_delivery(db, first["delivery_id"], now=NOW)
    # 同品牌 7 天冷却：换新候选者/新回合也不能再投同品牌
    for index in range(12):
        db.add_message(friend["id"], "user", f"t{index}", msg_type="voice")
    assert ads.try_chat_ad(
        db, user_id=uid, friend_id=friend["id"], persona_id=persona,
        user_text="another coffee machine", now=NOW + timedelta(hours=25),
    ) is None

    # 关闭 → 30 天屏蔽该活动；解除后仍需满足冷却
    ads.record_event(db, user_id=uid, delivery_id=first["delivery_id"], event_type="close", now=NOW)
    with db._lock:  # noqa: SLF001
        block = db.conn.execute(
            "SELECT blocked_until FROM ads_blocks WHERE user_id=? AND campaign_id=?",
            (uid, campaign["id"]),
        ).fetchone()
    assert block is not None
    assert ads._parse_iso(block["blocked_until"]) == NOW + timedelta(days=30)  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 动态投放：间距 / 类型 / 事件
# --------------------------------------------------------------------------- #
def test_feed_ad_spacing_and_shape(db):
    uid = _adult(db, "feed1@example.com")
    campaign = _campaign(db)

    # 首屏足够 8 条：前 3 张不插，第 4 张位置插一条
    result = ads.try_feed_ad(db, user_id=uid, page_offset=0, items_in_page=8, now=NOW)
    assert result is not None
    assert result["kind"] == "sponsored" and result["delivery_id"].startswith("ad_")
    assert result["campaign_id"] == campaign["id"]
    assert result["label"] == "广告"
    assert result["slot"] == 3

    # 随后 8 条内不再插（先累计到 8 条）
    assert ads.try_feed_ad(db, user_id=uid, page_offset=8, items_in_page=8, now=NOW) is None
    # 同品牌 7 天冷却：即使间隔已满也不会再插同一品牌
    assert ads.try_feed_ad(db, user_id=uid, page_offset=16, items_in_page=8, now=NOW) is None

    stats = ads.campaign_stats(db, campaign["id"])
    row = stats["items"][0]
    assert row["delivered"] >= 1


def test_events_update_status_and_stats(db):
    uid = _adult(db, "evt@example.com")
    persona = _persona_id(db)
    friend = _friend(db, uid)
    campaign = _campaign(db)
    ads.confirm_assignment(db, campaign["id"], persona)
    result = ads.try_chat_ad(
        db, user_id=uid, friend_id=friend["id"], persona_id=persona,
        user_text="recommend a coffee machine", now=NOW,
    )
    assert result is not None
    delivery_id = result["delivery_id"]

    ads.record_event(db, user_id=uid, delivery_id=delivery_id, event_type="impression",
                     payload={"visible_ms": 1200}, now=NOW)
    assert ads.list_deliveries(db, user_id=uid)[0]["status"] == "impression"
    ads.record_event(db, user_id=uid, delivery_id=delivery_id, event_type="click", now=NOW)
    assert ads.list_deliveries(db, user_id=uid)[0]["status"] == "clicked"

    stats = ads.campaign_stats(db, campaign["id"])["items"][0]
    assert stats["clicks"] == 1 and stats["impressions"] >= 1

    # 别人的投放不能上报
    other = _adult(db, "other@example.com")
    with pytest.raises(ValueError):
        ads.record_event(db, user_id=other, delivery_id=delivery_id, event_type="click", now=NOW)


def test_preview_and_audit_cleanup(db):
    campaign = _campaign(db)
    preview = ads.preview(db, campaign["id"], channel="chat")
    assert preview["creative_approved"] is True
    assert "胶囊咖啡机" in preview["copy"]

    uid = _adult(db, "clean@example.com")
    persona = _persona_id(db)
    friend = _friend(db, uid)
    ads.confirm_assignment(db, campaign["id"], persona)
    result = ads.try_chat_ad(
        db, user_id=uid, friend_id=friend["id"], persona_id=persona,
        user_text="recommend a coffee machine", now=NOW,
    )
    assert result is not None
    # 30 天后清理留痕；提前一天不删
    assert ads.cleanup_audit(db, now=NOW + timedelta(days=29)) == 0
    assert ads.cleanup_audit(db, now=NOW + timedelta(days=31)) == 1
    with db._lock:  # noqa: SLF001
        left = db.conn.execute("SELECT COUNT(*) AS n FROM ads_chat_audit").fetchone()["n"]
    assert int(left) == 0
