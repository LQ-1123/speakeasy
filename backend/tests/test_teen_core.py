"""青少年模式核心（分龄门禁 / 监护绑定 / 时长与宵禁 / 推送抑制 / 运营接口）。

覆盖升级方案第一期「青少年模式」验收要点：
- 14–17 岁必须先完成监护绑定；未知年龄只保留帮助入口（门禁开启时）；
- 内测默认 60 分钟/天、连续 30 分钟提醒、22:00–06:00 停用；监护人只能收紧；
- 年龄/监护状态变化即时作用于门禁结论（撤销监护立刻回到 guardian_required）；
- 未成年人无广告、无亲密关系（capabilities 已有，此处校验门禁与状态一致）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.router import router as admin_router
from app.v2.db import DB
from app.v3 import accounts, teen
from app.v3.schema import ensure_schema
from app.v3.service import V3Service

#: 固定参考时刻：Asia/Shanghai 2026-11-11 10:00 = UTC 02:00
DAY_UTC = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)
#: 宵禁内：Asia/Shanghai 22:30 = UTC 14:30
CURFEW_UTC = datetime(2026, 11, 11, 14, 30, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_AGE_GATE", "1")
    monkeypatch.setenv("SE_INVITE_REQUIRED", "0")
    monkeypatch.setenv("SE_MAIL_DEV", "1")
    database = DB(str(tmp_path / "teen.db"))
    database.init()
    ensure_schema(database, seed=False)
    accounts.ensure_accounts_schema(database)
    teen.ensure_teen_schema(database)
    yield database
    database.close()


def _user(db, email: str) -> dict:
    return accounts.register_email(db, email, "pa55word!", invite_code="")


def _teen(db, email: str = "teen@example.com") -> str:
    reg = _user(db, email)
    uid = reg["user_id"]
    accounts.set_age_verification(db, uid, age_group="teen")
    return uid


def _adult(db, email: str = "adult@example.com") -> str:
    reg = _user(db, email)
    accounts.set_age_verification(db, reg["user_id"], age_group="adult")
    return reg["user_id"]


# --------------------------------------------------------------------------- #
# 限制配置：只能收紧
# --------------------------------------------------------------------------- #
def test_minor_settings_default_and_tighten_only(db):
    uid = _teen(db)
    defaults = teen.get_minor_settings(db, uid)
    assert defaults["daily_minutes"] == 60
    assert defaults["session_minutes"] == 30
    assert defaults["curfew_start"] == "22:00" and defaults["curfew_end"] == "06:00"

    tightened = teen.set_minor_settings(db, uid, daily_minutes=30, session_minutes=20)
    assert tightened["daily_minutes"] == 30 and tightened["session_minutes"] == 20

    with pytest.raises(ValueError):
        teen.set_minor_settings(db, uid, daily_minutes=90)
    with pytest.raises(ValueError):
        teen.set_minor_settings(db, uid, session_minutes=60)
    with pytest.raises(ValueError):
        # 白天窗口 = 事实上取消宵禁，不允许
        teen.set_minor_settings(db, uid, curfew_start="10:00", curfew_end="12:00")

    wider = teen.set_minor_settings(db, uid, curfew_start="21:00", curfew_end="07:00")
    assert wider["curfew_start"] == "21:00" and wider["curfew_end"] == "07:00"


# --------------------------------------------------------------------------- #
# 监护绑定 / 撤销
# --------------------------------------------------------------------------- #
def test_guardian_bind_and_revoke_immediately_affects_gate(db):
    uid = _teen(db)
    assert teen.check_access(db, uid, now=DAY_UTC)["reason"] == "guardian_required"

    guardian = _adult(db, "parent@example.com")
    link = teen.bind_guardian(db, uid, guardian_user_id=guardian, method="ops")
    assert link["status"] == "active"
    assert teen.guardian_status(db, uid)["role"] == "minor"
    assert teen.guardian_status(db, guardian)["role"] == "guardian"
    assert teen.check_access(db, uid, now=DAY_UTC)["allowed"] is True

    assert teen.revoke_guardian(db, uid) is True
    access = teen.check_access(db, uid, now=DAY_UTC)
    assert access["allowed"] is False and access["reason"] == "guardian_required"
    # 年龄结论保留（14–17 岁仍成立），只是不再有监护人 → 门禁停在 guardian_required
    caps = accounts.capabilities(db, uid)
    assert caps["age"]["verified"] is True
    assert caps["age"]["guardian_bound"] is False


def test_guardian_code_flow(db):
    uid = _teen(db)
    guardian = _adult(db, "parent2@example.com")
    code = teen.request_guardian(db, uid)["code"]
    assert teen.guardian_status(db, uid)["status"] == "pending"

    with pytest.raises(ValueError):
        teen.confirm_guardian(db, guardian, "WRONGCODE")
    # 本人不能给自己当监护人
    with pytest.raises(ValueError):
        teen.confirm_guardian(db, uid, code)

    done = teen.confirm_guardian(db, guardian, code)
    assert done["status"] == "active"
    assert teen.check_access(db, uid, now=DAY_UTC)["allowed"] is True


# --------------------------------------------------------------------------- #
# 门禁矩阵
# --------------------------------------------------------------------------- #
def test_gate_matrix_for_adult_unknown_and_teen(db):
    adult = _adult(db)
    assert teen.check_access(db, adult, now=DAY_UTC)["allowed"] is True

    unknown = _user(db, "unknown@example.com")["user_id"]
    denied = teen.check_access(db, unknown, now=DAY_UTC)
    assert denied["allowed"] is False and denied["reason"] == "age_unverified"

    # local 永不受限（单用户联调/老客户端）
    assert teen.check_access(db, "local", now=DAY_UTC)["allowed"] is True


def test_daily_limit_and_curfew_and_reminder(db):
    uid = _teen(db)
    guardian = _adult(db, "parent3@example.com")
    teen.bind_guardian(db, uid, guardian_user_id=guardian, method="ops")

    # 连续活跃 35 分钟：应触发「连续 30 分钟提醒」但仍允许
    moment = DAY_UTC
    teen.record_activity(db, uid, now=moment)
    for _ in range(7):
        moment = moment + timedelta(minutes=5)
        teen.record_activity(db, uid, now=moment)
    usage = teen.usage_for(db, uid, now=moment)
    assert usage["continuous_minutes"] >= 30
    reminder = teen.check_access(db, uid, now=moment)
    assert reminder["allowed"] is True and reminder["reminder"] is True

    # 累计超过 60 分钟 → 当日上限
    while teen.usage_for(db, uid, now=moment)["seconds"] < 3600:
        moment = moment + timedelta(minutes=3)
        teen.record_activity(db, uid, now=moment)
    over = teen.check_access(db, uid, now=moment)
    assert over["allowed"] is False and over["reason"] == "daily_limit"

    # 宵禁：本地 22:30 直接拒绝，并给出到 06:00 的等待秒数
    curfew = teen.check_access(db, uid, now=CURFEW_UTC)
    assert curfew["allowed"] is False and curfew["reason"] == "curfew"
    assert curfew["retry_after_s"] > 0
    assert teen.push_suppressed(db, uid, now=CURFEW_UTC) is True


def test_blocked_persona(db):
    uid = _teen(db)
    guardian = _adult(db, "parent4@example.com")
    teen.bind_guardian(db, uid, guardian_user_id=guardian, method="ops")
    teen.set_minor_settings(db, uid, blocked_personas=["p_adrian"])
    blocked = teen.check_access(db, uid, persona_id="p_adrian", now=DAY_UTC)
    assert blocked["allowed"] is False and blocked["reason"] == "persona_blocked"
    assert teen.check_access(db, uid, persona_id="p_mia", now=DAY_UTC)["allowed"] is True


def test_reset_session_keeps_daily_total(db):
    uid = _teen(db)
    guardian = _adult(db, "parent5@example.com")
    teen.bind_guardian(db, uid, guardian_user_id=guardian, method="ops")
    teen.record_activity(db, uid, now=DAY_UTC)
    moment = DAY_UTC + timedelta(minutes=5)
    teen.record_activity(db, uid, now=moment)
    before = teen.usage_for(db, uid, now=moment)
    teen.reset_session(db, uid, now=moment)
    after = teen.usage_for(db, uid, now=moment)
    assert after["continuous_seconds"] == 0
    assert after["seconds"] == pytest.approx(before["seconds"], abs=0.1)


# --------------------------------------------------------------------------- #
# 运营接口（管理台）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def admin_client(db, monkeypatch):
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


def test_admin_can_bind_and_tighten(admin_client, db):
    uid = _teen(db)
    guardian = _adult(db, "parent6@example.com")

    bound = admin_client.post(
        f"/api/admin/accounts/{uid}/teen/guardian",
        json={"guardian_user_id": guardian, "note": "运营核验监护关系"},
    )
    assert bound.status_code == 200, bound.text
    payload = bound.json()
    assert payload["status"]["guardian"]["role"] == "minor"
    # 真实时间可能落在宵禁窗口内，这里只校验「不再因未监护被拒」
    assert payload["status"]["access"]["reason"] in ("", "curfew")

    tightened = admin_client.post(
        f"/api/admin/accounts/{uid}/teen/settings",
        json={"daily_minutes": 30, "curfew_start": "21:30", "curfew_end": "06:30"},
    )
    assert tightened.status_code == 200, tightened.text
    assert tightened.json()["settings"]["daily_minutes"] == 30

    bad = admin_client.post(
        f"/api/admin/accounts/{uid}/teen/settings", json={"daily_minutes": 120}
    )
    assert bad.status_code in (400, 422)

    revoked = admin_client.delete(f"/api/admin/accounts/{uid}/teen/guardian")
    assert revoked.status_code == 200
    assert revoked.json()["status"]["access"]["reason"] == "guardian_required"


def test_admin_teen_status_route_is_scoped_to_user(admin_client, db):
    uid = _teen(db)
    data = admin_client.get(f"/api/admin/accounts/{uid}/teen").json()
    assert data["user_id"] == uid
    assert data["settings"]["daily_minutes"] == 60
    assert data["guardian"]["role"] != "guardian"


# --------------------------------------------------------------------------- #
# 客户端自助接口
# --------------------------------------------------------------------------- #
def test_teen_router_status_and_heartbeat(db):
    from app.v3.teen_router import router as teen_router
    from fastapi import FastAPI as _FastAPI

    reg = _user(db, "self@example.com")
    uid = reg["user_id"]
    accounts.set_age_verification(db, uid, age_group="teen")
    app = _FastAPI()
    app.state.v2 = SimpleNamespace(db=db)
    app.include_router(teen_router)
    with TestClient(app) as client:
        headers = {"X-Session-Token": reg["session_token"]}
        status = client.get("/api/v3/teen/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["access"]["reason"] == "guardian_required"

        heartbeat = client.post("/api/v3/teen/heartbeat", headers=headers)
        assert heartbeat.status_code == 200
        assert heartbeat.json()["usage"]["last_turn_at"] != ""


def test_session_only_user_can_create_call(db):
    """账号体系：只有 X-Session-Token 的邮箱账号也能发起通话（不再要求旧设备头）。"""
    from app.v3.calls import router as calls_router

    reg = _user(db, "caller@example.com")
    uid = reg["user_id"]
    accounts.set_age_verification(db, uid, age_group="adult")
    friend = db.create_friend({"name": "Mia", "owner_user_id": uid})

    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db)
    app.state.v3 = SimpleNamespace()
    app.include_router(calls_router)
    with TestClient(app) as client:
        created = client.post(
            "/api/v3/calls",
            json={"friend_id": friend["id"]},
            headers={"X-Session-Token": reg["session_token"]},
        )
        assert created.status_code == 200, created.text
        assert created.json()["call_id"]

