"""v2 熟稔度 / 裂变判定离线单测（纯函数，无 DB、无网络）。"""

from __future__ import annotations

import re

import pytest

from app.v2 import social


# --------------------------------------------------------------------------- #
# closeness_level
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("turns", "expected"),
    [
        (0, 1),
        (1, 1),
        (9, 1),
        (10, 2),
        (11, 2),
        (29, 2),
        (30, 3),
        (79, 3),
        (80, 4),
        (149, 4),
        (150, 5),
        (1000, 5),
    ],
)
def test_closeness_level_thresholds(turns, expected):
    assert social.closeness_level(turns) == expected


def test_closeness_level_handles_bad_input():
    assert social.closeness_level(-10) == 1
    assert social.closeness_level(None) == 1
    assert social.closeness_level("") == 1
    assert social.closeness_level("12") == 2
    assert social.closeness_level("oops") == 1


def test_closeness_levels_stay_in_range():
    for turns in range(0, 500, 7):
        assert 1 <= social.closeness_level(turns) <= 5


# --------------------------------------------------------------------------- #
# closeness_style
# --------------------------------------------------------------------------- #
def test_closeness_styles_are_english_and_distinct():
    styles = [social.closeness_style(level) for level in range(1, 6)]
    assert all(styles)
    assert len(set(styles)) == 5
    for style in styles:
        assert style.isascii(), style
        assert "AI" not in style


def test_closeness_style_matches_level_intent():
    assert "polite" in social.closeness_style(1).lower()
    assert "slang" in social.closeness_style(2).lower()
    assert "tease" in social.closeness_style(3).lower()
    assert "inside jokes" in social.closeness_style(4).lower()
    assert "family" in social.closeness_style(5).lower()


def test_closeness_style_clamps_out_of_range():
    assert social.closeness_style(0) == social.closeness_style(1)
    assert social.closeness_style(-3) == social.closeness_style(1)
    assert social.closeness_style(99) == social.closeness_style(5)


# --------------------------------------------------------------------------- #
# should_refer
# --------------------------------------------------------------------------- #
def _friend(**overrides) -> dict:
    data = {"id": "f1", "name": "Marco", "status": "active", "total_turns": 20, "closeness_level": 2}
    data.update(overrides)
    return data


def test_should_refer_true_when_qualified():
    assert social.should_refer(_friend()) is True
    # 只给总轮数、等级由 total_turns 推出的情况也算
    assert social.should_refer({"total_turns": 15}) is True


def test_should_refer_requires_enough_turns():
    assert social.should_refer(_friend(total_turns=14)) is False
    assert social.should_refer(_friend(total_turns=0)) is False
    assert social.should_refer({"name": "No turns"}) is False


def test_should_refer_requires_level_two():
    assert social.should_refer(_friend(total_turns=20, closeness_level=1)) is False


def test_should_refer_false_when_already_referred():
    for key in ("has_referred", "already_referred", "referral_done", "referred_at"):
        assert social.should_refer(_friend(**{key: True})) is False
    assert social.should_refer(_friend(referred_friend_id="f9")) is False
    assert social.should_refer({}) is False
    assert social.should_refer(None) is False  # type: ignore[arg-type]


def test_should_refer_ignores_non_active_friend():
    assert social.should_refer(_friend(status="pending_request")) is False


# --------------------------------------------------------------------------- #
# referral_line
# --------------------------------------------------------------------------- #
def test_referral_line_mentions_new_friend():
    line = social.referral_line(_friend(name="Marco", city="Rome"), "Lena")
    assert "Lena" in line
    assert line.endswith(("?", "!"))
    assert len(line.split()) <= 40
    assert not re.search(r"[\u4e00-\u9fff]", line)


def test_referral_line_is_deterministic():
    friend = _friend(name="Marco", city="Rome")
    assert social.referral_line(friend, "Lena") == social.referral_line(friend, "Lena")


def test_referral_line_handles_sparse_input():
    line = social.referral_line({}, "")
    assert line
    assert not re.search(r"[\u4e00-\u9fff]", line)
    assert social.referral_line({}, "Priya").find("Priya") >= 0
