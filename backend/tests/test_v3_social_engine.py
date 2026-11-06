"""V3 社交事件引擎（纯函数）单元测试。

``app/v3/social_engine.py`` 不碰数据库、不调模型，只依赖注入的 ``rng``，
因此这里可以**完全确定性**地验证：标签抽取、候选排序、概率判定、延迟、
类型权重、每日/单帖上限等规则。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v3 import social_engine as engine  # noqa: E402
from app.v3.seed_data import PERSONAS  # noqa: E402


def _config(**overrides) -> engine.SocialConfig:
    base = dict(
        base_probability=1.0,
        type_weights={"like": 1.0, "comment": 0.0, "friend_request": 0.0},
        delay_min_s=0,
        delay_max_s=0,
        persona_cooldown_s=0,
        max_events_per_post=3,
        max_events_per_user_day=99,
        max_candidates=3,
        min_match_score=1,
    )
    base.update(overrides)
    return engine.SocialConfig(**base)


def _planned_tuple(planned):
    return [(p.persona_id, p.event_type, p.delay_s, p.score) for p in planned]


# --------------------------------------------------------------------------- #
# extract_tags
# --------------------------------------------------------------------------- #
def test_extract_tags_english():
    tags = engine.extract_tags("I love indie games and a good flat white coffee.")
    assert "coffee" in tags
    assert "gaming" in tags
    assert len(tags) == len(set(tags))


def test_extract_tags_chinese():
    tags = engine.extract_tags("今天去咖啡店，晚上听爵士乐。")
    assert set(tags) == {"coffee", "jazz"}


def test_extract_tags_explicit_tags_and_profile():
    # 显式标签：大小写归一、中文逗号分隔、去重、未收录标签保留
    tags = engine.extract_tags(
        "",
        explicit_tags=["Coffee", " Design ", "unknown tag", "COFFEE", "咖啡，茶"],
    )
    assert tags[:3] == ["coffee", "design", "unknown tag"]
    assert tags.count("coffee") == 1
    assert "咖啡" in tags and "茶" in tags

    # 画像兴趣作为弱信号补进来（不重复）
    merged = engine.extract_tags(
        "coffee is great", profile_interests=["Travel", "travel", "coffee"]
    )
    assert merged.count("coffee") == 1
    assert "travel" in merged


def test_extract_tags_empty_text():
    assert engine.extract_tags("", explicit_tags=None, profile_interests=None) == []
    assert engine.extract_tags("   ") == []


def test_match_score():
    persona = {"interests": ["coffee", "jazz", "travel"]}
    assert engine.match_score(persona, ["coffee"]) == 2
    assert engine.match_score(persona, ["coffee", "jazz"]) == 4
    assert engine.match_score(persona, ["climbing"]) == 0
    assert engine.match_score({"interests": []}, ["coffee"]) == 0


# --------------------------------------------------------------------------- #
# rank_candidates
# --------------------------------------------------------------------------- #
def test_rank_candidates_sorted_and_stable():
    tags = ["coffee"]  # p_adrian / p_annika / p_callum 各得 2 分（平局）
    first = engine.rank_candidates(PERSONAS, tags, limit=3)
    second = engine.rank_candidates(PERSONAS, tags, limit=3)
    assert [c["persona"]["id"] for c in first] == [c["persona"]["id"] for c in second]
    assert {c["score"] for c in first} == {2}
    assert len(first) == 3

    # 输入顺序打乱也保持同一输出顺序（平局用 persona id 稳定哈希）
    shuffled = list(PERSONAS)
    random.Random(7).shuffle(shuffled)
    third = engine.rank_candidates(shuffled, tags, limit=3)
    assert [c["persona"]["id"] for c in first] == [c["persona"]["id"] for c in third]

    # 高分在前：p_adrian / p_annika 同时命中 coffee+design（4 分），p_callum 只有 coffee（2 分）
    mixed = engine.rank_candidates(PERSONAS, ["coffee", "design"], limit=3)
    scores = [c["score"] for c in mixed]
    assert scores == sorted(scores, reverse=True)
    assert scores[:2] == [4, 4]
    # 分值确实来自重合标签
    assert all(c["score"] % 2 == 0 for c in mixed)


def test_rank_candidates_filters_and_flags():
    tags = ["coffee"]
    limited = engine.rank_candidates(PERSONAS, tags, limit=2)
    assert len(limited) == 2

    assert engine.rank_candidates(PERSONAS, tags, min_score=3) == []
    assert engine.rank_candidates(PERSONAS, [], min_score=1) == []

    # 非 active 人物不进候选
    with_inactive = [dict(p) for p in PERSONAS]
    with_inactive.append(
        {"id": "p_zz_inactive", "interests": ["coffee"], "status": "inactive"}
    )
    ids = [c["persona"]["id"] for c in engine.rank_candidates(with_inactive, tags, limit=10)]
    assert "p_zz_inactive" not in ids

    # is_friend 回调写进结果
    friend_flagged = engine.rank_candidates(
        PERSONAS, tags, is_friend=lambda p: p.get("id") == "p_adrian", limit=3
    )
    by_id = {c["persona"]["id"]: c for c in friend_flagged}
    assert by_id["p_adrian"]["is_friend"] is True
    assert all(
        c["is_friend"] is False for pid, c in by_id.items() if pid != "p_adrian"
    )


# --------------------------------------------------------------------------- #
# plan_events
# --------------------------------------------------------------------------- #
def test_plan_events_reproducible_with_seed():
    candidates = engine.rank_candidates(PERSONAS, ["coffee"])
    assert candidates

    cfg = _config(
        base_probability=0.55,
        delay_min_s=5,
        delay_max_s=60,
        type_weights={"like": 0.55, "comment": 0.32, "friend_request": 0.13},
    )
    planned1, attempts1 = engine.plan_events(
        candidates, rng=random.Random(42), config=cfg
    )
    planned2, attempts2 = engine.plan_events(
        candidates, rng=random.Random(42), config=cfg
    )
    assert _planned_tuple(planned1) == _planned_tuple(planned2)
    assert attempts1 == attempts2
    assert all(5 <= p.delay_s <= 60 for p in planned1)


def test_plan_events_zero_probability_means_no_action():
    candidates = engine.rank_candidates(PERSONAS, ["coffee"])
    cfg = _config(base_probability=0.0)
    planned, attempts = engine.plan_events(
        candidates, rng=random.Random(42), config=cfg
    )
    assert planned == []
    # 每个候选都记录了一次 probability_miss，最后补一条 no_event
    missed = [a for a in attempts if a["reason"] == "probability_miss"]
    assert {a["persona_id"] for a in missed} == {
        c["persona"]["id"] for c in candidates
    }
    assert any(a["event_type"] == "no_action" for a in attempts)
    assert attempts[-1]["reason"] == "no_event"


def test_plan_events_without_candidates():
    planned, attempts = engine.plan_events(
        [], rng=random.Random(1), config=_config()
    )
    assert planned == []
    assert attempts == [
        {
            "persona_id": "",
            "event_type": "no_action",
            "reason": "no_candidate",
            "roll": 0.0,
        }
    ]


def test_plan_events_respects_slots_and_delay_bounds():
    candidates = engine.rank_candidates(PERSONAS, ["coffee"])
    cfg = _config(delay_min_s=3, delay_max_s=3)

    planned, _ = engine.plan_events(
        candidates, rng=random.Random(42), config=cfg, remaining_slots=1
    )
    assert len(planned) == 1
    assert planned[0].delay_s == 3

    planned, _ = engine.plan_events(
        candidates, rng=random.Random(42), config=cfg, remaining_slots=0
    )
    assert planned == []


def test_plan_events_type_weights_and_friend_request_exclusion():
    candidates = engine.rank_candidates(PERSONAS, ["coffee"])[:1]

    planned, _ = engine.plan_events(
        candidates,
        rng=random.Random(42),
        config=_config(type_weights={"comment": 1.0}),
    )
    assert [p.event_type for p in planned] == ["comment"]

    # 已经是好友的人物不再发 friend_request（池子空了回落到 like）
    friend_candidate = dict(candidates[0])
    friend_candidate["is_friend"] = True
    planned, _ = engine.plan_events(
        [friend_candidate],
        rng=random.Random(42),
        config=_config(type_weights={"friend_request": 1.0}),
    )
    assert [p.event_type for p in planned] == ["like"]


# --------------------------------------------------------------------------- #
# SocialConfig.from_env
# --------------------------------------------------------------------------- #
def test_social_config_defaults(monkeypatch):
    for name in (
        "SE_SOCIAL_ENABLED",
        "SE_SOCIAL_BASE_PROB",
        "SE_SOCIAL_WEIGHTS",
        "SE_SOCIAL_DELAY_MIN_S",
        "SE_SOCIAL_DELAY_MAX_S",
        "SE_SOCIAL_PERSONA_COOLDOWN_S",
        "SE_SOCIAL_MAX_PER_POST",
        "SE_SOCIAL_MAX_PER_USER_DAY",
        "SE_SOCIAL_MAX_CANDIDATES",
        "SE_SOCIAL_MIN_MATCH_SCORE",
    ):
        monkeypatch.delenv(name, raising=False)

    cfg = engine.SocialConfig.from_env()
    assert cfg.enabled is True
    assert cfg.base_probability == pytest.approx(0.55)
    assert cfg.delay_min_s == 5 and cfg.delay_max_s == 60
    assert cfg.max_events_per_post == 2
    assert cfg.max_events_per_user_day == 6
    assert set(cfg.type_weights) == {"like", "comment", "friend_request"}


def test_social_config_env_overrides(monkeypatch):
    monkeypatch.setenv("SE_SOCIAL_ENABLED", "off")
    monkeypatch.setenv("SE_SOCIAL_BASE_PROB", "0.1")
    monkeypatch.setenv("SE_SOCIAL_WEIGHTS", "like:3,comment:0.5,bogus:9")
    monkeypatch.setenv("SE_SOCIAL_DELAY_MIN_S", "0")
    monkeypatch.setenv("SE_SOCIAL_DELAY_MAX_S", "10")
    monkeypatch.setenv("SE_SOCIAL_PERSONA_COOLDOWN_S", "60")
    monkeypatch.setenv("SE_SOCIAL_MAX_PER_POST", "4")
    monkeypatch.setenv("SE_SOCIAL_MAX_PER_USER_DAY", "2")
    monkeypatch.setenv("SE_SOCIAL_MAX_CANDIDATES", "2")
    monkeypatch.setenv("SE_SOCIAL_MIN_MATCH_SCORE", "2")

    cfg = engine.SocialConfig.from_env()
    assert cfg.enabled is False
    assert cfg.base_probability == pytest.approx(0.1)
    assert cfg.type_weights == {"like": 3.0, "comment": 0.5}
    assert (cfg.delay_min_s, cfg.delay_max_s) == (0, 10)
    assert cfg.persona_cooldown_s == 60
    assert cfg.max_events_per_post == 4
    assert cfg.max_events_per_user_day == 2
    assert cfg.max_candidates == 2
    assert cfg.min_match_score == 2

    public = cfg.public()
    assert public["base_probability"] == pytest.approx(0.1)
    assert public["type_weights"] == {"like": 3.0, "comment": 0.5}


def test_social_config_clamps_bad_values(monkeypatch):
    monkeypatch.setenv("SE_SOCIAL_BASE_PROB", "5")
    assert engine.SocialConfig.from_env().base_probability == 1.0

    monkeypatch.setenv("SE_SOCIAL_BASE_PROB", "not-a-number")
    assert engine.SocialConfig.from_env().base_probability == pytest.approx(0.55)

    monkeypatch.setenv("SE_SOCIAL_DELAY_MIN_S", "-9")
    monkeypatch.setenv("SE_SOCIAL_DELAY_MAX_S", "-1")
    monkeypatch.setenv("SE_SOCIAL_MAX_PER_POST", "0")
    monkeypatch.setenv("SE_SOCIAL_MAX_CANDIDATES", "0")
    monkeypatch.setenv("SE_SOCIAL_MAX_PER_USER_DAY", "-3")
    monkeypatch.setenv("SE_SOCIAL_WEIGHTS", "bogus:1")
    cfg = engine.SocialConfig.from_env()
    assert cfg.delay_min_s == 0 and cfg.delay_max_s == 0
    assert cfg.max_events_per_post == 1
    assert cfg.max_candidates == 1
    assert cfg.max_events_per_user_day == 0
    # 全是无效权重 → 回落默认权重
    assert set(cfg.type_weights) == {"like", "comment", "friend_request"}
