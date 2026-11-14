"""商业合作（广告）接进聊天 —— 集成测试（离线：mock LLM + Fake TTS，无网络）。

覆盖 task-8 的验收点
-------------------
1. 成年 + 已开启 + 已上架活动（品牌/双渠道/启用/审核通过 chat 素材/已确认人物分配）
   + 有需求文本 → 正常好友回复 + 独立 ``ad_message``；消息表存在 ``msg_type="ad"``
   行、``db.recent_messages``（prompt/记忆）不含广告、``ads.list_deliveries`` 为 delivered；
2. 泛聊 / 否定 / 不确定 / 心理困扰 → 无 ``ad_message``；
3. 未成年 / 未知年龄 / 未开启偏好 → 无 ``ad_message``；
4. 24 小时内第二次 → 无 ``ad_message``（频控）；
5. 文案只由「审核模板 + 商品事实」拼成，不含模型自由发挥；
6. 消息序列化：``_message_public`` 把 ``msg_type='ad'`` 的 context 解析成 ``ad`` 字段，
   历史消息列表也能看到广告。

实现方式：进程内直接构造 ``DB`` + ``ChatService``（mock LLM / Fake TTS），
用 ``app.v3.identity.set_current_user`` 绑定当前用户后调用 ``handle_text``，
后台旁路任务（润色/记忆/表达本）用 no-op ``spawn_bg`` 丢弃。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.audio_store import AudioStore  # noqa: E402
from app.v2.chat_service import ChatService  # noqa: E402
from app.v2.db import DB  # noqa: E402
from app.v2.media_store import MediaStore  # noqa: E402
from app.v2.memory import MemoryManager  # noqa: E402
from app.v2.router import _message_public, _turn_public  # noqa: E402
from app.v3 import accounts, ads, client_api, privacy  # noqa: E402
from app.v3.identity import reset_current_user, set_current_user  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402

pytestmark = pytest.mark.asyncio

#: 审核模板（chat 渠道）与商品事实：文案必须完全由这两部分组成
CHAT_TEMPLATE = "我最近在用 {facts}，你问的正好。"
FACT = "胶囊咖啡机 2 分钟出杯"
EXPECTED_COPY = f"我最近在用 {FACT}，你问的正好。"
LABEL = "广告 · 品牌合作"
DEMAND_TEXT = "I'm thinking about buying a coffee machine — any recommendation?"


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeLLM:
    """规则/模板 LLM：``uses_mock=True`` 时 ChatService 走 deterministic 人设回复。"""

    uses_mock = True

    def health(self) -> dict:
        return {"configured": False, "model": "mock", "mock": True}

    async def stream_chat(self, messages, **kwargs):  # pragma: no cover - mock 路径不走这里
        yield "Oh nice! "

    async def complete_json(self, *args, **kwargs):  # pragma: no cover
        return "{}"


class FakeVoice:
    """Fake TTS：只返回一个文件名，不真的合成音频（聊天链路只看 basename）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def synth_to_wav(self, text, voice_id, *, rate="+0%", pitch="+0Hz", out_dir):
        self.calls.append((text, voice_id))
        name = f"fake_{len(self.calls):04d}.wav"
        return str(Path(out_dir) / name), 800


