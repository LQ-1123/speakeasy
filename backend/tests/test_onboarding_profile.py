"""首次建档（Onboarding）状态持久化测试。

覆盖点
------
- 新注册用户 ``GET /api/v3/profile`` → 未完成（不依赖 localStorage）；
- 只填部分资料但没 ``complete_setup`` → 不会被误标完成；
- 完整资料 + ``complete_setup=true`` → 已完成后落库全部字段（多目标语言/分语言水平/
  自定义兴趣/沟通偏好），重复 complete 不改变首次完成时间；
- 缺必填字段时 400 且不落「完成」状态；
- 换设备（同一 user_id 重新注册）仍能读到完成状态；
- 不同 user_id 资料严格隔离；
- 老库迁移：迁移前已存在的资料行 / 已注册的 v3 账号标记为已完成（老用户不被强制重填），
  迁移后新注册账号仍未完成；迁移与 schema 初始化幂等；
- ``communication_preference`` 非法值忽略、合法值进 fact_sheet 并通过 bootstrap 返回；
- 老字段 PUT 调用（不传新参数）行为保持不变。

全部使用 ``tmp_path`` 下的临时数据库，不触碰 ``data/``。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v3.client_api import ensure_client_schema  # noqa: E402
from app.v3.client_api import router as client_router  # noqa: E402
from app.v3.router import router as v3_router  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.security import register_user  # noqa: E402
from app.v3.service import V3Service  # noqa: E402

#: 迁移前（没有 profile_setup_completed_at 列）的两张资料表 + v3_users
_LEGACY_DDL = """
CREATE TABLE user_profile (
    id INTEGER PRIMARY KEY DEFAULT 1,
    nickname TEXT DEFAULT 'Traveler',
    native_language TEXT DEFAULT 'zh-CN',
    target_languages JSON DEFAULT '["en"]',
    birthday TEXT DEFAULT '',
    fact_sheet JSON NOT NULL DEFAULT '{}',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE user_profiles (
    user_id TEXT PRIMARY KEY,
    nickname TEXT DEFAULT 'Traveler',
    native_language TEXT DEFAULT 'zh-CN',
    target_languages JSON DEFAULT '["en"]',
    birthday TEXT DEFAULT '',
    fact_sheet JSON NOT NULL DEFAULT '{}',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE v3_users (
    id TEXT PRIMARY KEY,
    device_key TEXT UNIQUE,
    token_hash TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT ''
);
"""


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #
def _make_app(tmp_path: Path):
    db = DB(str(tmp_path / "onboarding.db"))
    db.init()
    ensure_schema(db, seed=False)
    ensure_client_schema(db)
    service = V3Service(
        db, SimpleNamespace(llm=SimpleNamespace(uses_mock=True)), str(tmp_path)
    )
    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db)
    app.state.v3 = service
    app.include_router(v3_router)
    app.include_router(client_router)
    return app, db, service


@pytest.fixture()
def api(tmp_path):
    app, db, service = _make_app(tmp_path)
    with TestClient(app) as client:
        yield client, db, service
    db.close()


def _register(client: TestClient, device_key: str, display_name: str = ""):
    response = client.post(
        "/api/v3/auth/register",
        json={"device_key": device_key, "display_name": display_name},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    headers = {"X-User-Id": data["user_id"], "X-User-Token": data["token"]}
    return data, headers


def _get_state(client: TestClient, headers: dict) -> dict:
    response = client.get("/api/v3/profile", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _complete_payload(**overrides) -> dict:
    payload = {
        "nickname": "小白",
        "native_language": "zh",
        "target_languages": ["en", "de"],
        "birthday": "1998-05-01",
        "city": "上海",
        "job": "设计师",
        "gender": "female",
        "interests": ["咖啡", "#旅行"],
        "level": "能聊几句",
        "levels": {"en": "能聊几句", "de": "刚开始"},
        "communication_preference": "improve",
        "complete_setup": True,
    }
    payload.update(overrides)
    return payload


def _create_legacy_db(
    path: Path,
    *,
    local_nickname: str = "老本地",
    local_updated_at: str = "2024-05-06T07:08:09+00:00",
    user_profiles: tuple[tuple[str, str, str], ...] = (),
    v3_users: tuple[str, ...] = (),
) -> None:
    """构造「功能上线前」的库：旧版两张资料表 + 可选的老 v3_users 账号。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_LEGACY_DDL)
        conn.execute(
            "INSERT INTO user_profile (id, nickname, target_languages, fact_sheet, updated_at)"
            " VALUES (1, ?, '[\"en\"]', '{}', ?)",
            (local_nickname, local_updated_at),
        )
        for user_id, nickname, updated_at in user_profiles:
            conn.execute(
                "INSERT INTO user_profiles"
                " (user_id, nickname, target_languages, fact_sheet, updated_at)"
                " VALUES (?, ?, '[\"en\"]', '{}', ?)",
                (user_id, nickname, updated_at),
            )
        for user_id in v3_users:
            conn.execute(
                "INSERT INTO v3_users"
                " (id, device_key, token_hash, display_name, created_at, last_seen_at)"
                " VALUES (?, ?, 'h', 'Legacy', ?, ?)",
                (user_id, f"dev-{user_id}", local_updated_at, local_updated_at),
            )
        conn.commit()
    finally:
        conn.close()


def _columns(db: DB, table: str) -> set[str]:
    with db._lock:  # noqa: SLF001 - 测试直接读 PRAGMA
        return {row[1] for row in db.conn.execute(f"PRAGMA table_info({table})")}


# --------------------------------------------------------------------------- #
# 1) 新用户 = 未完成
# --------------------------------------------------------------------------- #
def test_new_user_setup_is_incomplete(api):
    client, _db, _service = api
    _data, headers = _register(client, "device_onboarding_0001")

    state = _get_state(client, headers)
    assert state["setup"]["completed"] is False
    assert state["setup"]["completed_at"] == ""
    # 默认占位昵称（Traveler）视为未填
    assert state["setup"]["missing"] == ["nickname"]
    assert state["profile"]["profile_setup_completed_at"] == ""


# --------------------------------------------------------------------------- #
# 2) 只填昵称不 complete → 不会被误标完成
# --------------------------------------------------------------------------- #
def test_partial_profile_without_complete_stays_incomplete(api):
    client, db, _service = api
    data, headers = _register(client, "device_onboarding_0002")

    res = client.put("/api/v3/profile", headers=headers, json={"nickname": "小白"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["profile"]["nickname"] == "小白"
    assert body["setup"]["completed"] is False
    assert body["setup"]["completed_at"] == ""

    # 刷新后仍未完成（服务端状态，不靠 localStorage）
    assert _get_state(client, headers)["setup"]["completed"] is False
    assert db.get_profile(user_id=data["user_id"])["profile_setup_completed_at"] == ""


# --------------------------------------------------------------------------- #
# 3) 完整资料 + complete_setup=true → 完成并全量落库
# --------------------------------------------------------------------------- #
def test_full_onboarding_persists_everything_and_completes(api):
    client, db, _service = api
    data, headers = _register(client, "device_onboarding_0003")
    user_id = data["user_id"]

    res = client.put("/api/v3/profile", headers=headers, json=_complete_payload())
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["setup"]["completed"] is True
    completed_at = body["setup"]["completed_at"]
    assert completed_at
    assert body["setup"]["missing"] == []

    profile = body["profile"]
    assert profile["nickname"] == "小白"
    assert profile["native_language"] == "zh"
    assert profile["target_languages"] == ["en", "de"]
    assert profile["birthday"] == "1998-05-01"
    assert profile["profile_setup_completed_at"] == completed_at

    fact = profile["fact_sheet"]
    assert fact["profile"]["city"] == "上海"
    assert fact["profile"]["major_or_job"] == "设计师"
    assert fact["profile"]["gender"] == "female"
    assert fact["profile"]["level"] == "能聊几句"
    assert fact["language_profile"]["levels"] == {"en": "能聊几句", "de": "刚开始"}
    # 兴趣标签统一为稳定 ID（咖啡→coffee、旅行→travel）
    assert fact["interests"] == ["coffee", "travel"]
    assert fact["communication_preference"] == "improve"

    # 真正落库（直接读 DB，而不是只看响应）
    stored = db.get_profile(user_id=user_id)
    assert stored["profile_setup_completed_at"] == completed_at
    assert stored["nickname"] == "小白"
    assert stored["target_languages"] == ["en", "de"]
    assert stored["fact_sheet"]["communication_preference"] == "improve"


# --------------------------------------------------------------------------- #
# 4) 刷新 / 换设备 / 重复 complete
# --------------------------------------------------------------------------- #
def test_refresh_and_repeat_complete_keep_first_completed_at(api):
    client, db, _service = api
    data, headers = _register(client, "device_onboarding_0004")
    user_id = data["user_id"]

    # 三步走：昵称 → 语言 → 最后一步完成
    assert client.put("/api/v3/profile", headers=headers, json={"nickname": "小白"}).status_code == 200
    assert (
        client.put(
            "/api/v3/profile",
            headers=headers,
            json={"native_language": "zh", "target_languages": ["en", "de"]},
        ).status_code
        == 200
    )
    final = client.put(
        "/api/v3/profile",
        headers=headers,
        json={"interests": ["咖啡"], "communication_preference": "natural", "complete_setup": True},
    )
    assert final.status_code == 200, final.text
    first_at = final.json()["setup"]["completed_at"]
    assert first_at

    # 模拟刷新
    assert _get_state(client, headers)["setup"]["completed_at"] == first_at

    # 模拟换设备：同一 device_key 重新注册 → 同一个 user_id，新 token 也能读到完成状态
    again, headers2 = _register(client, "device_onboarding_0004")
    assert again["user_id"] == user_id
    assert again["created"] is False
    state2 = _get_state(client, headers2)
    assert state2["setup"]["completed"] is True
    assert state2["setup"]["completed_at"] == first_at
    assert state2["profile"]["nickname"] == "小白"

    # 重复 complete 不改变首次完成时间（先把时间改成可辨识的旧值，确保断言不被同秒掩盖）
    with db._lock, db.conn:  # noqa: SLF001 - 测试直接改库
        db.conn.execute(
            "UPDATE user_profiles SET profile_setup_completed_at=?"
            " WHERE user_id=?",
            ("2020-01-01T00:00:00+00:00", user_id),
        )
    # 注意：重新注册会签发新 token（旧 token 失效），后续请求用 headers2
    repeated = client.put("/api/v3/profile", headers=headers2, json={"complete_setup": True})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["setup"]["completed"] is True
    assert repeated.json()["setup"]["completed_at"] == "2020-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# 5) 缺必填字段 → 400 且不标记完成
# --------------------------------------------------------------------------- #
def test_complete_without_required_fields_is_rejected(api):
    client, db, _service = api
    data, headers = _register(client, "device_onboarding_0005")
    user_id = data["user_id"]

    # 全新用户直接 complete（默认昵称不算已填）
    res = client.put("/api/v3/profile", headers=headers, json={"complete_setup": True})
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "profile_incomplete"
    assert detail["missing"] == ["nickname"]

    # 构造「资料缺失」：昵称/母语/目标语言全空（模拟脏数据/只填了可选项）
    db.update_profile(
        user_id=user_id, nickname="", native_language="", target_languages=[]
    )
    res = client.put(
        "/api/v3/profile",
        headers=headers,
        json={"city": "上海", "complete_setup": True},
    )
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "profile_incomplete"
    assert detail["missing"] == ["nickname", "native_language", "target_languages"]

    # 失败不能把资料/完成状态写成已完成
    state = _get_state(client, headers)
    assert state["setup"]["completed"] is False
    assert state["setup"]["completed_at"] == ""
    assert db.get_profile(user_id=user_id)["profile_setup_completed_at"] == ""


# --------------------------------------------------------------------------- #
# 6) 多用户隔离
# --------------------------------------------------------------------------- #
def test_profile_setup_is_isolated_between_users(api):
    client, db, _service = api
    alice, alice_headers = _register(client, "device_alice_0006")
    bob, bob_headers = _register(client, "device_bob_0006000")

    alice_res = client.put(
        "/api/v3/profile", headers=alice_headers, json=_complete_payload(nickname="Alice")
    )
    assert alice_res.status_code == 200, alice_res.text
    assert alice_res.json()["setup"]["completed"] is True

    bob_state = _get_state(client, bob_headers)
    assert bob_state["setup"]["completed"] is False
    assert bob_state["setup"]["completed_at"] == ""
    assert bob_state["profile"]["nickname"] != "Alice"

    # Bob 保存自己的资料：既不会带上 Alice 的资料，也不会改 Alice 的完成状态
    bob_res = client.put(
        "/api/v3/profile", headers=bob_headers, json={"nickname": "Bob"}
    )
    assert bob_res.status_code == 200, bob_res.text
    assert bob_res.json()["profile"]["nickname"] == "Bob"
    assert bob_res.json()["setup"]["completed"] is False
    assert "communication_preference" not in bob_res.json()["profile"]["fact_sheet"]

    alice_state = _get_state(client, alice_headers)
    assert alice_state["profile"]["nickname"] == "Alice"
    assert alice_state["setup"]["completed"] is True
    assert alice_state["profile"]["fact_sheet"]["communication_preference"] == "improve"
    assert alice_state["profile"]["fact_sheet"]["profile"]["city"] == "上海"

    assert db.get_profile(user_id=bob["user_id"])["profile_setup_completed_at"] == ""
    assert db.get_profile(user_id=alice["user_id"])["profile_setup_completed_at"] != ""


# --------------------------------------------------------------------------- #
# 7a) 老库迁移：已有资料行 → 已完成，且数据保留
# --------------------------------------------------------------------------- #
def test_legacy_profile_rows_are_migrated_as_completed(tmp_path):
    path = tmp_path / "legacy_profiles.db"
    _create_legacy_db(
        path,
        user_profiles=(("user_old", "OldUser", "2024-02-03T04:05:06+00:00"),),
        v3_users=("u_oldlegacy000000000000000000000000",),
    )
    db = DB(str(path))
    db.init()
    try:
        # 列已幂等补齐
        assert "profile_setup_completed_at" in _columns(db, "user_profile")
        assert "profile_setup_completed_at" in _columns(db, "user_profiles")

        # 本地老用户：沿用 updated_at 作为完成时间，原有资料不丢
        local = db.get_profile()
        assert local["nickname"] == "老本地"
        assert local["profile_setup_completed_at"] == "2024-05-06T07:08:09+00:00"

        # 多用户老资料行：已完成
        legacy = db.get_profile(user_id="user_old")
        assert legacy["nickname"] == "OldUser"
        assert legacy["profile_setup_completed_at"] == "2024-02-03T04:05:06+00:00"

        # 迁移前已注册的 v3 老账号：补 profile 行并标记完成
        old_account = db.get_profile(user_id="u_oldlegacy000000000000000000000000")
        assert old_account["profile_setup_completed_at"] != ""
        assert old_account["nickname"] == "Traveler"

        # 迁移后第一次出现的新 user_id：仍未完成（不能把所有人当成老用户）
        assert db.get_profile(user_id="u_newaftermigration00000000000000000")[
            "profile_setup_completed_at"
        ] == ""
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 7b) 老 v3 账号已注册 + 迁移后再注册新账号 → 老 completed / 新 incomplete
# --------------------------------------------------------------------------- #
def test_legacy_v3_account_completed_but_new_registration_incomplete(tmp_path):
    path = tmp_path / "legacy_v3.db"
    old_user_id = "u_oldaccount0000000000000000000000"
    _create_legacy_db(path, v3_users=(old_user_id,))
    db = DB(str(path))
    db.init()
    try:
        ensure_schema(db, seed=False)
        assert db.get_profile(user_id=old_user_id)["profile_setup_completed_at"] != ""

        # 迁移完成后才注册的新账号：仍未完成
        new_user = register_user(db, "device_after_migration_0001")
        assert new_user["created"] is True
        assert db.get_profile(user_id=new_user["user_id"])["profile_setup_completed_at"] == ""

        service = V3Service(db, SimpleNamespace(llm=None), str(tmp_path))
        state = service.profile_state(new_user["user_id"])
        assert state["setup"]["completed"] is False
        assert state["setup"]["completed_at"] == ""
        assert state["setup"]["missing"] == ["nickname"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 8) 迁移 / schema 幂等：重复初始化不报错、不改完成时间、不误标新用户
# --------------------------------------------------------------------------- #
def test_migration_and_schema_initialization_are_idempotent(tmp_path):
    path = tmp_path / "legacy_idempotent.db"
    old_user_id = "u_oldaccount0000000000000000000000"
    _create_legacy_db(
        path,
        user_profiles=(("user_old", "OldUser", "2024-02-03T04:05:06+00:00"),),
        v3_users=(old_user_id,),
    )
    db = DB(str(path))
    db.init()
    try:
        local_at = db.get_profile()["profile_setup_completed_at"]
        old_at = db.get_profile(user_id="user_old")["profile_setup_completed_at"]
        v3_at = db.get_profile(user_id=old_user_id)["profile_setup_completed_at"]

        # 迁移后注册的新用户只保存了部分资料（有行但未完成）
        new_user = register_user(db, "device_idempotent_0001")
        db.update_profile(user_id=new_user["user_id"], nickname="Newbie")
        assert db.get_profile(user_id=new_user["user_id"])["profile_setup_completed_at"] == ""

        # schema 反复初始化不报错
        ensure_schema(db, seed=False)
        ensure_schema(db, seed=False)
    finally:
        db.close()

    # 重新打开（模拟服务重启）：DB 初始化不报错，且不会重跑一次性回填
    db2 = DB(str(path))
    db2.init()
    try:
        assert db2.get_profile()["profile_setup_completed_at"] == local_at
        assert db2.get_profile(user_id="user_old")["profile_setup_completed_at"] == old_at
        assert db2.get_profile(user_id=old_user_id)["profile_setup_completed_at"] == v3_at
        # 迁移后新用户没有被误标完成（完成时间仍为空）
        assert db2.get_profile(user_id=new_user["user_id"])["profile_setup_completed_at"] == ""
        ensure_schema(db2, seed=False)
    finally:
        db2.close()


# --------------------------------------------------------------------------- #
# 9) communication_preference 校验 + bootstrap 返回状态
# --------------------------------------------------------------------------- #
def test_communication_preference_validation_and_bootstrap(api):
    client, _db, _service = api
    _data, headers = _register(client, "device_preference_0009")

    # 非法值忽略（不写入、不报错、不影响其它字段）
    res = client.put(
        "/api/v3/profile",
        headers=headers,
        json={"nickname": "小白", "communication_preference": "shout-at-me"},
    )
    assert res.status_code == 200, res.text
    assert "communication_preference" not in res.json()["profile"]["fact_sheet"]

    # 合法值写入 fact_sheet
    res = client.put(
        "/api/v3/profile",
        headers=headers,
        json={"communication_preference": "occasional"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["profile"]["fact_sheet"]["communication_preference"] == "occasional"

    # bootstrap（v3 client）自然带出 onboarding 状态与沟通偏好
    boot = client.get("/api/v3/client/bootstrap", headers=headers)
    assert boot.status_code == 200, boot.text
    boot_profile = boot.json()["profile"]
    assert boot_profile["profile_setup_completed_at"] == ""
    assert boot_profile["fact_sheet"]["communication_preference"] == "occasional"

    # 完成建档后 bootstrap 也能读到完成状态
    res = client.put(
        "/api/v3/profile",
        headers=headers,
        json=_complete_payload(nickname="小白", communication_preference="natural"),
    )
    assert res.status_code == 200, res.text
    completed_at = res.json()["setup"]["completed_at"]
    boot2 = client.get("/api/v3/client/bootstrap", headers=headers)
    assert boot2.status_code == 200, boot2.text
    assert boot2.json()["profile"]["profile_setup_completed_at"] == completed_at
    assert boot2.json()["profile"]["fact_sheet"]["communication_preference"] == "natural"


# --------------------------------------------------------------------------- #
# 10) 老 PUT 调用（不传新参数）行为不变 / complete_setup=false 可重置
# --------------------------------------------------------------------------- #
def test_legacy_put_payload_still_works(api):
    client, _db, _service = api
    _data, headers = _register(client, "device_legacyput_0010")

    res = client.put(
        "/api/v3/profile",
        headers=headers,
        json={
            "nickname": "旧字段",
            "target_languages": ["en"],
            "city": "上海",
            "interests": ["咖啡"],
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # 老客户端只读 profile：字段与清洗行为完全保持
    assert body["profile"]["nickname"] == "旧字段"
    assert body["profile"]["target_languages"] == ["en"]
    assert body["profile"]["fact_sheet"]["profile"]["city"] == "上海"
    # 兴趣标签统一为稳定 ID
    assert body["profile"]["fact_sheet"]["interests"] == ["coffee"]
    # 没传 complete_setup → 不标记完成
    assert body["setup"]["completed"] is False
    assert body["setup"]["completed_at"] == ""

    # 非法性别值仍被清空（原有行为）
    res2 = client.put("/api/v3/profile", headers=headers, json={"gender": "xyz"})
    assert res2.json()["profile"]["fact_sheet"]["profile"]["gender"] == ""


def test_complete_setup_false_can_reset_state(api):
    client, _db, _service = api
    _data, headers = _register(client, "device_reset_0011")

    done = client.put("/api/v3/profile", headers=headers, json=_complete_payload())
    assert done.status_code == 200, done.text
    assert done.json()["setup"]["completed"] is True

    reset = client.put("/api/v3/profile", headers=headers, json={"complete_setup": False})
    assert reset.status_code == 200, reset.text
    assert reset.json()["setup"]["completed"] is False
    assert reset.json()["setup"]["completed_at"] == ""
    # 重置只清完成状态，不清资料
    assert reset.json()["profile"]["nickname"] == "小白"
    assert reset.json()["profile"]["target_languages"] == ["en", "de"]
