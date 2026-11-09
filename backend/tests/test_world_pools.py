"""阶段 7：把「捏人素材池」接进真实捏人（兜底取池 + LLM prompt 素材提示 + 预览）。

覆盖：
- 兜底捏人：名字/职业/城市/兴趣都来自词库；同一索引确定性；不同索引不重名；
- 池子为空：退回内置模板文案（不崩）；
- LLM 捏人：prompt 里带素材池（名字/职业/城市/兴趣），且保留"可自己衍生"的语义；
- 预览：不写库、不调模型，池子过小时给出警告。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.v2 import llm_json
from app.v3 import daily_world, lexicon


@pytest.fixture()
def lib_dir(tmp_path, monkeypatch):
    target = tmp_path / "lexicons"
    target.mkdir()
    monkeypatch.setattr(lexicon, "lexicon_dir", lambda: str(target))
    lexicon.refresh()
    yield target
    monkeypatch.undo()
    lexicon.refresh()


def _world_payload() -> dict:
    return {
        "version": 1,
        "names": {"en": ["Aiden", "Bella", "Caleb", "Dora", "Eli"], "ja": ["Haruto"]},
        "occupations": {"any": ["Barista", "Chef", "Illustrator", "Teacher"]},
        "cities": {"any": ["Hamburg", "Seattle", "Seoul", "Melbourne"]},
        "interests": {
            "any": ["coffee", "books", "running", "surfing", "tea", "jazz",
                    "cats", "travel", "study"]
        },
    }


def test_fallback_persona_uses_pools(lib_dir):
    lexicon.save("world", _world_payload())
    persona = daily_world._fallback_persona(0, "2026-11-08", "en")  # noqa: SLF001
    assert persona["nickname"] in {"Aiden", "Bella", "Caleb", "Dora", "Eli"}
    assert persona["occupation"] in {"Barista", "Chef", "Illustrator", "Teacher"}
    assert persona["city"] in {"Hamburg", "Seattle", "Seoul", "Melbourne"}
    assert set(persona["interests"]).issubset(_world_payload()["interests"]["any"])
    assert len(persona["interests"]) == 4


def test_fallback_persona_deterministic_and_distinct(lib_dir):
    lexicon.save("world", _world_payload())
    first = daily_world._fallback_persona(0, "2026-11-08", "en")  # noqa: SLF001
    again = daily_world._fallback_persona(0, "2026-11-08", "en")  # noqa: SLF001
    assert first == again
    names = {
        daily_world._fallback_persona(i, "2026-11-08", "en")["nickname"]  # noqa: SLF001
        for i in range(5)
    }
    assert len(names) == 5  # 池子里 5 个名字，5 个人物不重名


def test_empty_pool_falls_back_to_template(lib_dir):
    # 不保存 world.json = 池子为空（校验也会拒绝"全空"的词库），走内置文案
    assert lexicon.load("world") is None
    persona = daily_world._fallback_persona(0, "2026-11-08", "en")  # noqa: SLF001
    assert persona["nickname"]  # 内置模板名 + 后缀
    assert persona["occupation"]
    assert persona["interests"] == ["coffee", "books", "walking"]


def test_pool_hint_reaches_llm_prompt(lib_dir, monkeypatch):
    lexicon.save("world", _world_payload())
    captured: dict[str, str] = {}

    async def fake_complete(llm, messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return {
            "nickname": "Aiden",
            "gender": "male",
            "age": 28,
            "country": "Germany",
            "city": "Hamburg",
            "language": "en",
            "occupation": "Barista",
            "bio": "Coffee first, then everything else.",
            "personality": "curious, calm",
            "interests": ["coffee", "books", "running", "tea"],
            "posts": [
                {"content": "Morning shift done. The machine finally behaved itself.", "tags": []},
                {"content": "Walked home the long way because the light was nice.", "tags": []},
            ],
        }

    monkeypatch.setattr(llm_json, "complete_json_safe", fake_complete)
    llm = SimpleNamespace(uses_mock=False)
    data = __import__("asyncio").run(
        daily_world._llm_persona(llm, 0, "2026-11-08", language="en", level="日常对话")  # noqa: SLF001
    )
    assert data is not None and data["nickname"] == "Aiden"
    prompt = captured["prompt"]
    assert "Aiden" in prompt and "Barista" in prompt and "Hamburg" in prompt
    assert "coffee" in prompt
    assert "pick from these or invent close variants" in prompt


def test_preview_personas_no_writes_and_warns_small_pool(lib_dir):
    lexicon.save("world", _world_payload())
    preview = daily_world.preview_personas(count=3, language="en")
    assert preview["count"] == 3
    assert preview["pools"]["names"] == 5
    assert all(item["nickname"] for item in preview["personas"])
    assert preview["warnings"] == []  # 5 个名字够 3 个人

    small = daily_world.preview_personas(count=3, language="ja")
    assert any("名字池只有 1" in item for item in small["warnings"])
