"""Prompt Composer（sonder-prompt-3.2.0）契约与反模板化验收。

覆盖任务书 §7 的九类断言：
1. A–H 模块齐全、顺序正确、版本号存在；
2. global 里没有硬编码用户画像（旧的 "Chinese person ... SCARED" 必须消失）；
3. user_persona 动态读取真实资料；资料缺失时明确「不知道、不要编」；
4. 不同人物角色卡不同、Speech DNA traits 影响 Communication Style；
5. behavior / author_note 永远最后且不可裁剪；examples 可被预算裁掉；
6. module_overrides 生效（不落库）；
7. 通话与文字同一角色卡来源、更短、无 examples；
8. db=None / 空 persona 全链路降级不抛异常；
9. first_message 不同人物不同开场、同一人物稳定、不以 Hi/How are you 开头、低水平更短。

全部确定性、零 LLM。
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from app.v2.db import DB
from app.v3 import first_message as fm
from app.v3 import prompt_composer as pc
from app.v3.schema import ensure_schema
from app.v3.service import V3Service
from app.v3.speech_dna import ensure_persona_dna

#: 文字模式的注入顺序（契约）
TEXT_KEYS = [
    "global",
    "language",
    "character_card",
    "scenario",
    "user_persona",
    "relationship_memory",
    "examples",
    "opening",
    "behavior",
    "author_note",
]

#: 通话模式的注入顺序（精简）
CALL_KEYS = ["global", "character_card", "scenario", "language", "behavior", "author_note"]


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "prompt.db"))
    database.init()
    ensure_schema(database, seed=True)
    asyncio.run(ensure_persona_dna(database, None, use_llm=False))
    yield database
    database.close()


def _persona(db: DB, nickname: str) -> dict:
    row = db.conn.execute("SELECT * FROM personas WHERE nickname=?", (nickname,)).fetchone()
    assert row is not None, f"找不到人物 {nickname}"
    return dict(row)


def _friend(db: DB, nickname: str, **over) -> dict:
    persona = _persona(db, nickname)
    payload = {
        "name": persona["nickname"],
        "persona_id": persona["id"],
        "city": persona.get("city") or "",
        "nationality": persona.get("country") or "",
        "native_lang": persona.get("language") or "en",
        "reply_language": persona.get("language") or "en",
        "personality_prompt": (persona.get("personality") or "")[:400],
        "closeness_level": 1,
        "total_turns": 0,
    }
    payload.update(over)
    return db.create_friend(payload)


# --------------------------------------------------------------------------- #
# 1. 模块齐全 + 顺序 + 版本
# --------------------------------------------------------------------------- #
def test_compose_has_all_modules_in_contract_order(db):
    persona = _persona(db, "Adrian")
    composed = pc.compose_for_persona(db, persona["id"], user_text="hey, how was your day?")

    assert composed.version == pc.PROMPT_VERSION
    assert composed.mode == "text"
    assert composed.language == "en"
    assert [module.key for module in composed.modules] == TEXT_KEYS
    for key in TEXT_KEYS:
        assert composed.module(key) is not None, key
    assert composed.module("behavior").enabled
    assert composed.module("author_note").enabled
    assert len(composed.text) > 800
    assert len(composed.text) <= pc.MAX_TEXT_CHARS

    data = composed.as_dict()
    assert set(data) >= {"version", "mode", "language", "chars", "modules", "full_text"}
    assert data["version"] == pc.PROMPT_VERSION
    assert data["chars"] == len(composed.text)
    assert data["full_text"] == composed.text
    assert [item["key"] for item in data["modules"]] == TEXT_KEYS
    assert all("text" in item for item in data["modules"])

    lean = composed.as_dict(include_text=False)
    assert "full_text" not in lean
    assert all("text" not in item for item in lean["modules"])

    # 版本登记表：至少包含当前版本，并且当前版本排第一
    assert pc.PROMPT_VERSIONS and pc.PROMPT_VERSIONS[0]["version"] == pc.PROMPT_VERSION
    summary = pc.prompt_summary(composed)
    assert summary["version"] == pc.PROMPT_VERSION
    assert [item["key"] for item in summary["modules"]] == TEXT_KEYS


# --------------------------------------------------------------------------- #
# 2. global 不包含硬编码用户画像
# --------------------------------------------------------------------------- #
def test_global_has_no_hardcoded_user_profile(db):
    persona = _persona(db, "Camila")
    composed = pc.compose_for_persona(db, persona["id"], user_text="hi")
    full = composed.text
    global_text = composed.module("global").text

    for banned in (
        "Chinese person",
        "reads English fine but is SCARED",
        "SCARED",
        "WHO YOU ARE TALKING TO",
        "a Chinese person",
    ):
        assert banned not in full, f"global 里仍有硬编码用户画像: {banned}"
    for required in ("fictional", "ai", "tendency", "emoji"):
        assert required in global_text.lower(), required
    # 「不是每轮都必须提问 / 用口头禅 / emoji」的反模板化原则必须在场
    lowered = global_text.lower()
    assert "every turn" in lowered
    assert "no quizzes" in lowered or "no grammar" in lowered


# --------------------------------------------------------------------------- #
# 3. user_persona 读取真实资料；缺失时不编造
# --------------------------------------------------------------------------- #
def _set_rich_profile(db: DB) -> None:
    fact = {
        "profile": {
            "nickname": "Alex",
            "city": "Chengdu",
            "major_or_job": "designer",
            "routine": "",
            "level": "B1",
        },
        "interests": ["photography", "hiking"],
        "language_profile": {"target": "en", "levels": {"en": "B1", "ja": "N5"}, "weaknesses": []},
        "recent_events": [],
        "communication_preference": "occasional",
    }
    db.update_profile(
        nickname="Alex",
        native_language="zh-CN",
        target_languages=["en", "ja"],
        fact_sheet=fact,
    )


def test_user_persona_reads_real_profile(db):
    _set_rich_profile(db)
    friend = _friend(db, "Adrian")
    composed = pc.compose_prompt(db, friend, user_text="hi")
    user_text = composed.module("user_persona").text
    language_text = composed.module("language").text

    for expected in ("Alex", "Chengdu", "designer", "photography", "hiking"):
        assert expected in user_text, expected
    # 分语言水平 + 纠错偏好都应进入 prompt（user_persona 或 language 模块）
    assert "B1" in composed.text
    assert "B1" in user_text or "B1" in language_text
    assert "correction" in composed.text.lower()
    # 用户主动填写的资料可以覆盖记忆推测
    assert "profile beats any guess" in user_text.lower()


def test_user_persona_prefers_real_nickname_over_stale_fact_placeholder(db):
    """回归：历史 fact_sheet.profile.nickname 常留着默认占位 "Traveler"。

    如果让它优先于 user_profiles.nickname，模型就永远不知道用户真实称呼。
    """
    fact = {
        "profile": {"nickname": "Traveler", "city": "", "major_or_job": ""},
        "interests": ["Programming"],
        "language_profile": {"levels": {"en": "能聊几句"}},
    }
    db.update_profile(
        nickname="Ming",
        native_language="zh",
        target_languages=["en"],
        fact_sheet=fact,
    )
    friend = _friend(db, "Adrian")
    user_text = pc.compose_prompt(db, friend, user_text="hi").module("user_persona").text
    assert "Their name: Ming." in user_text, user_text
    assert "Traveler" not in user_text, user_text


def test_update_profile_syncs_nickname_into_fact_sheet(db):
    """用户主动填写的昵称必须同步进 fact_sheet，避免占位值长期压制真实资料。"""
    service = V3Service(db, SimpleNamespace(), "")
    service.update_profile_fields(nickname="Ming", native_language="zh", targets=["en"])
    profile = db.get_profile()
    fact = profile.get("fact_sheet") or {}
    assert (fact.get("profile") or {}).get("nickname") == "Ming"
    assert profile.get("nickname") == "Ming"


def test_user_persona_does_not_invent_missing_data(db):
    friend = _friend(db, "Adrian")
    composed = pc.compose_prompt(db, friend, user_text="hi")
    user_text = composed.module("user_persona").text

    lowered = user_text.lower()
    assert "don't know" in lowered or "you simply" in lowered
    assert "never invent" in lowered
    # 默认资料不能凭空出现具体城市/职业/兴趣
    for invented in ("Chengdu", "photography", "designer", "Alex"):
        assert invented not in user_text
    assert "traveler" not in lowered


# --------------------------------------------------------------------------- #
# 4. 角色卡随人物变化，traits 映射到 Communication Style
# --------------------------------------------------------------------------- #
def test_character_cards_differ_and_reflect_traits(db):
    cards = {}
    for nickname in ("Adrian", "Camila", "Callum"):
        persona = _persona(db, nickname)
        composed = pc.compose_for_persona(db, persona["id"], user_text="hey")
        card = composed.module("character_card").text
        assert card and composed.module("character_card").enabled
        assert "Communication style" in card
        assert "Personality:" in card
        cards[nickname] = card

    assert cards["Adrian"] != cards["Camila"] != cards["Callum"]
    # Adrian 的 DNA 是 clipped / one_line / dry —— 这些特质必须落到描述里
    assert "clipped" in cards["Adrian"].lower()
    assert "one_line" in cards["Adrian"]
    assert "clipped" in cards["Adrian"].lower() or "dry" in cards["Adrian"].lower()
    # 人物差异不能只是换了名字：Communication style 段必须不同
    adrian_style = cards["Adrian"].split("Communication style:")[1][:120]
    camila_style = cards["Camila"].split("Communication style:")[1][:120]
    assert adrian_style != camila_style
    # 性格描述必须提醒「影响行为」而不是只换形容词
    assert "shape what you say" in cards["Adrian"].lower() or "react" in cards["Adrian"].lower()


# --------------------------------------------------------------------------- #
# 5. 预算：examples 可裁，behavior/author_note 永不裁且最后
# --------------------------------------------------------------------------- #
def test_behavior_and_author_note_survive_budget_and_are_last(db):
    persona = _persona(db, "Adrian")
    normal = pc.compose_for_persona(db, persona["id"], user_text="I'm exhausted, bad day")
    keys = [module.key for module in normal.modules]
    assert keys[-2:] == ["behavior", "author_note"]
    assert normal.module("behavior").enabled
    assert normal.module("author_note").enabled
    assert normal.module("author_note").text.startswith("This turn:")
    note = normal.module("author_note").text.lower()
    for required in ("question", "share", "disagree", "rest"):
        assert required in note, required
    assert len(normal.text) <= pc.MAX_TEXT_CHARS

    # 极端情况：巨长 memory + 巨大 examples 覆盖 → 仍必须保留 behavior / author_note
    friend = _friend(db, "Adrian")
    overloaded = pc.compose_prompt(
        db,
        friend,
        user_text="hey",
        memory_context="M" * 6000,
        module_overrides={"examples": "E" * 5000},
    )
    assert len(overloaded.text) <= pc.MAX_TEXT_CHARS
    overloaded_keys = [module.key for module in overloaded.modules]
    assert overloaded_keys[-2:] == ["behavior", "author_note"]
    assert overloaded.module("behavior").enabled
    assert overloaded.module("author_note").enabled
    examples = overloaded.module("examples")
    assert (not examples.enabled) or examples.chars < 5000
    assert overloaded.module("character_card").enabled
    assert overloaded.module("global").enabled


# --------------------------------------------------------------------------- #
# 6. module_overrides 生效（调试用，不落库）
# --------------------------------------------------------------------------- #
def test_module_overrides_take_effect(db):
    friend = _friend(db, "Adrian")
    original = pc.compose_prompt(db, friend, user_text="hey")
    assert "TEST-OVERRIDE" not in original.text

    overridden = pc.compose_prompt(
        db,
        friend,
        user_text="hey",
        module_overrides={"global": "[TEST-OVERRIDE] stay in character."},
    )
    assert overridden.module("global").text == "[TEST-OVERRIDE] stay in character."
    assert "[TEST-OVERRIDE]" in overridden.text

    cleared = pc.compose_prompt(
        db, friend, user_text="hey", module_overrides={"global": ""}
    )
    assert cleared.module("global").enabled is False
    assert "[TEST-OVERRIDE]" not in cleared.text


# --------------------------------------------------------------------------- #
# 7. 通话模式：同一角色卡来源、更短、无 examples
# --------------------------------------------------------------------------- #
def test_call_mode_shares_character_card_and_is_shorter(db):
    friend = _friend(db, "Adrian")
    text_prompt = pc.compose_prompt(db, friend, user_text="how was your day?")
    call_prompt = pc.compose_call_prompt(db, friend)
    persona = _persona(db, "Adrian")

    assert call_prompt.mode == "call"
    assert [module.key for module in call_prompt.modules] == CALL_KEYS
    assert len(call_prompt.text) <= pc.MAX_CALL_CHARS
    assert len(call_prompt.text) < len(text_prompt.text)
    assert "examples" not in [module.key for module in call_prompt.modules]
    assert "[Example voice references" not in call_prompt.text

    # 同一人设：姓名 / 职业 / 城市 两个模式都在
    for field in (persona["nickname"], persona["occupation"], persona["city"]):
        assert field in call_prompt.text, field
        assert field in text_prompt.text, field
    # 通话要求：DNA 听感行 + 通话轮次标记 + 长度提示
    assert "[Speech DNA on the phone]" in call_prompt.text
    assert "LIVE voice call" in call_prompt.text
    assert "max ~" in call_prompt.text


# --------------------------------------------------------------------------- #
# 8. 降级：db=None / 空 persona / 空 friend 都不抛异常
# --------------------------------------------------------------------------- #
def test_compose_degrades_without_db_or_persona():
    composed = pc.compose_prompt(None, {})
    assert composed.version == pc.PROMPT_VERSION
    assert [module.key for module in composed.modules] == TEXT_KEYS
    assert "fictional" in composed.text.lower()
    assert composed.module("behavior").enabled
    assert composed.module("author_note").enabled

    no_db_friend = pc.compose_prompt(None, {"name": "Nobody"})
    assert no_db_friend.text

    missing_persona = pc.compose_for_persona(None, "p_missing")
    assert missing_persona.mode == "text"
    assert missing_persona.text

    call = pc.compose_call_prompt(None, {})
    assert call.mode == "call"
    assert call.text

    empty_friend = pc.compose_prompt(None, {"persona_id": "", "reply_language": ""})
    assert "English" in empty_friend.text


# --------------------------------------------------------------------------- #
# 9. first_message：确定性 / 分人物 / 语言干净 / 低水平更短
# --------------------------------------------------------------------------- #
def test_first_message_is_deterministic_and_persona_specific(db):
    adrian = _persona(db, "Adrian")
    camila = _persona(db, "Camila")
    lin = _persona(db, "Lin")

    adrian_line = fm.first_message(adrian, language="en")
    assert adrian_line
    assert fm.first_message(adrian, language="en") == adrian_line  # 同一人物稳定
    assert fm.first_message(camila, language="en") != adrian_line  # 不同人物不同开场

    variants = fm.first_message_variants(adrian, language="en", k=3)
    assert len(variants) == 3
    assert len(set(variants)) == 3

    for line in variants + [adrian_line]:
        lowered = line.lower()
        assert not lowered.startswith("hi")
        assert not lowered.startswith("hello")
        assert not lowered.startswith("how are you")

    # 语言干净：英文不夹 CJK；中文不夹拉丁字母
    assert not re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", adrian_line)
    zh_line = fm.first_message(lin, language="zh")
    assert re.search(r"[\u4e00-\u9fff]", zh_line)
    assert not re.search(r"[A-Za-z]", zh_line)
    # 其它语言回退 en（至少不是乱码/空串）
    ja_line = fm.first_message(_persona(db, "Haruto"), language="ja")
    assert ja_line and not re.search(r"[\u3040-\u30ff]", ja_line)


def test_first_message_low_level_is_shorter(db):
    adrian = _persona(db, "Adrian")
    normal = fm.first_message_variants(adrian, language="en", k=5)
    short = fm.first_message_variants(adrian, language="en", level="beginner", k=5)
    assert normal and short
    assert sum(len(line) for line in short) < sum(len(line) for line in normal)
    # 同一个开场风格函数必须在场，且明确「不要客服欢迎语」
    opening = fm.opening_style_line(adrian, "en")
    assert opening
    assert "customer-service" in opening.lower() or "greeting" in opening.lower()


def test_persona_brief_exposes_first_message(db):
    persona = _persona(db, "Adrian")
    service = V3Service(db, SimpleNamespace(llm=None), "")
    brief = service._persona_brief(persona)
    assert "first_message" in brief
    assert brief["first_message"]
    assert brief["first_message"] == fm.first_message(persona, language=persona["language"])