class FakeEngines:
    def __init__(self) -> None:
        self.llm = FakeLLM()
        self.settings = SimpleNamespace(data_path="")


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_AGE_GATE", "1")
    monkeypatch.setenv("SE_INVITE_REQUIRED", "0")
    monkeypatch.setenv("SE_TTS_FISH", "0")  # 强制走 Edge/Fake，绝不打真实 TTS API
    db = DB(str(tmp_path / "ads_chat.db"))
    db.init()
    ensure_schema(db, seed=True)
    accounts.ensure_accounts_schema(db)
    client_api.ensure_client_schema(db)
    privacy.ensure_privacy_schema(db)
    ads.ensure_ads_schema(db)

    engines = FakeEngines()
    audio = AudioStore(str(tmp_path / "data"))
    media = MediaStore(str(tmp_path / "data"))
    memory = MemoryManager(db, engines.llm)
    chat = ChatService(
        engines, db, memory, FakeVoice(), SimpleNamespace(), audio, media, memory_idle_s=0
    )
    # 后台旁路（润色/记忆/表达本）与广告无关；直接关闭协程，避免测试里出现游离任务
    chat.spawn_bg = lambda coro: (coro.close(), None)[1]  # type: ignore[assignment]
    chat._schedule_memory = lambda _: None  # memory is an unrelated side effect in this fixture
    yield SimpleNamespace(db=db, chat=chat, data_dir=tmp_path)
    db.close()


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _set_preferences(db: DB, user_id: str, value: dict) -> None:
    with db._lock, db.conn:  # noqa: SLF001 - 与 DB 层共用连接（见 test_ads_core.py）
        db.conn.execute(
            "INSERT INTO client_preferences (user_id, value) VALUES (?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET value=excluded.value",
            (user_id, json.dumps(value)),
        )


def _register(db: DB, email: str) -> str:
    return accounts.register_email(db, email, "pa55word!", invite_code="")["user_id"]


def _adult(db: DB, email: str, *, opt_in: bool = True) -> str:
    uid = _register(db, email)
    accounts.set_age_verification(db, uid, age_group="adult")
    _set_preferences(
        db,
        uid,
        {"messages": True, "interactions": True, "sound": True, "theme": "system", "ads": opt_in},
    )
    return uid


def _persona_id(db: DB) -> str:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT id FROM personas LIMIT 1").fetchone()
    return str(row["id"])


def _friend(db: DB, user_id: str, persona_id: str, name: str = "Mia") -> dict:
    token = set_current_user(user_id)
    try:
        return db.create_friend(
            {"name": name, "persona_id": persona_id, "reply_language": "en", "status": "active"}
        )
    finally:
        reset_current_user(token)


def _campaign(db: DB, *, facts=(FACT,)) -> dict:
    """已上架活动：品牌 + 双渠道 + 审核通过的 chat/feed 素材（参考 test_ads_core.py）。"""
    brand = ads.create_brand(db, name="测试咖啡", industry="food", qualification="资质编号T-1")
    campaign = ads.create_campaign(
        db,
        brand_id=brand["id"],
        title="咖啡机合作",
        purpose="新品推荐",
        required_tags=["coffee"],
        facts=list(facts),
        landing_url="https://example.test/ad",
        channels=["chat", "feed"],
        created_by="test",
    )
    ads.add_creative(
        db,
        campaign_id=campaign["id"],
        channel="chat",
        title="聊天卡片",
        body_template=CHAT_TEMPLATE,
        facts=list(facts),
    )
    ads.add_creative(
        db,
        campaign_id=campaign["id"],
        channel="feed",
        title="动态卡片",
        body_template="今天也在用 {facts}。",
        facts=list(facts),
    )
    for creative in ads.list_creatives(db, campaign["id"]):
        ads.review_creative(db, creative["id"], approved=True, reviewer="test")
    ads.set_campaign_status(db, campaign["id"], "reviewed")
    ads.set_campaign_status(db, campaign["id"], "enabled")
    return campaign


async def _turn(env, user_id: str, friend_id: str, text: str) -> dict:
    """在指定用户上下文里跑一轮真实 ``_handle_user_text``。"""
    token = set_current_user(user_id)
    try:
        return await env.chat.handle_text(friend_id, text)
    finally:
        reset_current_user(token)


def _ad_rows(db: DB, friend_id: str) -> list[dict]:
    return [row for row in db.list_messages(friend_id, limit=100) if row.get("msg_type") == "ad"]


