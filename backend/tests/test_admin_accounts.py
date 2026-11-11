"""管理台 · 账号运营（邀请码 / 账号查询 / 年龄核验）验收。

覆盖：
- 邀请码创建、列表、停用；停用后注册被拒（邀请准入闭环）。
- 运营按邮箱/用户 id 查询账号，不返回密码哈希/token。
- 年龄核验写入独立表并即时反映到 capabilities（青少年无广告、未知年龄降级）。
- 写操作自动进 ``admin_audit``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.router import router as admin_router
from app.v2.db import DB
from app.v3 import accounts
from app.v3.schema import ensure_schema
from app.v3.service import V3Service


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "admin_accounts.db"))
    database.init()
    ensure_schema(database, seed=True)
    accounts.ensure_accounts_schema(database)
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


def _audit_count(db) -> int:
    return int(
        db.conn.execute("SELECT COUNT(*) AS n FROM admin_audit").fetchone()["n"]
    )


def test_invite_lifecycle(client, db):
    created = client.post(
        "/api/admin/accounts/invites",
        json={"note": "首轮内测", "max_uses": 2},
    )
    assert created.status_code == 200, created.text
    code = created.json()["code"]
    assert code

    listed = client.get("/api/admin/accounts/invites").json()["items"]
    row = next(item for item in listed if item["code"] == code)
    assert row["remaining"] == 2 and row["disabled"] is False

    # 重复创建同一码 → 409
    assert (
        client.post("/api/admin/accounts/invites", json={"code": code}).status_code == 409
    )

    # 停用后不能再被消费
    assert client.post(f"/api/admin/accounts/invites/{code}/disable").status_code == 200
    assert accounts.consume_invite_code(db, code) is False
    assert client.post("/api/admin/accounts/invites/not-a-code/disable").status_code == 404

    # 写操作进入审计
    assert _audit_count(db) >= 2


def test_account_search_and_age_verification(client, db):
    invite = client.post(
        "/api/admin/accounts/invites", json={"max_uses": 3}
    ).json()["code"]
    reg = accounts.register_email(db, "ops@example.com", "pa55word!", invite_code=invite)
    uid = reg["user_id"]

    found = client.get("/api/admin/accounts/users", params={"query": "ops@"}).json()["items"]
    assert [item["user_id"] for item in found] == [uid]
    assert found[0]["email"] == "ops@example.com"
    assert "password_hash" not in found[0]
    assert "token" not in str(found[0]).lower()

    # 青少年核验：无广告、无亲密关系、保留普通交流
    res = client.post(
        f"/api/admin/accounts/users/{uid}/age",
        json={"age_group": "teen", "method": "ops", "note": "监护关系已核验"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["record"]["age_group"] == "teen"
    assert payload["capabilities"]["age"]["verified"] is True
    assert payload["capabilities"]["features"]["ads"] is False
    assert payload["capabilities"]["features"]["intimate_relationship"] is False
    assert payload["capabilities"]["features"]["chat"] is True

    # 成年核验：可投广告
    adult = client.post(
        f"/api/admin/accounts/users/{uid}/age",
        json={"age_group": "adult"},
    ).json()
    assert adult["capabilities"]["features"]["ads"] is True

    # 非法取值 → 400
    assert (
        client.post(
            f"/api/admin/accounts/users/{uid}/age", json={"age_group": "elder"}
        ).status_code
        == 400
    )

    caps = client.get(f"/api/admin/accounts/users/{uid}/capabilities").json()
    assert caps["account"]["type"] == "email"
    assert caps["age"]["group"] == "adult"
