"""开放图库 / 人设硬过滤 / 生活动态测试（不联网）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v3.media import MediaItem, credit_text, is_life_safe, queries_for  # noqa: E402


def test_fish_voice_binding_is_stable_and_configured_only_with_key(monkeypatch):
    """音色身份由统一解析器给出：同一角色 token 稳定；Fish 只在有 key 时算「已配置」。"""
    from types import SimpleNamespace

    from app.voice import voice_registry

    monkeypatch.setenv("SE_TTS_FISH", "1")
    fish = SimpleNamespace(
        provider="fish",
        fish_api_key="k",
        fish_base_url="https://api.fish.audio",
        fish_model="s2.1-pro",
        fish_reference_id="ref-1",
        fish_format="mp3",
    )
    assert voice_registry.fish_configured(fish) is True
    persona = {"id": "p1", "nickname": "X", "fish_voice_id": "ref-1",
               "voice_id": "en-GB-RyanNeural"}
    first = voice_registry.binding_for_persona(persona, fish)
    again = voice_registry.binding_for_persona(persona, fish)
    assert first.token == again.token
    assert first.token.startswith("fish-")
    assert len(first.token) == len("fish-") + 8

    no_key = SimpleNamespace(
        provider="fish", fish_api_key="", fish_base_url="", fish_model="",
        fish_reference_id="ref-1", fish_format="mp3",
    )
    assert voice_registry.fish_configured(no_key) is False

    # 没有 Fish 音色的人物：身份就是自己的 Edge 声线
    edge_persona = {"id": "p2", "nickname": "Y", "fish_voice_id": "", "voice_id": "en-GB-RyanNeural"}
    assert voice_registry.binding_for_persona(edge_persona, fish).token == "edge-en-GB-RyanNeural"


def _item(title: str, tags: list[str] | None = None) -> MediaItem:
    return MediaItem(
        external_id="openverse:x", source="Openverse", title=title,
        tags=tags or [], license="CC0", author="Ann", page_url="https://x",
    )


def test_filters_reject_people_and_feminine_for_male():
    male = {"gender": "male", "interests": ["coding"]}
    female = {"gender": "female", "interests": ["coffee"]}

    assert is_life_safe(_item("espresso machine on wooden table", ["coffee", "cafe"]), male)
    assert not is_life_safe(_item("portrait of a man at work", ["office"]), male)
    assert not is_life_safe(_item("woman in white dress", ["fashion"]), male)
    # 女性人设可以出现裙子（但仍不能是人物肖像）
    assert is_life_safe(_item("blue dress on a hanger", ["fashion"]), female)
    assert not is_life_safe(_item("woman in blue dress", ["fashion"]), female)
    # 敏感词
    assert not is_life_safe(_item("whiskey bottle on table", ["drink"]), male)


def test_queries_follow_interests_and_occupation():
    persona = {"interests": ["coffee", "cycling"], "occupation": "Sound engineer"}
    queries = queries_for(persona)
    assert any("espresso" in q or "coffee" in q for q in queries)
    assert any("bicycle" in q or "bike" in q for q in queries)
    fallback = queries_for({"interests": [], "occupation": ""})
    assert fallback  # 有兜底检索词


def test_credit_text_contains_author_and_license():
    text = credit_text(_item("x"))
    assert "Ann" in text and "CC0" in text