# --------------------------------------------------------------------------- #
# 1) 命中：正常回复 + 独立广告消息 + delivered + 不进 prompt/记忆
# --------------------------------------------------------------------------- #
async def test_chat_ad_appended_after_normal_reply(env):
    db = env.db
    persona = _persona_id(db)
    uid = _adult(db, "happy@example.com")
    friend = _friend(db, uid, persona)
    campaign = _campaign(db)
    ads.confirm_assignment(db, campaign["id"], persona, confirmed=True)

    result = await _turn(env, uid, friend["id"], DEMAND_TEXT)

    # 正常回复照旧：friend_messages 只有聊天回复，不含广告
    assert result["friend_messages"], "正常好友回复不能丢"
    friend_texts = [m.get("transcript") or "" for m in result["friend_messages"]]
    assert all(EXPECTED_COPY not in text for text in friend_texts)

    # 广告是独立消息
    ad = result["ad_message"]
    assert ad is not None, "成年+已开启+有需求+已上架活动应该投放"
    for key in ("id", "copy", "label", "delivery_id", "brand_name", "landing_url"):
        assert ad.get(key), f"ad_message 缺字段 {key}"
    assert ad["label"] == LABEL
    assert ad["brand_name"] == "测试咖啡"
    assert ad["landing_url"] == "https://example.test/ad"
    # 文案只来自审核模板 + 商品事实（没有任何模型自由发挥的余地）
    assert ad["copy"] == EXPECTED_COPY
    assert FACT in ad["copy"]

    # 消息表：恰好一条 ad 行，context 带投放信息
    rows = _ad_rows(db, friend["id"])
    assert len(rows) == 1
    ad_row = rows[0]
    assert ad_row["id"] == ad["id"]
    assert ad_row["sender"] == "friend"
    context = json.loads(ad_row["context"])
    assert context["delivery_id"] == ad["delivery_id"]
    assert context["campaign_id"] == campaign["id"]
    assert context["brand_name"] == "测试咖啡"
    assert context["label"] == LABEL
    assert context["facts"] == [FACT]
    assert context["persona_id"] == persona

    # 广告不进 prompt/记忆（recent_messages 过滤）→ 也不进人物公共经历
    recent = db.recent_messages(friend["id"], 10)
    assert ad["id"] not in {row["id"] for row in recent}
    assert all(row.get("msg_type") != "ad" for row in recent)

    # 发送成功 → 投放状态 delivered，且回写到 message_id
    deliveries = ads.list_deliveries(db, user_id=uid)
    assert len(deliveries) == 1
    assert deliveries[0]["id"] == ad["delivery_id"]
    assert deliveries[0]["status"] == "delivered"
    assert deliveries[0]["channel"] == "chat"
    assert json.loads(deliveries[0]["context"])["message_id"] == ad["id"]


# --------------------------------------------------------------------------- #
# 2) 泛聊 / 否定 / 不确定 / 心理困扰 → 不投
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "hi",
        "算了不用了，我再想想",
        "不太确定，再说吧",
        "最近很焦虑，睡不着",
        "今天天气不错",
    ],
)
async def test_no_ad_without_purchase_need(env, text):
    db = env.db
    persona = _persona_id(db)
    uid = _adult(db, "negative@example.com")
    friend = _friend(db, uid, persona)
    campaign = _campaign(db)
    ads.confirm_assignment(db, campaign["id"], persona, confirmed=True)

    result = await _turn(env, uid, friend["id"], text)

    assert result["friend_messages"], "正常回复必须照常"
    assert result["ad_message"] is None
    assert _ad_rows(db, friend["id"]) == []
    assert ads.list_deliveries(db, user_id=uid) == []


# --------------------------------------------------------------------------- #
# 3) 未成年 / 未知年龄 / 未开启偏好 → 不投
# --------------------------------------------------------------------------- #
async def test_no_ad_for_minor_unknown_age_or_opt_out(env):
    db = env.db
    persona = _persona_id(db)
    campaign = _campaign(db)
    ads.confirm_assignment(db, campaign["id"], persona, confirmed=True)

    cases: list[tuple[str, str]] = []

    # 未成年（已核验 teen）：即使开了偏好也不投
    teen = _register(db, "teen@example.com")
    accounts.set_age_verification(db, teen, age_group="teen")
    _set_preferences(db, teen, {"ads": True})
    cases.append(("teen", teen))

    # 未知年龄（账号存在但未核验）
    unknown = _register(db, "unknown@example.com")
    _set_preferences(db, unknown, {"ads": True})
    cases.append(("unknown", unknown))

    # 成年但没主动开启合作推荐（默认 false）
    opted_out = _adult(db, "optout@example.com", opt_in=False)
    cases.append(("opt_out", opted_out))

    for name, uid in cases:
        friend = _friend(db, uid, persona, name=f"Mia-{name}")
        result = await _turn(env, uid, friend["id"], DEMAND_TEXT)
        assert result["friend_messages"], f"{name}: 正常回复必须照常"
        assert result["ad_message"] is None, f"{name}: 不应出现广告"
        assert _ad_rows(db, friend["id"]) == [], name
        assert ads.list_deliveries(db, user_id=uid) == [], name


