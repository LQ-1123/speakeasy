"""SpeakEasy 2.0 · PersonaSpawner 离线单测（mock LLM / 临时 SQLite，不联网）。

覆盖：
- mock LLM 下捏出字段完整的 friend 并真正入库；
- nationality / language / gender 过滤（含别名）与 strict 严格筛选；
- ``reply_language``（ja/ko/pt/es/de/it/fr/zh/en）与 ``language="en"`` 语义；
- ``db.used_archetype_ids()`` 去重、原型池耗尽后不崩；
- referral → ``pending_request`` + request_message；
- ``icebreaker`` 按 reply_language 生成（含日文/韩文确定性模板）；
- LLM 成功路径 / 失败 / 非法 JSON 的确定性兜底；
- personality_prompt 明确回复语言且含口音与口头禅约束。
"""

from __future__ import annotations

import json
import re

import pytest

from app.v2 import spawner as spawner_mod
from app.v2.db import DB
from app.v2.spawner import (
    LANGUAGE_MIX_MODES,
    ArchetypeNotFound,
    PersonaSpawner,
    language_display_name,
)
from app.v2.voices import archetype_by_id, load_archetypes

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_JA_RE = re.compile(r"[\u3040-\u30ff]")
_KO_RE = re.compile(r"[\uac00-\ud7a3]")


# --------------------------------------------------------------------------- #
# 测试替身 / fixture
# --------------------------------------------------------------------------- #
class MockLLM:
    """无 key 的 mock 后端：``uses_mock=True``，真实 ``complete_json`` 不应被调用。"""

    uses_mock = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete_json(self, messages, **kwargs):  # noqa: D401 - 测试替身
        self.calls.append({"messages": messages, **kwargs})
        raise AssertionError("mock 模式下不应调用真实 LLM")


