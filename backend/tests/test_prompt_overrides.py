"""Prompt 模块改写的持久化：保存后必须真的进到 prompt 里。

这个文件存在的理由
------------------
「Prompt 控制台」最容易做成假功能：界面能改、能存，但真实对话用的还是原文。
所以这里不只测 CRUD，重点是**端到端**——存进 ``prompt_overrides`` 之后，
``compose_prompt`` 组装出来的模块文本必须变。

另外钉死两条设计边界：
1. **动态模块只读**：直接往表里塞 ``scenario`` 的改写也不会生效，
   因为加载时会按 ``editable`` 过滤。否则运营改坏"角色此刻在做什么"是查不出来的。
2. **整体替换语义**：保存时没出现的模块 = 取消改写。
"""

from __future__ import annotations

import json

import pytest

from app.v2.db import DB
from app.v3 import prompt_composer, prompt_overrides
from app.v3.schema import ensure_schema


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "prompt.db"))
    database.init()
    ensure_schema(database, seed=False)
    now = "2026-11-11T00:00:00"
    with database._lock, database.conn:  # noqa: SLF001
        database.conn.execute(
            "INSERT INTO personas (id, nickname, language, occupation, city, interests,"
            " speech_dna, bio, personality, avatar_seed, created_at, lifecycle)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "p_test",
                "小林",
                "zh",
                "设计师",
                "上海",
                json.dumps(["咖啡"], ensure_ascii=False),
                json.dumps({}, ensure_ascii=False),
                "一个做设计的人",
                "温和",
                "seed-1",
                now,
                "guest",
            ),
        )
    yield database
    database.close()


def _compose(database, mode: str = "text"):
    row = prompt_composer.get_persona_row(database, "p_test")
    friend = prompt_composer.friend_for_persona(row, "p_test")
    return prompt_composer.compose_prompt(database, friend, user_text="在吗", mode=mode)


def _module_text(composed, key: str) -> str:
    for module in composed.modules:
        if module.key == key:
            return module.text
    return ""


# --------------------------------------------------------------------------- #
# 端到端：存了就必须生效
# --------------------------------------------------------------------------- #


def test_saved_override_reaches_real_compose(db):
    """保存 character_card 改写后，compose_prompt 产出的文本必须变。"""
    before = _module_text(_compose(db), "character_card")
    assert before, "原始 character_card 不该为空，否则这条测试没意义"

    marker = "这个角色说话总是先叹气。"
    result = prompt_overrides.replace(db, "p_test", "text", {"character_card": marker})
    assert result["ok"] is True
    assert result["saved"] == 1

    after = _module_text(_compose(db), "character_card")
    assert after == marker, "compose_prompt 没有读到已保存的改写——这就是'假功能'"


def test_override_does_not_leak_into_other_modes(db):
    """给 text 模式存的改写，不能影响 call 模式。"""
    prompt_overrides.replace(db, "p_test", "text", {"character_card": "只对付文字模式"})
    call_text = _module_text(_compose(db, "call"), "character_card")
    assert call_text != "只对付文字模式"


def test_override_does_not_leak_into_other_personas(db):
    """改写是按角色存的，不能串到别人身上。"""
    prompt_overrides.replace(db, "p_test", "text", {"character_card": "小林的专属"})
    other = prompt_composer.friend_for_persona(
        {"id": "p_other", "nickname": "阿May", "language": "en"}, "p_other"
    )
    composed = prompt_composer.compose_prompt(db, other, user_text="hi", mode="text")
    assert _module_text(composed, "character_card") != "小林的专属"


def test_style_module_is_empty_until_written(db):
    """「角色文风」默认不注入任何东西。"""
    composed = _compose(db)
    style = composed.module("style")
    assert style is not None
    assert style.enabled is False
    assert style.text == ""


def test_style_override_supports_tavern_macros(db):
    """酒馆风格文本里的 {{char}} 要替换成角色名，而不是原样喂给模型。"""
    marker = "用短句叙述，{{char}} 说话慢，偶尔停顿。"
    prompt_overrides.replace(db, "p_test", "text", {"style": marker})

    composed = _compose(db)
    style = _module_text(composed, "style")
    assert "小林" in style
    assert "{{char}}" not in style
    assert "<bot>" not in style
    assert style.startswith("用短句叙述")


def test_style_override_is_mode_scoped(db):
    """文字模式写文风，不影响通话模式（除非通话也单独写）。"""
    prompt_overrides.replace(db, "p_test", "text", {"style": "文字模式专属文风"})
    assert _module_text(_compose(db, "call"), "style") == ""


def test_explicit_override_wins_for_preview(db):
    """管理端"改完先预览"：显式传入的临时覆盖要盖过已保存的。"""
    prompt_overrides.replace(db, "p_test", "text", {"character_card": "已保存的版本"})
    row = prompt_composer.get_persona_row(db, "p_test")
    friend = prompt_composer.friend_for_persona(row, "p_test")
    composed = prompt_composer.compose_prompt(
        db, friend, user_text="在吗", mode="text",
        module_overrides={"character_card": "正在编辑、还没保存的版本"},
    )
    assert _module_text(composed, "character_card") == "正在编辑、还没保存的版本"