# --------------------------------------------------------------------------- #
# 4) 24 小时内第二次 → 频控拦截
# --------------------------------------------------------------------------- #
async def test_second_ad_within_24h_is_blocked(env):
    db = env.db
    persona = _persona_id(db)
    uid = _adult(db, "twice@example.com")
    friend = _friend(db, uid, persona)
    campaign = _campaign(db)
    ads.confirm_assignment(db, campaign["id"], persona, confirmed=True)

    first = await _turn(env, uid, friend["id"], DEMAND_TEXT)
    assert first["ad_message"] is not None

    second = await _turn(env, uid, friend["id"], "coffee machine again — where should I buy it?")
    assert second["friend_messages"], "第二次的正常回复不能受影响"
    assert second["ad_message"] is None, "24 小时内聊天广告只投一次"
    assert len(_ad_rows(db, friend["id"])) == 1
    assert len(ads.list_deliveries(db, user_id=uid)) == 1


# --------------------------------------------------------------------------- #
# 5) 消息序列化：历史消息里的广告 Attr 解析成 ad 字段
# --------------------------------------------------------------------------- #
async def test_message_public_exposes_ad_fields(env):
    db = env.db
    with db._lock:  # noqa: SLF001 - 本地用户建好友，仅用于满足 messages 外键
        friend = db.create_friend({"name": "Ser"})
    friend_id = friend["id"]
    context = json.dumps(
        {
            "delivery_id": "ad_test123",
            "campaign_id": "cmp_test",
            "campaign_version": 2,
            "brand_name": "测试咖啡",
            "label": LABEL,
            "landing_url": "https://example.test/ad",
            "facts": [FACT, "  ", "可回收胶囊"],
            "persona_id": "p_test",
        },
        ensure_ascii=False,
    )
    row = db.add_message(friend_id, "friend", EXPECTED_COPY, msg_type="ad", context=context)

    public = _message_public(row)
    assert public is not None
    # 原始字段保留（历史消息列表仍能渲染气泡）
    assert public["transcript"] == EXPECTED_COPY
    assert public["msg_type"] == "ad"
    assert public["context"] == context
    # 结构化 ad 字段
    assert public["ad"]["delivery_id"] == "ad_test123"
    assert public["ad"]["campaign_id"] == "cmp_test"
    assert public["ad"]["campaign_version"] == 2
    assert public["ad"]["brand_name"] == "测试咖啡"
    assert public["ad"]["label"] == LABEL
    assert public["ad"]["landing_url"] == "https://example.test/ad"
    assert public["ad"]["facts"] == [FACT, "可回收胶囊"]
    assert public["ad"]["persona_id"] == "p_test"

    # 非广告消息不新增 ad 字段
    plain = _message_public(db.add_message(friend_id, "friend", "hola", msg_type="text"))
    assert plain is not None and "ad" not in plain

    # context 坏掉时不炸、也不伪造 ad 字段
    broken = _message_public(
        db.add_message(friend_id, "friend", "broken", msg_type="ad", context="{not json")
    )
    assert broken is not None and "ad" not in broken

    # 回合返回体：ad_message 与 friend_messages 分开序列化
    turn = _turn_public(
        {
            "user_message": db.add_message(friend_id, "user", "hi", msg_type="text"),
            "friend_messages": [db.add_message(friend_id, "friend", "hey", msg_type="text")],
            "ad_message": row,
        }
    )
    assert turn["ad_message"]["ad"]["delivery_id"] == "ad_test123"
    assert all(m["msg_type"] != "ad" for m in turn["friend_messages"])
