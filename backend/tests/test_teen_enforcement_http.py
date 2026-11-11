"""青少年模式（分龄门禁）接入 HTTP 聊天与信息流 —— 集成测试。

覆盖 task-4 的验收点
--------------------
1. 成年账号（``age_group=adult``）在 ``SE_AGE_GATE=1`` 下仍可正常访问
   ``/api/v3/feed`` 与 ``/api/v2/chat/{friend_id}/text``（200）；
2. 未知年龄账号在内容/聊天入口统一 403，``detail == "minor:age_unverified"``；
3. teen + 已监护：白天可聊（200）；当日使用量超过 60 分钟 → 403 ``minor:daily_limit``；
   本地时间 22:30（宵禁内）→ 403 ``minor:curfew``；
4. teen 未完成监护绑定 → 403 ``minor:guardian_required``；
5. teen 聊一次后 ``v3_minor_usage`` 有记录（``last_turn_at`` 非空）；
6. 无身份头（``local``）仍 200，不误伤单用户联调。

实现方式：进程内自建 FastAPI（v2 + v3 router）+ 临时 SQLite，账号走
``accounts.register_email`` + ``X-Session-Token`` 会话头；时间用
``monkeypatch.setattr(teen, "_now", ...)`` 固定为 UTC，故本地（Asia/Shanghai）
22:30 = UTC 14:30。
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timezone
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
from app.v2.router import router as v2_router  # noqa: E402
from app.v3 import accounts, teen  # noqa: E402
from app.v3.identity import reset_current_user, set_current_user  # noqa: E402
from app.v3.router import router as v3_router  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.service import V3Service  # noqa: E402

#: 本地（Asia/Shanghai）白天 12:00 = UTC 04:00
DAYTIME_UTC = datetime(2026, 1, 5, 4, 0, 0, tzinfo=timezone.utc)
#: 本地（Asia/Shanghai）22:30 = UTC 14:30（内测默认宵禁 22:00–06:00 内）
CURFEW_UTC = datetime(2026, 1, 5, 14, 30, 0, tzinfo=timezone.utc)
#: 与上面两个时刻对应的本地自然日（v3_minor_usage.day）
LOCAL_DAY = "2026-01-05"


# --------------------------------------------------------------------------- #
# 测试替身 / 装配
# --------------------------------------------------------------------------- #
class FakeChat:
    """最小聊天替身：只实现 ``/text`` 需要的 ``handle_text``。

    真实聊天链路（LLM/TTS/记忆）由 ``test_v2_api.py`` 覆盖；本文件只验证门禁接线，
    因此这里返回结构相同的固定回合，避免测试依赖 LLM/TTS。
    """

    def __init__(self, db: DB) -> None:
        self.db = db

    async def handle_text(
        self, friend_id: str, text: str, context: str | None = None, client_message_id: str | None = None
    ) -> dict:
        friend = self.db.get_friend(friend_id)
        if friend is None:
            raise KeyError(friend_id)
        user_message = self.db.add_message(friend_id, "user", text, msg_type="text")
        friend_message = self.db.add_message(
            friend_id, "friend", f"Sure — you said {text!r}."
        )
        return {
            "user_message": user_message,
            "friend_messages": [friend_message],
            "suggestion_pending": False,
            "closeness_level": friend.get("closeness_level", 1),
            "total_turns": friend.get("total_turns", 0),
            "reply_language": friend.get("reply_language") or "en",
            "stt": {"backend": "text", "latency_ms": 0.0},
        }


def _make_app(tmp_path: Path):
    db = DB(str(tmp_path / "teen_http.db"))
    db.init()
    # 只要空表结构：门禁判定不需要种子人物/动态
    ensure_schema(db, seed=False)
    accounts.ensure_accounts_schema(db)
    teen.ensure_teen_schema(db)

    service = V3Service(
        db, SimpleNamespace(llm=SimpleNamespace(uses_mock=True)), str(tmp_path)
    )
    service.rng = random.Random(7)

    services = SimpleNamespace(
        db=db,
        chat=FakeChat(db),
        onboarding_pending=False,
        followup_inflight=set(),
        spawn_bg=lambda coro: (coro.close(), None)[1],
    )

    app = FastAPI()
    app.state.v2 = services
    app.state.v3 = service
    app.include_router(v2_router)
    app.include_router(v3_router)
    return app, db, services


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_AGE_GATE", "1")
    monkeypatch.setenv("SE_INVITE_REQUIRED", "0")  # 测试不需要邀请码
    monkeypatch.delenv("SE_REQUIRE_AUTH", raising=False)
    app, db, services = _make_app(tmp_path)
    with TestClient(app) as client:
        yield client, db, services
    db.close()


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _register(db: DB, email: str) -> tuple[str, dict[str, str]]:
    """邮箱注册并直接拿会话头（X-Session-Token）。"""
    data = accounts.register_email(db, email, "pa55word!")
    return data["user_id"], {"X-Session-Token": str(data["session_token"])}


def _make_friend(db: DB, user_id: str, *, name: str = "Mia") -> dict:
    """给指定用户建一个好友（owner=user_id），不依赖 spawner/LLM。"""
    token = set_current_user(user_id)
    try:
        return db.create_friend({"name": name, "reply_language": "en", "status": "active"})
    finally:
        reset_current_user(token)


def _freeze_clock(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(teen, "_now", lambda: moment)


def _bump_usage(db: DB, user_id: str, seconds: float, day: str = LOCAL_DAY) -> None:
    """直接写当日使用量（模拟已经聊了 N 秒）。"""
    with db._lock, db.conn:  # noqa: SLF001 - 测试直接构造场景
        db.conn.execute(
            "INSERT INTO v3_minor_usage (user_id, day, seconds, continuous_started_at,"
            " last_turn_at) VALUES (?,?,?, '', '')"
            " ON CONFLICT(user_id, day) DO UPDATE SET seconds=excluded.seconds",
            (user_id, day, float(seconds)),
        )


def _usage_row(db: DB, user_id: str) -> dict | None:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT * FROM v3_minor_usage WHERE user_id=? ORDER BY day DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _detail(response) -> str:
    return str((response.json() or {}).get("detail") or "")


V3_FEED_ENDPOINTS: tuple[tuple[str, str, dict], ...] = (
    ("get", "/api/v3/feed", {}),
    ("get", "/api/v3/feed/summary", {}),
    ("get", "/api/v3/discover/next", {}),
    ("get", "/api/v3/personas", {}),
    ("get", "/api/v3/personas/p_test", {}),
    ("get", "/api/v3/personas/p_test/posts", {}),
    ("post", "/api/v3/posts", {"json": {"content": "hello", "visibility": "private"}}),
    ("get", "/api/v3/events", {}),
    ("get", "/api/v3/friendships", {}),
)


# --------------------------------------------------------------------------- #
# 1) 成年账号不受门禁影响
# --------------------------------------------------------------------------- #
def test_adult_account_can_use_feed_and_chat(api):
    client, db, _ = api
    adult_id, headers = _register(db, "adult@example.com")
    accounts.set_age_verification(db, adult_id, age_group="adult")
    friend = _make_friend(db, adult_id)

    feed = client.get("/api/v3/feed", headers=headers)
    assert feed.status_code == 200, feed.text
    assert "items" in feed.json()

    chat = client.post(
        f"/api/v2/chat/{friend['id']}/text",
        json={"text": "hi there"},
        headers=headers,
    )
    assert chat.status_code == 200, chat.text
    assert chat.json()["friend_messages"]


# --------------------------------------------------------------------------- #
# 2) 未知年龄：所有内容/聊天入口 403 age_unverified，资料接口不拦
# --------------------------------------------------------------------------- #
def test_unknown_age_is_blocked_everywhere(api):
    client, db, _ = api
    unknown_id, headers = _register(db, "unknown@example.com")
    friend = _make_friend(db, unknown_id)

    for method, path, kwargs in V3_FEED_ENDPOINTS:
        response = getattr(client, method)(path, headers=headers, **kwargs)
        assert response.status_code == 403, f"{method} {path}: {response.text}"
        assert _detail(response) == "minor:age_unverified", f"{method} {path}"

    chat_endpoints = (
        ("post", f"/api/v2/chat/{friend['id']}/text", {"json": {"text": "hello"}}),
        (
            "post",
            f"/api/v2/chat/{friend['id']}/voice",
            {"files": {"audio": ("voice.webm", b"fake-audio")}},
        ),
        (
            "post",
            f"/api/v2/chat/{friend['id']}/image",
            {"files": {"image": ("photo.jpg", b"fake-image")}},
        ),
        (
            "post",
            f"/api/v2/chat/{friend['id']}/call/turn",
            {"files": {"audio": ("call.webm", b"fake-audio")}},
        ),
        (
            "post",
            f"/api/v2/chat/{friend['id']}/call/turn-text",
            {"json": {"text": "hello", "duration_ms": 0}},
        ),
        (
            "post",
            f"/api/v2/chat/{friend['id']}/call/summary",
            {"json": {"duration_s": 3, "turns": 1}},
        ),
    )
    for method, path, kwargs in chat_endpoints:
        response = getattr(client, method)(path, headers=headers, **kwargs)
        assert response.status_code == 403, f"{method} {path}: {response.text}"
        assert _detail(response) == "minor:age_unverified", f"{method} {path}"

    # 资料类接口不加门禁：未知年龄仍可读到自己的资料/身份
    assert client.get("/api/v3/me", headers=headers).status_code == 200
    assert client.get("/api/v3/profile", headers=headers).status_code == 200


# --------------------------------------------------------------------------- #
# 3) teen + 已监护：白天可用、超时/宵禁被拒
# --------------------------------------------------------------------------- #
def test_guardian_bound_teen_daily_limit_and_curfew(api, monkeypatch):
    client, db, _ = api
    adult_id, _adult_headers = _register(db, "guardian@example.com")
    accounts.set_age_verification(db, adult_id, age_group="adult")
    teen_id, headers = _register(db, "teen@example.com")
    teen.bind_guardian(db, teen_id, guardian_user_id=adult_id, method="ops")
    friend = _make_friend(db, teen_id)

    _freeze_clock(monkeypatch, DAYTIME_UTC)
    allowed = client.post(
        f"/api/v2/chat/{friend['id']}/text",
        json={"text": "good afternoon"},
        headers=headers,
    )
    assert allowed.status_code == 200, allowed.text

    # 超过每日 60 分钟 → daily_limit
    _bump_usage(db, teen_id, 60 * 60)
    limited = client.post(
        f"/api/v2/chat/{friend['id']}/text",
        json={"text": "one more"},
        headers=headers,
    )
    assert limited.status_code == 403, limited.text
    assert _detail(limited) == "minor:daily_limit"

    # 本地 22:30（UTC 14:30）→ curfew（宵禁优先于时长判定）
    _freeze_clock(monkeypatch, CURFEW_UTC)
    curfew = client.post(
        f"/api/v2/chat/{friend['id']}/text",
        json={"text": "still here"},
        headers=headers,
    )
    assert curfew.status_code == 403, curfew.text
    assert _detail(curfew) == "minor:curfew"


# --------------------------------------------------------------------------- #
# 4) teen 未监护：guardian_required
# --------------------------------------------------------------------------- #
def test_unguarded_teen_requires_guardian(api):
    client, db, _ = api
    teen_id, headers = _register(db, "teen-noguardian@example.com")
    # 核验为 teen，但没有监护绑定（guardian_user_id 为空）
    accounts.set_age_verification(db, teen_id, age_group="teen")
    friend = _make_friend(db, teen_id)

    chat = client.post(
        f"/api/v2/chat/{friend['id']}/text",
        json={"text": "hello"},
        headers=headers,
    )
    assert chat.status_code == 403, chat.text
    assert _detail(chat) == "minor:guardian_required"

    feed = client.get("/api/v3/feed", headers=headers)
    assert feed.status_code == 403, feed.text
    assert _detail(feed) == "minor:guardian_required"


# --------------------------------------------------------------------------- #
# 5) teen 聊一次后 v3_minor_usage 有记录
# --------------------------------------------------------------------------- #
def test_teen_activity_is_recorded(api, monkeypatch):
    client, db, _ = api
    adult_id, _ = _register(db, "guardian2@example.com")
    accounts.set_age_verification(db, adult_id, age_group="adult")
    teen_id, headers = _register(db, "teen-usage@example.com")
    teen.bind_guardian(db, teen_id, guardian_user_id=adult_id, method="ops")
    friend = _make_friend(db, teen_id)
    _freeze_clock(monkeypatch, DAYTIME_UTC)

    response = client.post(
        f"/api/v2/chat/{friend['id']}/text",
        json={"text": "record my usage"},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    row = _usage_row(db, teen_id)
    assert row is not None, "通过门禁后应写入 v3_minor_usage"
    assert row["day"] == LOCAL_DAY
    assert str(row["last_turn_at"]), "last_turn_at 应记录本次活动时间"
    assert float(row["seconds"] or 0) >= 0


# --------------------------------------------------------------------------- #
# 6) local（无身份头）不受门禁影响
# --------------------------------------------------------------------------- #
def test_local_identity_is_not_gated(api):
    client, db, _ = api
    friend = _make_friend(db, "local", name="Local Friend")

    feed = client.get("/api/v3/feed")
    assert feed.status_code == 200, feed.text

    chat = client.post(
        f"/api/v2/chat/{friend['id']}/text",
        json={"text": "local dev chat"},
    )
    assert chat.status_code == 200, chat.text
    # local 不落未成年使用量表
    assert _usage_row(db, "local") is None