# --------------------------------------------------------------------------- #
# 动态模块必须只读
# --------------------------------------------------------------------------- #


def test_dynamic_module_is_readonly_even_if_written_directly(db):
    """绕过 API 直接往表里写 scenario 改写，也不该生效。

    这是关键防线：`scenario` 描述"角色此刻在做什么"，由运行时数据算出。
    让它可改写会让 prompt 与真实状态矛盾，而这种错很难从日志看出来。
    """
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO prompt_overrides (persona_id, mode, module_key, text, updated_at)"
            " VALUES (?,?,?,?,?)",
            ("p_test", "text", "scenario", "假的场景覆盖", "2026-11-11T00:00:00"),
        )
    composed = _compose(db)
    assert _module_text(composed, "scenario") != "假的场景覆盖"


def test_editability_comes_from_backend_meta():
    """可改/只读的边界来自模块来源，不是前端猜的。"""
    meta = {item["key"]: item for item in prompt_composer.module_meta("text")}
    # 固定来源：可写
    for key in ("character_card", "language", "examples", "opening", "global"):
        assert meta[key]["editable"] is True, f"{key} 应该可改写"
    # 运行时来源：只读
    for key in ("scenario", "user_persona", "relationship_memory", "behavior", "author_note"):
        assert meta[key]["editable"] is False, f"{key} 应该只读（运行时数据）"


# --------------------------------------------------------------------------- #
# 整体替换语义
# --------------------------------------------------------------------------- #


def test_replace_removes_modules_not_in_payload(db):
    """保存时没出现的模块 = 取消改写（整体替换，不是逐条 upsert）。"""
    prompt_overrides.replace(db, "p_test", "text", {"character_card": "A", "language": "B"})
    assert set(prompt_overrides.load(db, "p_test", "text")) == {"character_card", "language"}

    result = prompt_overrides.replace(db, "p_test", "text", {"character_card": "A2"})
    assert result["saved"] == 1
    assert result["removed"] == 1
    assert set(prompt_overrides.load(db, "p_test", "text")) == {"character_card"}


def test_empty_string_clears_a_single_module(db):
    prompt_overrides.replace(db, "p_test", "text", {"character_card": "有内容", "language": "x"})
    prompt_overrides.replace(db, "p_test", "text", {"language": "x", "character_card": ""})
    assert set(prompt_overrides.load(db, "p_test", "text")) == {"language"}


def test_clear_removes_all(db):
    prompt_overrides.replace(db, "p_test", "text", {"character_card": "A"})
    prompt_overrides.clear(db, "p_test", "text")
    assert prompt_overrides.load(db, "p_test", "text") == {}


def test_over_long_text_is_rejected(db):
    too_long = "x" * (prompt_overrides.MAX_OVERRIDE_CHARS + 1)
    result = prompt_overrides.replace(db, "p_test", "text", {"character_card": too_long})
    assert result["ok"] is False
    assert any("超过" in issue for issue in result["issues"])


def test_invalid_arguments_are_rejected(db):
    assert prompt_overrides.replace(db, "", "text", {"a": "b"})["ok"] is False
    assert prompt_overrides.replace(db, "p_test", "", {"a": "b"})["ok"] is False
    assert prompt_overrides.replace(db, "p_test", "text", {"a": 123})["ok"] is False  # type: ignore[dict-item]


def test_broken_table_does_not_break_compose(db):
    """读改写失败时必须静默降级，不能让整轮对话挂掉。"""
    original = prompt_overrides.load

    def boom(*_a, **_kw):
        raise RuntimeError("模拟坏表")

    prompt_overrides.load = boom  # type: ignore[assignment]
    try:
        composed = _compose(db)
        assert _module_text(composed, "character_card"), "读改写失败也不能丢原始模块"
    finally:
        prompt_overrides.load = original  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 模板
# --------------------------------------------------------------------------- #


def test_template_roundtrip(db):
    created = prompt_overrides.save_template(
        db, "冷淡风", {"character_card": "话少"}, description="试一下"
    )
    assert created["ok"] is True
    templates = prompt_overrides.list_templates(db)
    assert [t["name"] for t in templates] == ["冷淡风"]
    assert templates[0]["modules"] == {"character_card": "话少"}

    deleted = prompt_overrides.delete_template(db, created["id"])
    assert deleted["ok"] is True
    assert prompt_overrides.list_templates(db) == []


def test_template_requires_modules(db):
    assert prompt_overrides.save_template(db, "空模板", {})["ok"] is False
    assert prompt_overrides.save_template(db, "", {"a": "b"})["ok"] is False


def test_deleting_missing_template_reports_error(db):
    assert prompt_overrides.delete_template(db, "tpl_nope")["ok"] is False


def test_template_does_not_auto_apply(db):
    """模板是复制源，不是引用——建模板不该悄悄改变角色行为。"""
    before = _module_text(_compose(db), "character_card")
    prompt_overrides.save_template(db, "模板A", {"character_card": "模板内容"})
    assert _module_text(_compose(db), "character_card") == before


def test_count_by_persona(db):
    prompt_overrides.replace(db, "p_test", "text", {"character_card": "A", "language": "B"})
    assert prompt_overrides.count_by_persona(db).get("p_test") == 2
