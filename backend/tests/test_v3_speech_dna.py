"""Speech DNA 模块单元测试（纯逻辑 + 幂等写库）。

覆盖点（对应 docs/PERSONA_UPGRADE_PLAN.md §3.5）
------------------------------------------------
- ``normalize_dna`` 对 ``{}`` / ``"{}"`` / 非法枚举 / 缺键 → 13 键齐全的合法 dict；
- ``derive_dna`` 确定性 + 12 个种子人物两两至少 3 个字段不同；
- ``dna_traits`` 全部落在 0..1；
- ``dna_prompt_block`` 含「不强制提问 / 允许一句话 / 允许不同意」等约束；
- ``example_bank`` 对不同 persona 输出不同，且 12 个场景齐全；
- ``pick_examples`` 同 scene 优先、k 上限、rng 固定可复现；
- ``ensure_persona_dna`` 空库补齐 12 条/人、二次幂等、mock / 失败 LLM 下走兜底、
  且永不抛异常。

测试全部使用 ``tmp_path`` 下的临时 SQLite，不碰 ``data/``、不调真实 LLM。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.personas.custom_store import CustomStore  # noqa: E402
from app.v3 import speech_dna as sd  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.seed_data import PERSONAS, ensure_persona_voices  # noqa: E402

TRAIT_KEYS = {
    "initiative",
    "warmth",
    "humour",
    "directness",
    "verbosity",
    "disclosure",
    "emoji",
    "slang",
    "playfulness",
    "social_distance",
}

EMOJI_CHARS = "🙂😅😌✨🙃☕"


def _by_id(persona_id: str) -> dict:
    for persona in PERSONAS:
        if persona["id"] == persona_id:
            return persona
    raise KeyError(persona_id)


def _count(db: DB, table: str) -> int:
    with db._lock:  # noqa: SLF001 - 测试里直接读计数
        return int(db.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


@pytest.fixture()
def seeded_db(tmp_path):
    db = DB(str(tmp_path / "speech_dna.db"))
    db.init()
    ensure_schema(db)
    yield db
    db.close()


# --------------------------------------------------------------------------- #
# 常量 / 归一化
# --------------------------------------------------------------------------- #
def test_constants_match_contract():
    assert sd.SPEECH_DNA_VERSION == "1"
    assert len(sd.DNA_KEYS) == 13
    assert len(set(sd.DNA_KEYS)) == 13
    assert len(sd.SCENES) == 12
    assert len(set(sd.SCENES)) == 12
    assert set(sd.DNA_ENUMS) == set(sd.DNA_KEYS)
    for key, values in sd.DNA_ENUMS.items():
        assert isinstance(values, tuple) and len(values) >= 3, key


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        "{}",
        "",
        "not-json",
        "[]",
        {"directness": "blunt", "verbosity": "one_line"},
        {"directness": "VERY_BLUNT"},
        {"verbosity": 3},
        {"humor_style": None},
        {"sentence_rhythm": "clipped", "unexpected": "x"},
    ],
)
def test_normalize_dna_always_returns_full_valid_dna(raw):
    dna = sd.normalize_dna(raw)
    for key in sd.DNA_KEYS:
        assert dna[key] in sd.DNA_ENUMS[key], (key, dna[key])
    assert isinstance(dna["signature_phrases"], list)
    assert isinstance(dna["banned_phrases"], list)
    assert isinstance(dna["punctuation_habit"], str)
    assert isinstance(dna["note"], str)


def test_normalize_dna_is_case_insensitive_and_keeps_valid_values():
    dna = sd.normalize_dna({"directness": " BLUNT ", "emoji_usage": "Never"})
    assert dna["directness"] == "blunt"
    assert dna["emoji_usage"] == "never"


def test_normalize_dna_clamps_optional_phrases():
    dna = sd.normalize_dna(
        {
            "signature_phrases": ["a", "b", "c", "d", "e", "a", ""],
            "banned_phrases": ["x", 42, " y  ", "y"],
            "punctuation_habit": "x" * 500,
            "note": "  too   many   spaces  ",
        }
    )
    assert dna["signature_phrases"] == ["a", "b", "c", "d"]
    assert dna["banned_phrases"] == ["x", "y"]
    assert len(dna["punctuation_habit"]) <= 80
    assert dna["note"] == "too many spaces"


# --------------------------------------------------------------------------- #
# 种子语料
# --------------------------------------------------------------------------- #
def test_seed_personas_all_have_legal_speech_dna():
    assert len(PERSONAS) == 12
    for persona in PERSONAS:
        dna_raw = persona.get("speech_dna")
        assert isinstance(dna_raw, dict), persona["id"]
        assert sd._is_valid_dna(dna_raw), persona["id"]
        dna = sd.normalize_dna(dna_raw)
        assert dna["signature_phrases"], persona["id"]
        assert dna["banned_phrases"], persona["id"]


def test_non_english_personas_use_native_signature_phrases():
    non_ascii_langs = {"ja", "ko", "zh"}
    for persona in PERSONAS:
        if persona["language"] == "en":
            continue
        phrases = persona["speech_dna"]["signature_phrases"]
        assert phrases, persona["id"]
        if persona["language"] in non_ascii_langs:
            assert any(any(ord(ch) > 127 for ch in phrase) for phrase in phrases), persona["id"]


def test_seed_speech_dna_personas_are_pairwise_distinct():
    for a, b in itertools.combinations(PERSONAS, 2):
        diff = [key for key in sd.DNA_KEYS if a["speech_dna"][key] != b["speech_dna"][key]]
        assert len(diff) >= 3, (a["nickname"], b["nickname"], diff)


def test_seed_persona_hallmarks():
    adrian = _by_id("p_adrian")["speech_dna"]
    assert adrian["directness"] == "blunt"
    assert adrian["humor_style"] == "dry"
    assert adrian["sentence_rhythm"] == "clipped"
    assert adrian["verbosity"] == "one_line"
    assert adrian["emoji_usage"] == "never"

    callum = _by_id("p_callum")["speech_dna"]
    assert callum["humor_style"] == "sarcastic"
    assert callum["sentence_rhythm"] == "fragmented"
    assert callum["slang_usage"] == "heavy"
    assert "mate" in callum["signature_phrases"]


# --------------------------------------------------------------------------- #
# derive_dna
# --------------------------------------------------------------------------- #
def test_derive_dna_is_deterministic_and_valid():
    for persona in PERSONAS:
        first = sd.derive_dna(persona)
        second = sd.derive_dna(dict(persona))
        assert first == second, persona["id"]
        for key in sd.DNA_KEYS:
            assert first[key] in sd.DNA_ENUMS[key], (persona["id"], key)


def test_derive_dna_ignores_speech_dna_key():
    for persona in PERSONAS:
        stripped = {k: v for k, v in persona.items() if k != "speech_dna"}
        assert sd.derive_dna(persona) == sd.derive_dna(stripped), persona["id"]


def test_derive_dna_seed_personas_are_pairwise_distinct():
    derived = [sd.derive_dna(persona) for persona in PERSONAS]
    for a, b in itertools.combinations(list(zip(PERSONAS, derived)), 2):
        (pa, da), (pb, db) = a, b
        diff = [key for key in sd.DNA_KEYS if da[key] != db[key]]
        assert len(diff) >= 3, (pa["nickname"], pb["nickname"], diff)


def test_derive_dna_handles_minimal_persona():
    dna = sd.derive_dna({"id": "p_unknown"})
    assert sd.dna_traits(dna)  # 不崩、可继续用
    assert all(key in dna for key in sd.DNA_KEYS)


# --------------------------------------------------------------------------- #
# dna_traits
# --------------------------------------------------------------------------- #
def test_dna_traits_are_all_normalized():
    for persona in PERSONAS:
        traits = sd.dna_traits(persona["speech_dna"])
        assert set(traits) == TRAIT_KEYS
        for key, value in traits.items():
            assert isinstance(value, float), (persona["id"], key)
            assert 0.0 <= value <= 1.0, (persona["id"], key, value)


def test_dna_traits_reflect_dna_differences():
    adrian = sd.dna_traits(_by_id("p_adrian")["speech_dna"])
    annika = sd.dna_traits(_by_id("p_annika")["speech_dna"])
    haruto = sd.dna_traits(_by_id("p_haruto")["speech_dna"])
    camila = sd.dna_traits(_by_id("p_camila")["speech_dna"])
    callum = sd.dna_traits(_by_id("p_callum")["speech_dna"])

    assert adrian["directness"] > annika["directness"]  # blunt > diplomatic
    assert camila["verbosity"] > haruto["verbosity"]  # long > one_line
    assert callum["humour"] > haruto["humour"]  # sarcastic > none
    assert camila["emoji"] > adrian["emoji"]  # often > never
    assert callum["slang"] > annika["slang"]  # heavy > rare


# --------------------------------------------------------------------------- #
# dna_prompt_block
# --------------------------------------------------------------------------- #
def test_dna_prompt_block_contains_required_constraints():
    block = sd.dna_prompt_block(_by_id("p_adrian")["speech_dna"])
    low = block.lower()
    for needle in (
        "tendenc",
        "do not force a question",
        "one-line",
        "disagree",
        "humour",
        "directness",
        "self-disclosure",
        "topic shifts",
        "signature",
    ):
        assert needle in low, needle
    assert "right" in block  # signature phrase 被注入
    # 标点习惯必须是完整英文短语（旧实现按 24 字符截断成 "never a."）
    assert "never an exclamation mark" in block
    assert "never a." not in block
    assert "banned" not in low or "never say" in low
    assert len(block) <= 900


def test_dna_prompt_block_respects_max_chars_and_language():
    ja_persona = _by_id("p_haruto")["speech_dna"]
    block = sd.dna_prompt_block(ja_persona, language="ja")
    assert "Japanese" in block
    assert "まあね" in block

    tiny = sd.dna_prompt_block(ja_persona, max_chars=120)
    assert len(tiny) <= 120


# --------------------------------------------------------------------------- #
# scene_for_context
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("user_text", "behavior", "expected"),
    [
        ("I should go to bed now.", "respond", "wrap_up"),
        ("Goodnight, talk tomorrow.", "respond", "wrap_up"),
        ("It's been a while. How have you been?", "respond", "reconnect"),
        ("Honestly, running is overrated.", "respond", "disagree"),
        ("haha that's funny", "tease", "joke"),
        ("I've had a rough week.", "comfort", "comfort"),
        ("I got the job! Finally!", "respond", "happy"),
        ("I'm not sure what to do.", "respond", "unsure"),
        ("Can I ask you something personal?", "respond", "private_question"),
        ("Hey", "respond", "short_reply"),
        ("Hey, how's it going?", "respond", "small_talk"),
        ("What did you do today?", "share", "life_share"),
        ("I'm exhausted today.", "respond", "tired"),
    ],
)
def test_scene_for_context_mapping(user_text, behavior, expected):
    scene = sd.scene_for_context(
        user_text=user_text, behavior=behavior, dna=_by_id("p_annika")["speech_dna"]
    )
    assert scene == expected
    assert scene in sd.SCENES


def test_scene_for_context_behavior_takes_priority():
    """行为层已经消化过意图：behavior 映射优先于字面文本。"""
    adrian = _by_id("p_adrian")["speech_dna"]
    assert sd.scene_for_context(user_text="I failed my exam", behavior="comfort", dna=adrian) == "comfort"
    assert sd.scene_for_context(user_text="I failed my exam", behavior="joke", dna=adrian) == "joke"
    assert sd.scene_for_context(user_text="I disagree with you", behavior="share", dna=adrian) == "life_share"
    assert sd.scene_for_context(user_text="Hey", behavior="comfort", dna=adrian) == "comfort"
    assert sd.scene_for_context(user_text="I got the job!!", behavior="ask_question", dna=adrian) == "small_talk"
    assert sd.scene_for_context(user_text="ok bye", behavior="end_conversation", dna=adrian) == "wrap_up"
    assert sd.scene_for_context(user_text="anything at all", behavior="acknowledge", dna=adrian) == "short_reply"
    assert sd.scene_for_context(user_text="I disagree with you", behavior="disagree", dna=adrian) == "disagree"


@pytest.mark.parametrize(
    ("user_text", "behavior", "expected"),
    [
        ("Hey, how was your weekend?", "respond", "small_talk"),
        ("Hey, how was your day?", "respond", "small_talk"),
        ("I got the job!!", "respond", "happy"),
        ("I am exhausted", "respond", "tired"),
        ("haha that is funny", "respond", "joke"),
        ("I disagree with you", "respond", "disagree"),
        ("ok bye talk later", "respond", "wrap_up"),
        ("I failed my exam", "respond", "comfort"),
        ("I'm stressed about work", "respond", "comfort"),
        ("that is too personal", "respond", "private_question"),
    ],
)
def test_scene_for_context_lead_acceptance_cases(user_text, behavior, expected):
    """Lead 集成验收里发现偏差的真实用户输入（Adrian，behavior='respond'）。"""
    scene = sd.scene_for_context(
        user_text=user_text, behavior=behavior, dna=_by_id("p_adrian")["speech_dna"]
    )
    assert scene == expected, user_text


def test_scene_for_context_reaches_every_scene():
    dna = _by_id("p_annika")["speech_dna"]
    cases = [
        ("I should go to bed now.", "respond"),
        ("It's been a while. How have you been?", "respond"),
        ("Honestly, running is overrated.", "respond"),
        ("haha that's funny", "tease"),
        ("I've had a rough week.", "comfort"),
        ("I got the job! Finally!", "respond"),
        ("I'm not sure what to do.", "respond"),
        ("Can I ask you something personal?", "respond"),
        ("Hey", "respond"),
        ("Hey, how's it going?", "respond"),
        ("What did you do today?", "share"),
        ("I'm exhausted today.", "respond"),
    ]
    scenes = {sd.scene_for_context(user_text=text, behavior=act, dna=dna) for text, act in cases}
    assert scenes == set(sd.SCENES)


# --------------------------------------------------------------------------- #
# example_bank
# --------------------------------------------------------------------------- #
def test_example_bank_covers_all_scenes():
    for persona in PERSONAS:
        bank = sd.example_bank(persona, persona["speech_dna"])
        assert [row["scene"] for row in bank] == list(sd.SCENES), persona["id"]
        for row in bank:
            assert row["user_line"] and row["persona_line"]
            assert row["language"] == persona["language"]


def test_example_bank_differs_between_personas():
    sets = {
        persona["id"]: {row["user_line"] for row in sd.example_bank(persona, persona["speech_dna"])}
        for persona in PERSONAS
    }
    for a, b in itertools.combinations(sets, 2):
        assert sets[a] != sets[b], (a, b)


def test_example_bank_reflects_dna():
    adrian = sd.example_bank(_by_id("p_adrian"), _by_id("p_adrian")["speech_dna"])
    camila = sd.example_bank(_by_id("p_camila"), _by_id("p_camila")["speech_dna"])
    callum = sd.example_bank(_by_id("p_callum"), _by_id("p_callum")["speech_dna"])
    haruto = sd.example_bank(_by_id("p_haruto"), _by_id("p_haruto")["speech_dna"])

    adrian_by_scene = {row["scene"]: row["persona_line"] for row in adrian}
    camila_by_scene = {row["scene"]: row["persona_line"] for row in camila}
    callum_by_scene = {row["scene"]: row["persona_line"] for row in callum}
    haruto_by_scene = {row["scene"]: row["persona_line"] for row in haruto}

    # emoji：never → 一个都不该出现；often → 至少出现一次
    assert not any(any(ch in row["persona_line"] for ch in EMOJI_CHARS) for row in adrian)
    assert any(any(ch in row["persona_line"] for ch in EMOJI_CHARS) for row in camila)

    # 句长：long 的人明显比 one_line 的人长
    assert len(camila_by_scene["life_share"]) > len(haruto_by_scene["life_share"])

    # 幽默 / 分歧方式改变主句（英文人物）
    assert "masterpiece" in callum_by_scene["joke"]
    assert "doing its best" in adrian_by_scene["joke"]
    # 非英语人物的正文必须是母语（详见下方语言测试）
    assert re.search(r"[\u3040-\u30ff]", haruto_by_scene["disagree"])


# --------------------------------------------------------------------------- #
# example_bank 多语言兜底正文（8 种非英语语言 × 12 场景）
# --------------------------------------------------------------------------- #
#: 日/韩/中必须出现该文字的字符
NATIVE_SCRIPT = {
    "ja": re.compile(r"[\u3040-\u30ff]"),
    "ko": re.compile(r"[\uac00-\ud7af]"),
    "zh": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]"),
}

#: 拉丁语系语言的特征词（只要至少命中一个就说明不是整段英文）
LATIN_MARKERS = {
    "pt": (
        "tudo", "você", "hoje", "notícia", "nossa", "não", "gente", "depende",
        "sinceramente", "oi", "dormir", "tempo", "correria", "passei", "valeu",
    ),
    "es": (
        "bien", "trabajo", "hoy", "qué", "día", "lunes", "acuerdo", "vaya",
        "depende", "idea", "hola", "dormir", "cuánto", "poco", "alegro",
    ),
    "de": (
        "gut", "arbeit", "heute", "freut", "ja", "langer", "montag", "anders",
        "klingt", "kommt", "keine", "hey", "schlaf", "lange", "bestes",
    ),
    "fr": (
        "ça", "toi", "aujourd", "boulot", "génial", "journée", "lundi", "accord",
        "air", "dépend", "idée", "salut", "dormir", "longtemps", "comment",
    ),
    "it": (
        "bene", "cucina", "oggi", "bello", "giornata", "lunedì", "d'accordo",
        "settimana", "dipende", "sinceramente", "ciao", "dormire", "quanto",
        "capisco",
    ),
}


def _synthetic_persona(persona_id: str, language: str) -> dict:
    return {
        "id": persona_id,
        "nickname": "Test",
        "language": language,
        "city": "Somewhere",
        "occupation": "designer",
        "interests": ["coffee", "books"],
        "personality": "Calm and curious.",
        "ai_config": {"temperament": "calm", "energy": 0.5},
    }


@pytest.mark.parametrize("language", ["en", "ja", "ko", "pt", "es", "de", "fr", "it", "zh"])
def test_example_bank_language_field_matches_body_language(language):
    persona = _synthetic_persona(f"p_{language}", language)
    bank = sd.example_bank(persona, sd.derive_dna(persona))
    assert [row["scene"] for row in bank] == list(sd.SCENES)
    assert all(row["language"] == language for row in bank)
    assert all(row["persona_line"].strip() for row in bank)
    if language in NATIVE_SCRIPT:
        for row in bank:
            assert NATIVE_SCRIPT[language].search(row["persona_line"]), (row["scene"], row["persona_line"])


@pytest.mark.parametrize(
    ("persona_id", "language"),
    [("p_haruto", "ja"), ("p_jiwoo", "ko"), ("p_lin", "zh")],
)
def test_cjk_seed_personas_have_native_examples(persona_id, language):
    persona = _by_id(persona_id)
    bank = sd.example_bank(persona, persona["speech_dna"])
    assert all(row["language"] == language for row in bank)
    for row in bank:
        assert NATIVE_SCRIPT[language].search(row["persona_line"]), (row["scene"], row["persona_line"])


@pytest.mark.parametrize(
    ("persona_id", "language"),
    [("p_camila", "pt"), ("p_ines", "es"), ("p_giulia", "it"), ("p_morten", "de")],
)
def test_latin_seed_personas_have_own_language_examples(persona_id, language):
    persona = _by_id(persona_id)
    bank = sd.example_bank(persona, persona["speech_dna"])
    assert all(row["language"] == language for row in bank)
    markers = LATIN_MARKERS[language]
    for row in bank:
        line = row["persona_line"].lower()
        assert any(marker in line for marker in markers), (row["scene"], row["persona_line"])


def test_french_fallback_bank_is_french():
    persona = _synthetic_persona("p_fr", "fr")
    bank = sd.example_bank(persona, sd.derive_dna(persona))
    assert all(row["language"] == "fr" for row in bank)
    markers = LATIN_MARKERS["fr"]
    for row in bank:
        assert any(marker in row["persona_line"].lower() for marker in markers), row["persona_line"]


@pytest.mark.parametrize("persona_id", ["p_jiwoo", "p_haruto", "p_camila", "p_ines"])
def test_locale_bank_body_differs_from_english_bank(persona_id):
    persona = _by_id(persona_id)
    native = {row["scene"]: row["persona_line"] for row in sd.example_bank(persona, persona["speech_dna"])}
    english = {
        row["scene"]: row["persona_line"]
        for row in sd.example_bank({**persona, "language": "en"}, persona["speech_dna"])
    }
    assert all(native[scene] != english[scene] for scene in sd.SCENES)
    assert all(row["language"] == "en" for row in sd.example_bank({**persona, "language": "en"}, persona["speech_dna"]))


def test_example_bank_unknown_language_falls_back_to_english():
    persona = dict(_by_id("p_adrian"), language="ru")
    bank = sd.example_bank(persona, persona["speech_dna"])
    assert all(row["language"] == "en" for row in bank)
    assert "busy" in bank[0]["persona_line"].lower()

    empty_language = {**_by_id("p_adrian"), "language": ""}
    assert all(row["language"] == "en" for row in sd.example_bank(empty_language, empty_language["speech_dna"]))


def test_example_bank_is_deterministic_across_languages():
    for persona in PERSONAS:
        first = sd.example_bank(persona, persona["speech_dna"])
        second = sd.example_bank(persona, persona["speech_dna"])
        assert first == second, persona["id"]
    for language in ("ja", "ko", "pt", "es", "de", "fr", "it", "zh"):
        persona = _synthetic_persona(f"p_{language}", language)
        assert sd.example_bank(persona, sd.derive_dna(persona)) == sd.example_bank(
            persona, sd.derive_dna(persona)
        )


# --------------------------------------------------------------------------- #
# pick_examples / example_block
# --------------------------------------------------------------------------- #
def _example_row(scene, user_line, persona_line, ordinal, language="en"):
    return {
        "scene": scene,
        "user_line": user_line,
        "persona_line": persona_line,
        "note": "",
        "language": language,
        "ordinal": ordinal,
    }


def test_pick_examples_prefers_same_scene_and_caps_k():
    rows = [
        _example_row("small_talk", "coffee please", "sure", 0),
        _example_row("joke", "tell me a joke", "no", 1),
        _example_row("joke", "another joke", "still no", 2),
        _example_row("comfort", "I am sad", "oh no", 3),
    ]
    picked = sd.pick_examples(rows, scene="joke", k=2)
    assert len(picked) == 2
    assert all(row["scene"] == "joke" for row in picked)
    assert len(sd.pick_examples(rows, scene="joke", k=99)) == 4
    assert sd.pick_examples(rows, scene="joke", k=0) == []
    assert sd.pick_examples([], scene="joke", k=2) == []


def test_pick_examples_uses_context_overlap():
    rows = [
        _example_row("life_share", "I went running this morning", "nice, I ran too", 0),
        _example_row("life_share", "I made coffee today", "I love coffee", 1),
    ]
    picked = sd.pick_examples(rows, scene="life_share", context_text="coffee coffee", k=1)
    assert picked[0]["ordinal"] == 1


def test_pick_examples_rng_is_reproducible():
    rows = [_example_row("small_talk", f"line {i}", f"reply {i}", i) for i in range(6)]
    first = sd.pick_examples(rows, scene="small_talk", k=3, rng=random.Random(7))
    second = sd.pick_examples(rows, scene="small_talk", k=3, rng=random.Random(7))
    assert first == second
    # 不传 rng 时按 ordinal 稳定排序
    plain = sd.pick_examples(rows, scene="small_talk", k=2)
    assert [row["ordinal"] for row in plain] == [0, 1]


def test_example_block_shape_and_language_priority():
    assert sd.example_block([], language="en") == ""

    rows = [
        _example_row("small_talk", "hello there", "english reply", 0, language="en"),
        _example_row("small_talk", "안녕", "korean reply", 1, language="ko"),
    ]
    block = sd.example_block(rows, language="ko")
    assert "imitate the VOICE, never copy the lines" in block
    assert block.index("korean reply") < block.index("english reply")

    short = sd.example_block(rows, language="en", max_chars=80)
    assert len(short) <= 80
    assert short  # 至少保留开头指令（截断后不为空）


# --------------------------------------------------------------------------- #
# dna_from_persona_row / load / save
# --------------------------------------------------------------------------- #
def test_dna_from_persona_row(seeded_db):
    row = seeded_db.conn.execute("SELECT * FROM personas WHERE id='p_adrian'").fetchone()
    dna = sd.dna_from_persona_row(row)
    assert dna == sd.normalize_dna(_by_id("p_adrian")["speech_dna"])

    bad = sd.dna_from_persona_row({"id": "p_x", "speech_dna": "not json", "personality": "dry"})
    assert bad == sd.derive_dna({"id": "p_x", "personality": "dry"})

    good = sd.dna_from_persona_row(
        {"id": "p_adrian", "speech_dna": json.dumps(_by_id("p_adrian")["speech_dna"])}
    )
    assert good["humor_style"] == "dry"


def test_save_examples_replaces_as_a_whole(seeded_db):
    persona = _by_id("p_adrian")
    rows = sd.example_bank(persona, persona["speech_dna"])
    assert sd.save_examples(seeded_db, persona["id"], rows) == 12

    loaded = sd.load_examples(seeded_db, persona["id"])
    assert [row["scene"] for row in loaded] == list(sd.SCENES)
    assert all(row["persona_id"] == persona["id"] for row in loaded)

    # 整体替换：再写 1 条后只剩 1 条
    assert sd.save_examples(seeded_db, persona["id"], rows[:1]) == 1
    assert len(sd.load_examples(seeded_db, persona["id"])) == 1

    assert sd.save_examples(seeded_db, persona["id"], []) == 0
    assert sd.load_examples(seeded_db, persona["id"]) == []
    assert sd.load_examples(seeded_db, "") == []
    assert sd.save_examples(seeded_db, "", rows) == 0


# --------------------------------------------------------------------------- #
# ensure_persona_dna
# --------------------------------------------------------------------------- #
async def test_ensure_persona_dna_fills_then_is_idempotent(seeded_db):
    first = await sd.ensure_persona_dna(seeded_db, None)
    total = _count(seeded_db, "persona_example_dialogs")
    assert total == len(PERSONAS) * len(sd.SCENES) == 144
    assert first["examples"] == 144
    assert first["llm"] is False
    # seed 语料在 ensure_schema 时已写入 speech_dna：这里只是「解析确认」，无需重写。
    # 返回的 dna 计数语义是「本轮解析/确认了 DNA 的人物数」（跳过的不算）。
    assert first["dna"] == len(PERSONAS)

    for persona in PERSONAS:
        assert len(sd.load_examples(seeded_db, persona["id"])) == 12
        row = seeded_db.conn.execute("SELECT speech_dna FROM personas WHERE id=?", (persona["id"],)).fetchone()
        assert sd._is_valid_dna(row["speech_dna"])

    # 写进 DB 的示例也必须「language 与正文一致」：韩语人物不能存英文正文
    ko_rows = sd.load_examples(seeded_db, "p_jiwoo")
    assert all(row["language"] == "ko" for row in ko_rows)
    assert all(NATIVE_SCRIPT["ko"].search(row["persona_line"]) for row in ko_rows)

    second = await sd.ensure_persona_dna(seeded_db, None)
    assert second == {"dna": 0, "examples": 0, "llm": False}
    assert _count(seeded_db, "persona_example_dialogs") == 144


async def test_ensure_persona_dna_force_rewrites(seeded_db):
    await sd.ensure_persona_dna(seeded_db, None)
    again = await sd.ensure_persona_dna(seeded_db, None, force=True)
    assert again["dna"] == len(PERSONAS)
    assert again["examples"] == 144
    assert _count(seeded_db, "persona_example_dialogs") == 144


async def test_ensure_persona_dna_regenerates_missing_rows(seeded_db):
    await sd.ensure_persona_dna(seeded_db, None)
    with seeded_db._lock, seeded_db.conn:  # noqa: SLF001 - 模拟老数据/损坏
        seeded_db.conn.execute(
            "DELETE FROM persona_example_dialogs WHERE persona_id=?", ("p_adrian",)
        )
        seeded_db.conn.execute(
            "UPDATE personas SET speech_dna='{}' WHERE id=?", ("p_adrian",)
        )

    summary = await sd.ensure_persona_dna(seeded_db, None)
    assert summary["dna"] == 1
    assert summary["examples"] == 12
    assert len(sd.load_examples(seeded_db, "p_adrian")) == 12
    assert _count(seeded_db, "persona_example_dialogs") == 144
    row = seeded_db.conn.execute("SELECT speech_dna FROM personas WHERE id='p_adrian'").fetchone()
    assert sd.dna_from_persona_row(row) == sd.normalize_dna(_by_id("p_adrian")["speech_dna"])


async def test_ensure_persona_dna_with_explicit_persona_list(seeded_db):
    summary = await sd.ensure_persona_dna(seeded_db, None, PERSONAS)
    assert summary["examples"] == 144
    assert _count(seeded_db, "persona_example_dialogs") == 144


async def test_ensure_persona_dna_mock_llm_falls_back(seeded_db):
    llm = SimpleNamespace(uses_mock=True)
    summary = await sd.ensure_persona_dna(seeded_db, llm)
    assert summary["llm"] is False
    assert summary["examples"] == 144
    assert all(len(sd.load_examples(seeded_db, p["id"])) == 12 for p in PERSONAS)


class _BoomLLM:
    uses_mock = False

    async def complete_json(self, messages, *, schema_hint="", temperature=None, max_tokens=None):
        raise RuntimeError("network down")


class _GoodLLM:
    uses_mock = False

    def __init__(self, payload: str | None = None, delay: float = 0.0):
        self.payload = payload if payload is not None else _llm_payload()
        self.delay = delay
        self.calls = 0

    async def complete_json(self, messages, *, schema_hint="", temperature=None, max_tokens=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.payload


def _llm_payload() -> str:
    return json.dumps(
        {
            "examples": [
                {
                    "scene": scene,
                    "user_line": f"user asks about {scene}",
                    "persona_line": f"LLM-{scene}",
                    "note": "from llm",
                }
                for scene in sd.SCENES
            ]
        }
    )


async def test_ensure_persona_dna_llm_failure_falls_back(seeded_db):
    summary = await sd.ensure_persona_dna(seeded_db, _BoomLLM())
    assert summary["llm"] is False
    assert summary["examples"] == 144
    assert _count(seeded_db, "persona_example_dialogs") == 144


async def test_ensure_persona_dna_llm_success_is_used(seeded_db):
    llm = _GoodLLM()
    summary = await sd.ensure_persona_dna(seeded_db, llm)
    assert summary["llm"] is True
    assert summary["examples"] == 144
    assert llm.calls == len(PERSONAS)  # 每个 persona 一次调用

    loaded = sd.load_examples(seeded_db, "p_adrian")
    assert [row["persona_line"] for row in loaded] == [f"LLM-{scene}" for scene in sd.SCENES]
    assert all(row["language"] == "en" for row in loaded)


async def test_ensure_persona_dna_llm_invalid_json_falls_back(seeded_db):
    summary = await sd.ensure_persona_dna(seeded_db, _GoodLLM(payload="this is not json"))
    assert summary["llm"] is False
    assert summary["examples"] == 144


async def test_ensure_persona_dna_llm_timeout_falls_back(seeded_db, monkeypatch):
    monkeypatch.setattr(sd, "_llm_timeout_s", lambda: 0.01)
    llm = _GoodLLM(delay=0.2)
    summary = await sd.ensure_persona_dna(seeded_db, llm)
    assert summary["llm"] is False
    assert summary["examples"] == 144


async def test_ensure_persona_dna_use_llm_false_skips_llm(seeded_db):
    llm = _GoodLLM()
    summary = await sd.ensure_persona_dna(seeded_db, llm, use_llm=False)
    assert summary["llm"] is False
    assert llm.calls == 0
    assert summary["examples"] == 144


async def test_ensure_persona_dna_writes_dna_version(seeded_db):
    await sd.ensure_persona_dna(seeded_db, None)
    row = seeded_db.conn.execute(
        "SELECT value FROM v3_meta WHERE key='persona_dna_version'"
    ).fetchone()
    assert row is not None and row["value"] == sd.SPEECH_DNA_VERSION


async def test_ensure_persona_dna_never_raises_on_broken_db():
    class _BrokenDB:
        def __init__(self) -> None:
            self._lock = threading.RLock()

        @property
        def conn(self):
            raise RuntimeError("db not initialised")

    summary = await sd.ensure_persona_dna(_BrokenDB(), None)  # type: ignore[arg-type]
    assert summary == {"dna": 0, "examples": 0, "llm": False}


async def test_ensure_persona_dna_never_raises_on_broken_llm(seeded_db):
    class _BrokenCompleteLLM:
        uses_mock = False

        async def complete_json(self, *args, **kwargs):
            raise ValueError("bad payload")

    summary = await sd.ensure_persona_dna(seeded_db, _BrokenCompleteLLM())
    assert summary["llm"] is False
    assert summary["examples"] == 144


# --------------------------------------------------------------------------- #
# 阶段四：口音下线 + Fish 音色库一人一色
# --------------------------------------------------------------------------- #
#: 假 Fish 音色池（真调 Fish 会因本机网络不可达失败，测试一律注入假池）
FAKE_VOICE_POOL = [
    {"id": "fish-en-f1", "languages": ["en"], "tags": ["female"]},
    {"id": "fish-en-m1", "languages": ["en"], "tags": ["male"]},
    {"id": "fish-ko-f1", "languages": ["ko"], "tags": ["female"]},
    {"id": "fish-ko-m1", "languages": ["ko"], "tags": ["male"]},
    {"id": "fish-any-f1", "languages": [], "tags": ["female"]},
    {"id": "fish-any-m1", "languages": [], "tags": ["male"]},
    {"id": "fish-neutral", "languages": [], "tags": []},
]

#: 阶段四修复前的「全局默认参考音色」：不该被当成某个角色的专属音色
GLOBAL_DEFAULT_REF = "e3cd384158934cc9a01029cd7d278634"


@pytest.fixture()
def voice_db(tmp_path):
    db = DB(str(tmp_path / "voice.db"))
    db.init()
    ensure_schema(db, seed=False)
    yield db
    db.close()


def _insert_persona(db: DB, pid: str, *, language: str = "en", gender: str = "female", fish: str = "") -> None:
    with db._lock, db.conn:  # noqa: SLF001 - 测试里直接造数据
        db.conn.execute(
            "INSERT INTO personas (id, nickname, language, gender, fish_voice_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (pid, pid, language, gender, fish),
        )


def _insert_friend(db: DB, friend_id: str, *, persona_id: str = "", fish: str = "") -> None:
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO friends (id, name, persona_id, fish_voice_id) VALUES (?, ?, ?, ?)",
            (friend_id, friend_id, persona_id, fish),
        )


def _link_friendship(db: DB, user_id: str, persona_id: str, friend_id: str) -> None:
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO friendships (id, user_id, persona_id, status, friend_id, created_at, updated_at)"
            " VALUES (?, ?, ?, 'accepted', ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (f"fr_{user_id}_{persona_id}", user_id, persona_id, friend_id),
        )


def _voices(db: DB) -> dict[str, str]:
    with db._lock:  # noqa: SLF001
        return {
            str(row["id"]): str(row["fish_voice_id"] or "")
            for row in db.conn.execute("SELECT id, fish_voice_id FROM personas").fetchall()
        }


def _friend_voices(db: DB) -> dict[str, str]:
    with db._lock:  # noqa: SLF001
        return {
            str(row["id"]): str(row["fish_voice_id"] or "")
            for row in db.conn.execute("SELECT id, fish_voice_id FROM friends").fetchall()
        }


async def test_ensure_persona_voices_unique_and_language_gender_priority(voice_db):
    _insert_persona(voice_db, "p_en_f", language="en", gender="female")
    _insert_persona(voice_db, "p_en_m", language="en", gender="male")
    _insert_persona(voice_db, "p_ko_f", language="ko", gender="female")
    _insert_persona(voice_db, "p_ko_m", language="ko", gender="male")
    _insert_persona(voice_db, "p_ru_f", language="ru", gender="female")
    _insert_persona(voice_db, "p_ru_m", language="ru", gender="male")

    assigned = await ensure_persona_voices(voice_db, limit=20, pool=FAKE_VOICE_POOL)
    assert assigned == 6

    voices = _voices(voice_db)
    assert all(voices.values())
    assert len(set(voices.values())) == 6  # 一人一色，绝无重复

    # 优先级：同语言+同性别 → 同性别 → 任意
    assert voices["p_en_f"] == "fish-en-f1"
    assert voices["p_en_m"] == "fish-en-m1"
    assert voices["p_ko_f"] == "fish-ko-f1"
    assert voices["p_ko_m"] == "fish-ko-m1"
    assert voices["p_ru_f"] == "fish-any-f1"
    assert voices["p_ru_m"] == "fish-any-m1"


async def test_ensure_persona_voices_is_idempotent(voice_db):
    _insert_persona(voice_db, "p_a", language="en", gender="female")
    _insert_persona(voice_db, "p_b", language="en", gender="male")

    assert await ensure_persona_voices(voice_db, limit=10, pool=FAKE_VOICE_POOL) == 2
    first = _voices(voice_db)
    # 再跑一次：0 写入，映射完全不变
    assert await ensure_persona_voices(voice_db, limit=10, pool=FAKE_VOICE_POOL) == 0
    assert _voices(voice_db) == first
    # limit=0 不分配
    _insert_persona(voice_db, "p_c", language="ko", gender="female")
    assert await ensure_persona_voices(voice_db, limit=0, pool=FAKE_VOICE_POOL) == 0
    assert _voices(voice_db)["p_c"] == ""


async def test_ensure_persona_voices_pool_unavailable_returns_zero(voice_db, monkeypatch):
    _insert_persona(voice_db, "p_a")
    _insert_persona(voice_db, "p_b")
    # 空池
    assert await ensure_persona_voices(voice_db, pool=[]) == 0
    assert set(_voices(voice_db).values()) == {""}

    # 真实路径：Fish 网络不可达 → 安全跳过、不抛异常
    from app.voice import fish_tts

    async def _boom(**kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(fish_tts, "list_models", _boom)
    assert await ensure_persona_voices(voice_db) == 0
    assert set(_voices(voice_db).values()) == {""}


async def test_ensure_persona_voices_repairs_duplicate_ids(voice_db):
    _insert_persona(voice_db, "p_1", fish="dup-ref")
    _insert_persona(voice_db, "p_2", fish="dup-ref")
    _insert_persona(voice_db, "p_3", fish="dup-ref")

    assigned = await ensure_persona_voices(voice_db, limit=10, pool=FAKE_VOICE_POOL)
    assert assigned == 2  # 先出现的保留，后来重复的两个被重新分配

    voices = _voices(voice_db)
    assert voices["p_1"] == "dup-ref"
    assert len(set(voices.values())) == 3  # 不再有两人共用一色


async def test_ensure_persona_voices_keeps_existing_values(voice_db):
    _insert_persona(voice_db, "p_keep", fish="my-unique-voice")
    _insert_persona(voice_db, "p_new")
    assert await ensure_persona_voices(voice_db, limit=10, pool=FAKE_VOICE_POOL) == 1
    voices = _voices(voice_db)
    assert voices["p_keep"] == "my-unique-voice"  # 已有值不覆盖
    assert voices["p_new"] != "my-unique-voice"


async def test_ensure_persona_voices_reassigns_global_default_reference(voice_db):
    _insert_persona(voice_db, "p_a", fish=GLOBAL_DEFAULT_REF)
    assigned = await ensure_persona_voices(
        voice_db, limit=10, pool=FAKE_VOICE_POOL, invalid_refs=(GLOBAL_DEFAULT_REF,)
    )
    assert assigned == 1
    assert _voices(voice_db)["p_a"] != GLOBAL_DEFAULT_REF
    assert _voices(voice_db)["p_a"]


async def test_ensure_persona_voices_pool_exhaustion_never_duplicates(voice_db):
    _insert_persona(voice_db, "p_a", language="en", gender="female")
    _insert_persona(voice_db, "p_b", language="en", gender="female")
    _insert_persona(voice_db, "p_c", language="en", gender="female")
    tiny_pool = [
        {"id": "fish-1", "languages": ["en"], "tags": ["female"]},
        {"id": "fish-2", "languages": ["en"], "tags": ["female"]},
    ]
    assert await ensure_persona_voices(voice_db, limit=10, pool=tiny_pool) == 2
    voices = _voices(voice_db)
    assert len([v for v in voices.values() if v]) == 2
    assert len(set(voices.values()) - {""}) == 2  # 池子不够时宁可留空也不重复


async def test_ensure_persona_voices_syncs_friends_rows(voice_db):
    _insert_persona(voice_db, "p_1", language="en", gender="female")
    _insert_persona(voice_db, "p_2", language="en", gender="male")
    # 两种历史链接方式都要同步：
    # 1) friends.persona_id 直连
    _insert_friend(voice_db, "f_direct", persona_id="p_1")
    # 2) friendships.friend_id 映射（V2 好友行不一定有 persona_id）
    _insert_friend(voice_db, "f_mapped")
    _link_friendship(voice_db, "u1", "p_2", "f_mapped")
    # 无关好友不能被误改
    _insert_friend(voice_db, "f_other")

    assert await ensure_persona_voices(voice_db, limit=10, pool=FAKE_VOICE_POOL) == 2

    personas = _voices(voice_db)
    friends = _friend_voices(voice_db)
    assert friends["f_direct"] == personas["p_1"]
    assert friends["f_mapped"] == personas["p_2"]
    assert friends["f_other"] == ""
    assert friends["f_direct"] and friends["f_mapped"]


async def test_ensure_persona_voices_syncs_stale_friend_rows(voice_db):
    """即使本轮没有新分配（音色已合法），陈旧的 friends 快照也要对齐。"""
    _insert_persona(voice_db, "p_1", language="en", gender="female", fish="keep-voice")
    _insert_friend(voice_db, "f_direct", persona_id="p_1", fish="stale-voice")
    _insert_friend(voice_db, "f_mapped", fish="stale-voice")
    _link_friendship(voice_db, "u1", "p_1", "f_mapped")

    assigned = await ensure_persona_voices(voice_db, limit=10, pool=FAKE_VOICE_POOL)
    assert assigned == 0  # 已有值不覆盖
    friends = _friend_voices(voice_db)
    assert friends["f_direct"] == "keep-voice"
    assert friends["f_mapped"] == "keep-voice"


async def test_ensure_persona_voices_never_raises_on_broken_db():
    class _BrokenDB:
        def __init__(self) -> None:
            self._lock = threading.RLock()

        @property
        def conn(self):
            raise RuntimeError("db not initialised")

    assert await ensure_persona_voices(_BrokenDB(), pool=FAKE_VOICE_POOL) == 0  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 口音系统下线（后端）
# --------------------------------------------------------------------------- #
def test_accent_market_module_is_deprecated():
    from app.voice import accent_market as am

    assert am.ACCENT_MARKET_DEPRECATED is True
    assert am.DEPRECATION_MESSAGE
    # 旧函数仍可读（老库/降级路径不炸），只是不再用于给角色挑声线
    assert am.get_accent("en-GB-RyanNeural") is not None


def test_market_accent_routes_are_marked_deprecated():
    from app.routers import market_api

    deprecated = {
        "/api/market/summary",
        "/api/market/regions",
        "/api/market/accents",
        "/api/market/preview",
        "/api/market/assign",
    }
    seen = set()
    for route in market_api.router.routes:
        path = getattr(route, "path", "")
        if path in deprecated:
            seen.add(path)
            assert route.deprecated is True, path
    assert seen == deprecated


async def test_market_assign_endpoint_returns_410():
    from fastapi import HTTPException

    from app.routers.market_api import AssignIn, market_assign

    with pytest.raises(HTTPException) as exc:
        await market_assign(AssignIn(persona_id="p_adrian", voice="en-GB-RyanNeural"))
    assert exc.value.status_code == 410


def test_custom_store_no_longer_uses_accent_market(tmp_path):
    store = CustomStore(str(tmp_path))
    # 空 voice → 只按性别给 Edge 降级声线，不再查口音市场
    cp = store.upsert_persona({"name": "Ava", "gender": "female", "voice": ""})
    public = cp.to_public()
    assert public["voice"] == "en-US-AvaMultilingualNeural"
    assert public["accent_label"] == ""
    assert public["flag"] == ""
    assert public["accent_kind"] == ""
    assert public["voice_mode"] == "fish"


async def test_custom_persona_api_no_longer_validates_voice_by_accent(tmp_path):
    from app.routers.market_api import CustomPersonaIn, upsert_custom_persona

    custom = CustomStore(str(tmp_path))
    lib = SimpleNamespace(custom=custom, reload_custom=lambda: None)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(engines=SimpleNamespace(personas=lib)))
    )
    # 以前：不属于口音目录的声线会 400；现在直接接受（voice 只作为 Edge 降级）
    result = await upsert_custom_persona(
        request, CustomPersonaIn(name="Test", gender="male", voice="totally-unknown-voice")
    )
    assert result["ok"] is True
    assert result["persona"]["voice"] == "totally-unknown-voice"
    assert result["persona"]["accent_label"] == ""

