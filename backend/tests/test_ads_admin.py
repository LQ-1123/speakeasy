"""管理台 · 商业运营（``/api/admin/ads*``）验收。

覆盖运营最小闭环：
品牌 → 活动（草稿→审核→启用；修改产生新版本、可还原历史）→ 素材独立审核
→ 角色分配建议+确认 → 预览 → 投放记录/汇总 → 留痕清理。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.router import router as admin_router
from app.v2.db import DB
from app.v3 import ads
from app.v3.schema import ensure_schema
from app.v3.service import V3Service


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "admin_ads.db"))
    database.init()
    ensure_schema(database, seed=True)
    ads.ensure_ads_schema(database)
    yield database
    database.close()


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.delenv("SE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)
    app = FastAPI()
    app.include_router(admin_router, dependencies=[Depends(auth.require_admin)])
    app.state.v2 = SimpleNamespace(db=db)
    app.state.v3 = V3Service(db, SimpleNamespace(llm=None))
    app.state.conversation_store = None
    with TestClient(app) as http:
        yield http


def _brand(client) -> dict:
    res = client.post(
        "/api/admin/ads/brands",
        json={"name": "咖啡品牌", "industry": "food", "qualification": "资质-1"},
    )
    assert res.status_code == 200, res.text
    return res.json()["brand"]


def _campaign(client, brand_id: str) -> dict:
    res = client.post(
        "/api/admin/ads/campaigns",
        json={
            "brand_id": brand_id,
            "title": "新品咖啡机",
            "purpose": "新品推荐",
            "required_tags": ["咖啡", "coffee"],
            "facts": ["2 分钟出杯"],
            "landing_url": "https://example.test/ad",
            "channels": ["chat", "feed"],
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["campaign"]


def test_brand_campaign_creative_flow(client):
    brand = _brand(client)
    assert brand["id"].startswith("br_")
    campaign = _campaign(client, brand["id"])
    assert campaign["status"] == "draft" and campaign["version"] == 1
    # 标签已统一为稳定 ID
    assert campaign["required_tags"] == ["coffee"]

    # 素材：审核前不可投放；驳回必须写原因
    creative = client.post(
        "/api/admin/ads/creatives",
        json={
            "campaign_id": campaign["id"], "channel": "chat",
            "title": "聊天卡片", "body_template": "最近在用 {facts}。", "facts": ["2 分钟出杯"],
        },
    ).json()["creative"]
    assert creative["status"] == "draft"
    assert client.get("/api/admin/ads/creatives", params={"campaign_id": campaign["id"],
                                                          "approved_only": True}).json()["items"] == []
    bad = client.post(
        f"/api/admin/ads/creatives/{creative['id']}/review", json={"approved": False, "note": ""}
    )
    assert bad.status_code == 400
    ok = client.post(
        f"/api/admin/ads/creatives/{creative['id']}/review",
        json={"approved": True, "note": "通过"},
    )
    assert ok.status_code == 200 and ok.json()["creative"]["status"] == "approved"

    # 状态机：draft 不能直接 enabled；审核后逐级
    assert client.post(
        f"/api/admin/ads/campaigns/{campaign['id']}/status", json={"status": "enabled"}
    ).status_code == 400
    assert client.post(
        f"/api/admin/ads/campaigns/{campaign['id']}/status", json={"status": "reviewed"}
    ).status_code == 200
    assert client.post(
        f"/api/admin/ads/campaigns/{campaign['id']}/status", json={"status": "enabled"}
    ).status_code == 200

    # 已发布后修改 → 新版本 + 回到 draft
    patched = client.patch(
        f"/api/admin/ads/campaigns/{campaign['id']}", json={"title": "新品咖啡机 v2"}
    )
    assert patched.status_code == 200
    body = patched.json()["campaign"]
    assert body["status"] == "draft" and body["version"] == 2

    versions = client.get(f"/api/admin/ads/campaigns/{campaign['id']}/versions").json()["items"]
    assert [item["version"] for item in versions] == [2, 1]
    restored = client.post(f"/api/admin/ads/campaigns/{campaign['id']}/restore/1")
    assert restored.status_code == 200
    assert restored.json()["campaign"]["title"] == "新品咖啡机"
    assert restored.json()["campaign"]["status"] == "draft"


def test_assignments_preview_stats_and_deliveries(client, db):
    brand = _brand(client)
    campaign = _campaign(client, brand["id"])
    suggested = client.post(
        f"/api/admin/ads/campaigns/{campaign['id']}/assignments/suggest", params={"limit": 3}
    )
    assert suggested.status_code == 200
    items = suggested.json()["items"]
    assert items
    persona_id = items[0]["persona_id"]
    confirmed = client.post(
        f"/api/admin/ads/campaigns/{campaign['id']}/assignments/{persona_id}/confirm"
    )
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed"

    # 预览：审核通过的素材 + 事实
    creative = client.post(
        "/api/admin/ads/creatives",
        json={"campaign_id": campaign["id"], "channel": "chat", "title": "t",
              "body_template": "推荐 {facts}", "facts": ["2 分钟出杯"]},
    ).json()["creative"]
    client.post(f"/api/admin/ads/creatives/{creative['id']}/review", json={"approved": True})
    preview = client.get(
        f"/api/admin/ads/campaigns/{campaign['id']}/preview", params={"channel": "chat"}
    ).json()
    assert preview["creative_approved"] is True
    assert "2 分钟出杯" in preview["copy"]

    # 造一条投放记录 → 列表与汇总可见
    cid = campaign["id"]
    ads.set_campaign_status(db, cid, "reviewed")
    ads.set_campaign_status(db, cid, "enabled")
    # 用 ads.try_chat_ad 需要成年用户与偏好；这里直接插一条记录验证运营视图
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO ads_deliveries (id, user_id, campaign_id, campaign_version, brand_id,"
            " channel, persona_id, creative_id, status, reason, position, reserved_at,"
            " confirmed_at, last_event_at, context) VALUES"
            " ('ad_test1','u1',?,1,?,'chat',?,'cr_x','impression','',0,"
            " '2026-11-12T04:00:00+00:00','','{}','{}')",
            (cid, campaign["brand_id"], persona_id),
        )
    listed = client.get("/api/admin/ads/deliveries", params={"campaign_id": cid}).json()["items"]
    assert listed and listed[0]["id"] == "ad_test1"
    stats = client.get("/api/admin/ads/stats", params={"campaign_id": cid}).json()["items"]
    assert stats and stats[0]["impressions"] == 1


def test_audit_cleanup_endpoint(client, db):
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO ads_chat_audit (delivery_id, user_id, campaign_id, campaign_version,"
            " match_reason, trigger_text, pre_context, copy, created_at, delete_after)"
            " VALUES ('ad_old','u1','c1',1,'{}','x','','y','2026-10-01T00:00:00+00:00',"
            " '2026-10-31T00:00:00+00:00')"
        )
    removed = client.post("/api/admin/ads/audit/cleanup")
    assert removed.status_code == 200 and removed.json()["deleted"] == 1
