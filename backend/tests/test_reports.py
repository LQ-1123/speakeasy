"""内容举报（通用举报表 + 用户侧/运营侧接口）验收。

覆盖：登录要求、对象/类别校验、24h 去重、每日上限、我的举报列表、
运营状态机（reviewing→resolved/rejected，不允许从终态变更）、管理台汇总。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.router import router as admin_router
from app.v2.db import DB
from app.v3 import accounts, reports
from app.v3.reports_router import router as reports_router
from app.v3.schema import ensure_schema
from app.v3.service import V3Service

NOW = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_INVITE_REQUIRED", "0")
    database = DB(str(tmp_path / "reports.db"))
    database.init()
    ensure_schema(database, seed=False)
    accounts.ensure_accounts_schema(database)
    reports.ensure_reports_schema(database)
    yield database
    database.close()


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.delenv("SE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)
    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db)
    app.state.v3 = V3Service(db, SimpleNamespace(llm=None))
    app.include_router(reports_router)
    app.include_router(admin_router, dependencies=[Depends(auth.require_admin)])
    with TestClient(app) as http:
        yield http


def _user(db: DB, email: str) -> tuple[str, dict]:
    reg = accounts.register_email(db, email, "pa55word!", invite_code="")
    return reg["user_id"], {"X-Session-Token": reg["session_token"]}


def test_create_and_list_own_reports(client, db):
    uid, headers = _user(db, "r1@example.com")
    created = client.post(
        "/api/v3/reports",
        headers=headers,
        json={"target_type": "post", "target_id": "pp_1", "category": "spam",
              "detail": "重复刷屏"},
    )
    assert created.status_code == 200, created.text
    body = created.json()["report"]
    assert body["status"] == "open" and body["target_type"] == "post"

    mine = client.get("/api/v3/reports/mine", headers=headers).json()["items"]
    assert [item["id"] for item in mine] == [body["id"]]

    # 未登录 → 401
    assert client.post(
        "/api/v3/reports",
        json={"target_type": "post", "target_id": "pp_1", "category": "spam"},
    ).status_code == 401


def test_validation_and_duplicate_dedup(client, db):
    uid, headers = _user(db, "r2@example.com")
    assert client.post(
        "/api/v3/reports", headers=headers,
        json={"target_type": "comment", "target_id": "c1", "category": "nope"},
    ).status_code == 422
    assert client.post(
        "/api/v3/reports", headers=headers,
        json={"target_type": "unknown", "target_id": "x", "category": "spam"},
    ).status_code == 422
    assert client.post(
        "/api/v3/reports", headers=headers,
        json={"target_type": "comment", "target_id": "", "category": "spam"},
    ).status_code == 422

    first = client.post(
        "/api/v3/reports", headers=headers,
        json={"target_type": "comment", "target_id": "c1", "category": "spam"},
    )
    assert first.status_code == 200
    dup = client.post(
        "/api/v3/reports", headers=headers,
        json={"target_type": "comment", "target_id": "c1", "category": "harassment"},
    )
    assert dup.status_code == 400
    assert "已举报" in dup.json()["detail"]


def test_daily_limit(db):
    uid = accounts.register_email(db, "r3@example.com", "pa55word!", invite_code="")["user_id"]
    for index in range(reports.MAX_PER_DAY):
        reports.create_report(
            db, reporter_user_id=uid, target_type="post", target_id=f"pp_{index}",
            category="spam", now=NOW,
        )
    with pytest.raises(PermissionError):
        reports.create_report(
            db, reporter_user_id=uid, target_type="post", target_id="pp_over",
            category="spam", now=NOW,
        )


def test_admin_triage_flow(client, db):
    uid, headers = _user(db, "r4@example.com")
    created = client.post(
        "/api/v3/reports", headers=headers,
        json={"target_type": "persona", "target_id": "p_mia", "category": "harassment"},
    ).json()["report"]

    listed = client.get("/api/admin/reports", params={"status": "open"}).json()["items"]
    assert any(item["id"] == created["id"] for item in listed)

    reviewed = client.post(
        f"/api/admin/reports/{created['id']}/status",
        json={"status": "reviewing", "resolution": "开始核查"},
    )
    assert reviewed.status_code == 200 and reviewed.json()["report"]["status"] == "reviewing"

    # 终态不可再变
    done = client.post(
        f"/api/admin/reports/{created['id']}/status",
        json={"status": "resolved", "resolution": "已处理"},
    )
    assert done.status_code == 200 and done.json()["report"]["status"] == "resolved"
    assert client.post(
        f"/api/admin/reports/{created['id']}/status", json={"status": "reviewing"}
    ).status_code == 400

    stats = client.get("/api/admin/reports/stats").json()["items"]
    assert any(item["status"] == "resolved" and item["count"] == 1 for item in stats)
