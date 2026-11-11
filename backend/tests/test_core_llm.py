"""LLM 客户端、mock 引擎、润色建议、角色库测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.llm.client import ChatMessage, LLMClient, LLMConfigError, LLMError
from app.config import LLMSettings


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 运行时配置：base_url 规范化（只填域名要自动补 /v1）
# --------------------------------------------------------------------------- #
def test_runtime_config_normalizes_base_url():
    from app.llm.runtime_config import LLMRuntimeConfig

    cases = {
        "https://note3-prev-api.askdiandian.com": "https://note3-prev-api.askdiandian.com/v1",
        "note3-prev-api.askdiandian.com/": "https://note3-prev-api.askdiandian.com/v1",
        "http://127.0.0.1:8000": "http://127.0.0.1:8000/v1",
        "https://api.deepseek.com/v1/": "https://api.deepseek.com/v1",
        "https://api.deepseek.com/v1/chat/completions": "https://api.deepseek.com/v1",
        "": "",
    }
    for raw, expected in cases.items():
        assert LLMRuntimeConfig(base_url=raw, model="m").base_url == expected, raw


def test_runtime_config_strips_pasted_whitespace():
    from app.llm.runtime_config import LLMRuntimeConfig

    cfg = LLMRuntimeConfig(base_url=" https://x.test/v1 ", api_key=" ak_x \n", model=" dots ")
    assert cfg.base_url == "https://x.test/v1"
    assert cfg.api_key == "ak_x"
    assert cfg.model == "dots"


# --------------------------------------------------------------------------- #
# LLMClient（无 key → mock）
# --------------------------------------------------------------------------- #
def test_client_without_key_reports_unconfigured():
    client = LLMClient(LLMSettings(api_key="", mock_when_missing_key=True))
    h = client.health()
    assert h["configured"] is False
    assert "model" in h
    assert isinstance(h, dict)


def test_client_without_key_streams_mock():
    client = LLMClient(LLMSettings(api_key="", mock_when_missing_key=True))

    async def go():
        out = []
        async for t in client.stream_chat([ChatMessage("user", "Could I get a flat white?")]):
            out.append(t)
        await client.aclose()
        return out

    parts = run(go())
    assert parts, "无 key 时必须产出 mock 流"
    text = "".join(parts)
    assert len(text) > 5


def test_client_configured_with_key():
    client = LLMClient(LLMSettings(api_key="sk-fake-for-test"))
    assert client.health()["configured"] is True


def test_client_health_never_raises():
    for cfg in (LLMSettings(), LLMSettings(api_key="x"), LLMSettings(base_url="")):
        assert isinstance(LLMClient(cfg).health(), dict)


# --------------------------------------------------------------------------- #
# mock_llm
# --------------------------------------------------------------------------- #
def test_mock_stream_produces_words():
    from app.llm.mock_llm import mock_stream_chat

    async def go():
        return [t async for t in mock_stream_chat([ChatMessage("user", "Hello there")])]

    parts = run(go())
    assert parts
    assert "".join(parts).strip()


def test_mock_reply_short_enough():
    from app.llm.mock_llm import compose_reply

    for persona in ("oliver", "raj", "marco", "emily", "priya"):
        system = ChatMessage("system", f"You are {persona}. Speak English only.")
        reply = compose_reply([system, ChatMessage("user", "What do you think about my plan?")])
        assert reply
        words = reply.split()
        assert len(words) <= 40, f"{persona} 回复 {len(words)} 词过长: {reply!r}"


def test_mock_reply_uses_persona_catchphrase():
    from app.llm.mock_llm import compose_reply

    cases = {
        "oliver": "Right then",
        "raj": "Actually",
        "marco": "Allora",
        "emily": "Okay so",
        "priya": "Can, can",
    }
    for persona, phrase in cases.items():
        system = ChatMessage("system", f"You are {persona}.")
        reply = compose_reply([system, ChatMessage("user", "Hi")])
        assert phrase.lower() in reply.lower(), f"{persona} 缺少口头禅 {phrase!r}: {reply!r}"


def test_mock_different_topics_give_different_replies():
    from app.llm.mock_llm import compose_reply

    system = ChatMessage("system", "You are oliver.")
    coffee = compose_reply([system, ChatMessage("user", "Could I get a flat white coffee?")])
    lunch = compose_reply([system, ChatMessage("user", "What do you recommend for a quick lunch?")])
    bye = compose_reply([system, ChatMessage("user", "Anyway I need to go now, bye!")])
    assert len({coffee, lunch, bye}) == 3, f"不同话题应得到不同回复: {coffee!r} / {lunch!r} / {bye!r}"


def test_mock_avoids_repeating_itself():
    """连续相似输入不应复读同一句（拼上历史说话后应不同）。"""
    from app.llm.mock_llm import compose_reply

    system = ChatMessage("system", "You are oliver.")
    first = compose_reply([system, ChatMessage("user", "What do you recommend?")])
    hist = [
        system,
        ChatMessage("user", "What do you recommend?"),
        ChatMessage("assistant", first),
        ChatMessage("user", "What do you recommend?"),
    ]
    second = compose_reply(hist)
    assert second != first, "重复提问时应换一句，而不是逐字复读"


def test_mock_chinese_input_still_english():
    from app.llm.mock_llm import compose_reply

    reply = compose_reply([
        ChatMessage("system", "You are oliver."),
        ChatMessage("user", "这个咖啡怎么点？"),
    ])
    # 不应包含中文字符（角色只说英文）
    assert not any("\u4e00" <= c <= "\u9fff" for c in reply), f"回复含中文: {reply!r}"


def test_mock_reply_no_markdown():
    from app.llm.mock_llm import compose_reply

    for persona in ("oliver", "raj", "marco", "emily", "priya"):
        reply = compose_reply([
            ChatMessage("system", f"You are {persona}."),
            ChatMessage("user", "Tell me something."),
        ])
        assert "**" not in reply
        assert "*" not in reply
        assert "#" not in reply
        assert "`" not in reply


def test_mock_topic_detection():
    from app.llm.mock_llm import detect_topic

    assert detect_topic("Could I get a flat white?") == "coffee"
    assert detect_topic("The API deploy failed") == "api"
    assert detect_topic("What do you recommend for lunch?") == "food"
    assert detect_topic("Anyway, bye!") in ("farewell", "_default")
    assert detect_topic("xyzzy plugh") == "_default"


# --------------------------------------------------------------------------- #
# 润色建议（JSON 容错）
# --------------------------------------------------------------------------- #
def test_suggestion_parses_clean_json():
    from app.llm.suggestions import parse_suggestions

    raw = json.dumps([
        {"original": "I very like it", "better": "I really like it", "why": "very 修饰动词", "register": "neutral"}
    ])
    got = parse_suggestions(raw)
    assert len(got) == 1
    assert got[0].better == "I really like it"


def test_suggestion_parses_fenced_json():
    from app.llm.suggestions import parse_suggestions

    raw = '```json\n[{"original":"a","better":"b","why":"w","register":"casual"}]\n```'
    got = parse_suggestions(raw)
    assert len(got) == 1


def test_suggestion_parses_json_with_prose():
    from app.llm.suggestions import parse_suggestions

    raw = 'Sure! Here are suggestions:\n[{"original":"a","better":"b","why":"w"}]\nHope that helps.'
    got = parse_suggestions(raw)
    assert len(got) == 1


def test_suggestion_drops_invalid_entries():
    from app.llm.suggestions import parse_suggestions

    raw = json.dumps([
        {"original": "a", "better": "b", "why": "w"},
        {"original": "x"},  # 缺 better
        "not an object",
        {"original": "", "better": "b2", "why": "w2"},  # 空 original
    ])
    got = parse_suggestions(raw)
    assert all(s.original and s.better for s in got)
    assert len(got) <= 2


def test_suggestion_garbage_returns_empty():
    from app.llm.suggestions import parse_suggestions

    for bad in ("", "not json", "{", "null", "[]", "{}", "[1,2,3]"):
        assert parse_suggestions(bad) == [], f"{bad!r} 应返回空列表"


def test_suggestion_dict_wrapper():
    from app.llm.suggestions import parse_suggestions

    raw = json.dumps({"items": [{"original": "a", "better": "b", "why": "w"}]})
    got = parse_suggestions(raw)
    assert len(got) == 1


# --------------------------------------------------------------------------- #
# 角色库
# --------------------------------------------------------------------------- #
def test_personas_load(personas):
    assert len(personas.all()) >= 3
    ids = {p.id for p in personas.all()}
    assert {"oliver", "raj", "marco"} <= ids


def test_personas_required_fields(personas):
    for p in personas.all():
        assert p.id
        assert p.name
        assert p.system_prompt
        assert p.accent
        assert p.opening_line
        # 自定义角色（u_ 开头）用 Edge 声线说话，本来就没有参考音频；
        # 内置角色必须带参考音频（XTTS 零样本克隆用）。
        if not p.id.startswith("u_"):
            assert p.voice.ref_audio


def test_persona_public_hides_prompt(personas):
    pub = personas.get("oliver").public()
    assert "system_prompt" not in pub
    assert "id" in pub and "name" in pub
    # 必须可 JSON 序列化
    json.dumps(pub)


def test_persona_unknown_falls_back(personas):
    p = personas.get("nonexistent_persona")
    assert p.id == personas.default_id


def test_system_prompt_contains_global_rules(personas):
    sp = personas.system_prompt_for("oliver", "free")
    assert "spoken English" in sp.lower() or "output contract" in sp.lower()
    assert "No markdown" in sp or "no markdown" in sp.lower()


def test_system_prompt_includes_mode(personas):
    free = personas.system_prompt_for("oliver", "free")
    drill = personas.system_prompt_for("oliver", "drill")
    assert free != drill, "不同模式应产生不同 system prompt"


def test_modes_available(personas):
    modes = personas.modes()
    ids = {m.id for m in modes}
    assert {"free", "task", "drill"} <= ids


def test_personas_json_is_valid():
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "app" / "personas" / "personas.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "personas" in data
    for p in data["personas"]:
        assert p["id"] and p["system_prompt"]