class StubLLM:
    """可控的假 LLM：返回指定字符串（或抛错），模拟真实 API。"""

    uses_mock = False

    def __init__(self, payload: str = "", *, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.calls: list[dict] = []

    async def complete_json(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self.fail:
            raise RuntimeError("simulated network failure")
        return self.payload


@pytest.fixture
def db(tmp_path):
    database = DB(str(tmp_path / "v2.db"))
    database.init()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def mock_llm():
    return MockLLM()


@pytest.fixture
def spawner(db, mock_llm):
    return PersonaSpawner(db, mock_llm)


# --------------------------------------------------------------------------- #
# spawn：字段完整 + 入库
# --------------------------------------------------------------------------- #
async def test_spawn_with_mock_llm_creates_complete_friend(spawner, db) -> None:
    friend = await spawner.spawn()
    archetype = archetype_by_id(friend["archetype_id"])
    assert archetype is not None
    locale = archetype["locale_info"]
    vibe = archetype["vibe_constraints"]

    # 身份 / 声线 / 地理
    assert friend["id"]
    assert friend["name"] in archetype["name_pool"]
    assert friend["archetype_id"] == archetype["archetype_id"]
    assert friend["voice_id"] == archetype["voice_id"]
    assert friend["voice_rate"] == archetype["prosody"]["rate"]
    assert friend["voice_pitch"] == archetype["prosody"]["pitch"]
    assert friend["nationality"] == locale["country"]
    assert friend["city"] in locale["city_pool"]
    assert friend["timezone"] == locale["timezone"]
    assert friend["native_lang"] == locale["native_lang"]

    # 人设字段
    assert friend["age"] in range(vibe["age_range"][0], vibe["age_range"][1] + 1)
    assert friend["occupation"] in archetype["occupation_pool"]
    assert friend["status"] == "active"
    assert friend["referred_by_friend_id"] is None
    assert friend["request_message"] == ""
    assert friend["language_mix_mode"] in LANGUAGE_MIX_MODES
    assert friend["reply_language"] == "en"  # 无语言/国家筛选默认英语
    assert friend["bio"]
    assert friend["initial_context"]
    assert friend["personality_traits"]
    assert friend["speech_quirks"]

    stored = db.get_friend(friend["id"])
    assert stored is not None
    assert stored["name"] == friend["name"]
    assert stored["archetype_id"] == friend["archetype_id"]
    assert stored["reply_language"] == "en"
    # initial_context / personality_traits 无独立 DB 列，但已内嵌进 personality_prompt
    assert "initial_context" not in stored
    assert "personality_traits" not in stored
    assert friend["initial_context"] in stored["personality_prompt"]
    assert db.used_archetype_ids() == {friend["archetype_id"]}
    assert [f["id"] for f in db.list_friends()] == [friend["id"]]


async def test_personality_prompt_is_english_and_in_character(spawner) -> None:
    friend = await spawner.spawn()
    archetype = archetype_by_id(friend["archetype_id"])
    prompt = friend["personality_prompt"]

    assert friend["name"] in prompt
    assert archetype["accent_label"] in prompt
    assert not _CJK_RE.search(prompt), "personality_prompt 应该是英文"
    # 口头禅 + 句长/角色约束
    assert any(q in prompt for q in friend["speech_quirks"])
    assert "Match the actual message" in prompt
    # 合规升级：不再要求「绝不承认是 AI」，改为「被问到时诚实说明」
    assert "AI companion, not a real human" in prompt
    assert "TTS voice already carries the accent" in prompt


# --------------------------------------------------------------------------- #
# 过滤条件
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("kwargs", "country", "lang", "gender"),
    [
        ({"nationality": "Germany", "language": "de", "gender": "female"},
         "Germany", "de", "female"),
        ({"nationality": "Italy"}, "Italy", "it", None),
        ({"language": "fr"}, "France", "fr", None),
        ({"gender": "male", "language": "de"}, "Germany", "de", "male"),
        ({"nationality": "China", "gender": "female"}, "China", "zh", "female"),
    ],
)
async def test_spawn_respects_filters(spawner, kwargs, country, lang, gender) -> None:
    friend = await spawner.spawn(**kwargs)
    archetype = archetype_by_id(friend["archetype_id"])
    assert archetype is not None
    assert friend["nationality"] == country
    assert friend["native_lang"] == lang
    if gender:
        assert archetype["vibe_constraints"]["gender"] == gender


async def test_spawn_supports_nationality_language_gender_aliases(spawner) -> None:
    friend = await spawner.spawn(nationality="USA", language="English", gender="woman")
    assert friend["nationality"] == "United States"
    assert friend["native_lang"] == "en"
    archetype = archetype_by_id(friend["archetype_id"])
    assert archetype["vibe_constraints"]["gender"] == "female"


async def test_pick_archetype_is_sync_and_filtered(spawner) -> None:
    picked = spawner.pick_archetype(nationality="Ireland", gender="female")
    assert picked["locale_info"]["country"] == "Ireland"
    assert picked["vibe_constraints"]["gender"] == "female"


# --------------------------------------------------------------------------- #
# used archetype 去重
# --------------------------------------------------------------------------- #
async def test_spawn_dedupes_used_archetypes(spawner, db) -> None:
    friends = [await spawner.spawn() for _ in range(5)]
    ids = [f["archetype_id"] for f in friends]
    assert len(ids) == len(set(ids)), f"archetype 被重复使用: {ids}"
    assert db.used_archetype_ids() == set(ids)


async def test_spawn_does_not_crash_when_pool_is_exhausted(spawner, db) -> None:
    pool_size = len(load_archetypes())
    friends = [await spawner.spawn() for _ in range(pool_size)]
    assert len({f["archetype_id"] for f in friends}) == pool_size

    # 池子耗尽后允许复用，但绝不能抛错 / 丢数据
    extra = await spawner.spawn()
    assert extra["id"] in {f["id"] for f in db.list_friends()}
    assert extra["archetype_id"] in {a["archetype_id"] for a in load_archetypes()}


async def test_spawn_is_deterministic_across_fresh_dbs(tmp_path, mock_llm) -> None:
    dbs = []
    try:
        for name in ("first.db", "second.db"):
            fresh = DB(str(tmp_path / name))
            fresh.init()
            dbs.append(fresh)
        first = await PersonaSpawner(dbs[0], mock_llm).spawn()
        second = await PersonaSpawner(dbs[1], mock_llm).spawn()
    finally:
        for fresh in dbs:
            fresh.close()

    # 同一个原型 + 同样的 DB 状态 → 确定性兜底结果完全一致
    assert second["archetype_id"] == first["archetype_id"]
    assert second["name"] == first["name"]
    assert second["age"] == first["age"]
    assert second["city"] == first["city"]
    assert second["occupation"] == first["occupation"]
    assert second["bio"] == first["bio"]
    assert second["initial_context"] == first["initial_context"]
    assert first["name"] in archetype_by_id(first["archetype_id"])["name_pool"]


# --------------------------------------------------------------------------- #
# referral / 状态
# --------------------------------------------------------------------------- #
async def test_referral_creates_pending_request(spawner, db) -> None:
    referrer = await spawner.spawn(gender="male")
    friend = await spawner.spawn(referred_by_friend_id=referrer["id"])

    assert friend["status"] == "pending_request"
    assert friend["referred_by_friend_id"] == referrer["id"]
    assert friend["request_message"] == f"Yo! {referrer['name']} told me about you."
    assert db.get_friend(friend["id"])["status"] == "pending_request"
    assert {f["id"] for f in db.list_friends(status="pending_request")} == {friend["id"]}


async def test_explicit_status_is_preserved(spawner) -> None:
    friend = await spawner.spawn(status="active")
    assert friend["status"] == "active"


# --------------------------------------------------------------------------- #
# icebreaker
# --------------------------------------------------------------------------- #
async def test_icebreaker_non_empty_english_with_question_hook(spawner) -> None:
    friend = await spawner.spawn()
    message = await spawner.icebreaker(friend)

    assert isinstance(message, str)
    assert 5 < len(message) <= 220
    assert "?" in message, "破冰消息必须带问题钩子"
    assert re.search(r"[A-Za-z]{2}", message)
    assert not _CJK_RE.search(message), "破冰消息必须是英文"
    assert friend["name"] in message


async def test_icebreaker_works_for_db_fetched_friend(spawner, db) -> None:
    friend = await spawner.spawn()
    fetched = db.get_friend(friend["id"])  # 没有 initial_context 的内存附加字段
    message = await spawner.icebreaker(fetched)
    assert message.strip()
    assert "?" in message


# --------------------------------------------------------------------------- #
# LLM 成功 / 失败兜底
# --------------------------------------------------------------------------- #
async def test_spawn_uses_llm_persona_when_configured(db) -> None:
    payload = json.dumps(
        {
            "name": "Zara",
            "age": 29,
            "occupation": "marine biologist at a coral reef lab",
            "personality_traits": ["bold", "curious", "chatty"],
            "speech_quirks": ["honestly", "you know"],
            "language_mix_mode": "en_with_native_flavor",
            "bio": "Zara spends her mornings counting fish and her evenings surfing.",
            "initial_context": "She just got back from a dive and is drying her gear.",
        }
    )
    llm = StubLLM(payload)
    spawner = PersonaSpawner(db, llm)
    friend = await spawner.spawn(nationality="Australia", gender="female")

    assert len(llm.calls) == 1
    assert llm.calls[0]["messages"][0]["role"] == "system"
    assert friend["name"] == "Zara"
    assert friend["age"] == 29
    assert friend["occupation"] == "marine biologist at a coral reef lab"
    assert friend["personality_traits"] == ["bold", "curious", "chatty"]
    assert friend["speech_quirks"] == ["honestly", "you know"]
    assert friend["language_mix_mode"] == "en_with_native_flavor"
    assert friend["initial_context"].startswith("She just got back")
    assert "Zara" in friend["personality_prompt"]


async def test_spawn_normalizes_unknown_language_mix_mode(db) -> None:
    payload = json.dumps(
        {
            "name": "Lena",
            "age": 30,
            "occupation": "ceramics teacher",
            "personality_traits": ["calm"],
            "speech_quirks": ["echt"],
            "language_mix_mode": "whatever-mode",
            "bio": "Lena teaches ceramics in Leipzig.",
            "initial_context": "She is packing up her studio for the night.",
        }
    )
    friend = await PersonaSpawner(db, StubLLM(payload)).spawn()
    assert friend["language_mix_mode"] in LANGUAGE_MIX_MODES


async def test_spawn_falls_back_when_llm_raises(db) -> None:
    spawner = PersonaSpawner(db, StubLLM(fail=True))
    friend = await spawner.spawn()
    archetype = archetype_by_id(friend["archetype_id"])
    assert friend["name"] in archetype["name_pool"]
    assert friend["initial_context"]
    assert friend["personality_prompt"]


async def test_spawn_falls_back_on_invalid_json(db) -> None:
    spawner = PersonaSpawner(db, StubLLM("sorry, I cannot do JSON today"))
    friend = await spawner.spawn()
    archetype = archetype_by_id(friend["archetype_id"])
    assert friend["name"] in archetype["name_pool"]
    assert friend["bio"]


async def test_icebreaker_uses_llm_message_when_available(db) -> None:
    referrer = await PersonaSpawner(db, MockLLM()).spawn()
    llm = StubLLM(json.dumps({"message": "Hey! Marco said you're learning English too — what are you into?"}))
    spawner = PersonaSpawner(db, llm)
    message = await spawner.icebreaker(referrer)
    assert message.startswith("Hey! Marco said")
    assert len(llm.calls) == 1


# --------------------------------------------------------------------------- #
# 多语言好友：reply_language / strict 筛选 / 多语言破冰
# --------------------------------------------------------------------------- #
async def test_strict_language_ja_returns_japanese_friend(spawner, db) -> None:
    friend = await spawner.spawn(language="ja", strict_filters=True)
    archetype = archetype_by_id(friend["archetype_id"])

    assert friend["reply_language"] == "ja"
    assert friend["native_lang"] == "ja"
    assert friend["nationality"] == "Japan"
    assert friend["voice_id"].startswith("ja-JP-")
    assert friend["timezone"] == "Asia/Tokyo"
    assert friend["language_mix_mode"] == "native_primary"
    assert archetype["locale_info"]["native_lang"] == "ja"
    assert db.get_friend(friend["id"])["reply_language"] == "ja"

    prompt = friend["personality_prompt"]
    assert "natural Japanese (日本語)" in prompt
    assert "Never switch to English" in prompt
    # 非英语好友的口头禅应来自原型（日文）
    assert any(_JA_RE.search(str(q)) for q in friend["speech_quirks"])


async def test_strict_country_and_gender_resolves_native_reply_language(spawner) -> None:
    friend = await spawner.spawn(nationality="Japan", gender="female", strict_filters=True)
    assert friend["reply_language"] == "ja"  # 只给国家 → 用原型母语
    assert friend["archetype_id"] == "jp_female_01"
    archetype = archetype_by_id(friend["archetype_id"])
    assert archetype["vibe_constraints"]["gender"] == "female"


async def test_strict_language_en_with_country_japan_replies_english(spawner) -> None:
    friend = await spawner.spawn(nationality="Japan", language="en", strict_filters=True)
    assert friend["nationality"] == "Japan"
    assert friend["native_lang"] == "ja"
    assert friend["reply_language"] == "en"  # 语言筛选优先：用户要英语回复
    assert friend["language_mix_mode"] == "en_only"
    prompt = friend["personality_prompt"]
    assert "Reply with spoken English only" in prompt
    assert "natural Japanese" not in prompt


async def test_strict_language_ko_and_pt_resolve_correctly(spawner) -> None:
    ko = await spawner.spawn(language="ko", strict_filters=True)
    assert ko["reply_language"] == "ko"
    assert ko["nationality"] == "Korea"
    assert ko["voice_id"].startswith("ko-KR-")

    pt = await spawner.spawn(language="pt", strict_filters=True)
    assert pt["reply_language"] == "pt"
    assert pt["nationality"] == "Brazil"
    assert pt["voice_id"].startswith("pt-BR-")


async def test_strict_country_only_uses_native_language_for_non_english(spawner) -> None:
    friend = await spawner.spawn(nationality="Italy", strict_filters=True)
    assert friend["reply_language"] == "it"

    uk = await spawner.spawn(nationality="UK", strict_filters=True)
    assert uk["reply_language"] == "en"


async def test_strict_unsupported_language_raises_archetype_not_found(spawner) -> None:
    with pytest.raises(ArchetypeNotFound) as ei:
        await spawner.spawn(language="ar", strict_filters=True)
    detail = ei.value.detail
    assert "ar" in detail
    assert "支持的语种" in detail
    assert "ja" in detail  # 当前支持列表里包含新语种
    assert str(ei.value) == detail


async def test_strict_unsupported_country_raises_archetype_not_found(spawner) -> None:
    with pytest.raises(ArchetypeNotFound):
        await spawner.spawn(nationality="Narnia", strict_filters=True)


async def test_strict_never_silently_widens_gender(spawner) -> None:
    # Japan 原型有男/女；非二元性别严格筛选必须报错而不是退回英语美国好友
    with pytest.raises(ArchetypeNotFound):
        await spawner.spawn(nationality="Japan", gender="nonbinary", strict_filters=True)


async def test_non_strict_language_unsupported_still_creates_friend(spawner) -> None:
    # onboarding/referral 用默认 strict_filters=False：容错、不抛
    friend = await spawner.spawn(language="ar")
    assert friend["id"]
    assert friend["reply_language"] == "en"  # 不支持的语言回退英语


async def test_pick_archetype_strict_flag(spawner) -> None:
    picked = spawner.pick_archetype(language="ja", gender="male", strict=True)
    assert picked["voice_id"] == "ja-JP-KeitaNeural"
    with pytest.raises(ArchetypeNotFound):
        spawner.pick_archetype(language="ar", strict=True)


async def test_icebreaker_is_japanese_for_ja_friend(spawner) -> None:
    friend = await spawner.spawn(language="ja", strict_filters=True)
    message = await spawner.icebreaker(friend)

    assert isinstance(message, str) and message.strip()
    assert friend["name"] in message
    assert _JA_RE.search(message), f"日文好友破冰应为日文: {message!r}"
    assert "？" in message or "?" in message


async def test_icebreaker_is_korean_for_ko_friend(spawner) -> None:
    friend = await spawner.spawn(language="ko", strict_filters=True)
    message = await spawner.icebreaker(friend)
    assert _KO_RE.search(message), f"韩文好友破冰应为韩文: {message!r}"
    assert "?" in message


async def test_icebreaker_templates_cover_required_languages() -> None:
    # 无 LLM 时至少覆盖 ja/ko/pt/es/de/it/fr/zh + en，每种 2-3 条模板
    required = {"en", "ja", "ko", "pt", "es", "de", "it", "fr", "zh"}
    assert required <= set(spawner_mod._ICEBREAKER_TEMPLATES)
    assert required <= set(spawner_mod._ICEBREAKER_QUESTIONS_BY_LANG)
    for language in required:
        assert 2 <= len(spawner_mod._ICEBREAKER_TEMPLATES[language]) <= 3
    for language in required - {"en"}:
        assert 2 <= len(spawner_mod._ICEBREAKER_QUESTIONS_BY_LANG[language]) <= 3
    assert len(spawner_mod._ICEBREAKER_QUESTIONS_BY_LANG["en"]) >= 2


def test_language_display_name() -> None:
    assert language_display_name("ja") == "Japanese (日本語)"
    assert language_display_name("ko") == "Korean (한국어)"
    assert language_display_name("pt") == "Brazilian Portuguese (português)"
    assert language_display_name("en") == "English"


async def test_spawn_llm_mode_is_forced_native_primary_for_non_english(db) -> None:
    payload = json.dumps(
        {
            "name": "Yuki",
            "age": 28,
            "occupation": "illustrator",
            "personality_traits": ["calm"],
            "speech_quirks": ["えっと"],
            "language_mix_mode": "en_only",
            "bio": "Yuki draws illustrations in Tokyo.",
            "initial_context": "She is finishing a sketch at home.",
        }
    )
    friend = await PersonaSpawner(db, StubLLM(payload)).spawn(language="ja", strict_filters=True)
    assert friend["name"] == "Yuki"
    assert friend["reply_language"] == "ja"
    assert friend["language_mix_mode"] == "native_primary"
