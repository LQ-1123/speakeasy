"""词库管理（控制面）测试。

覆盖：
- 校验器：枚举值必须带合法数值映射、默认值必须存在、场景不重复且 ≥8、禁用词类型正确；
- 保存：校验不过**不落盘**；通过则原子写 + 保留上一版；
- 热生效：改一个枚举的数值映射 → `dna_traits` 立刻变；停用一个取值 → `normalize_dna` 回落默认；
- 回滚：恢复备份 / 删除文件回到代码默认值；
- 管理端接口：仅本机可访问（带 token 时按 token 校验）、页面可打开、保存被拒时返回问题列表。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin.router import page_router, router as admin_router
from app.v2.db import DB
from app.v3 import lexicon, speech_dna
from app.v3.schema import ensure_schema
from app.v3.speech_dna import normalize_dna


@pytest.fixture()
def lib_dir(tmp_path, monkeypatch):
    """把词库目录指到临时目录，避免污染真实 data/lexicons。"""
    target = tmp_path / "lexicons"
    target.mkdir()
    monkeypatch.setattr(lexicon, "lexicon_dir", lambda: str(target))
    lexicon.refresh()  # 空目录 → 保持代码默认值
    yield target
    monkeypatch.undo()
    lexicon.refresh()  # 恢复真实词库


def _dna_payload(**overrides) -> dict:
    fields = {}
    for field in speech_dna.DNA_KEYS:
        trait_map = speech_dna._TRAIT_BY_FIELD.get(field)  # noqa: SLF001
        values = {}
        for value in speech_dna.DNA_ENUMS[field]:
            meta = {"enabled": True}
            if trait_map and value in trait_map:
                meta["trait"] = trait_map[value]
            values[value] = meta
        fields[field] = {"values": values, "default": speech_dna.DEFAULT_DNA[field]}
    payload = {"version": 1, "fields": fields}
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def test_validate_dna_accepts_exported_shape(lib_dir):
    assert lexicon.validate("dna", _dna_payload()) == []


def test_validate_dna_rejects_bad_trait_and_missing_default(lib_dir):
    payload = _dna_payload()
    payload["fields"]["verbosity"]["values"]["long"]["trait"] = 1.6
    payload["fields"]["verbosity"]["default"] = "nonexistent"
    issues = lexicon.validate("dna", payload)
    assert any("trait" in msg for msg in issues)
    assert any("default" in msg for msg in issues)


def test_validate_scenes_and_phrases(lib_dir):
    assert lexicon.validate("scenes", {"scenes": [{"key": f"s{i}"} for i in range(8)]}) == []
    dup = {"scenes": [{"key": "a"} for _ in range(8)]}
    assert any("重复" in msg for msg in lexicon.validate("scenes", dup))
    assert any("太少" in msg for msg in lexicon.validate("scenes", {"scenes": [{"key": "a"}]}))
    bad_phrases = {"banned_global": [""]}
    assert any("banned_global" in msg for msg in lexicon.validate("phrases", bad_phrases))


# --------------------------------------------------------------------------- #
# 保存 / 回滚 / 热生效
# --------------------------------------------------------------------------- #
def test_save_rejects_invalid_without_writing(lib_dir):
    payload = _dna_payload()
    payload["fields"]["verbosity"]["values"]["long"]["trait"] = 3.0
    with pytest.raises(lexicon.LexiconError) as exc:
        lexicon.save("dna", payload)
    assert exc.value.issues
    assert not (lib_dir / "dna.json").exists()


def test_save_writes_backup_and_hot_applies(lib_dir):
    first = _dna_payload()
    lexicon.save("dna", first)
    assert (lib_dir / "dna.json").exists()

    traits_before = speech_dna.dna_traits({"verbosity": "long"})["verbosity"]

    # 把 long 的 verbosity 数值从 0.92 改成 0.2 → 热生效后 traits 立刻变
    second = json.loads(json.dumps(first))
    second["fields"]["verbosity"]["values"]["long"]["trait"] = 0.2
    lexicon.save("dna", second)
    traits_after = speech_dna.dna_traits({"verbosity": "long"})["verbosity"]
    assert traits_before > 0.9 and traits_after == pytest.approx(0.2)
    assert (lib_dir / "dna.bak.json").exists()


def test_disable_value_falls_back_to_default(lib_dir):
    payload = _dna_payload()
    payload["fields"]["verbosity"]["values"]["long"]["enabled"] = False
    lexicon.save("dna", payload)
    assert "long" not in speech_dna.DNA_ENUMS["verbosity"]
    # 旧角色的 long 现在是非法值 → normalize 回落到该字段默认值
    assert normalize_dna({"verbosity": "long"})["verbosity"] == speech_dna.DEFAULT_DNA["verbosity"]


def test_reset_restores_previous_version(lib_dir):
    lexicon.save("dna", _dna_payload())
    bad_ok = json.loads(json.dumps(_dna_payload()))
    bad_ok["fields"]["verbosity"]["values"]["long"]["trait"] = 0.5
    lexicon.save("dna", bad_ok)
    assert speech_dna.dna_traits({"verbosity": "long"})["verbosity"] == pytest.approx(0.5)
    result = lexicon.reset_to_default("dna")
    assert result["restored"] == "backup"
    assert speech_dna.dna_traits({"verbosity": "long"})["verbosity"] != pytest.approx(0.5)


def test_reset_without_backup_restores_code_defaults_in_place(lib_dir):
    enums = speech_dna.DNA_ENUMS
    trait_map = speech_dna._TRAIT_BY_FIELD["verbosity"]
    payload = _dna_payload()
    payload["fields"]["verbosity"]["values"]["long"]["enabled"] = False
    lexicon.save("dna", payload)
    assert "long" not in speech_dna.DNA_ENUMS["verbosity"]
    assert lexicon.reset_to_default("dna")["restored"] == "code-default"
    assert "long" in speech_dna.DNA_ENUMS["verbosity"]
    assert speech_dna.DNA_ENUMS is enums
    assert speech_dna._TRAIT_BY_FIELD["verbosity"] is trait_map
    assert trait_map["long"] == pytest.approx(0.92)


def test_global_banned_phrases_reach_prompt(lib_dir):
    lexicon.save(
        "phrases",
        {"banned_global": ["totally banned line"], "signature_pools": {}},
    )
    block = speech_dna.dna_prompt_block(
        {"verbosity": "short"}, language="en", max_chars=2000
    )
    assert "totally banned line" in block


# --------------------------------------------------------------------------- #
# 管理端 API
# --------------------------------------------------------------------------- #
@pytest.fixture()
def admin_client(lib_dir, tmp_path):
    db = DB(str(tmp_path / "admin.db"))
    db.init()
    ensure_schema(db, seed=True)
    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db, voice=None)
    app.include_router(admin_router)
    app.include_router(page_router)
    with TestClient(app) as client:
        yield client
    db.close()


def test_admin_page_served(admin_client):
    # 第 4 期起 /admin 只有 React 管理台，旧词库页面（page.py）已删除。
    res = admin_client.get("/admin")
    assert res.status_code == 200
    assert "/admin/login" not in res.text

    legacy = admin_client.get("/admin/legacy")
    assert legacy.status_code == 200
    # 旧页面已删：该路径现在只是新控制台的深链接（catch-all），
    # 内容里不该再有旧页面的「词库管理」标题。
    assert "词库管理" not in legacy.text


def test_admin_api_list_get_put_validate_usage(admin_client, monkeypatch):
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    listing = admin_client.get("/api/admin/lexicons").json()
    assert {i["name"] for i in listing["items"]} == {"dna", "scenes", "phrases", "events", "rhythm", "world"}

    # 保存非法 → 明确返回问题，且不落盘
    bad = admin_client.put("/api/admin/lexicons/dna", json={"data": {"fields": {}}})
    assert bad.status_code == 200 and bad.json()["ok"] is False
    assert bad.json()["issues"]

    # 保存合法 → ok + 生效摘要
    good = admin_client.put("/api/admin/lexicons/dna", json={"data": _dna_payload()})
    assert good.json()["ok"] is True
    assert good.json()["applied"]["dna_fields"] == len(speech_dna.DNA_KEYS)

    # 校验接口
    valid = admin_client.post(
        "/api/admin/lexicons/dna/validate", json={"data": _dna_payload()}
    ).json()
    assert valid["ok"] is True

    # 使用统计（种子人物还没有 DNA → personas=0）
    usage = admin_client.get("/api/admin/usage").json()
    assert "dna" in usage and "scenes" in usage

    # 回滚接口
    reset = admin_client.post("/api/admin/lexicons/dna/reset").json()
    assert reset["ok"] is True


def test_admin_requires_token_when_configured(admin_client, monkeypatch):
    monkeypatch.setenv("SE_ADMIN_TOKEN", "s3cret")
    denied = admin_client.get("/api/admin/status")
    assert denied.status_code == 403
    ok = admin_client.get("/api/admin/status", headers={"X-Admin-Token": "s3cret"})
    assert ok.status_code == 200
